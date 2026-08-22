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


def _read_status(text: str) -> str | None:
    """回答冒頭の読了タグ [読了]/[途中]/[未読] を抽出する。"""
    for tag in ("読了", "途中", "未読"):
        if f"[{tag}]" in text or f"【{tag}】" in text:
            return tag
    return None


def _today() -> str:
    jst = datetime.timezone(datetime.timedelta(hours=9))
    return datetime.datetime.now(jst).strftime("%Y-%m-%d")


def _add_days(date_str: str, days: int) -> str:
    d = datetime.date.fromisoformat(date_str) + datetime.timedelta(days=days)
    return d.isoformat()


def _collect_user_input(after_id: str | None) -> tuple[str, dict]:
    """前回の出題以降のユーザー投稿を収集し、回答本文とコマンドを返す。"""
    msgs = discord.fetch_messages_after(after_id, limit=100)
    answers: list[str] = []
    commands = {"skip": False, "pause": None, "resume": False, "request": None, "feedback": None}
    for m in msgs:
        if not discord.is_user_message(m):
            continue
        content = (m.get("content") or "").strip()
        if not content:
            continue
        low = content.lower()
        if low.startswith("!skip"):
            commands["skip"] = True
        elif low.startswith("!pause"):
            # 「!pause 7」で7日後に自動再開。日数省略時は !resume まで無期限。
            rest = content[len("!pause"):].strip()
            days = int(rest.split()[0]) if rest.split() and rest.split()[0].isdigit() else None
            commands["pause"] = {"days": days}
        elif low.startswith("!resume"):
            commands["resume"] = True
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


_THEORY_KW = ["averaging", "observability", "lyapunov", "port-hamiltonian", "passivity",
              "automatica", "math.oc", "nonlinear", "manifold", "contraction", "riemann"]
_PRACTICAL_KW = ["industrial electronics", "power electronics", "transactions on ind",
                 "tie", "tia", "tpel", "drive", "implementation", "dsp", "experimental"]

# 未習の前提知識がこの数を超えたら Day0 で消化しきれないとみなし、後送りする（修正4 b-2）
_MAX_PREREQ_FOR_DAY0 = 4
# 後送りの試行上限。1回につき PDF 付きの生成呼び出しが1回増えるため2回まで。
# 使い切ってもなお前提過多なら、最後の候補を配信して Day0 を分割して消化する。
_MAX_DEFERRALS = 2
# 台帳が空のうちは既知の概念も未習と判定されるため、既習がこの数に達するまで後送りしない
_LEDGER_WARMUP = 10
# Day0 の1日あたりの概念数（1概念2〜3文 × 15分の制約から）
_DAY0_PER_DAY = 3


def _difficulty_type(pm: dict) -> str:
    text = f"{pm.get('venue', '')} {pm.get('title', '')}".lower()
    th = any(k in text for k in _THEORY_KW)
    pr = any(k in text for k in _PRACTICAL_KW)
    if th and not pr:
        return "理論型"
    if pr and not th:
        return "実務型"
    return "混合/不明"


def _queue_entry(fetched: dict) -> dict:
    """後送り用のキュー項目。ロードマップ上の位置づけも保持する。"""
    pm = fetched["paper_meta"]
    return {
        "title": pm.get("title", ""),
        "s2_paper_id": pm.get("s2_paper_id", ""),
        "pdf_url": pm.get("pdf_url", ""),
        "url": pm.get("url"),
        "authors": pm.get("authors", []),
        "year": pm.get("year"),
        "venue": pm.get("venue", ""),
        "roadmap_position": fetched.get("roadmap_position", ""),
        "assigned_sections": fetched.get("assigned_hint", "全体"),
        "phase": fetched.get("phase"),
    }


