"""
Ingest FMP news → CSV (raw layer) + institutional_risk.db (master).

Sources pulled:
    1. /stable/news/general-latest        (broad market news, no ticker)
    2. /stable/news/stock-latest          (ticker-tagged equity news)
    3. /stable/news/press-releases-latest (company press releases)
    4. /stable/news/stock?symbols=TICKER  (per active ticker in company_master)

Flow per item:
    raw row -> data/raw_articles/fmp_news_YYYYMMDD_HHMMSS.csv
            -> upsert into articles (dedup on url_hash)
            -> match title+snippet against query_taxonomy
            -> insert into article_keyword_hits

Idempotent: re-running the same items is a no-op (url_hash unique).
"""
from __future__ import annotations

import csv
import sys
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.config import RAW_ARTICLES_DIR, RISK_DB_PATH
from src.fmp import FMPClient
from src.risk_db import (
    load_active_taxonomy,
    load_active_tickers,
    match_keywords,
    tag_article_hits,
    upsert_article,
)
from src.risk_schema import connect, init_db


GLOBAL_FEEDS: list[tuple[str, str, str, int]] = [
    # (label, fmp_method, source_api, pages)
    ("general",        "news_general_latest",         "fmp_general", 10),
    ("stock",          "news_stock_latest",           "fmp_stock",   10),
    ("press_releases", "news_press_releases_latest",  "fmp_press",   10),
]
PAGES_PER_TICKER = 5
LIMIT = 100

RAW_FIELDS = [
    "published", "ticker", "company", "source", "domain",
    "title", "url", "snippet", "source_api", "query_type",
]


def _domain(url: str | None) -> str | None:
    if not url:
        return None
    try:
        host = urlparse(url).netloc.lower()
        return host[4:] if host.startswith("www.") else host
    except Exception:
        return None


def _to_raw_row(item: dict, source_api: str, query_type: str) -> dict:
    return {
        "published":   item.get("publishedDate"),
        "ticker":      (item.get("symbol") or "").upper() or None,
        "company":     None,  # FMP news doesn't carry company name; resolved at score-time
        "source":      item.get("publisher") or item.get("site"),
        "domain":      _domain(item.get("url")) or item.get("site"),
        "title":       (item.get("title") or "").strip(),
        "url":         item.get("url"),
        "snippet":     (item.get("text") or "").strip(),
        "source_api":  source_api,
        "query_type":  query_type,
    }


def main() -> None:
    fmp = FMPClient()
    conn = connect(RISK_DB_PATH)
    init_db(conn)

    rules = load_active_taxonomy(conn)
    print(f"Loaded {len(rules)} active taxonomy rules.")

    active_tickers = load_active_tickers(conn)
    print(f"Active tickers: {[t for t, _ in active_tickers]}")

    raw_dir = Path(RAW_ARTICLES_DIR)
    raw_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    raw_path = raw_dir / f"fmp_news_{stamp}.csv"

    seen_urls: set[str] = set()
    raw_rows: list[dict] = []
    inserted = 0
    matched = 0
    hits_total = 0

    def _process_item(item: dict, source_api: str, query_type: str) -> None:
        nonlocal inserted, matched, hits_total
        url = (item.get("url") or "").strip()
        if not url or url in seen_urls:
            return
        seen_urls.add(url)

        raw = _to_raw_row(item, source_api, query_type)
        raw_rows.append(raw)

        article_id, did_insert = upsert_article(
            conn,
            ticker=raw["ticker"],
            company=raw["company"],
            published=raw["published"],
            title=raw["title"] or "(untitled)",
            url=raw["url"],
            domain=raw["domain"],
            source=raw["source"],
            snippet=raw["snippet"],
            source_country=None,
            query_type=raw["query_type"],
            source_api=raw["source_api"],
            raw_path=str(raw_path),
        )
        if did_insert:
            inserted += 1

        blob = f"{raw['title']}  {raw['snippet']}"
        hits = match_keywords(blob, rules)
        if hits:
            new_hits = tag_article_hits(conn, article_id, hits)
            if new_hits:
                matched += 1
                hits_total += new_hits

    # Global feeds
    for label, method_name, source_api, pages in GLOBAL_FEEDS:
        print(f"\n=== {label} ===")
        method = getattr(fmp, method_name)
        for page in range(pages):
            try:
                batch = method(page=page, limit=LIMIT)
            except Exception as e:
                print(f"  page {page}: ERROR {e}")
                break
            if not batch:
                print(f"  page {page}: empty, stopping")
                break
            for item in batch:
                _process_item(item, source_api, query_type=label)
            print(f"  page {page}: {len(batch):3d} items  (cumulative: inserted={inserted} matched_articles={matched})")
            conn.commit()
            if len(batch) < LIMIT:
                break

    # Per-ticker pulls
    for ticker, company in active_tickers:
        print(f"\n=== ticker:{ticker} ({company}) ===")
        for page in range(PAGES_PER_TICKER):
            try:
                batch = fmp.news_stock(symbols=ticker, page=page, limit=LIMIT)
            except Exception as e:
                print(f"  page {page}: ERROR {e}")
                break
            if not batch:
                print(f"  page {page}: empty, stopping")
                break
            for item in batch:
                _process_item(item, source_api="fmp_stock_ticker", query_type=f"ticker:{ticker}")
            print(f"  page {page}: {len(batch):3d} items  (cumulative: inserted={inserted} matched_articles={matched})")
            conn.commit()
            if len(batch) < LIMIT:
                break

    # Write the raw CSV
    with open(raw_path, "w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=RAW_FIELDS)
        w.writeheader()
        w.writerows(raw_rows)
    print(f"\nWrote raw CSV: {raw_path}  ({len(raw_rows)} rows)")

    # Summary
    print(f"\nInserted articles : {inserted}")
    print(f"Articles w/ hits  : {matched}")
    print(f"Keyword-hit rows  : {hits_total}")

    print("\n--- hits by category ---")
    for r in conn.execute(
        """
        SELECT t.category, COUNT(DISTINCT h.article_id) AS n_articles
        FROM article_keyword_hits h
        JOIN query_taxonomy t ON t.id = h.taxonomy_id
        GROUP BY t.category
        ORDER BY n_articles DESC;
        """
    ):
        print(f"  {r['category']:15s}  {r['n_articles']}")

    print("\n--- top 25 articles with hits (newest) ---")
    for r in conn.execute(
        """
        SELECT a.published, a.ticker, a.title,
               GROUP_CONCAT(t.category || ':' || t.keyword, ' | ') AS kws
        FROM articles a
        JOIN article_keyword_hits h ON h.article_id = a.id
        JOIN query_taxonomy t       ON t.id = h.taxonomy_id
        GROUP BY a.id
        ORDER BY a.published DESC
        LIMIT 25;
        """
    ):
        sym = f"[{r['ticker']}] " if r["ticker"] else ""
        print(f"  {r['published']}  {sym}{r['title'][:110]}")
        print(f"      -> {r['kws']}")

    conn.close()


if __name__ == "__main__":
    main()
