"""朝ジョブ（JST 7:00 / cron: '0 22 * * *'）。

1. 回答チェック（前夜の出題以降のユーザー投稿を収集）
2. 採点（誤答原因の推定を含む）
3. ペナルティ判定（Beeminder）
4. 学習ログ更新 & commit
5. 次の論文選定
6. 配信メッセージ生成 & 投稿
7. 夜の出題を事前生成して pending_quiz.json に保存
"""
from __future__ import annotations

import datetime
import sys
import traceback

from lib import beeminder, claude, discord, s2, store


def _today() -> str:
    # JST の日付
    jst = datetime.timezone(datetime.timedelta(hours=9))
    return datetime.datetime.now(jst).strftime("%Y-%m-%d")


def _collect_user_input(after_id: str | None) -> tuple[str, dict]:
    """前夜の出題以降のユーザー投稿を収集し、回答本文とコマンドを返す。"""
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


def _grade_and_post(pending: dict, user_answers_raw: str) -> dict:
    """採点して結果を Discord に投稿し、ログ用の results を返す。"""
    result = claude.grade(pending, user_answers_raw, store.recent_log(14))

    lines = ["📝 **採点結果**", ""]
    for r in result.get("results", []):
        verdict = {"correct": "✅ 正解", "partial": "△ 部分的に正解", "incorrect": "❌ 不正解"}.get(
            r.get("verdict"), r.get("verdict")
        )
        lines.append(f"**Q{r.get('q')}** {verdict}")
        if r.get("note"):
            lines.append(f"　{r['note']}")
        if r.get("cause"):
            lines.append(f"　原因: {r['cause']}")
        if r.get("explanation"):
            lines.append(f"　{r['explanation']}")
        lines.append("")
    if result.get("advice"):
        lines.append(f"💡 {result['advice']}")
    discord.post_text("\n".join(lines))
    return result


def _select_and_deliver(user_requests: list[str], today: str) -> dict:
    """次の論文を選定し、配信して pending_quiz を返す。"""
    roadmap = store.load_roadmap()
    recent = store.recent_log(14)

    # 検索方針を生成
    plan = claude.plan_search(roadmap, recent, user_requests)
    queries = plan.get("search_queries") or []
    guidance = plan.get("guidance", "")
    if not queries:
        raise RuntimeError("検索クエリが生成されませんでした。")

    # 候補を検索（既読を除外）
    candidates = s2.search(queries, store.read_paper_ids())
    if not candidates:
        raise RuntimeError("オープンアクセスPDFのある候補が見つかりませんでした。")

    # Claude が候補から1本選定（番号指定でIDの取り違えを防ぐ）
    shortlist = candidates[:15]
    numbered_text = "\n\n".join(
        f"{i}. {c.summary_for_prompt()}" for i, c in enumerate(shortlist, start=1)
    )
    selection = claude.select_paper(numbered_text, guidance, roadmap, user_requests)

    # 1始まりの index を検証。範囲外なら分野不一致の可能性が高いのでエラーにする
    try:
        idx = int(selection.get("index"))
    except (TypeError, ValueError):
        idx = 0
    if not (1 <= idx <= len(shortlist)):
        raise RuntimeError(
            f"選定 index が不正でした (index={selection.get('index')}, 候補数={len(shortlist)})。"
        )
    chosen_first = shortlist[idx - 1]
    # 選定した候補を先頭に、以降はPDF取得失敗時のフォールバック用
    ordered = [chosen_first] + [c for c in candidates if c is not chosen_first]

    # PDF 取得（最大3候補までフォールバック）
    chosen: s2.Candidate | None = None
    pdf_bytes: bytes | None = None
    pdf_text: str | None = None
    for cand in ordered[:3]:
        pdf_bytes = s2.fetch_pdf(cand.pdf_url)
        if pdf_bytes is not None:
            chosen = cand
            break
        # サイズ超過や取得失敗時はテキスト抽出フォールバックも試す
    if chosen is None:
        # 最終手段: 先頭候補のPDFをテキスト抽出で試す
        for cand in ordered[:3]:
            raw = s2.fetch_pdf(cand.pdf_url, max_bytes=200 * 1024 * 1024)
            if raw:
                pdf_text = claude._extract_pdf_text(raw)
                if pdf_text:
                    chosen = cand
                    break
    if chosen is None:
        raise RuntimeError("候補のPDFをいずれも取得できませんでした。")

    paper_meta = {
        "title": chosen.title,
        "authors": chosen.authors,
        "year": chosen.year,
        "venue": chosen.venue,
        "url": chosen.landing_url,
        "pdf_url": chosen.pdf_url,
    }
    roadmap_position = selection.get("roadmap_position", "")
    assigned_sections_hint = selection.get("assigned_sections", "全体")

    # 配信内容 + 出題を生成（読む範囲は本文に基づいて生成側が確定する）
    gen = claude.generate_delivery_and_quiz(
        pdf_bytes, pdf_text, paper_meta, roadmap_position, assigned_sections_hint
    )
    # 生成側が本文から確定した読む範囲を採用（推測を上書き）
    assigned_sections = gen.get("assigned_sections") or assigned_sections_hint

    # 配信メッセージを投稿（Embed）
    authors = ", ".join(chosen.authors[:4]) + (" et al." if len(chosen.authors) > 4 else "")
    description = (
        f"**要約**\n{gen.get('summary', '')}\n\n"
        f"**今日の読みどころ**\n{gen.get('reading_guide', '')}\n\n"
        f"**ロードマップ**\n{roadmap_position}\n"
        f"**読む範囲**: {assigned_sections}"
    )[:4000]
    embed = {
        "title": chosen.title[:250],
        "url": chosen.pdf_url,
        "description": description,
        "color": 0x4E79A7,
        "fields": [
            {"name": "著者", "value": (authors or "不明")[:1000], "inline": True},
            {"name": "年 / 出典", "value": f"{chosen.year or '?'} / {chosen.venue or '不明'}"[:1000], "inline": True},
        ],
        "footer": {"text": "📄 PDFはタイトルまたはリンクから開けます"},
    }
    discord.post_embed(embed, extra_content=f"🌅 **今日の論文** ({today})")

    # 出題ストックを構築
    pending = {
        "date": today,
        "paper": {
            "title": chosen.title,
            "s2_paper_id": chosen.paper_id,
            "url": chosen.landing_url,
            "pdf_url": chosen.pdf_url,
            "phase": selection.get("phase"),
            "assigned_sections": assigned_sections,
        },
        "summary": gen.get("summary"),
        "reading_guide": gen.get("reading_guide"),
        "questions": gen.get("questions", []),
        "model_answers": gen.get("model_answers", []),
        "key_points": gen.get("key_points"),
        "assigned_sections": assigned_sections,
        "roadmap_position": roadmap_position,
        "posted": False,
        "quiz_message_id": None,
    }
    # 同じ朝にクイズも投稿する（夜ジョブは廃止）
    pending["quiz_message_id"] = _post_quiz(pending)
    pending["posted"] = True
    return pending