def _easier_request(unlearned: list[str]) -> str:
    """後送り時に検索・選定へ渡す要望（未習概念を扱う入門的論文を優先させる）。"""
    names = "、".join(unlearned[:5])
    return (
        f"直前の候補は未習の前提概念（{names}）が多く、後送りしました。"
        "次は、それらの概念そのものを扱う入門的・チュートリアル的な論文やレビュー論文を優先してください。"
        "新規性より基礎の説明が丁寧なものを選んでください。"
    )


def _activate_next(user_requests: list[str], today: str) -> dict:
    """次の論文を取得・生成する。

    未習の前提が多すぎる論文は後送りし（キュー末尾に戻す）、その未習概念を扱う
    易しい論文を探しに行く（修正5）。後送りは _MAX_DEFERRALS 回まで。使い切っても
    前提過多なら、最後に取得した論文をそのまま配信し Day0 を分割して消化する。
    """
    requests = list(user_requests)
    deferred: list[dict] = []  # 後送り分。取り直しを防ぐため最後にまとめて戻す
    fetched = gen = None
    unlearned: list[str] = []
    try:
        for attempt in range(_MAX_DEFERRALS + 1):
            fetched = _fetch_next_paper(requests, today)
            pm = fetched["paper_meta"]
            gen = claude.generate_delivery_and_quiz(
                fetched["pdf_bytes"], fetched["pdf_text"], pm,
                fetched["roadmap_position"], fetched["assigned_hint"],
            )
            unlearned = store.record_prerequisites(pm["s2_paper_id"], gen.get("prerequisites") or [])
            warmed_up = store.concept_stats()["既習"] >= _LEDGER_WARMUP
            if (
                attempt < _MAX_DEFERRALS
                and warmed_up
                and len(unlearned) > _MAX_PREREQ_FOR_DAY0
            ):
                # 前提過多 → 後送りし、未習概念を扱う易しい論文を探す（捨てない・通知しない）
                print(f"[morning] 前提過多のため後送り: {pm.get('title', '')[:60]} "
                      f"(未習 {len(unlearned)}件)")
                deferred.append(_queue_entry(fetched))
                requests = list(user_requests) + [_easier_request(unlearned)]
                continue
            break
    finally:
        for item in deferred:
            store.queue_push_back(item)
    return _build_active(fetched, gen, unlearned, today)


def _build_active(fetched: dict, gen: dict, unlearned: list[str], today: str) -> dict:
    """論文を配信し、未習前提があれば Day0 から、無ければ Day1 から開始する。"""
    pm = fetched["paper_meta"]
    store.add_delivered(pm["s2_paper_id"], pm["title"])

    assigned = gen.get("assigned_sections") or fetched["assigned_hint"]
    reading_plan = gen.get("reading_plan") or []
    if len(reading_plan) < 3:
        reading_plan = (list(reading_plan) + [assigned, assigned, assigned])[:3]

    authors_list = pm.get("authors") or []
    authors = ", ".join(authors_list[:4]) + (" et al." if len(authors_list) > 4 else "")
    plan_lines = "\n".join(f"**Day{i}**: {_strip_label(reading_plan[i - 1])}" for i in range(1, 4))

    prereqs = gen.get("prerequisites") or []
    prereq_block = ""
    if prereqs:
        items = []
        for p in prereqs[:6]:
            if isinstance(p, dict):
                items.append(f"・**{p.get('concept', '')}**: {p.get('intuition', '')}")
            else:
                items.append(f"・{p}")
        prereq_block = "**前提知識（詰まらない最小限の直観）**\n" + "\n".join(items) + "\n\n"
    skip = (gen.get("skip_sections") or "").strip()
    skip_block = f"**初読で飛ばしてよい箇所**\n{skip}\n\n" if skip else ""
    dtype = _difficulty_type(pm)

    description = (
        f"**要約**\n{gen.get('summary', '')}\n\n"
        f"{prereq_block}"
        f"**3日間の読書プラン**（1日約10〜17分）\n{plan_lines}\n\n"
        f"{skip_block}"
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
            {"name": "難易度型", "value": dtype, "inline": True},
        ],
        "footer": {"text": "📄 3日かけて読みます。Day1から順に読み進めてください"},
    }
    discord.post_embed(embed, extra_content=f"🌅 **新しい論文（3日サイクル）** ({today})")

    active = {
        "paper": {
            "title": pm["title"], "s2_paper_id": pm["s2_paper_id"], "url": pm.get("url"),
            "pdf_url": pm["pdf_url"], "phase": fetched.get("phase"),
            "authors": authors_list, "year": pm.get("year"), "venue": pm.get("venue"),
        },
        "summary": gen.get("summary"),
        "reading_plan": reading_plan,
        "assigned_sections": assigned,
        "roadmap_position": fetched.get("roadmap_position", ""),
        "questions": gen.get("questions", []),
        "model_answers": gen.get("model_answers", []),
        "evidence_quotes": gen.get("evidence_quotes", []),
        "evidence_sections": gen.get("evidence_sections", []),
        "prerequisites": prereqs,
        "skip_sections": skip,
        "key_points": gen.get("key_points"),
        "cycle_day": 1,
        "posted_date": today,
        "quiz_message_id": None,
        "graded_date": None,
        "reduced": False,
    }

    if unlearned:
        # 未習の前提概念がある → Day0（前提知識の確認）から開始（修正4b）
        active["stage"] = "day0"
        active["day0_concepts"] = unlearned
        active["day0_index"] = 0
        active["quiz_message_id"] = _post_day0(active)
    else:
        active["stage"] = "paper"
        active["quiz_message_id"] = _post_question(active, 1)
    return active


