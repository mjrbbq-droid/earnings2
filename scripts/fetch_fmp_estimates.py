# scripts/fetch_fmp_estimates.py
"""
Fetch earnings estimates + surprises from FMP for all constituents (or specific tickers).

Usage:
    python -m scripts.fetch_fmp_estimates                          # all R2K constituents
    python -m scripts.fetch_fmp_estimates --tickers DHI,KBH,LEN   # specific tickers
    python -m scripts.fetch_fmp_estimates --index sp500            # S&P 500 universe
    python -m scripts.fetch_fmp_estimates --skip-estimates         # surprises only
    python -m scripts.fetch_fmp_estimates --skip-surprises         # estimates only
"""
from __future__ import annotations

import argparse
import json
import math
import sqlite3
import sys
from datetime import datetime, timezone

from src.config import DB_PATH
from src.fmp import FMPClient
from src.fmp_schema import init_fmp_schema

INLINE_THRESHOLD = 0.02  # ±$0.02 EPS counts as inline


def classify_beat_miss(actual: float | None, estimated: float | None) -> str | None:
    if actual is None or estimated is None:
        return None
    diff = actual - estimated
    if abs(diff) <= INLINE_THRESHOLD:
        return "inline"
    return "beat" if diff > 0 else "miss"


def _resolve_tickers(conn: sqlite3.Connection, index_name: str) -> list[str]:
    """Get tickers from the latest constituent snapshot."""
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


def fetch_estimates(client: FMPClient, conn: sqlite3.Connection, tickers: list[str]):
    now_utc = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    total = len(tickers)

    for i, ticker in enumerate(tickers, 1):
        print(f"  [{i}/{total}] estimates: {ticker}", end="")
        try:
            rows = client.analyst_estimates(ticker, period="quarter", limit=30)
        except Exception as e:
            print(f" ERROR: {e}")
            continue

        if not rows:
            print(" (no data)")
            continue

        n = 0
        for r in rows:
            date_str = r.get("date")
            if not date_str:
                continue

            # Parse fiscal year/quarter from date if possible
            fy = None
            fq = None
            try:
                dt = datetime.strptime(date_str, "%Y-%m-%d")
                fy = dt.year
                # Approximate quarter from month
                fq = (dt.month - 1) // 3 + 1
            except (ValueError, TypeError):
                pass

            conn.execute(
                """
                INSERT INTO fmp_earnings_estimates
                (ticker, date, fiscal_year, fiscal_quarter,
                 estimated_revenue, actual_revenue, revenue_surprise,
                 estimated_eps, actual_eps, eps_surprise,
                 number_analysts_est, updated_at_utc, raw_json)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(ticker, date) DO UPDATE SET
                  fiscal_year=excluded.fiscal_year,
                  fiscal_quarter=excluded.fiscal_quarter,
                  estimated_revenue=excluded.estimated_revenue,
                  actual_revenue=excluded.actual_revenue,
                  revenue_surprise=excluded.revenue_surprise,
                  estimated_eps=excluded.estimated_eps,
                  actual_eps=excluded.actual_eps,
                  eps_surprise=excluded.eps_surprise,
                  number_analysts_est=excluded.number_analysts_est,
                  updated_at_utc=excluded.updated_at_utc,
                  raw_json=excluded.raw_json;
                """,
                (
                    ticker,
                    date_str,
                    fy,
                    fq,
                    r.get("estimatedRevenueAvg"),
                    r.get("actualRevenue"),          # may be null for future
                    r.get("revenueSurprise"),
                    r.get("estimatedEpsAvg"),
                    r.get("actualEps"),              # may be null for future
                    r.get("epsSurprise"),
                    r.get("numberAnalystsEstimatedEps") or r.get("numberAnalystEstimatedRevenue"),
                    now_utc,
                    json.dumps(r, ensure_ascii=False),
                ),
            )
            n += 1

        conn.commit()
        print(f" ->{n} rows")


def fetch_surprises(client: FMPClient, conn: sqlite3.Connection, tickers: list[str]):
    now_utc = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    total = len(tickers)

    for i, ticker in enumerate(tickers, 1):
        print(f"  [{i}/{total}] surprises: {ticker}", end="")
        try:
            rows = client.earnings_surprises(ticker)
        except Exception as e:
            print(f" ERROR: {e}")
            continue

        if not rows:
            print(" (no data)")
            continue

        n = 0
        for r in rows:
            date_str = r.get("date")
            if not date_str:
                continue

            actual = r.get("actualEarningResult")
            estimated = r.get("estimatedEarning")

            surprise_abs = None
            surprise_pct = None
            if actual is not None and estimated is not None:
                surprise_abs = actual - estimated
                if estimated and estimated != 0:
                    surprise_pct = round((actual - estimated) / abs(estimated) * 100, 2)

            beat_miss = classify_beat_miss(actual, estimated)

            conn.execute(
                """
                INSERT INTO fmp_earnings_surprises
                (ticker, date, fiscal_ending, estimated_eps, actual_eps,
                 surprise_abs, surprise_pct, beat_miss, updated_at_utc, raw_json)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(ticker, date) DO UPDATE SET
                  fiscal_ending=excluded.fiscal_ending,
                  estimated_eps=excluded.estimated_eps,
                  actual_eps=excluded.actual_eps,
                  surprise_abs=excluded.surprise_abs,
                  surprise_pct=excluded.surprise_pct,
                  beat_miss=excluded.beat_miss,
                  updated_at_utc=excluded.updated_at_utc,
                  raw_json=excluded.raw_json;
                """,
                (
                    ticker,
                    date_str,
                    r.get("fiscalDateEnding"),
                    estimated,
                    actual,
                    surprise_abs,
                    surprise_pct,
                    beat_miss,
                    now_utc,
                    json.dumps(r, ensure_ascii=False),
                ),
            )
            n += 1

        conn.commit()
        print(f" ->{n} rows")


def main():
    parser = argparse.ArgumentParser(description="Fetch FMP earnings estimates and surprises.")
    parser.add_argument("--tickers", default=None,
                        help="Comma-separated tickers (default: all from latest constituent snapshot)")
    parser.add_argument("--index", default="russell2000",
                        help="Index to pull universe from (default: russell2000)")
    parser.add_argument("--skip-estimates", action="store_true")
    parser.add_argument("--skip-surprises", action="store_true")
    args = parser.parse_args()

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    init_fmp_schema(conn)

    # Resolve ticker list
    if args.tickers:
        tickers = [t.strip().upper() for t in args.tickers.split(",") if t.strip()]
    else:
        tickers = _resolve_tickers(conn, args.index)
        if not tickers:
            print(f"No constituents found for {args.index}.  Run fetch_fmp_constituents first.")
            sys.exit(1)

    print(f"Processing {len(tickers)} tickers.")
    client = FMPClient()

    if not args.skip_estimates:
        print("\n-- Analyst Estimates --")
        fetch_estimates(client, conn, tickers)

    if not args.skip_surprises:
        print("\n-- Earnings Surprises --")
        fetch_surprises(client, conn, tickers)

    conn.close()
    print(f"\nDone.  DB: {DB_PATH}")


if __name__ == "__main__":
    main()
