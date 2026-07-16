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
        "あなたはPMSMセンサレス制御の学習コーチです。与えられた論文を読み、"
        "15分で読むための日本語ガイドと、理解度確認クイズ3問を作成します。\n"
        "出題カテゴリは固定（ただし要約より深い粒度にすること）:\n"
        "- Q1（論点把握）: この論文が解決しようとした課題の本質・なぜそれが問題なのか\n"
        "- Q2（手法理解）: 提案手法の核となる仕組み・原理（切替基準や設計の要点など）\n"
        "- Q3（進歩性）: 従来手法と比べた具体的な改善点・検証方法・残る限界\n\n"
        "【最重要・ネタバレ防止】配信時の要約(summary)と読みどころ(reading_guide)は、"
        "『何を扱う論文か・なぜ重要か・どこを読むべきか』を伝える動機づけに留めること。"
        "提案手法の具体的な仕組み、切替の数値基準、定量的な検証結果、新規性の核心といった"
        "『クイズの答えそのもの』を要約・読みどころに書いてはいけない（それらは読者が本文で"
        "確認する対象）。クイズ3問は、要約だけでは答えられず、指定セクションの詳細（式や図の"
        "意味、設計の根拠、定量的な結果、残る限界）を読んで初めて答えられる深さにすること。"
        "要約の文言をなぞるだけの問いは禁止。\n"
        "各問は短い文章(1〜3文)で答えられる形式。すべて日本語。必ず有効な JSON のみを返してください。"
    )
    lead = (
        f"論文メタ情報: {json.dumps(paper_meta, ensure_ascii=False)}\n"
        f"読むべきセクションの候補（参考。実際の論文の章立てと違えば無視してよい）: {assigned_sections_hint}\n"
        f"ロードマップ上の位置づけ: {roadmap_position}\n\n"
        "上記の論文について、次の JSON 形式で返してください:\n"
        "{\n"
        '  "assigned_sections": "この論文で実際に読むべきセクションを、論文中に存在する章・節・図表名で簡潔に指定（例: Sec.1, 2.3-2.4, Fig.4-5）。存在しない章名を書かないこと。reading_guide と必ず一致させる",\n'
        '  "summary": "3〜4文の日本語要約。何が課題で、どんなアプローチを提案し、大枠で何が新しいかを見出しレベルで述べる。ただし切替の数値基準・具体的な機構・定量的な検証結果などクイズの答えは書かない",\n'
        '  "reading_guide": "今日の読みどころ。どこを読むべきか・飛ばしてよい箇所・注目図表へ誘導する。クイズの答えは明かさない",\n'
        '  "questions": ["Q1", "Q2", "Q3"],\n'
        '  "model_answers": ["Q1の模範解答", "Q2の模範解答", "Q3の模範解答"],\n'
        '  "key_points": "採点時に参照する、assigned_sections の内容の要点（日本語、箇条書き可）。ここには答えを詳しく書いてよい（配信されず採点時のみ使用）"\n'
        "}"
    )
    content = build_paper_content(lead, pdf_bytes, pdf_text)
    return _message(system, content, max_tokens=5000)


# ---- 4. 採点と誤答原因の推定 ----------------------------------------------

def grade(quiz: dict, user_answers_raw: str, recent_log: list[dict]) -> dict:
    """回答を採点し、誤答の原因を推定してフィードバックを生成する。"""
    system = (
        "あなたはPMSMセンサレス制御の学習コーチです。ユーザーの回答を採点し、"
        "誤答があればその原因を推定します。原因分類は次の4つのいずれか:\n"
        "- 時間不足: 該当箇所まで読めていない\n"
        "- 概念の誤解: 読んだが原理を取り違えている\n"
        "- 前提知識の不足: 論文以前の基礎概念でつまずいている\n"
        "- 問題の読み違え: 理解はしているが問いとずれた回答をしている\n"
        "すべて日本語。必ず有効な JSON のみを返してください。"
    )
    context = {
        "summary": quiz.get("summary"),
        "key_points": quiz.get("key_points"),
        "questions": quiz.get("questions"),
        "model_answers": quiz.get("model_answers"),
        "assigned_sections": quiz.get("assigned_sections"),
    }
    lead = (
        "論文の要約・要点・出題・模範解答:\n"
        f"{json.dumps(context, ensure_ascii=False, indent=2)}\n\n"
        "直近2週間の学習ログ（誤答傾向の文脈）:\n"
        f"{json.dumps(recent_log, ensure_ascii=False, indent=2)}\n\n"
        "ユーザーの回答（生テキスト。冒頭に読了状況 [読了]/[途中]/[未読] が付く想定）:\n"
        f"{user_answers_raw}\n\n"
        "次の JSON 形式で採点結果を返してください:\n"
        "{\n"
        '  "reported_status": "読了 | 途中 | 未読（回答から推定）",\n'
        '  "results": [\n'
        '    {"q": 1, "verdict": "correct|partial|incorrect", "cause": "誤答時のみ原因分類、正解ならnull", '
        '"note": "簡潔な解説", "explanation": "誤答時の補足（基礎トピックの提示など）"}\n'
        "  ],\n"
        '  "advice": "採点結果を踏まえた次回への一言アドバイス（日本語）",\n'
        '  "adjustment": "翌日以降の選定への反映提案（例: 同テーマの易しめ、基礎解説を追加、読む範囲を狭める）"\n'
        "}\n"
        "results は3問分、q は 1,2,3 の順で必ず含めてください。"
    )
    return _message(system, lead, max_tokens=3000)
