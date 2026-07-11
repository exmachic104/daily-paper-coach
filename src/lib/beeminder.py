"""Beeminder 連携。

ゴールタイプは Do More（1日1データポイント必須）。回答があった朝、または
!skip / 配信失敗による免除日に value=1 を送信する。データポイントを送らない日は
Beeminder 側で自動的に derail（課金）が発生する。
"""
from __future__ import annotations

import requests

from .config import config


def _datapoints_url() -> str:
    return (
        f"https://www.beeminder.com/api/v1/users/{config.BEEMINDER_USERNAME}"
        f"/goals/{config.BEEMINDER_GOAL}/datapoints.json"
    )


def submit_datapoint(comment: str, value: int = 1) -> bool:
    """データポイント（value=1）を送信して目標達成を記録する。

    DRY_RUN_PENALTY=1 の間は送信をスキップする（通しテスト用）。
    成功時 True、送信スキップ・失敗時 False を返す。
    """
    if config.DRY_RUN_PENALTY:
        print(f"[beeminder] DRY_RUN: データポイント送信をスキップ ({comment})")
        return False

    config.require("BEEMINDER_USERNAME", "BEEMINDER_AUTH_TOKEN", "BEEMINDER_GOAL")
    data = {
        "auth_token": config.BEEMINDER_AUTH_TOKEN,
        "value": value,
        "comment": comment,
    }
    try:
        resp = requests.post(_datapoints_url(), data=data, timeout=30)
        resp.raise_for_status()
        print(f"[beeminder] データポイント送信成功: {comment}")
        return True
    except Exception as e:  # noqa: BLE001
        print(f"[beeminder] データポイント送信失敗: {e}")
        return False
