"""リポジトリ内の JSON/データファイルの読み書きと git commit。

学習ログ・ロードマップ・出題ストックはすべてリポジトリ内のファイルで管理し、
GitHub Actions から commit & push する。
"""
from __future__ import annotations

import json
import os
import subprocess
from typing import Any

# このファイルは src/lib/store.py にある。リポジトリ直下は 2 つ上。
_REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
DATA_DIR = os.path.join(_REPO_ROOT, "data")

LOG_PATH = os.path.join(DATA_DIR, "log.json")
ROADMAP_PATH = os.path.join(DATA_DIR, "roadmap.json")
PENDING_QUIZ_PATH = os.path.join(DATA_DIR, "pending_quiz.json")


def _read_json(path: str, default: Any) -> Any:
    if not os.path.exists(path):
        return default
    with open(path, "r", encoding="utf-8") as f:
        content = f.read().strip()
    if not content:
        return default
    return json.loads(content)


def _write_json(path: str, data: Any) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.write("\n")


# ---- 学習ログ -------------------------------------------------------------

def load_log() -> list[dict]:
    return _read_json(LOG_PATH, [])


def append_log(record: dict) -> None:
    log = load_log()
    log.append(record)
    _write_json(LOG_PATH, log)


def recent_log(days: int = 14) -> list[dict]:
    """直近 N 件のログ（誤答傾向の文脈用）。日付比較ではなく末尾 N 件で近似。"""
    return load_log()[-days:]


def read_paper_ids() -> set[str]:
    """既読論文の s2_paper_id 集合（重複選定の回避に使う）。"""
    ids: set[str] = set()
    for rec in load_log():
        pid = (rec.get("paper") or {}).get("s2_paper_id")
        if pid:
            ids.add(pid)
    return ids


# ---- ロードマップ ---------------------------------------------------------

def load_roadmap() -> dict:
    return _read_json(ROADMAP_PATH, {})


def save_roadmap(roadmap: dict) -> None:
    _write_json(ROADMAP_PATH, roadmap)


# ---- 出題ストック ---------------------------------------------------------

def load_pending_quiz() -> dict | None:
    if not os.path.exists(PENDING_QUIZ_PATH):
        return None
    data = _read_json(PENDING_QUIZ_PATH, None)
    return data or None


def save_pending_quiz(quiz: dict) -> None:
    _write_json(PENDING_QUIZ_PATH, quiz)


def clear_pending_quiz() -> None:
    if os.path.exists(PENDING_QUIZ_PATH):
        os.remove(PENDING_QUIZ_PATH)


# ---- git --------------------------------------------------------------

def git_commit_and_push(message: str) -> None:
    """data/ 配下の変更を commit & push する。

    GitHub Actions 上では actions/checkout のトークンで push できる。
    ローカルでは通常の git 認証を使う。変更が無ければ何もしない。
    """
    def run(*args: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            ["git", *args], cwd=_REPO_ROOT, capture_output=True, text=True
        )

    # CI 上で user が未設定でも commit できるようにする
    if os.environ.get("GITHUB_ACTIONS") == "true":
        run("config", "user.name", "daily-paper-coach[bot]")
        run("config", "user.email", "actions@github.com")

    run("add", "data")
    status = run("status", "--porcelain", "data")
    if not status.stdout.strip():
        print("[store] 変更なし。commit をスキップします。")
        return

    commit = run("commit", "-m", message)
    if commit.returncode != 0:
        print("[store] git commit 失敗:", commit.stderr)
        return

    push = run("push")
    if push.returncode != 0:
        print("[store] git push 失敗:", push.stderr)
    else:
        print("[store] commit & push 完了:", message)
