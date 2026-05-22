# scripts/fetch_fmp_constituents.py
"""
Fetch Russell 2000 (or S&P 500) constituents from FMP and store in SQLite.

Usage:
    python -m scripts.fetch_fmp_constituents
    python -m scripts.fetch_fmp_constituents --index sp500
"""
from __future__ import annotations

import argparse
import json
import sqlite3
from datetime import datetime, timezone

from src.config import DB_PATH
from src.fmp import FMPClient
from src.fmp_schema import init_fmp_schema


def main():
    parser = argparse.ArgumentParser(description="Fetch index constituents from FMP.")
    parser.add_argument("--index", default="russell2000", choices=["russell2000", "sp500"],
                        help="Which index to fetch (default: russell2000)")
    args = parser.parse_args()

    client = FMPClient()
    now_utc = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    print(f"Fetching {args.index} constituents from FMP...")
    if args.index == "russell2000":
        data = client.russell2000_constituents()
    else:
        data = client.sp500_constituents()

    if not data:
        print("No data returned.  Check your FMP_API_KEY and plan tier.")
        return

    print(f"Received {len(data)} constituents.")

    conn = sqlite3.connect(DB_PATH)
    init_fmp_schema(conn)

    inserted = 0
    with conn:
        for row in data:
            ticker = (row.get("symbol") or "").strip().upper()
            if not ticker:
                continue

            conn.execute(
                """
                INSERT INTO fmp_constituents
                (ticker, company_name, sector, sub_sector, industry, market_cap,
                 weight, index_name, cik, founded, asof_date, updated_at_utc, raw_json)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(index_name, ticker, asof_date) DO UPDATE SET
                  company_name=excluded.company_name,
                  sector=excluded.sector,
                  sub_sector=excluded.sub_sector,
                  industry=excluded.industry,
                  market_cap=excluded.market_cap,
                  weight=excluded.weight,
                  cik=excluded.cik,
                  founded=excluded.founded,
                  updated_at_utc=excluded.updated_at_utc,
                  raw_json=excluded.raw_json;
                """,
                (
                    ticker,
                    row.get("name"),
                    row.get("sector"),
                    row.get("subSector"),
                    row.get("headQuarter"),      # FMP R2K returns HQ, not industry — we'll enrich later
                    row.get("marketCap"),
                    row.get("weight"),
                    args.index,
                    row.get("cik"),
                    row.get("founded"),
                    today,
                    now_utc,
                    json.dumps(row, ensure_ascii=False),
                ),
            )
            inserted += 1

    conn.close()
    print(f"Loaded {inserted} constituents into fmp_constituents (index={args.index}, asof={today}).")
    print(f"DB: {DB_PATH}")


if __name__ == "__main__":
    main()
