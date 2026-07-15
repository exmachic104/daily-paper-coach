"""一回限りの差し替え配信スクリプト。

本日配信された論文が既読と重複していた場合に、その論文を既読記録へ追加して
除外し、別の論文を選び直して配信し直す。採点は行わない。
使い終わったらこのスクリプトとワークフローは削除してよい。
"""
from __future__ import annotations

import sys
import traceback

import morning
from lib import discord, store


def main() -> None:
    try:
        today = morning._today()
        pending = store.load_pending_quiz()
        if pending:
            p = pending.get("paper", {})
            # 重複していた論文を既読記録に入れて今後も除外する
            store.add_delivered(p.get("s2_paper_id", ""), p.get("title", ""))
            discord.post_text(
                "⚠️ 本日の配信が既読論文と重複していたため差し替えます。"
                "先ほどのクイズには回答せず、この後の新しいクイズにご回答ください。"
            )
        store.clear_pending_quiz()

        new_pending = morning._select_and_deliver([], today)
        store.save_pending_quiz(new_pending)
        store.git_commit_and_push(f"redeliver {today}: replace duplicate paper")
        print("差し替え配信 完了")
    except Exception as e:  # noqa: BLE001
        print(traceback.format_exc(), file=sys.stderr)
        try:
            discord.post_text(f"⚠️ **差し替え配信でエラー**\n```\n{str(e)[:1500]}\n```")
        except Exception:  # noqa: BLE001
            pass
        sys.exit(1)


if __name__ == "__main__":
    main()
