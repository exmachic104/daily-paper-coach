"""環境変数ベースの設定。

GitHub Actions では Secrets が環境変数として渡される。ローカル（Windows）でも
同じ環境変数を設定すれば手動実行できる。
"""
from __future__ import annotations

import os


def _get(name: str, default: str | None = None, required: bool = False) -> str | None:
    val = os.environ.get(name, default)
    if required and not val:
        raise RuntimeError(
            f"必須の環境変数 {name} が設定されていません。REQUIREMENTS.md の §10 を参照してください。"
        )
    return val


class Config:
    # Anthropic
    ANTHROPIC_API_KEY = _get("ANTHROPIC_API_KEY")
    # モデルは差し替え可能。既定は最新の Opus。
    ANTHROPIC_MODEL = _get("ANTHROPIC_MODEL", "claude-opus-4-8")

    # Discord
    DISCORD_WEBHOOK_URL = _get("DISCORD_WEBHOOK_URL")
    DISCORD_BOT_TOKEN = _get("DISCORD_BOT_TOKEN")
    DISCORD_CHANNEL_ID = _get("DISCORD_CHANNEL_ID")

    # Beeminder
    BEEMINDER_USERNAME = _get("BEEMINDER_USERNAME")
    BEEMINDER_AUTH_TOKEN = _get("BEEMINDER_AUTH_TOKEN")
    BEEMINDER_GOAL = _get("BEEMINDER_GOAL", "paper-quiz")

    # OpenAlex polite pool 用メール（Gmail 可）
    USER_EMAIL = _get("USER_EMAIL", "anonymous@example.com")

    # 通しテストが終わるまでペナルティを無効化できる
    DRY_RUN_PENALTY = _get("DRY_RUN_PENALTY", "0") == "1"

    @classmethod
    def require(cls, *names: str) -> None:
        """指定した設定値が揃っているか検証する。"""
        missing = [n for n in names if not getattr(cls, n, None)]
        if missing:
            raise RuntimeError(
                "必須の環境変数が未設定です: " + ", ".join(missing)
            )


config = Config()