def _post_quiz(pending: dict) -> str | None:
    """理解度確認クイズを Discord に投稿し、投稿メッセージ ID を返す。"""
    questions = pending.get("questions", [])
    title = pending.get("paper", {}).get("title", "本日の論文")
    lines = [f"📝 **理解度確認クイズ** — {title}", ""]
    for i, q in enumerate(questions, start=1):
        lines.append(f"**Q{i}.** {q}")
        lines.append("")
    lines.append("――――――――――――――")
    lines.append("回答方法:")
    lines.append("`A1: ... / A2: ... / A3: ...` の形式で、このチャンネルに返信してください。")
    lines.append("冒頭に読了状況を1つ添えてください: `[読了]` `[途中]` `[未読]`")
    lines.append("期限: 明朝7:00の採点まで（今日中に論文を読んで回答してください）")
    text = "\n".join(lines)
    return discord.post_embed(
        {
            "title": f"理解度確認クイズ — {title[:200]}",
            "description": text[:4000],
            "color": 0xF28E2B,
        }
    )


def run_morning() -> None:
    today = _today()

    # 冪等ガード: 本日分を既に完了していれば二重実行をスキップ
    # （朝ジョブを複数 cron 枠で走らせても安全にするため）
    if store.get_last_morning_date() == today:
        print(f"[morning] 本日({today})分は処理済みのためスキップします。")
        return

    pending = store.load_pending_quiz()

    after_id = pending.get("quiz_message_id") if pending else None
    user_answers_raw, commands = _collect_user_input(after_id)
    user_requests = [r for r in (commands.get("request"), commands.get("feedback")) if r]

    # --- 前夜の出題に対する採点・ペナルティ判定 ---
    if pending is not None:
        paper = pending.get("paper", {})
        if commands.get("skip"):
            # 休み: ペナルティ対象外。同じ論文を翌日に持ち越す。
            beeminder.submit_datapoint(f"skip {today}", value=1)
            discord.post_text("😴 `!skip` を受け付けました。今日はお休み、ペナルティ対象外です。同じ論文を明日に持ち越します。")
            store.append_log({
                "date": today, "paper": paper, "answered": False,
                "self_reported_status": None, "results": [], "penalty": False,
                "skipped": True,
            })
            store.set_last_morning_date(today)
            store.git_commit_and_push(f"log: skip {today}")
            return  # pending_quiz は残し、翌日に同じ論文を再出題

        if user_answers_raw.strip():
            # 回答あり → posted に関わらず採点する（解答を取りこぼさない）
            result = _grade_and_post(pending, user_answers_raw)
            beeminder.submit_datapoint(f"answered {pending.get('date')}", value=1)
            store.append_log({
                "date": pending.get("date", today),
                "paper": paper,
                "answered": True,
                "self_reported_status": result.get("reported_status"),
                "results": result.get("results", []),
                "penalty": False,
                "adjustment": result.get("adjustment"),
            })
        elif pending.get("posted"):
            # 出題済みなのに回答なし → データポイントを送らず derail（課金）
            discord.post_text("⚠️ 前回の出題への回答が確認できませんでした。未回答のためペナルティが発生します。")
            store.append_log({
                "date": pending.get("date", today),
                "paper": paper,
                "answered": False,
                "self_reported_status": None,
                "results": [],
                "penalty": True,
            })
        else:
            # 出題が投稿されておらず回答も無い → ペナルティ対象外
            discord.post_text("ℹ️ 前回の出題が投稿されておらず回答も無いため、採点・ペナルティはありません。")
        store.clear_pending_quiz()

    # --- 次の論文の選定・配信・出題（同じ朝にクイズも投稿）---
    new_pending = _select_and_deliver(user_requests, today)
    store.save_pending_quiz(new_pending)
    store.set_last_morning_date(today)
    store.git_commit_and_push(f"morning {today}: log update, paper & quiz")


def main() -> None:
    try:
        run_morning()
    except Exception as e:  # noqa: BLE001
        tb = traceback.format_exc()
        print(tb, file=sys.stderr)
        try:
            discord.post_text(f"⚠️ **朝ジョブでエラーが発生しました**\n```\n{str(e)[:1500]}\n```")
        except Exception:  # noqa: BLE001
            pass
        sys.exit(1)


if __name__ == "__main__":
    main()
