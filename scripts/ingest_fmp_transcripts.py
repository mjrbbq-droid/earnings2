# scripts/ingest_fmp_transcripts.py
"""
Bridge FMP transcripts into the earnings_calls table so the existing pipeline
(chunks -> signatures -> forensics -> z-scores -> pulses) works unchanged.

Usage:
    python -m scripts.ingest_fmp_transcripts                          # all pending
    python -m scripts.ingest_fmp_transcripts --tickers DHI,KBH,LEN   # specific tickers
"""
from __future__ import annotations

import argparse
import hashlib
import sqlite3
from datetime import datetime, timezone

from src.config import DB_PATH
from src.db import connect, init_db, insert_earnings_call


def _content_hash(ticker: str, call_date: str, content: str) -> str:
    """Deterministic hash for FMP transcript content."""
    payload = f"{ticker}|{call_date}|{content}".encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _already_ingested(conn: sqlite3.Connection) -> set[tuple[str, str]]:
    """Return set of (ticker, call_date) already in earnings_calls from FMP."""
    rows = conn.execute(
        "SELECT ticker, call_date FROM earnings_calls WHERE source = 'fmp';"
    ).fetchall()
    return {(r["ticker"], r["call_date"]) for r in rows}


def _pending_transcripts(
    conn: sqlite3.Connection,
    ingested: set[tuple[str, str]],
    tickers: list[str] | None = None,
) -> list[dict]:
    """Get FMP transcripts not yet in earnings_calls."""
    if tickers:
        placeholders = ",".join("?" for _ in tickers)
        rows = conn.execute(
            f"SELECT * FROM fmp_transcripts WHERE ticker IN ({placeholders}) ORDER BY ticker, call_date;",
            tickers,
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM fmp_transcripts ORDER BY ticker, call_date;"
        ).fetchall()

    pending = []
    for r in rows:
        if (r["ticker"], r["call_date"]) not in ingested:
            pending.append(dict(r))
    return pending


def _lookup_company_info(conn: sqlite3.Connection, ticker: str) -> dict:
    """Get company_name, sector, industry from fmp_constituents."""
    row = conn.execute(
        """
        SELECT company_name, sector, industry
        FROM fmp_constituents
        WHERE ticker = ?
          AND asof_date = (SELECT MAX(asof_date) FROM fmp_constituents)
        LIMIT 1;
        """,
        (ticker,),
    ).fetchone()
    if row:
        return {"company_name": row["company_name"], "sector": row["sector"], "industry": row["industry"]}
    return {"company_name": None, "sector": None, "industry": None}


def main():
    parser = argparse.ArgumentParser(description="Ingest FMP transcripts into earnings_calls.")
    parser.add_argument("--tickers", default=None,
                        help="Comma-separated tickers (default: all pending)")
    args = parser.parse_args()

    conn = connect(DB_PATH)
    init_db(conn)

    tickers = None
    if args.tickers:
        tickers = [t.strip().upper() for t in args.tickers.split(",") if t.strip()]

    ingested = _already_ingested(conn)
    pending = _pending_transcripts(conn, ingested, tickers)

    if not pending:
        print("No new FMP transcripts to ingest.")
        return

    print(f"Ingesting {len(pending)} FMP transcripts into earnings_calls...")
    now_utc = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    created = 0
    skipped = 0

    for tx in pending:
        ticker = tx["ticker"]
        call_date = tx["call_date"]
        content = tx["content"]

        # Skip if already in earnings_calls from any source (PDF, etc.)
        existing = conn.execute(
            "SELECT id FROM earnings_calls WHERE ticker = ? AND call_date = ? LIMIT 1;",
            (ticker, call_date),
        ).fetchone()
        if existing:
            skipped += 1
            continue

        info = _lookup_company_info(conn, ticker)
        file_hash = _content_hash(ticker, call_date, content)

        call_id = insert_earnings_call(
            conn,
            ticker=ticker,
            company=info["company_name"],
            industry=info["industry"],
            call_date=call_date,
            fiscal_year=tx.get("fiscal_year"),
            fiscal_quarter=tx.get("fiscal_quarter"),
            source="fmp",
            source_path=f"fmp://{ticker}/{call_date}",
            file_hash=file_hash,
            raw_text=content,
            created_at_utc=now_utc,
            event_type="earnings_call",
        )

        # Backfill sector/industry from R2K data
        if info["sector"] or info["industry"]:
            conn.execute(
                """
                UPDATE earnings_calls
                SET sector_factset = COALESCE(sector_factset, ?),
                    industry_factset = COALESCE(industry_factset, ?)
                WHERE id = ?;
                """,
                (info["sector"], info["industry"], call_id),
            )
            conn.commit()

        created += 1
        print(f"  {ticker} {call_date} -> id={call_id}")

    print(f"\nDone. {created} ingested, {skipped} skipped (already in earnings_calls).")
    print(f"DB: {DB_PATH}")
    conn.close()


if __name__ == "__main__":
    main()
