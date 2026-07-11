"""Discord 連携。

- 投稿: Webhook（論文配信・出題・採点結果）
- 履歴取得: Bot REST API（ユーザーの回答・コマンドの読み取り）

常駐 Bot は不要。ジョブ実行時に HTTP で読み書きするだけ。
"""
from __future__ import annotations

import time
from typing import Any

import requests

from .config import config

API_BASE = "https://discord.com/api/v10"
# 1 メッセージ 2000 文字制限
MAX_LEN = 1900


def _chunks(text: str, size: int = MAX_LEN) -> list[str]:
    """2000 文字制限に合わせて分割する。できるだけ改行で区切る。"""
    if len(text) <= size:
        return [text]
    parts: list[str] = []
    remaining = text
    while len(remaining) > size:
        cut = remaining.rfind("\n", 0, size)
        if cut <= 0:
            cut = size
        parts.append(remaining[:cut])
        remaining = remaining[cut:].lstrip("\n")
    if remaining:
        parts.append(remaining)
    return parts


def post_text(text: str) -> None:
    """プレーンテキストを Webhook で投稿する（必要に応じて分割）。"""
    for chunk in _chunks(text):
        _post_webhook({"content": chunk})


def post_embed(embed: dict, extra_content: str | None = None) -> str | None:
    """Embed を Webhook で投稿する。投稿メッセージの ID を返す。

    Embed の description は 4096 文字制限があるため、長い場合は本文へ回す。
    """
    payload: dict[str, Any] = {"embeds": [embed]}
    if extra_content:
        payload["content"] = extra_content[:MAX_LEN]
    return _post_webhook(payload, wait=True)


def _post_webhook(payload: dict, wait: bool = False) -> str | None:
    url = config.DISCORD_WEBHOOK_URL
    if not url:
        raise RuntimeError("DISCORD_WEBHOOK_URL が未設定です。")
    if wait:
        url = url + ("&" if "?" in url else "?") + "wait=true"

    for attempt in range(4):
        resp = requests.post(url, json=payload, timeout=30)
        if resp.status_code == 429:  # レート制限
            retry = resp.json().get("retry_after", 1)
            time.sleep(float(retry) + 0.5)
            continue
        resp.raise_for_status()
        if wait and resp.content:
            return resp.json().get("id")
        return None
    raise RuntimeError("Discord Webhook 投稿がレート制限で失敗しました。")


def fetch_messages_after(message_id: str | None, limit: int = 100) -> list[dict]:
    """対象チャンネルの、指定メッセージ以降の履歴を取得する。

    Bot Token と Message Content Intent が必要。message_id が None の場合は
    最新 limit 件を取得する。返り値は古い順。
    """
    config.require("DISCORD_BOT_TOKEN", "DISCORD_CHANNEL_ID")
    headers = {"Authorization": f"Bot {config.DISCORD_BOT_TOKEN}"}
    params: dict[str, Any] = {"limit": min(limit, 100)}
    if message_id:
        params["after"] = message_id

    url = f"{API_BASE}/channels/{config.DISCORD_CHANNEL_ID}/messages"
    resp = requests.get(url, headers=headers, params=params, timeout=30)
    resp.raise_for_status()
    # Discord は新しい順で返すので古い順に並べ替える
    return list(reversed(resp.json()))


def is_user_message(msg: dict) -> bool:
    """Webhook/Bot ではなく、ユーザー本人の投稿かどうか。"""
    if msg.get("webhook_id"):
        return False
    author = msg.get("author") or {}
    if author.get("bot"):
        return False
    return True
