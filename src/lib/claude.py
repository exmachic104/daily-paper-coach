"""Anthropic API (Claude) 連携。

論文選定・要約・出題・採点・誤答原因推定を担う。PDF は document ブロック
（base64）で渡し、サイズ超過時はテキスト抽出にフォールバックする。
出題・採点・フィードバックはすべて日本語。
"""
from __future__ import annotations

import base64
import json
import re
from typing import Any

import anthropic

from .config import config

_client: anthropic.Anthropic | None = None


def _get_client() -> anthropic.Anthropic:
    global _client
    if _client is None:
        config.require("ANTHROPIC_API_KEY")
        _client = anthropic.Anthropic(api_key=config.ANTHROPIC_API_KEY)
    return _client


def _extract_json(text: str) -> Any:
    """Claude の応答から JSON を頑健に取り出す。"""
    text = text.strip()
    # ```json ... ``` コードフェンスを除去
    fence = re.search(r"```(?:json)?\s*(.+?)```", text, re.DOTALL)
    if fence:
        text = fence.group(1).strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        # 最初の { から最後の } までを抽出して再試行
        start, end = text.find("{"), text.rfind("}")
        if start != -1 and end != -1 and end > start:
            return json.loads(text[start : end + 1])
        raise


def _message(
    system: str,
    user_content: Any,
    max_tokens: int = 4000,
    expect_json: bool = True,
) -> Any:
    """Messages API を呼び出し、テキスト（or JSON）を返す。

    JSON 期待時、解析に失敗したら数回リトライする（一過性の不正 JSON 対策）。
    """
    client = _get_client()
    attempts = 3 if expect_json else 1
    last_err: Exception | None = None
    for i in range(attempts):
        resp = client.messages.create(
            model=config.ANTHROPIC_MODEL,
            max_tokens=max_tokens,
            system=system,
            output_config={"effort": "medium"},
            messages=[{"role": "user", "content": user_content}],
        )
        text = "".join(b.text for b in resp.content if b.type == "text")
        if not expect_json:
            return text
        try:
            return _extract_json(text)
        except (json.JSONDecodeError, ValueError) as e:
            last_err = e
            print(f"[claude] JSON 解析失敗 (試行 {i + 1}/{attempts}): {e}")
    raise RuntimeError(f"Claude 応答の JSON 解析に失敗しました: {last_err}")


# ---- PDF/テキストのコンテンツブロック化 -----------------------------------

def _pdf_block(pdf_bytes: bytes) -> dict:
    b64 = base64.standard_b64encode(pdf_bytes).decode("utf-8")
    return {
        "type": "document",
        "source": {
            "type": "base64",
            "media_type": "application/pdf",
            "data": b64,
        },
    }


def _extract_pdf_text(pdf_bytes: bytes) -> str | None:
    """pypdf があればテキスト抽出する（サイズ超過時のフォールバック）。"""
    try:
        import io

        from pypdf import PdfReader

        reader = PdfReader(io.BytesIO(pdf_bytes))
        pages = [p.extract_text() or "" for p in reader.pages]
        return "\n\n".join(pages)[:120_000]
    except Exception as e:  # noqa: BLE001
        print(f"[claude] PDF テキスト抽出失敗: {e}")
        return None


def build_paper_content(
    lead_text: str,
    pdf_bytes: bytes | None,
    pdf_text: str | None = None,
) -> list[dict]:
    """PDF（またはテキスト）+ 指示テキストのコンテンツブロックを構築する。"""
    blocks: list[dict] = []
    if pdf_bytes is not None:
        blocks.append(_pdf_block(pdf_bytes))
    elif pdf_text:
        blocks.append({"type": "text", "text": f"論文本文（抽出テキスト）:\n{pdf_text}"})
    blocks.append({"type": "text", "text": lead_text})
    return blocks


# ---- 1. 検索方針の生成 ----------------------------------------------------

