"""一回限りの採点復旧スクリプト。

13日のクイズ（data/recover_quiz.json）に対するユーザーの解答を Discord から
取得して採点し、結果を投稿する。配信・出題・状態更新は行わない。
使い終わったらこのスクリプトとワークフロー・復旧用ファイルは削除してよい。
"""
from __future__ import annotations

import json
import os
import sys

from lib import claude, discord, store

RECOVER_PATH = os.path.join(store.DATA_DIR, "recover_quiz.json")


def _collect_recent_answers(limit: int = 60) -> str:
    """直近のユーザー投稿から、コマンドでない解答らしきものを収集する。"""
    msgs = discord.fetch_messages_after(None, limit=limit)
    answers: list[str] = []
    for m in msgs:
        if not discord.is_user_message(m):
            continue
        content = (m.get("content") or "").strip()
        if not content or content.startswith("!"):
            continue
        answers.append(content)
    # 直近の解答ブロックを対象にする（末尾数件）
    return "\n".join(answers[-5:])


def main() -> None:
    if not os.path.exists(RECOVER_PATH):
        print("recover_quiz.json が見つかりません。", file=sys.stderr)
        sys.exit(1)
    quiz = json.load(open(RECOVER_PATH, encoding="utf-8"))

    answers = _collect_recent_answers()
    if not answers.strip():
        discord.post_text("ℹ️ 復旧採点: Discord から解答を取得できませんでした。")
        print("解答が見つかりませんでした。", file=sys.stderr)
        sys.exit(1)

    print("取得した解答:\n", answers)
    result = claude.grade(quiz, answers, store.recent_log(14))

    title = (quiz.get("paper") or {}).get("title", "")
    lines = [f"📝 **採点結果（{quiz.get('date')} 分の復旧採点）** — {title}", ""]
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

    # ログにも記録（重複しないよう date で確認）
    if not any(rec.get("date") == quiz.get("date") for rec in store.load_log()):
        store.append_log({
            "date": quiz.get("date"),
            "paper": quiz.get("paper", {}),
            "answered": True,
            "self_reported_status": result.get("reported_status"),
            "results": result.get("results", []),
            "penalty": False,
            "adjustment": result.get("adjustment"),
            "recovered": True,
        })
        store.git_commit_and_push(f"recover: grade {quiz.get('date')}")
    print("復旧採点 完了")


if __name__ == "__main__":
    main()
