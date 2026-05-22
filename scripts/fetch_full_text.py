"""
Fetch full article text for scored-relevant articles.

For each article in articles where:
    - it has a Claude score with relevance >= MIN_RELEVANCE
    - we have not already fetched it (no row in article_text yet, or previous attempt errored)
fetch the URL, run trafilatura on the HTML, and store the result in article_text.

Status values:
    success            — clean extraction, > MIN_GOOD_CHARS characters
    paywall_or_short   — HTTP 200 but extracted text < MIN_GOOD_CHARS (probably paywalled, JS-rendered, or partial)
    unsupported        — URL we know we can't extract (YouTube, video embeds, PDFs we don't OCR, etc.)
    error              — HTTP 4xx/5xx
    timeout            — request timeout

Idempotent: re-runs skip articles whose status is already 'success'.
Re-fetches failed attempts so transient errors get retried.
"""
from __future__ import annotations

import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

import requests
import trafilatura

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.config import ANTHROPIC_MODEL, RISK_DB_PATH
from src.risk_db import utcnow
from src.risk_schema import connect, init_db
from src.risk_scoring import PROMPT_VERSION

MIN_RELEVANCE = 30
MIN_GOOD_CHARS = 500
HTTP_TIMEOUT = 20
PAUSE_BETWEEN = 1.0   # politeness delay between hosts

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

UNSUPPORTED_DOMAINS = {
    "youtube.com",
    "youtu.be",
    "vimeo.com",
    "tiktok.com",
    "instagram.com",
}


def is_unsupported(url: str) -> str | None:
    try:
        host = urlparse(url).netloc.lower()
        host = host[4:] if host.startswith("www.") else host
    except Exception:
        return None
    for d in UNSUPPORTED_DOMAINS:
        if host == d or host.endswith("." + d):
            return host
    if url.lower().endswith(".pdf"):
        return "pdf"
    return None


def _fetch_via(url: str) -> dict:
    """Single attempt against `url`. Always returns; never raises."""
    try:
        resp = requests.get(
            url,
            timeout=HTTP_TIMEOUT,
            headers={"User-Agent": USER_AGENT, "Accept-Language": "en-US,en;q=0.9"},
            allow_redirects=True,
        )
    except requests.Timeout:
        return {"fetch_status": "timeout", "error_msg": f"timeout {HTTP_TIMEOUT}s",
                "http_status": None, "raw_html_size": 0, "extracted_chars": 0, "extracted_text": None}
    except Exception as e:
        return {"fetch_status": "error", "error_msg": f"{type(e).__name__}: {e}",
                "http_status": None, "raw_html_size": 0, "extracted_chars": 0, "extracted_text": None}

    raw_html = resp.text or ""
    raw_size = len(raw_html)

    if resp.status_code >= 400:
        return {"fetch_status": "error", "error_msg": f"HTTP {resp.status_code}",
                "http_status": resp.status_code, "raw_html_size": raw_size,
                "extracted_chars": 0, "extracted_text": None}

    extracted = trafilatura.extract(
        raw_html,
        include_comments=False,
        include_tables=False,
        favor_precision=True,
        url=url,
    ) or ""

    n = len(extracted)
    status = "success" if n >= MIN_GOOD_CHARS else "paywall_or_short"
    return {
        "fetch_status": status,
        "error_msg": None if status == "success" else f"only {n} chars extracted",
        "http_status": resp.status_code,
        "raw_html_size": raw_size,
        "extracted_chars": n,
        "extracted_text": extracted if n > 0 else None,
    }


def find_wayback_url(url: str) -> str | None:
    """Query archive.org availability API → closest snapshot URL, or None."""
    try:
        r = requests.get(
            "https://archive.org/wayback/available",
            params={"url": url},
            timeout=15,
            headers={"User-Agent": USER_AGENT},
        )
        if r.status_code != 200:
            return None
        snap = (r.json() or {}).get("archived_snapshots", {}).get("closest", {})
        if snap.get("available") and snap.get("url"):
            return snap["url"]
    except Exception:
        return None
    return None


def _needs_wayback_fallback(result: dict) -> bool:
    if result["fetch_status"] == "paywall_or_short":
        return True
    if result["fetch_status"] == "error" and result["http_status"] in (401, 403, 451):
        return True
    return False