def plan_search(
    roadmap: dict, recent_log: list[dict], user_requests: list[str]
) -> dict:
    """学習ログとロードマップから次に読むべき論文の検索クエリ方針を生成する。"""
    system = (
        "あなたはPMSMセンサレス制御を研究者レベルまで積み上げる学習コーチです。"
        "学習ログとロードマップに基づき、次に読むべき論文を探すための英語の検索クエリを設計します。"
        "必ず有効な JSON のみを返してください。"
    )
    payload = {
        "roadmap": roadmap,
        "recent_log": recent_log,
        "user_requests": user_requests,
    }
    topic = roadmap.get("topic", "")
    lead = (
        "以下の JSON はロードマップ・直近の学習ログ・ユーザーからの要望です。\n"
        f"{json.dumps(payload, ensure_ascii=False, indent=2)}\n\n"
        f"【最重要】対象分野は「{topic}」です。検索クエリは必ずこの分野に限定してください。"
        "各クエリに分野を特定する英語の専門用語（例: PMSM, sensorless control, "
        "permanent magnet synchronous motor, position estimation 等）を必ず含め、"
        "無関係な分野（通信・ネットワーク等）の論文がヒットしないようにしてください。\n\n"
        "次に読むべき論文を検索するための方針を、次の JSON 形式で返してください:\n"
        "{\n"
        '  "search_queries": ["英語の検索クエリを2〜4個。上記の分野語を必ず含める"],\n'
        '  "guidance": "選定時に重視する観点（難易度・テーマ・分量）を日本語で簡潔に"\n'
        "}\n"
        "正答率が高く安定していればフェーズを進め、『前提知識の不足』が続くなら基礎寄り、"
        "『時間不足』が続くなら分量の少ない論文を狙ってください。"
    )
    return _message(system, lead, max_tokens=1200)


# ---- 2. 候補からの選定 ----------------------------------------------------

def select_paper(
    numbered_candidates: str,
    guidance: str,
    roadmap: dict,
    user_requests: list[str],
) -> dict:
    """番号付き候補一覧から1本を選び、フェーズ・読む範囲・位置づけを決める。"""
    topic = roadmap.get("topic", "")
    system = (
        f"あなたは「{topic}」の学習コーチです。候補論文から今日読む1本を選定します。"
        f"【最重要】必ず「{topic}」に合致する論文を選んでください。分野違いの論文"
        "（通信・ネットワーク等、対象分野と無関係なもの）は絶対に選ばないこと。"
        "15分（約15分の精読）で読める分量になるよう、長い論文は読むセクションを指定します。"
        "必ず有効な JSON のみを返してください。"
    )
    lead = (
        "候補論文一覧（各行の先頭の番号で参照します）:\n"
        f"{numbered_candidates}\n\n"
        f"対象分野: {topic}\n"
        f"選定方針: {guidance}\n"
        f"ロードマップ現在位置: {json.dumps(roadmap.get('current_position', {}), ensure_ascii=False)}\n"
        f"ユーザー要望: {json.dumps(user_requests, ensure_ascii=False)}\n\n"
        "次の JSON 形式で1本を選定してください:\n"
        "{\n"
        '  "index": 選んだ候補の先頭の番号(整数, 1始まり),\n'
        '  "phase": フェーズ番号(整数),\n'
        '  "assigned_sections": "読むべきセクション（例: Sec.1-3、全体でも可）",\n'
        '  "roadmap_position": "ロードマップ上の位置づけ（例: フェーズ2: モデルベース手法 3/5本目）",\n'
        '  "reason": "選定理由を日本語で簡潔に"\n'
        "}\n"
        f"注意: assigned_sections と roadmap_position は、選んだ番号の論文そのものに"
        "対応させること。対象分野に合致する候補が一つも無い場合のみ、最も近いものを選び"
        "reason にその旨を明記してください。"
    )
    return _message(system, lead, max_tokens=1200)


# ---- 3. 配信メッセージ + 出題の生成 ---------------------------------------

