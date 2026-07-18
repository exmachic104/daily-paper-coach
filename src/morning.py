"""朝ジョブ（JST 7:00 / 7:40）。

3日サイクル運用:
- 論文は3日に1本。Day1で配信（3日間の読書プラン付き）、Day1=Q1・Day2=Q2・Day3=Q3を
  1日1問ずつ出題する。
- 毎朝、前日の1問を採点し（未回答ならペナルティ、回答すればBeeminder 1点）、当日の問いを出す。
- 未消化の論文はキュー（state.json の queue）から新規検索より優先して消化する。

冪等性: state.json の last_morning_date で同日二重実行をスキップ。採点は配信の前に
コミットするため、二重実行しても採点・ペナルティは一度だけ。
"""
from __future__ import annotations

import datetime
import re
import sys
import traceback

from lib import beeminder, claude, discord, s2, store

_DAY_LABEL = {1: "課題把握", 2: "手法理解", 3: "進歩性"}

# 生成物の先頭に混入しがちな 'Day1（…）:' 'Q1（…）:' 等のラベルを除去する
_LABEL_RE = re.compile(r"^\s*(?:Day\s*\d+|Q\d+)(?:（[^）]*）)?\s*[:：]\s*")


def _strip_label(text: str) -> str:
    return _LABEL_RE.sub("", text or "")


def _today() -> str:
    jst = datetime.timezone(datetime.timedelta(hours=9))
    return datetime.datetime.now(jst).strftime("%Y-%m-%d")


def _collect_user_input(after_id: str | None) -> tuple[str, dict]:
    """前回の出題以降のユーザー投稿を収集し、回答本文とコマンドを返す。"""
    msgs = discord.fetch_messages_after(after_id, limit=100)
    answers: list[str] = []
    commands = {"skip": False, "request": None, "feedback": None}
    for m in msgs:
        if not discord.is_user_message(m):
            continue
        content = (m.get("content") or "").strip()
        if not content:
            continue
        low = content.lower()
        if low.startswith("!skip"):
            commands["skip"] = True
        elif low.startswith("!request"):
            commands["request"] = content[len("!request"):].strip()
        elif low.startswith("!feedback"):
            commands["feedback"] = content[len("!feedback"):].strip()
        else:
            answers.append(content)
    return "\n".join(answers), commands


# ---- 次の論文の取得（キュー優先、無ければ検索） --------------------------

def _fetch_next_paper(user_requests: list[str], today: str) -> dict:
    """次に学習する論文を取得する。キューを優先し、空なら新規検索する。

    戻り値: paper_meta / pdf_bytes / pdf_text / roadmap_position / assigned_hint / phase
    """
    # --- キュー優先 ---
    while store.get_queue():
        meta = store.get_queue()[0]
        pdf_bytes = s2.fetch_pdf(meta["pdf_url"])
        pdf_text = None
        if pdf_bytes is None:
            raw = s2.fetch_pdf(meta["pdf_url"], max_bytes=200 * 1024 * 1024)
            pdf_text = claude._extract_pdf_text(raw) if raw else None
        if pdf_bytes is None and not pdf_text:
            print(f"[morning] キュー論文のPDF取得失敗のため除外: {meta.get('title')}")
            store.queue_pop()
            continue
        store.queue_pop()
        return {
            "paper_meta": {
                "title": meta.get("title", ""),
                "authors": meta.get("authors", []),
                "year": meta.get("year"),
                "venue": meta.get("venue", ""),
                "url": meta.get("url") or meta.get("pdf_url"),
                "pdf_url": meta["pdf_url"],
                "s2_paper_id": meta.get("s2_paper_id", ""),
            },
            "pdf_bytes": pdf_bytes,
            "pdf_text": pdf_text,
            "roadmap_position": meta.get("roadmap_position", "（キューから消化）"),
            "assigned_hint": meta.get("assigned_sections", "全体"),
            "phase": meta.get("phase"),
        }

    # --- 新規検索 ---
    roadmap = store.load_roadmap()
    recent = store.recent_log(14)
    plan = claude.plan_search(roadmap, recent, user_requests)
    queries = plan.get("search_queries") or []
    guidance = plan.get("guidance", "")
    if not queries:
        raise RuntimeError("検索クエリが生成されませんでした。")

    candidates = s2.search(
        queries, store.excluded_ids(), exclude_titles=store.excluded_titles()
    )
    if not candidates:
        raise RuntimeError("オープンアクセスPDFのある候補が見つかりませんでした。")

    shortlist = candidates[:15]
    numbered = "\n\n".join(
        f"{i}. {c.summary_for_prompt()}" for i, c in enumerate(shortlist, start=1)
    )
    selection = claude.select_paper(numbered, guidance, roadmap, user_requests)
    try:
        idx = int(selection.get("index"))
    except (TypeError, ValueError):
        idx = 0
    if not (1 <= idx <= len(shortlist)):
        raise RuntimeError(
            f"選定 index が不正でした (index={selection.get('index')}, 候補数={len(shortlist)})。"
        )
    chosen_first = shortlist[idx - 1]
    ordered = [chosen_first] + [c for c in candidates if c is not chosen_first]

    chosen: s2.Candidate | None = None
    pdf_bytes: bytes | None = None
    pdf_text: str | None = None
    for cand in ordered[:3]:
        pdf_bytes = s2.fetch_pdf(cand.pdf_url)
        if pdf_bytes is not None:
            chosen = cand
            break
    if chosen is None:
        for cand in ordered[:3]:
            raw = s2.fetch_pdf(cand.pdf_url, max_bytes=200 * 1024 * 1024)
            if raw:
                pdf_text = claude._extract_pdf_text(raw)
                if pdf_text:
                    chosen = cand
                    break
    if chosen is None:
        raise RuntimeError("候補のPDFをいずれも取得できませんでした。")

    return {
        "paper_meta": {
            "title": chosen.title,
            "authors": chosen.authors,
            "year": chosen.year,
            "venue": chosen.venue,
            "url": chosen.landing_url,
            "pdf_url": chosen.pdf_url,
            "s2_paper_id": chosen.paper_id,
        },
        "pdf_bytes": pdf_bytes,
        "pdf_text": pdf_text,
        "roadmap_position": selection.get("roadmap_position", ""),
        "assigned_hint": selection.get("assigned_sections", "全体"),
        "phase": selection.get("phase"),
    }