def fetch_one(canonical_url: str) -> dict:
    """
    Try origin first; on paywall-like failure, fall back to archive.org wayback.
    Always returns a dict; never raises. Sets fetch_source = 'origin' or 'wayback',
    and fetched_url = whatever URL produced the final result.
    """
    unsupported_reason = is_unsupported(canonical_url)
    if unsupported_reason:
        return {
            "fetch_status": "unsupported",
            "error_msg": f"skipped ({unsupported_reason})",
            "http_status": None, "raw_html_size": 0,
            "extracted_chars": 0, "extracted_text": None,
            "fetched_url": canonical_url, "fetch_source": "origin",
        }

    origin = _fetch_via(canonical_url)
    origin["fetched_url"] = canonical_url
    origin["fetch_source"] = "origin"

    if not _needs_wayback_fallback(origin):
        return origin

    # Try wayback as a rescue
    print("    -> trying wayback fallback...")
    wb_url = find_wayback_url(canonical_url)
    if not wb_url:
        return origin

    wayback = _fetch_via(wb_url)
    wayback["fetched_url"] = wb_url
    wayback["fetch_source"] = "wayback"

    # Accept wayback only if it actually improves the result
    if wayback["fetch_status"] == "success":
        return wayback
    if wayback["extracted_chars"] > origin["extracted_chars"]:
        return wayback
    return origin


def articles_to_fetch(conn) -> list[dict]:
    """Articles with relevance >= MIN_RELEVANCE that we have NOT successfully fetched yet."""
    rows = conn.execute(
        """
        SELECT a.id, a.ticker, a.title, a.url, a.domain, s.relevance, s.severity, s.stance
        FROM articles a
        JOIN article_scores s
          ON s.article_id     = a.id
         AND s.model          = ?
         AND s.prompt_version = ?
        LEFT JOIN article_text t ON t.article_id = a.id
        WHERE s.relevance >= ?
          AND (t.article_id IS NULL OR t.fetch_status NOT IN ('success', 'unsupported'))
        ORDER BY ABS(s.severity) DESC, s.relevance DESC;
        """,
        (ANTHROPIC_MODEL, PROMPT_VERSION, MIN_RELEVANCE),
    ).fetchall()
    return [dict(r) for r in rows]


def upsert_text(conn, article_id: int, result: dict) -> None:
    conn.execute(
        """
        INSERT INTO article_text
            (article_id, url, fetched_at_utc, http_status, fetch_status, error_msg,
             raw_html_size, extracted_chars, extracted_text, extractor, extractor_version,
             fetch_source)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(article_id) DO UPDATE SET
            url               = excluded.url,
            fetched_at_utc    = excluded.fetched_at_utc,
            http_status       = excluded.http_status,
            fetch_status      = excluded.fetch_status,
            error_msg         = excluded.error_msg,
            raw_html_size     = excluded.raw_html_size,
            extracted_chars   = excluded.extracted_chars,
            extracted_text    = excluded.extracted_text,
            extractor         = excluded.extractor,
            extractor_version = excluded.extractor_version,
            fetch_source      = excluded.fetch_source;
        """,
        (
            article_id, result["fetched_url"], utcnow(),
            result["http_status"], result["fetch_status"], result["error_msg"],
            result["raw_html_size"], result["extracted_chars"], result["extracted_text"],
            "trafilatura", trafilatura.__version__,
            result["fetch_source"],
        ),
    )


def main() -> None:
    conn = connect(RISK_DB_PATH)
    init_db(conn)

    articles = articles_to_fetch(conn)
    print(f"Articles to fetch: {len(articles)}\n")

    last_host: str | None = None
    for i, art in enumerate(articles, 1):
        url = art["url"]
        sym = f"[{art['ticker']}] " if art["ticker"] else ""
        print(f"--- {i}/{len(articles)} ---")
        print(f"{sym}{art['title'][:100]}")
        print(f"  url: {url}")

        host = (urlparse(url).netloc.lower() if url else "")
        if last_host == host:
            time.sleep(PAUSE_BETWEEN)
        last_host = host

        result = fetch_one(url)
        upsert_text(conn, art["id"], result)
        conn.commit()

        tag = f"via {result['fetch_source']}"
        if result["fetch_status"] == "success":
            print(f"  OK  {result['extracted_chars']} chars from {result['raw_html_size']:>6} bytes HTML  ({tag})")
        else:
            print(f"  {result['fetch_status'].upper()}  {result['error_msg']}  ({tag})")
        print()

    # ── Summary ─────────────────────────────────────────────────────────
    print("=" * 70)
    print("FETCH STATUS BY DOMAIN\n")
    for r in conn.execute(
        """
        SELECT a.domain, t.fetch_status, t.fetch_source, COUNT(*) AS n,
               AVG(t.extracted_chars) AS avg_chars
        FROM article_text t
        JOIN articles a ON a.id = t.article_id
        GROUP BY a.domain, t.fetch_status, t.fetch_source
        ORDER BY a.domain, t.fetch_status;
        """
    ):
        chars = f"{int(r['avg_chars']):>5} avg chars" if r['avg_chars'] else ""
        print(f"  {r['domain']:35s}  {r['fetch_status']:18s}  src={r['fetch_source']:7s}  n={r['n']}  {chars}")

    print("\nTOTAL CHARACTERS NOW STORED:")
    total = conn.execute(
        "SELECT COALESCE(SUM(extracted_chars), 0) AS n FROM article_text;"
    ).fetchone()["n"]
    print(f"  {total:,} chars across all successfully extracted articles")

    conn.close()


if __name__ == "__main__":
    main()