def generate_delivery_and_quiz(
    pdf_bytes: bytes | None,
    pdf_text: str | None,
    paper_meta: dict,
    roadmap_position: str,
    assigned_sections_hint: str,
) -> dict:
    """PDF を読み、配信内容・読む範囲・夜の出題3問・模範解答・採点用要点を生成する。

    読む範囲(assigned_sections)は、この生成ステップが実際の論文本文に基づいて
    確定する（選定ステップの推測はあくまで参考）。読みどころと必ず整合させる。
    """
    system = (
        "あなたはPMSMセンサレス制御の学習コーチです。与えられた論文を1日約15分×3日で読むための"
        "日本語ガイドと、毎日1問ずつの理解度確認クイズ3問を作成します。\n"
        "出題カテゴリは固定（ただし要約より深い粒度にすること）:\n"
        "- Q1（課題把握）: この論文が解決しようとした課題の本質・なぜそれが問題なのか\n"
        "- Q2（手法理解）: 提案手法の核となる仕組み・原理（切替基準や設計の要点など）\n"
        "- Q3（進歩性）: 従来手法と比べた具体的な改善点・検証方法・残る限界\n\n"
        "【最重要・範囲内自己検証（逐語引用の強制）】各問について、その答えの根拠となる文を、"
        "『その日までの読む範囲(reading_plan)の中』から**逐語で**引用すること（evidence_quotes）。"
        "- Q1 は Day1 の範囲、Q2 は Day1+Day2、Q3 は Day1〜Day3 全体から引用すること。\n"
        "- 問いが複数の論点を含む場合（例: 低速側と高速側の両方の理由）、論点ごとに引用を用意すること。\n"
        "- ある論点について、その日までの範囲から逐語引用が取れない場合は、"
        "『その論点が書かれている節を reading_plan の該当日に追加して範囲を広げる』か、"
        "『問いをその日の範囲だけで答えられる形に狭める』こと。範囲外の内容を問うてはならない。\n"
        "- 引用は論文本文からの完全な逐語コピーにすること（後で検証可能にするため。要約や言い換えは不可）。\n"
        "- 各日の読む範囲は約15分（10〜17分）で読める分量に収めること。\n\n"
        "【前提知識・読み飛ばし】\n"
        "- prerequisites: この論文で詰まらないために必要な前提概念を列挙する。各概念に、厳密な理解ではなく"
        "『この論文で詰まらない最小限の直観』を2〜3文で書く（教科書的解説は禁止。長くしない）。\n"
        "- skip_sections: 初読で飛ばしてよい箇所を明示する（例: 定理の厳密な証明は結論だけ使えば先に進める）。\n\n"
        "【ネタバレ防止】要約(summary)と読書プランは『何を扱うか・なぜ重要か・どこを読むか』の"
        "動機づけに留め、クイズの答えそのものは書かないこと。要約の文言をなぞるだけの問いも禁止。\n"
        "各問は短い文章(1〜3文)で答えられる形式。すべて日本語。必ず有効な JSON のみを返してください。"
    )
    lead = (
        f"論文メタ情報: {json.dumps(paper_meta, ensure_ascii=False)}\n"
        f"読むべきセクションの候補（参考。実際の論文の章立てと違えば無視してよい）: {assigned_sections_hint}\n"
        f"ロードマップ上の位置づけ: {roadmap_position}\n\n"
        "次の JSON 形式で返してください（reading_plan/questions/evidence_quotes/evidence_sections/"
        "model_answers は必ず3要素、Day1/Day2/Day3 と Q1/Q2/Q3 が対応）:\n"
        "{\n"
        '  "assigned_sections": "論文全体で読むべきセクションを実在する章・節・図表名で（例: Sec.1, 2.3-2.4, Fig.4-5）",\n'
        '  "summary": "3〜4文の日本語要約。見出しレベル。クイズの答えは書かない",\n'
        '  "prerequisites": [{"concept": "前提概念名", "intuition": "詰まらない最小限の直観を2〜3文で", "why": "この論文のどこで必要か"}],\n'
        '  "skip_sections": "初読で飛ばしてよい箇所（無ければ空文字）",\n'
        '  "reading_plan": ["その日に読む範囲と狙いの本文のみ。接頭辞（Day1 等）は付けない", "…", "…"],\n'
        '  "questions": ["問いの本文のみ。接頭辞（Q1 等）は付けない", "…", "…"],\n'
        '  "evidence_quotes": [["Q1の答えの根拠となる、Day1範囲からの逐語引用（論点ごとに1件、計1〜3件）"], ["Q2用（Day1+2から）"], ["Q3用（全体から）"]],\n'
        '  "evidence_sections": [["各引用の出所（節・式番号）"], ["…"], ["…"]],\n'
        '  "model_answers": ["Q1の模範解答", "Q2の模範解答", "Q3の模範解答"],\n'
        '  "key_points": "採点時に参照する要点（配信されず採点時のみ使用）"\n'
        "}\n"
        "各配列要素の先頭に『Day1』『Q1』などのラベルを書かないこと（表示側で付与する）。"
    )
    content = build_paper_content(lead, pdf_bytes, pdf_text)
    gen = _message(system, content, max_tokens=6000)

    # 範囲内自己検証: 各問に逐語引用があるか軽く点検（無ければ1回だけ再生成）
    def _has_evidence(g: dict) -> bool:
        ev = g.get("evidence_quotes") or []
        return len(ev) >= 3 and all(isinstance(e, list) and any(str(x).strip() for x in e) for e in ev[:3])

    if not _has_evidence(gen):
        print("[claude] evidence_quotes が不足のため1回再生成します。")
        retry_lead = lead + (
            "\n\n前回の出力は各問の evidence_quotes（指定範囲からの逐語引用）が不足していました。"
            "必ず全問に、その日までの範囲からの逐語引用を付けてください。"
            "引用が取れない問いは、範囲を広げるか問いを狭めて調整してください。"
        )
        gen = _message(system, build_paper_content(retry_lead, pdf_bytes, pdf_text), max_tokens=6000)
    return gen


