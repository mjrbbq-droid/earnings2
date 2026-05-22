# scripts/backfill_factset_classification.py
from __future__ import annotations

import sqlite3
from pathlib import Path

DB = "./data/databases/earnings.db"


def ensure_columns(conn: sqlite3.Connection) -> None:
    cols = [r[1] for r in conn.execute("PRAGMA table_info(earnings_calls)").fetchall()]

    stmts = []
    if "sector_fs" not in cols:
        stmts.append("ALTER TABLE earnings_calls ADD COLUMN sector_fs TEXT")
    if "industry_fs" not in cols:
        stmts.append("ALTER TABLE earnings_calls ADD COLUMN industry_fs TEXT")
    if "mkt_value_fs" not in cols:
        stmts.append("ALTER TABLE earnings_calls ADD COLUMN mkt_value_fs REAL")
    if "asof_date_fs" not in cols:
        stmts.append("ALTER TABLE earnings_calls ADD COLUMN asof_date_fs TEXT")

    for s in stmts:
        conn.execute(s)
    conn.commit()

    if stmts:
        print("Added columns:", stmts)
    else:
        print("Columns already exist. No ALTER TABLE needed.")


def main() -> None:
    db_path = Path(DB)
    if not db_path.exists():
        raise SystemExit(f"DB not found: {db_path.resolve()}")

    conn = sqlite3.connect(DB)

    # Ensure FactSet columns exist on earnings_calls
    ensure_columns(conn)

    # Verify FactSet snapshot exists
    r = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='universe_factset_snapshot'"
    ).fetchone()
    if not r:
        raise SystemExit("Missing table universe_factset_snapshot. Load the FactSet screen first.")

    snap_date = conn.execute("SELECT MAX(asof_date) FROM universe_factset_snapshot").fetchone()[0]
    if not snap_date:
        raise SystemExit("No rows in universe_factset_snapshot. Load the FactSet screen first.")

    # Backfill by joining tickers to latest snapshot
    conn.execute(
        """
        UPDATE earnings_calls
        SET sector_fs = (
                SELECT u.sector
                FROM universe_factset_snapshot u
                WHERE u.asof_date = ? AND u.ticker = earnings_calls.ticker
            ),
            industry_fs = (
                SELECT u.industry
                FROM universe_factset_snapshot u
                WHERE u.asof_date = ? AND u.ticker = earnings_calls.ticker
            ),
            mkt_value_fs = (
                SELECT u.mkt_value
                FROM universe_factset_snapshot u
                WHERE u.asof_date = ? AND u.ticker = earnings_calls.ticker
            ),
            asof_date_fs = ?
        WHERE ticker IS NOT NULL AND ticker != '';
        """,
        (snap_date, snap_date, snap_date, snap_date),
    )
    conn.commit()

    # Diagnostics: how many calls got mapped?
    total = conn.execute("SELECT COUNT(*) FROM earnings_calls").fetchone()[0]
    mapped = conn.execute("SELECT COUNT(*) FROM earnings_calls WHERE industry_fs IS NOT NULL").fetchone()[0]
    unmapped = total - mapped

    print("====")
    print("FactSet snapshot date used:", snap_date)
    print("Total calls:", total)
    print("Mapped calls:", mapped)
    print("Unmapped calls:", unmapped)
    print("DB:", db_path.resolve())

    conn.close()


if __name__ == "__main__":
    main()