def _activate_paper(fetched: dict, today: str) -> dict:
    """論文を配信し、3日サイクルの active 状態を作って Day1 の問いを出す。"""
    pm = fetched["paper_meta"]
    store.add_delivered(pm["s2_paper_id"], pm["title"])

    gen = claude.generate_delivery_and_quiz(
        fetched["pdf_bytes"], fetched["pdf_text"], pm,
        fetched["roadmap_position"], fetched["assigned_hint"],
    )
    assigned = gen.get("assigned_sections") or fetched["assigned_hint"]
    reading_plan = gen.get("reading_plan") or []
    if len(reading_plan) < 3:
        reading_plan = (list(reading_plan) + [assigned, assigned, assigned])[:3]

    authors_list = pm.get("authors") or []
    authors = ", ".join(authors_list[:4]) + (" et al." if len(authors_list) > 4 else "")
    plan_lines = "\n".join(f"**Day{i}**: {_strip_label(reading_plan[i - 1])}" for i in range(1, 4))
    description = (
        f"**要約**\n{gen.get('summary', '')}\n\n"
        f"**3日間の読書プラン**（1日約10〜17分）\n{plan_lines}\n\n"
        f"**ロードマップ**\n{fetched.get('roadmap_position', '')}"
    )[:4000]
    embed = {
        "title": pm["title"][:250],
        "url": pm["pdf_url"],
        "description": description,
        "color": 0x4E79A7,
        "fields": [
            {"name": "著者", "value": (authors or "不明")[:1000], "inline": True},
            {"name": "年 / 出典",
             "value": f"{pm.get('year') or '?'} / {pm.get('venue') or '不明'}"[:1000],
             "inline": True},
        ],
        "footer": {"text": "📄 3日かけて読みます。Day1から順に読み進めてください"},
    }
    discord.post_embed(embed, extra_content=f"🌅 **新しい論文（3日サイクル）** ({today})")

    active = {
        "paper": {
            "title": pm["title"],
            "s2_paper_id": pm["s2_paper_id"],
            "url": pm.get("url"),
            "pdf_url": pm["pdf_url"],
            "phase": fetched.get("phase"),
            "authors": authors_list,
            "year": pm.get("year"),
            "venue": pm.get("venue"),
        },
        "summary": gen.get("summary"),
        "reading_plan": reading_plan,
        "assigned_sections": assigned,
        "roadmap_position": fetched.get("roadmap_position", ""),
        "questions": gen.get("questions", []),
        "model_answers": gen.get("model_answers", []),
        "key_points": gen.get("key_points"),
        "cycle_day": 1,
        "posted_date": today,
        "quiz_message_id": None,
        "graded_date": None,
    }
    active["quiz_message_id"] = _post_question(active, 1)
    return active