def _day0_chunk(active: dict) -> list[str]:
    """今日提示する Day0 の概念（1日 _DAY0_PER_DAY 個まで）。"""
    names = active.get("day0_concepts", []) or []
    start = active.get("day0_index", 0)
    return names[start : start + _DAY0_PER_DAY]


def _post_day0(active: dict) -> str | None:
    """Day0: その日の分の前提概念の直観だけを提示し、掴めたら [読了] を求める。

    概念が _DAY0_PER_DAY を超える場合は Day0-1, Day0-2 … と複数日に分割する（仕様107行）。
    """
    names = active.get("day0_concepts", []) or []
    chunk = _day0_chunk(active)
    total_days = max(1, -(-len(names) // _DAY0_PER_DAY))  # 切り上げ
    day_no = active.get("day0_index", 0) // _DAY0_PER_DAY + 1
    label = f"Day0-{day_no}/{total_days}" if total_days > 1 else "Day0"
    intuitions = store.concept_intuitions(chunk)
    title = (active.get("paper") or {}).get("title", "本日の論文")
    lines = [f"🧩 **{label}: 前提知識の確認**", "",
             "次の論文を読む前に、以下の前提概念の『詰まらない最小限の直観』を掴んでください"
             f"（厳密な理解は不要）。\n対象論文: {title}", ""]
    for it in intuitions:
        lines.append(f"・**{it['concept']}**: {it['intuition']}")
    remaining = len(names) - active.get("day0_index", 0) - len(chunk)
    if remaining > 0:
        lines += ["", f"（この論文の前提はあと{remaining}個あります。明日以降に分けて確認します）"]
    next_step = "明日は残りの前提を確認します。" if remaining > 0 else "明日から論文本体に入ります。"
    lines += [
        "",
        "――――――――――――――",
        f"直観が掴めたら `[読了]` と返信してください（{next_step}）。",
        "まだ曖昧なら `[途中]` と、どこが分からないかを書いてください（重点解説します）。",
        "期限: 明朝7:00まで",
    ]
    return discord.post_embed({
        "title": f"{label} 前提知識 — {title[:180]}",
        "description": "\n".join(lines)[:4000],
        "color": 0x59A14F,
    })


def _post_question(active: dict, day: int, extra: str | None = None, reduced: bool = False) -> str | None:
    """その日の1問を、当日の読む範囲とともに投稿する。

    extra: [途中] 時の重点解説など。reduced: [未読] 時に範囲を軽くする指示を添える。
    """
    questions = active.get("questions", [])
    if not (1 <= day <= len(questions)):
        return None
    plan = active.get("reading_plan", [])
    read = plan[day - 1] if day - 1 < len(plan) else active.get("assigned_sections", "")
    title = (active.get("paper") or {}).get("title", "本日の論文")
    read_line = _strip_label(read)
    if reduced:
        read_line = f"{read_line}\n（今日は範囲の前半だけで構いません。少しずつ進めましょう）"
    lines = [
        f"📝 **今日のクイズ（Day{day}/3・{_DAY_LABEL.get(day, '')}）**", "",
        f"**Q{day}.** {_strip_label(questions[day - 1])}", "",
        f"📖 今日読む範囲: {read_line}", "",
    ]
    if extra:
        lines += [f"💡 **ヒント（前回の詰まりに対応）**\n{extra}", ""]
    lines += [
        "――――――――――――――",
        "このチャンネルに返信してください。冒頭に読了状況 `[読了]`/`[途中]`/`[未読]` を1つ添えて。",
        "詰まった箇所があれば具体的に書いてください（次回そこを重点解説します）。",
        "期限: 明朝7:00の採点まで",
    ]
    return discord.post_embed({
        "title": f"Day{day} クイズ — {title[:180]}",
        "description": "\n".join(lines)[:4000],
        "color": 0xF28E2B,
    })


def _maybe_hint(active: dict, day_index: int, answer_text: str) -> str | None:
    """[途中] 回答に実質的な記述があれば、詰まりに対応するヒントを生成する。"""
    if len(answer_text.strip()) < 8:
        return None
    try:
        return claude.explain_stuck(active, day_index, answer_text)
    except Exception as e:  # noqa: BLE001
        print(f"[morning] ヒント生成失敗: {e}")
        return None


def _post_progress() -> None:
    """概念台帳と読了率の進捗を提示する（修正6）。両曲線が寝れば到達間近。"""
    stats = store.concept_stats()
    log = store.load_log()
    # 採点側の表記ゆれ（例: 「読了（回答から推定、タグマーカーなし）」）を取りこぼさない
    statuses = [str(r.get("self_reported_status") or "") for r in log]
    finished = sum(1 for s in statuses if s.startswith("読了"))
    partial = sum(1 for s in statuses if s.startswith("途中"))
    total = finished + partial
    ratio = f"{finished}/{total}" if total else "—"
    discord.post_text(
        f"📈 **進捗** 前提概念: 既習{stats['既習']} / 詰まり{stats['詰まった']} / "
        f"未習{stats['未習']}（計{stats['total']}）"
        f" ｜ 読了率: {ratio}（読了{finished}・途中{partial}）"
    )


_VERDICT_LABEL = {"correct": "✅ 正解", "partial": "△ 部分的に正解", "incorrect": "❌ 不正解"}


def _post_resume_history(active: dict) -> None:
    """再開時に、その論文でここまでに答えた日の回答と講評を振り返る。

    Day2 で休止したなら Day1 の、Day3 なら Day1・Day2 の履歴を提示する。
    """
    day = active.get("cycle_day", 1)
    past = [h for h in (active.get("history") or []) if h.get("day", 0) < day]
    if not past:
        return
    lines = ["休止前に答えた分の回答と講評です。ここまでの流れを思い出してから今日の問いに進んでください。", ""]
    for h in past:
        lines.append(
            f"**Day{h.get('day')}・{_DAY_LABEL.get(h.get('day'), '')}"
            f"（{h.get('date', '')}）** {_VERDICT_LABEL.get(h.get('verdict'), h.get('verdict') or '')}"
        )
        if h.get("question"):
            lines.append(f"　Q. {h['question']}")
        if h.get("answer"):
            lines.append(f"　あなたの回答: {h['answer']}")
        if h.get("note"):
            lines.append(f"　講評: {h['note']}")
        if h.get("cause"):
            lines.append(f"　原因: {h['cause']}")
        if h.get("advice"):
            lines.append(f"　💡 {h['advice']}")
        lines.append("")
    title = (active.get("paper") or {}).get("title", "")
    discord.post_embed({
        "title": f"📜 これまでの経過 — {title[:180]}",
        "description": "\n".join(lines)[:4000],
        "color": 0x4E79A7,
    })


def _post_single_grade(result: dict, day: int) -> None:
    verdict = _VERDICT_LABEL.get(result.get("verdict"), result.get("verdict"))
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
    answered = bool(user_answers_raw.strip())
    status = _read_status(user_answers_raw)

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

    # --- 休止中: 何もしない。!resume または期限到来で再開する ---
    # 休止の投稿は次の出題まで取得範囲に残り続けるため、毎朝読み直しても再通知しない。
    just_resumed = False
    pause = store.get_pause()
    if pause:
        until = pause.get("until")
        auto = bool(until and until <= today)
        if not (commands.get("resume") or auto):
            print(f"[morning] 休止中（{pause.get('since')}〜）のため配信・採点をスキップします。")
            return
        store.set_pause(None)
        just_resumed = True
        reason = "期限に到達したため自動再開します" if auto and not commands.get("resume") else "休止を解除しました"
        discord.post_text(f"▶️ {reason}。今日から再開します。")
        if active:
            _post_resume_history(active)
            # 休止前に出したまま未採点の問いは、ペナルティ無しで確定させる
            if active.get("posted_date"):
                active["graded_date"] = active["posted_date"]
            if active.get("stage") == "day0":
                active["quiz_message_id"] = _post_day0(active)
            else:
                active["quiz_message_id"] = _post_question(
                    active, active.get("cycle_day", 1), reduced=active.get("reduced", False)
                )
            active["posted_date"] = today
            store.set_active(active)
            store.set_last_morning_date(today)
            store.git_commit_and_push(f"morning {today}: resume")
            return
        # 学習中の論文が無ければ、そのまま通常フロー（新しい論文の配信）へ

    # --- !pause: 長期休止に入る（!resume まで、または指定日数まで）---
    if commands.get("pause") is not None and not just_resumed:
        days = commands["pause"].get("days")
        until = _add_days(today, days) if days else None
        store.set_pause({"since": today, "until": until})
        limit = f"{until} に自動再開します" if until else "`!resume` と投稿するまで再開しません"
        discord.post_text(
            f"⏸️ 休止しました（{limit}）。休止中は配信・採点・ペナルティをすべて停止します。"
            "再開時は、止めた時点の問いから続きます。"
        )
        store.set_last_morning_date(today)
        store.git_commit_and_push(f"pause {today}")
        return

    # --- Day0（前提知識の確認日）: クイズ採点ではなく [読了] で本体へ進む ---
    if active and active.get("stage") == "day0":
        if active.get("posted_date") == today:
            store.set_last_morning_date(today)
            store.git_commit_and_push(f"morning {today}: finalize day0")
            return
        chunk = _day0_chunk(active)
        if answered and status not in ("途中", "未読"):
            # その日の分を既習化し、残りがあれば次の Day0 へ、無ければ論文本体へ
            store.mark_concepts(chunk, "既習")
            beeminder.submit_datapoint(f"day0 {active.get('posted_date')}", value=1)
            active["day0_index"] = active.get("day0_index", 0) + len(chunk)
            if active["day0_index"] < len(active.get("day0_concepts", [])):
                discord.post_text("✅ 前提知識の確認、今日の分は完了です。続きを確認します。")
                active["quiz_message_id"] = _post_day0(active)
            else:
                discord.post_text("✅ 前提知識の確認、完了です。今日から論文本体に入ります。")
                active["stage"] = "paper"
                active["cycle_day"] = 1
                active["quiz_message_id"] = _post_question(active, 1)
            active["posted_date"] = today
        else:
            if not answered:
                discord.post_text(
                    "⚠️ Day0（前提知識）の確認が未回答でした。未回答のためペナルティが発生します。"
                )
                store.append_log({
                    "date": active.get("posted_date"), "paper": active.get("paper", {}),
                    "day": 0, "answered": False, "penalty": True,
                })
            else:
                # [途中]/[未読] → 掴めていない概念として台帳に残し、同じ分を再掲する
                store.mark_concepts(chunk, "詰まった")
                beeminder.submit_datapoint(f"day0 {active.get('posted_date')}", value=1)
            active["quiz_message_id"] = _post_day0(active)
            active["posted_date"] = today
        store.set_active(active)
        store.set_last_morning_date(today)
        store.git_commit_and_push(f"morning {today}: day0")
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
            # 詰まりの逆方向センサー（修正6）: 既習に上げずに台帳へ残す
            blocking = [
                str(c).strip() for c in (result.get("blocking_concepts") or []) if str(c).strip()
            ]
            if blocking:
                store.mark_concepts(blocking, "詰まった")
            # 再開時の振り返り用に、その日の回答と講評を論文単位で残す
            questions = active.get("questions") or []
            active.setdefault("history", []).append({
                "day": day,
                "date": pdate,
                "question": _strip_label(questions[day - 1]) if day - 1 < len(questions) else "",
                "answer": user_answers_raw.strip()[:400],
                "verdict": result.get("verdict"),
                "note": result.get("note"),
                "cause": result.get("cause"),
                "advice": result.get("advice"),
            })
            store.append_log({
                "date": pdate, "paper": paper, "day": day, "answered": True,
                "self_reported_status": result.get("reported_status"),
                "verdict": result.get("verdict"), "cause": result.get("cause"),
                "blocking_concepts": blocking,
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

    # --- Phase 2: 当日の問いを出す（読了タグで進む/留まるを分岐）---
    active = store.get_active()
    if active and active.get("posted_date") == today:
        # 本日出題済み（二重実行）→ 確定のみ
        store.set_last_morning_date(today)
        store.git_commit_and_push(f"morning {today}: finalize")
        return

    if active is None:
        # 初回 → 次の論文をアクティブ化
        store.set_active(_activate_next(user_requests, today))
    else:
        day = active.get("cycle_day", 1)
        if not answered:
            # 未回答 → 進めず同じ問いを再掲（ペナルティは Phase1 で通知済み）
            active["quiz_message_id"] = _post_question(active, day, reduced=active.get("reduced", False))
            active["posted_date"] = today
            store.set_active(active)
        elif status == "途中":
            # 途中 → 進めず同じ問いを再掲。詰まりに対応するヒントを添える
            hint = _maybe_hint(active, day - 1, user_answers_raw)
            active["quiz_message_id"] = _post_question(active, day, extra=hint,
                                                        reduced=active.get("reduced", False))
            active["posted_date"] = today
            store.set_active(active)
        elif status == "未読":
            # 未読 → 進めず、範囲を軽くして同じ問いを再掲
            active["reduced"] = True
            active["quiz_message_id"] = _post_question(active, day, reduced=True)
            active["posted_date"] = today
            store.set_active(active)
        elif day < 3:
            # 読了（またはタグ無しの回答）→ 次の日へ進む
            active["reduced"] = False
            active["cycle_day"] = day + 1
            active["quiz_message_id"] = _post_question(active, day + 1)
            active["posted_date"] = today
            store.set_active(active)
        else:
            # 3日サイクル完了 → 完走した論文の前提を既習化し、進捗を提示して次へ。
            # ただし採点で詰まりが記録された概念は既習に上げない（修正6のセンサー）。
            ledger = store.get_concepts()
            done_names = [
                p.get("concept") for p in active.get("prerequisites", [])
                if isinstance(p, dict) and p.get("concept")
                and ledger.get(p["concept"], {}).get("status") != "詰まった"
            ]
            store.mark_concepts(done_names, "既習")
            _post_progress()
            store.set_active(_activate_next(user_requests, today))

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
