# scripts/fetch_fmp_transcripts.py
"""
Fetch earnings call transcripts from FMP and store in SQLite.
Transcripts can then be fed into the existing chunking + signature pipeline.

Usage:
    python -m scripts.fetch_fmp_transcripts --tickers DHI,KBH,LEN
    python -m scripts.fetch_fmp_transcripts                         # all R2K constituents
    python -m scripts.fetch_fmp_transcripts --years 2024,2025,2026
    python -m scripts.fetch_fmp_transcripts --limit 50              # first N tickers only
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
from datetime import datetime, timezone

from src.config import DB_PATH
from src.fmp import FMPClient
from src.fmp_schema import init_fmp_schema


def _resolve_tickers(conn: sqlite3.Connection, index_name: str) -> list[str]:
    rows = conn.execute(
        """
        SELECT DISTINCT ticker FROM fmp_constituents
        WHERE index_name = ?
          AND asof_date = (SELECT MAX(asof_date) FROM fmp_constituents WHERE index_name = ?)
        ORDER BY ticker;
        """,
        (index_name, index_name),
    ).fetchall()
    return [r[0] for r in rows]


def _existing_transcripts(conn: sqlite3.Connection, ticker: str) -> set[str]:
    """Return set of call_date strings already stored for this ticker."""
    rows = conn.execute(
        "SELECT call_date FROM fmp_transcripts WHERE ticker = ?;",
        (ticker,),
    ).fetchall()
    return {r[0] for r in rows}


def main():
    parser = argparse.ArgumentParser(description="Fetch FMP earnings call transcripts.")
    parser.add_argument("--tickers", default=None,
                        help="Comma-separated tickers (default: all from latest constituent snapshot)")
    parser.add_argument("--index", default="russell2000")
    parser.add_argument("--years", default=None,
                        help="Comma-separated years to fetch (default: 2023-2026)")
    parser.add_argument("--limit", type=int, default=0,
                        help="Limit to first N tickers (0 = all)")
    args = parser.parse_args()

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    init_fmp_schema(conn)

    # Resolve tickers
    if args.tickers:
        tickers = [t.strip().upper() for t in args.tickers.split(",") if t.strip()]
    else:
        tickers = _resolve_tickers(conn, args.index)
        if not tickers:
            print(f"No constituents for {args.index}.  Run fetch_fmp_constituents first.")
            sys.exit(1)

    if args.limit > 0:
        tickers = tickers[: args.limit]

    years = [2023, 2024, 2025, 2026]
    if args.years:
        years = [int(y.strip()) for y in args.years.split(",")]

    quarters = [1, 2, 3, 4]

    print(f"Fetching transcripts for {len(tickers)} tickers x years {years}")
    client = FMPClient()
    now_utc = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    total_new = 0
    total_skip = 0

    for ti, ticker in enumerate(tickers, 1):
        existing = _existing_transcripts(conn, ticker)
        ticker_new = 0

        for year in years:
            for qtr in quarters:
                try:
                    data = client.transcript(ticker, year, qtr)
                except Exception as e:
                    print(f"  [{ti}/{len(tickers)}] {ticker} Q{qtr} {year}: ERROR {e}")
                    continue

                if not data:
                    continue

                for item in data:
                    call_date = (item.get("date") or "")[:10]  # trim to YYYY-MM-DD
                    if not call_date:
                        continue

                    if call_date in existing:
                        total_skip += 1
                        continue

                    content = item.get("content", "")
                    if not content or len(content) < 200:
                        continue

                    conn.execute(
                        """
                        INSERT INTO fmp_transcripts
                        (ticker, call_date, fiscal_year, fiscal_quarter,
                         content, char_count, updated_at_utc)
                        VALUES (?, ?, ?, ?, ?, ?, ?)
                        ON CONFLICT(ticker, call_date) DO UPDATE SET
                          fiscal_year=excluded.fiscal_year,
                          fiscal_quarter=excluded.fiscal_quarter,
                          content=excluded.content,
                          char_count=excluded.char_count,
                          updated_at_utc=excluded.updated_at_utc;
                        """,
                        (
                            ticker,
                            call_date,
                            item.get("year") or year,
                            item.get("quarter") or qtr,
                            content,
                            len(content),
                            now_utc,
                        ),
                    )
                    existing.add(call_date)
                    ticker_new += 1

        conn.commit()
        if ticker_new > 0:
            print(f"  [{ti}/{len(tickers)}] {ticker}: {ticker_new} new transcripts")
        total_new += ticker_new

    conn.close()
    print(f"\nDone.  {total_new} new, {total_skip} skipped (already stored).")
    print(f"DB: {DB_PATH}")


if __name__ == "__main__":
    main()