def grade_single(
    active: dict, day_index: int, user_answer: str, recent_log: list[dict]
) -> dict:
    """その日の1問だけを採点し、誤答なら原因を推定する。"""
    questions = active.get("questions", [])
    model_answers = active.get("model_answers", [])
    if not (0 <= day_index < len(questions)):
        raise RuntimeError(f"day_index {day_index} が不正です。")
    evidence_quotes = active.get("evidence_quotes") or []
    evidence = evidence_quotes[day_index] if day_index < len(evidence_quotes) else []
    evidence_sections = active.get("evidence_sections") or []
    ev_sections = evidence_sections[day_index] if day_index < len(evidence_sections) else []

    system = (
        "あなたはPMSMセンサレス制御の学習コーチです。ユーザーの回答を1問だけ採点し、"
        "誤答があれば原因を推定します。\n"
        "【採点基準】模範解答との表現一致ではなく、論文本文からの逐語引用(evidence_quotes)との"
        "『内容の整合』で正誤を判定すること。模範解答と言い回しが異なっても、evidence_quotes の"
        "内容と整合していれば正答とする。誤りを指摘する際は、根拠として本文の該当箇所（evidence_sections の"
        "節・式番号）を提示すること。\n"
        "原因分類は次の4つのいずれか:\n"
        "- 時間不足: 該当箇所まで読めていない\n"
        "- 概念の誤解: 読んだが原理を取り違えている\n"
        "- 前提知識の不足: 論文以前の基礎概念でつまずいている\n"
        "- 問題の読み違え: 理解はしているが問いとずれた回答をしている\n"
        "すべて日本語。必ず有効な JSON のみを返してください。"
    )
    context = {
        "question": questions[day_index],
        "evidence_quotes": evidence,
        "evidence_sections": ev_sections,
        "model_answer_reference": model_answers[day_index] if day_index < len(model_answers) else "",
        "key_points": active.get("key_points"),
        "assigned_sections": active.get("assigned_sections"),
    }
    lead = (
        "今日の問いと、正誤判定の基準となる本文からの逐語引用（evidence_quotes）:\n"
        f"{json.dumps(context, ensure_ascii=False, indent=2)}\n\n"
        "直近2週間の学習ログ（誤答傾向の文脈）:\n"
        f"{json.dumps(recent_log, ensure_ascii=False, indent=2)}\n\n"
        "ユーザーの回答（生テキスト。冒頭に読了状況 [読了]/[途中]/[未読] が付く想定）:\n"
        f"{user_answer}\n\n"
        "evidence_quotes と照らして採点し、次の JSON 形式で返してください:\n"
        "{\n"
        '  "reported_status": "読了 | 途中 | 未読（回答から推定）",\n'
        '  "verdict": "correct|partial|incorrect",\n'
        '  "cause": "誤答時のみ原因分類、正解ならnull",\n'
        '  "note": "簡潔な解説（根拠の節・式番号を含める）",\n'
        '  "explanation": "誤答時の補足（前提知識の提示など）",\n'
        '  "advice": "次への一言アドバイス（日本語）"\n'
        "}"
    )
    return _message(system, lead, max_tokens=2000)


def explain_stuck(active: dict, day_index: int, answer_text: str) -> str:
    """[途中] 回答の詰まりに対する、答えを与えない最小限のヒントを生成する。"""
    questions = active.get("questions", [])
    q = questions[day_index] if 0 <= day_index < len(questions) else ""
    ev = active.get("evidence_quotes") or []
    evidence = ev[day_index] if day_index < len(ev) else []
    system = (
        "あなたはPMSMセンサレス制御の学習コーチです。学習者が今日の問いで詰まっています。"
        "答えを直接与えず、詰まりを解くための最小限のヒントを2〜3文で示してください。"
        "必要なら関連する前提概念の直観や、本文の該当箇所（節・式番号）を指し示してください。"
        "テキストのみを返してください（JSONにしない）。"
    )
    payload = {
        "question": q,
        "evidence_quotes": evidence,
        "prerequisites": active.get("prerequisites") or [],
        "learner_answer": answer_text,
    }
    lead = "次の情報をもとにヒントを作成:\n" + json.dumps(payload, ensure_ascii=False, indent=2)
    return _message(system, lead, max_tokens=600, expect_json=False)
