"""論文検索とPDF取得。

- 主: Semantic Scholar API（キーなし）
- フォールバック: OpenAlex API（キー不要、mailto で polite pool）
- 補助: arXiv API

オープンアクセスPDFが存在する候補のみを返す。
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

import requests

from .config import config

S2_SEARCH = "https://api.semanticscholar.org/graph/v1/paper/search"
OPENALEX_SEARCH = "https://api.openalex.org/works"


@dataclass
class Candidate:
    """論文候補の共通表現。"""

    paper_id: str  # s2_paper_id 相当（ソース接頭辞付き）
    title: str
    abstract: str
    year: int | None
    venue: str
    authors: list[str]
    pdf_url: str
    landing_url: str
    source: str  # "semantic_scholar" | "openalex" | "arxiv"
    extra: dict[str, Any] = field(default_factory=dict)

    def summary_for_prompt(self) -> str:
        auth = ", ".join(self.authors[:4])
        if len(self.authors) > 4:
            auth += ", et al."
        abs = (self.abstract or "")[:600]
        return (
            f"[{self.paper_id}] {self.title} ({self.year or '?'}, {self.venue})\n"
            f"  著者: {auth}\n"
            f"  概要: {abs}"
        )


# ---- Semantic Scholar -----------------------------------------------------

def _search_semantic_scholar(query: str, limit: int = 10) -> list[Candidate]:
    fields = "title,abstract,year,venue,authors,openAccessPdf,url,externalIds"
    params = {"query": query, "limit": limit, "fields": fields}

    last_exc: Exception | None = None
    for attempt in range(4):
        resp = requests.get(S2_SEARCH, params=params, timeout=30)
        if resp.status_code == 429:  # レート制限 → 指数バックオフ
            time.sleep(2 ** attempt)
            last_exc = RuntimeError("Semantic Scholar rate limited (429)")
            continue
        resp.raise_for_status()
        data = resp.json()
        out: list[Candidate] = []
        for p in data.get("data", []):
            oa = p.get("openAccessPdf") or {}
            pdf = oa.get("url")
            if not pdf:
                continue  # OA PDF 必須
            out.append(
                Candidate(
                    paper_id=p.get("paperId", ""),
                    title=p.get("title") or "",
                    abstract=p.get("abstract") or "",
                    year=p.get("year"),
                    venue=p.get("venue") or "",
                    authors=[a.get("name", "") for a in (p.get("authors") or [])],
                    pdf_url=pdf,
                    landing_url=p.get("url") or pdf,
                    source="semantic_scholar",
                )
            )
        return out
    raise last_exc or RuntimeError("Semantic Scholar 検索に失敗しました。")


# ---- OpenAlex -------------------------------------------------------------

def _search_openalex(query: str, limit: int = 10) -> list[Candidate]:
    params = {
        "search": query,
        "filter": "is_oa:true",
        "per-page": limit,
        "mailto": config.USER_EMAIL,
    }
    resp = requests.get(OPENALEX_SEARCH, params=params, timeout=30)
    resp.raise_for_status()
    data = resp.json()
    out: list[Candidate] = []
    for w in data.get("results", []):
        oa = w.get("open_access") or {}
        pdf = oa.get("oa_url")
        if not pdf:
            continue
        # abstract は inverted index で提供されるため復元する
        abstract = _reconstruct_abstract(w.get("abstract_inverted_index"))
        authorships = w.get("authorships") or []
        authors = [
            (a.get("author") or {}).get("display_name", "") for a in authorships
        ]
        venue = ((w.get("primary_location") or {}).get("source") or {}).get(
            "display_name", ""
        )
        out.append(
            Candidate(
                paper_id=w.get("id", "").rsplit("/", 1)[-1],  # 例: W1234567890
                title=w.get("title") or w.get("display_name") or "",
                abstract=abstract,
                year=w.get("publication_year"),
                venue=venue or "",
                authors=authors,
                pdf_url=pdf,
                landing_url=w.get("id") or pdf,
                source="openalex",
            )
        )
    return out


def _reconstruct_abstract(inverted: dict | None) -> str:
    if not inverted:
        return ""
    positions: list[tuple[int, str]] = []
    for word, idxs in inverted.items():
        for i in idxs:
            positions.append((i, word))
    positions.sort()
    return " ".join(w for _, w in positions)[:1200]


# ---- 統合検索 -------------------------------------------------------------

def search(queries: list[str], exclude_ids: set[str], limit_per_query: int = 8) -> list[Candidate]:
    """複数クエリで検索し、OA PDF のある候補を重複排除して返す。

    Semantic Scholar を主に使い、429 で失敗する場合は OpenAlex にフォールバック。
    """
    candidates: list[Candidate] = []
    seen: set[str] = set(exclude_ids)

    for q in queries:
        results: list[Candidate] = []
        try:
            results = _search_semantic_scholar(q, limit_per_query)
        except Exception as e:  # noqa: BLE001
            print(f"[s2] Semantic Scholar 失敗 ('{q}'): {e} → OpenAlex にフォールバック")
            try:
                results = _search_openalex(q, limit_per_query)
            except Exception as e2:  # noqa: BLE001
                print(f"[s2] OpenAlex も失敗 ('{q}'): {e2}")
                continue

        for c in results:
            key = c.paper_id or c.pdf_url
            if not key or key in seen or not c.title:
                continue
            seen.add(key)
            candidates.append(c)

    return candidates


# ---- PDF 取得 -------------------------------------------------------------

def fetch_pdf(url: str, max_bytes: int = 30 * 1024 * 1024) -> bytes | None:
    """PDF をダウンロードする。取得失敗・サイズ超過時は None を返す。"""
    try:
        headers = {"User-Agent": f"daily-paper-coach ({config.USER_EMAIL})"}
        resp = requests.get(url, headers=headers, timeout=60, stream=True)
        resp.raise_for_status()
        content = b""
        for chunk in resp.iter_content(chunk_size=1 << 16):
            content += chunk
            if len(content) > max_bytes:
                print(f"[s2] PDF がサイズ上限を超過: {url}")
                return None
        # 簡易 PDF 判定
        if not content[:5].startswith(b"%PDF"):
            print(f"[s2] PDF ではない可能性: {url}（Content-Type 未確認のまま続行不可）")
            # HTML ランディングページ等は不可
            if b"%PDF" not in content[:1024]:
                return None
        return content
    except Exception as e:  # noqa: BLE001
        print(f"[s2] PDF 取得失敗 ({url}): {e}")
        return None
