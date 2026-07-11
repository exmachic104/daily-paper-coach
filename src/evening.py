"""夜ジョブ（JST 21:00 / cron: '0 12 * * *'）。

1. pending_quiz.json を読み、質問3問を Discord に投稿する
2. 回答フォーマットの案内を付記する
3. 投稿メッセージ ID を pending_quiz.json に記録する（翌朝の回答収集の基点）

pending_quiz が無い場合は配信失敗として通知し、翌朝のペナルティ判定をスキップさせる。
"""
from __future__ import annotations

import sys
import traceback

from lib import discord, store


def run_evening() -> None:
    pending = store.load_pending_quiz()

    if not pending or not pending.get("questions"):
        discord.post_text(
            "🌙 本日の配信に失敗していたため出題はありません。ペナルティ対象外です。"
        )
        return

    questions = pending.get("questions", [])
    title = pending.get("paper", {}).get("title", "本日の論文")

    lines = [f"🌙 **今夜の理解度確認クイズ** — {title}", ""]
    for i, q in enumerate(questions, start=1):
        lines.append(f"**Q{i}.** {q}")
        lines.append("")
    lines.append("――――――――――――――")
    lines.append("回答方法:")
    lines.append("`A1: ... / A2: ... / A3: ...` の形式で、このチャンネルに返信してください。")
    lines.append("冒頭に読了状況を1つ添えてください: `[読了]` `[途中]` `[未読]`")
    lines.append("期限: 明朝7:00の採点まで")

    text = "\n".join(lines)
    # 投稿メッセージIDを記録して翌朝の回答収集の基点にする
    message_id = discord.post_embed(
        {
            "title": f"理解度確認クイズ — {title[:200]}",
            "description": text[:4000],
            "color": 0xF28E2B,
        }
    )

    pending["posted"] = True
    pending["quiz_message_id"] = message_id
    store.save_pending_quiz(pending)
    store.git_commit_and_push(f"evening {pending.get('date')}: quiz posted")


def main() -> None:
    try:
        run_evening()
    except Exception as e:  # noqa: BLE001
        tb = traceback.format_exc()
        print(tb, file=sys.stderr)
        try:
            discord.post_text(f"⚠️ **夜ジョブでエラーが発生しました**\n```\n{str(e)[:1500]}\n```")
        except Exception:  # noqa: BLE001
            pass
        sys.exit(1)


if __name__ == "__main__":
    main()