def _post_question(active: dict, day: int) -> str | None:
    """その日の1問を、当日の読む範囲とともに投稿する。"""
    questions = active.get("questions", [])
    if not (1 <= day <= len(questions)):
        return None
    plan = active.get("reading_plan", [])
    read = plan[day - 1] if day - 1 < len(plan) else active.get("assigned_sections", "")
    title = (active.get("paper") or {}).get("title", "本日の論文")
    lines = [
        f"📝 **今日のクイズ（Day{day}/3・{_DAY_LABEL.get(day, '')}）**", "",
        f"**Q{day}.** {_strip_label(questions[day - 1])}", "",
        f"📖 今日読む範囲: {_strip_label(read)}", "",
        "――――――――――――――",
        "このチャンネルに返信してください。冒頭に読了状況 `[読了]`/`[途中]`/`[未読]` を1つ添えて。",
        "期限: 明朝7:00の採点まで",
    ]
    return discord.post_embed({
        "title": f"Day{day} クイズ — {title[:180]}",
        "description": "\n".join(lines)[:4000],
        "color": 0xF28E2B,
    })


def _post_single_grade(result: dict, day: int) -> None:
    verdict = {"correct": "✅ 正解", "partial": "△ 部分的に正解", "incorrect": "❌ 不正解"}.get(
        result.get("verdict"), result.get("verdict")
    )
    lines = [f"📝 **採点結果（Day{day}・{_DAY_LABEL.get(day, '')}）**", "", f"**Q{day}** {verdict}"]
    if result.get("note"):
        lines.append(f"　{result['note']}")
    if result.get("cause"):
        lines.append(f"　原因: {result['cause']}")
    if result.get("explanation"):
        lines.append(f"　{result['explanation']}")
    if result.get("advice"):
        lines.append("")
        lines.append(f"💡 {result['advice']}")
    discord.post_text("\n".join(lines))


def run_morning() -> None:
    today = _today()
    if store.get_last_morning_date() == today:
        print(f"[morning] 本日({today})分は処理済みのためスキップします。")
        return

    active = store.get_active()
    after_id = active.get("quiz_message_id") if active else None
    user_answers_raw, commands = _collect_user_input(after_id)
    user_requests = [r for r in (commands.get("request"), commands.get("feedback")) if r]

    # --- !skip: 今日は休み。採点・出題を持ち越す ---
    if commands.get("skip"):
        beeminder.submit_datapoint(f"skip {today}", value=1)
        discord.post_text(
            "😴 `!skip` を受け付けました。今日はお休み・ペナルティ対象外です。"
            "今日の問いは明日まで持ち越します。"
        )
        store.set_last_morning_date(today)
        store.git_commit_and_push(f"skip {today}")
        return

    # --- Phase 1: 前日に出した1問を採点（未採点かつ本日出題分でない場合）---
    if (
        active
        and active.get("quiz_message_id")
        and active.get("graded_date") != active.get("posted_date")
        and active.get("posted_date") != today
    ):
        day = active.get("cycle_day", 1)
        pdate = active.get("posted_date")
        paper = active.get("paper", {})
        if user_answers_raw.strip():
            result = claude.grade_single(active, day - 1, user_answers_raw, store.recent_log(14))
            _post_single_grade(result, day)
            beeminder.submit_datapoint(f"answered {pdate} Q{day}", value=1)
            store.append_log({
                "date": pdate, "paper": paper, "day": day, "answered": True,
                "self_reported_status": result.get("reported_status"),
                "verdict": result.get("verdict"), "cause": result.get("cause"),
                "penalty": False,
            })
        else:
            discord.post_text(
                f"⚠️ Day{day} の問いへの回答が確認できませんでした。未回答のためペナルティが発生します。"
            )
            store.append_log({
                "date": pdate, "paper": paper, "day": day, "answered": False,
                "self_reported_status": None, "verdict": None, "penalty": True,
            })
        active["graded_date"] = pdate
        store.set_active(active)
        # 採点を配信の前に確定コミット（二重実行時の再採点/二重ペナルティ防止）
        store.git_commit_and_push(f"morning {today}: grade Day{day}")

    # --- Phase 2: 当日の問いを出す（本日出題済みなら確定のみ）---
    active = store.get_active()
    if active and active.get("posted_date") == today:
        store.set_last_morning_date(today)
        store.git_commit_and_push(f"morning {today}: finalize")
        return

    if active and active.get("cycle_day", 1) < 3:
        # 同じ論文の次の日の問い
        day = active["cycle_day"] + 1
        active["cycle_day"] = day
        active["quiz_message_id"] = _post_question(active, day)
        active["posted_date"] = today
        store.set_active(active)
    else:
        # 3日サイクル完了、または初回 → 次の論文をアクティブ化（Day1）
        fetched = _fetch_next_paper(user_requests, today)
        store.set_active(_activate_paper(fetched, today))

    store.set_last_morning_date(today)
    store.git_commit_and_push(f"morning {today}: advance")


def main() -> None:
    try:
        run_morning()
    except Exception as e:  # noqa: BLE001
        print(traceback.format_exc(), file=sys.stderr)
        try:
            discord.post_text(f"⚠️ **朝ジョブでエラーが発生しました**\n```\n{str(e)[:1500]}\n```")
        except Exception:  # noqa: BLE001
            pass
        sys.exit(1)


if __name__ == "__main__":
    main()
