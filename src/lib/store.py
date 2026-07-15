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
STATE_PATH = os.path.join(DATA_DIR, "state.json")


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


def _norm_title(t: str) -> str:
    """重複排除用のタイトル正規化（s2 と同一ルール）。"""
    import re

    return re.sub(r"[^a-z0-9]", "", (t or "").lower())


def read_paper_ids() -> set[str]:
    """ログ由来の既読 s2_paper_id 集合。"""
    ids: set[str] = set()
    for rec in load_log():
        pid = (rec.get("paper") or {}).get("s2_paper_id")
        if pid:
            ids.add(pid)
    return ids


def read_paper_titles() -> set[str]:
    """ログ由来の既読タイトル（正規化）集合。"""
    titles: set[str] = set()
    for rec in load_log():
        t = (rec.get("paper") or {}).get("title")
        if t:
            titles.add(_norm_title(t))
    return titles


# ---- 配信済み論文の記録（採点を待たずに既読化する） -----------------------

def _delivered() -> list[dict]:
    return _read_json(STATE_PATH, {}).get("delivered", [])


def add_delivered(paper_id: str, title: str) -> None:
    """配信した論文を state.json に記録する（重複選定の防止）。"""
    state = _read_json(STATE_PATH, {})
    lst = state.get("delivered", [])
    if paper_id and not any(d.get("id") == paper_id for d in lst):
        lst.append({"id": paper_id, "title": title})
    state["delivered"] = lst
    _write_json(STATE_PATH, state)


def delivered_ids() -> set[str]:
    return {d["id"] for d in _delivered() if d.get("id")}


def delivered_titles() -> set[str]:
    return {_norm_title(d["title"]) for d in _delivered() if d.get("title")}


def excluded_ids() -> set[str]:
    """選定から除外すべき id（ログ既読 + 配信済み）。"""
    return read_paper_ids() | delivered_ids()


def excluded_titles() -> set[str]:
    """選定から除外すべきタイトル（ログ既読 + 配信済み、正規化）。"""
    return read_paper_titles() | delivered_titles()


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


# ---- 実行状態（冪等化用） --------------------------------------------------

def get_last_morning_date() -> str | None:
    """最後に朝ジョブを完了した日付（JST）。同日二重実行のスキップに使う。"""
    return _read_json(STATE_PATH, {}).get("last_morning_date")


def set_last_morning_date(date: str) -> None:
    state = _read_json(STATE_PATH, {})
    state["last_morning_date"] = date
    _write_json(STATE_PATH, state)


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
        # 競合等で失敗したら rebase して1回だけ再試行（状態消失の防止）
        print("[store] git push 失敗、rebase して再試行:", push.stderr)
        run("pull", "--rebase")
        push = run("push")
    if push.returncode != 0:
        print("[store] git push 再試行も失敗:", push.stderr)
    else:
        print("[store] commit & push 完了:", message)
