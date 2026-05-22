"""
Pull GDELT 2.0 DOC API for police-related news per watchlist ticker.

GDELT covers all global news in 100+ languages indexed every 15 min.
Free, no auth. Endpoint: https://api.gdeltproject.org/api/v2/doc/doc

Query: company_name AND (defund police OR police OR criminal justice).
Time range: 2020-01-01 to today.
"""
from __future__ import annotations

import hashlib
import sys
import time
import urllib.parse
from datetime import datetime, timezone
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.config import RISK_DB_PATH
from src.risk_db import utcnow
from src.risk_schema import connect, init_db

GDELT = "https://api.gdeltproject.org/api/v2/doc/doc"


def gdelt_query(company_name: str, *,
                start: str = "20200101000000",
                end: str | None = None) -> list[dict]:
    """One-shot GDELT search. Returns parsed JSON 'articles' list."""
    if end is None:
        end = datetime.now(timezone.utc).strftime("%Y%m%d000000")
    # Quoted phrase + police-related context. Limit to English.
    q = f'"{company_name}" (defund OR "police reform" OR "police violence" OR "anti-police")'
    params = {
        "query":      q,
        "mode":       "ArtList",
        "startdatetime": start,
        "enddatetime":   end,
        "maxrecords": 75,
        "format":     "json",
        "sort":       "DateDesc",
        "sourcelang": "english",
    }
    r = requests.get(GDELT, params=params, timeout=60)
    if not r.ok:
        return []
    try:
        data = r.json()
    except Exception:
        return []
    return data.get("articles") or []


def url_hash(url: str | None) -> str | None:
    if not url:
        return None
    return hashlib.sha256(url.strip().lower().encode("utf-8")).hexdigest()


def main() -> None:
    conn = connect(RISK_DB_PATH)
    init_db(conn)

    # Targets: same as USAspending — anti/mixed/pro_net companies
    targets = conn.execute(
        """
        SELECT ticker, company_name
        FROM company_stance_investigation
        WHERE net_position IN ('anti_police_net','mixed','pro_police_net')
        ORDER BY net_position, ticker;
        """
    ).fetchall()

    print(f"Pulling GDELT for {len(targets)} tickers\n")

    n_articles_total = 0
    for i, r in enumerate(targets, 1):
        ticker = r["ticker"]
        name = r["company_name"] or ""
        short = name.split(",")[0].split(" Inc")[0].split(" Corp")[0].split(" Co.")[0].strip()
        if len(short) < 4:
            short = name
        try:
            articles = gdelt_query(short)
        except Exception as e:
            print(f"  {i:3d}/{len(targets)}  {ticker:6s}  ERROR {e}")
            continue

        n_kept = 0
        for a in articles:
            uh = url_hash(a.get("url"))
            if not uh:
                continue
            try:
                conn.execute(
                    """
                    INSERT OR IGNORE INTO gdelt_news
                        (ticker, company_name, query_used, title, url, domain,
                         seen_date, language, sourcecountry, tone, url_hash, fetched_at_utc)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
                    """,
                    (
                        ticker, name, short, a.get("title"), a.get("url"),
                        a.get("domain"), a.get("seendate"),
                        a.get("language"), a.get("sourcecountry"),
                        None, uh, utcnow(),
                    ),
                )
                n_kept += 1
            except Exception:
                pass
        conn.commit()
        n_articles_total += n_kept
        print(f"  {i:3d}/{len(targets)}  {ticker:6s}  {short[:35]:35s}  articles={n_kept}")

        # Be polite to GDELT
        time.sleep(0.6)

    print(f"\nTotal GDELT articles loaded: {n_articles_total}\n")

    # Quick summary
    print("=== GDELT counts per ticker (where > 0) ===")
    for r in conn.execute(
        """
        SELECT ticker, COUNT(*) AS n
        FROM gdelt_news
        GROUP BY ticker
        HAVING COUNT(*) > 0
        ORDER BY n DESC;
        """
    ):
        print(f"  {r['ticker']:6s}  n={r['n']}")

    conn.close()


if __name__ == "__main__":
    main()
