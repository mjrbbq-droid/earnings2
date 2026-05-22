# scripts/enrich_fmp_profiles.py
"""
Enrich fmp_constituents with sector/industry from FMP company profiles.
The R2K constituents endpoint doesn't return proper GICS industry — this fills the gap.

Usage:
    python -m scripts.enrich_fmp_profiles
    python -m scripts.enrich_fmp_profiles --tickers DHI,KBH
    python -m scripts.enrich_fmp_profiles --batch-size 30   # profiles per API call
"""
from __future__ import annotations

import argparse
import json
import sqlite3
from datetime import datetime, timezone

from src.config import DB_PATH
from src.fmp import FMPClient
from src.fmp_schema import init_fmp_schema


def _tickers_needing_enrichment(conn: sqlite3.Connection) -> list[str]:
    """Constituents where industry is NULL or empty."""
    rows = conn.execute(
        """
        SELECT DISTINCT ticker FROM fmp_constituents
        WHERE asof_date = (SELECT MAX(asof_date) FROM fmp_constituents)
          AND (industry IS NULL OR industry = '' OR sector IS NULL OR sector = '')
        ORDER BY ticker;
        """
    ).fetchall()
    return [r[0] for r in rows]


def main():
    parser = argparse.ArgumentParser(description="Enrich constituents with FMP company profiles.")
    parser.add_argument("--tickers", default=None, help="Comma-separated (default: all needing enrichment)")
    parser.add_argument("--force", action="store_true", help="Re-enrich even if sector/industry already set")
    parser.add_argument("--batch-size", type=int, default=30, help="Profiles per batch API call")
    args = parser.parse_args()

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    init_fmp_schema(conn)

    if args.tickers:
        tickers = [t.strip().upper() for t in args.tickers.split(",")]
    elif args.force:
        rows = conn.execute(
            "SELECT DISTINCT ticker FROM fmp_constituents WHERE asof_date = (SELECT MAX(asof_date) FROM fmp_constituents) ORDER BY ticker;"
        ).fetchall()
        tickers = [r[0] for r in rows]
    else:
        tickers = _tickers_needing_enrichment(conn)

    if not tickers:
        print("All constituents already enriched.  Use --force to re-enrich.")
        return

    print(f"Enriching {len(tickers)} tickers (batch_size={args.batch_size})...")
    client = FMPClient()
    now_utc = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    updated = 0
    for start in range(0, len(tickers), args.batch_size):
        batch = tickers[start: start + args.batch_size]
        print(f"  Batch {start // args.batch_size + 1}: {batch[0]}..{batch[-1]} ({len(batch)} tickers)")

        try:
            profiles = client.company_profile_batch(batch)
        except Exception as e:
            print(f"    ERROR: {e}")
            continue

        for p in profiles:
            symbol = (p.get("symbol") or "").upper()
            if not symbol:
                continue

            conn.execute(
                """
                UPDATE fmp_constituents
                SET sector     = COALESCE(?, sector),
                    industry   = COALESCE(?, industry),
                    market_cap = COALESCE(?, market_cap),
                    updated_at_utc = ?
                WHERE ticker = ?
                  AND asof_date = (SELECT MAX(asof_date) FROM fmp_constituents);
                """,
                (
                    p.get("sector"),
                    p.get("industry"),
                    p.get("mktCap"),
                    now_utc,
                    symbol,
                ),
            )
            updated += 1

        conn.commit()

    conn.close()
    print(f"\nUpdated {updated} profiles.  DB: {DB_PATH}")


if __name__ == "__main__":
    main()
