# src/risk_db.py
"""
Helpers for institutional_risk.db — article upsert, taxonomy load + keyword
matching, hit tagging.

CSV is the ingestion layer; this module is the writer + matcher for the SQLite
master store.
"""
from __future__ import annotations

import hashlib
import re
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _sha256(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


def url_hash(url: str | None) -> str | None:
    if not url:
        return None
    return _sha256(url.strip().lower())


def title_hash(title: str | None) -> str | None:
    if not title:
        return None
    return _sha256(title.strip().lower())


# ── articles ─────────────────────────────────────────────────────────────
def upsert_article(
    conn: sqlite3.Connection,
    *,
    ticker: str | None,
    company: str | None,
    published: str | None,
    title: str,
    url: str | None,
    domain: str | None,
    source: str | None,
    snippet: str | None,
    source_country: str | None,
    query_type: str | None,
    source_api: str,
    raw_path: str | None,
) -> tuple[int, bool]:
    """
    Upsert by url_hash. Returns (article_id, inserted).
    `inserted=False` means an existing row matched on url_hash.
    """
    uh = url_hash(url)
    th = title_hash(title)

    if uh:
        existing = conn.execute(
            "SELECT id FROM articles WHERE url_hash = ? LIMIT 1;", (uh,)
        ).fetchone()
        if existing:
            return int(existing["id"]), False

    cur = conn.execute(
        """
        INSERT INTO articles
            (ticker, company, published, title, url, domain, source, snippet,
             source_country, query_type, source_api, raw_path,
             url_hash, title_hash, ingested_at_utc)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
        """,
        (
            (ticker or "").strip().upper() or None,
            company,
            published,
            title.strip(),
            url,
            domain,
            source,
            snippet,
            source_country,
            query_type,
            source_api,
            raw_path,
            uh,
            th,
            utcnow(),
        ),
    )
    return int(cur.lastrowid), True


# ── taxonomy ─────────────────────────────────────────────────────────────
@dataclass(frozen=True)
class TaxonomyRule:
    id: int
    category: str
    keyword: str
    severity: int
    stance: str | None
    pattern: re.Pattern


def _compile_keyword(kw: str) -> re.Pattern:
    """
    Word-boundary, case-insensitive regex from a literal keyword phrase.
    Multiple whitespace tolerated. Hyphens preserved.
    """
    parts = [re.escape(tok) for tok in kw.strip().split()]
    body = r"\s+".join(parts)
    return re.compile(rf"\b{body}\b", re.IGNORECASE)


def load_active_taxonomy(conn: sqlite3.Connection) -> list[TaxonomyRule]:
    rows = conn.execute(
        """
        SELECT id, category, keyword, severity, stance
        FROM query_taxonomy
        WHERE active = 1
        ORDER BY severity DESC, id;
        """
    ).fetchall()
    return [
        TaxonomyRule(
            id=int(r["id"]),
            category=r["category"],
            keyword=r["keyword"],
            severity=int(r["severity"]),
            stance=r["stance"],
            pattern=_compile_keyword(r["keyword"]),
        )
        for r in rows
    ]


def match_keywords(
    text: str, rules: list[TaxonomyRule]
) -> list[tuple[int, str]]:
    """
    Returns [(taxonomy_id, matched_text), ...] for each rule whose pattern hits.
    First match per rule is captured (matched_text).
    """
    hits: list[tuple[int, str]] = []
    for rule in rules:
        m = rule.pattern.search(text)
        if m:
            hits.append((rule.id, m.group(0)))
    return hits


def tag_article_hits(
    conn: sqlite3.Connection,
    article_id: int,
    hits: list[tuple[int, str]],
) -> int:
    """
    Insert hits, ignoring duplicates (article × taxonomy is primary key).
    Returns number of new rows inserted.
    """
    if not hits:
        return 0
    cur = conn.executemany(
        """
        INSERT OR IGNORE INTO article_keyword_hits (article_id, taxonomy_id, matched_text)
        VALUES (?, ?, ?);
        """,
        [(article_id, tax_id, matched) for tax_id, matched in hits],
    )
    return cur.rowcount or 0


# ── companies ────────────────────────────────────────────────────────────
def load_active_tickers(conn: sqlite3.Connection) -> list[tuple[str, str]]:
    rows = conn.execute(
        "SELECT ticker, company FROM companies WHERE status = 'active' ORDER BY ticker;"
    ).fetchall()
    return [(r["ticker"], r["company"]) for r in rows]
