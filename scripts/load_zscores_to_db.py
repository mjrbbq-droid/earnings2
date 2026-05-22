# scripts/load_zscores_to_db.py
from __future__ import annotations

import glob
import sqlite3
from pathlib import Path
from datetime import datetime, timezone
from typing import Optional

import pandas as pd

DB = "./data/databases/earnings.db"
DATA_DIR = Path("./data")


def ensure_schema(conn: sqlite3.Connection) -> None:
    """
    Ensures the call_zscores table + indexes exist.

    Design:
      - We keep the historical PK (ticker, call_date, source_file) for compatibility.
      - We ALSO enforce a hard uniqueness rule: one row per (ticker, call_date).
      - UPSERTs target ON CONFLICT(ticker, call_date) via the unique index.
    """
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS call_zscores (
            ticker TEXT NOT NULL,
            call_date TEXT,                 -- YYYY-MM-DD when available
            fiscal_year INTEGER,
            fiscal_quarter INTEGER,

            -- link to earnings_calls (filled when we can match)
            earnings_call_id INTEGER,

            -- metrics (not all will exist for every industry/version)
            mpi_raw REAL, mpi_z REAL,
            dqi_raw REAL, dqi_z REAL,
            ssi_raw REAL, ssi_z REAL,
            eri_raw REAL, eri_z REAL,
            lri_raw REAL, lri_z REAL,

            pmi_raw REAL, pmi_z REAL,
            bsi_raw REAL, bsi_z REAL,

            -- audit
            source_file TEXT NOT NULL,
            loaded_at_utc TEXT NOT NULL,

            -- historical PK (kept for backwards compatibility)
            PRIMARY KEY (ticker, call_date, source_file)
        );

        -- HARD RULE: one row per call
        CREATE UNIQUE INDEX IF NOT EXISTS ux_call_zscores_ticker_calldate
            ON call_zscores(ticker, call_date);

        -- Helpful indexes
        CREATE INDEX IF NOT EXISTS ix_call_zscores_ticker_date
            ON call_zscores(ticker, call_date);

        CREATE INDEX IF NOT EXISTS ix_call_zscores_call_id
            ON call_zscores(earnings_call_id);

        -- Latest zscores per ticker (by call_date, then loaded_at)
        CREATE VIEW IF NOT EXISTS call_zscores_latest AS
        WITH ranked AS (
            SELECT
                *,
                ROW_NUMBER() OVER (
                    PARTITION BY ticker
                    ORDER BY
                        CASE WHEN call_date IS NULL THEN 0 ELSE 1 END DESC,
                        call_date DESC,
                        loaded_at_utc DESC
                ) AS rn
            FROM call_zscores
        )
        SELECT * FROM ranked WHERE rn = 1;
        """
    )
    conn.commit()


def find_call_id(conn: sqlite3.Connection, ticker: str, call_date: Optional[str]) -> Optional[int]:
    """
    Match to earnings_calls by ticker + call_date, EARNINGS calls only.
    Uses event_type_clean if present; otherwise falls back to event_type.
    """
    if not call_date:
        return None

    r = conn.execute(
        """
        SELECT id
        FROM earnings_calls
        WHERE ticker = ?
          AND call_date = ?
          AND COALESCE(event_type_clean, event_type, 'earnings_call') = 'earnings_call'
        ORDER BY id DESC
        LIMIT 1;
        """,
        (ticker, call_date),
    ).fetchone()

    return int(r[0]) if r else None


def col_float(row: pd.Series, name: str) -> Optional[float]:
    if name not in row.index:
        return None
    v = row[name]
    try:
        if pd.isna(v):
            return None
        return float(v)
    except Exception:
        return None


def normalize_call_date(series: pd.Series) -> pd.Series:
    """
    Normalize call_date to YYYY-MM-DD where possible.
    Accepts strings like '2024-02-01', '02/01/2024', timestamps, etc.
    Unparseable -> None.
    """
    s = series.astype(str).str.strip()
    s = s.replace({"nan": None, "None": None, "": None})
    parsed = pd.to_datetime(s, errors="coerce", utc=True)
    out = parsed.dt.strftime("%Y-%m-%d")
    out = out.where(parsed.notna(), None)
    return out


def main() -> None:
    files = sorted(glob.glob(str(DATA_DIR / "*_zscores.csv")))
    if not files:
        raise SystemExit("No *_zscores.csv files found in ./data")

    loaded_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    ensure_schema(conn)

    n_rows = 0
    n_files = 0
    n_skipped_no_match = 0

    with conn:
        for f in files:
            df = pd.read_csv(f)
            if df.empty:
                continue

            n_files += 1
            source_file = Path(f).name

            # Ensure ticker column
            if "ticker" not in df.columns:
                inferred = source_file.replace("_zscores.csv", "")
                df["ticker"] = inferred

            # normalize tickers
            df["ticker"] = df["ticker"].astype(str).str.upper().str.strip()

            # normalize call_date if present
            if "call_date" in df.columns:
                df["call_date"] = normalize_call_date(df["call_date"])
            else:
                df["call_date"] = None

            for _, r in df.iterrows():
                ticker = str(r["ticker"]).upper().strip()
                call_date = r["call_date"] if "call_date" in r.index and pd.notna(r["call_date"]) else None

                fy = (
                    int(r["fiscal_year"])
                    if "fiscal_year" in r.index and pd.notna(r["fiscal_year"]) and str(r["fiscal_year"]).strip() != ""
                    else None
                )
                fq = (
                    int(r["fiscal_quarter"])
                    if "fiscal_quarter" in r.index and pd.notna(r["fiscal_quarter"]) and str(r["fiscal_quarter"]).strip() != ""
                    else None
                )

                # Must match a real EARNINGS call to load z-scores
                call_id = find_call_id(conn, ticker, call_date)
                if call_id is None:
                    n_skipped_no_match += 1
                    continue

                conn.execute(
                    """
                    INSERT INTO call_zscores (
                        ticker, call_date, fiscal_year, fiscal_quarter, earnings_call_id,
                        mpi_raw, mpi_z, dqi_raw, dqi_z, ssi_raw, ssi_z, eri_raw, eri_z, lri_raw, lri_z,
                        pmi_raw, pmi_z, bsi_raw, bsi_z,
                        source_file, loaded_at_utc
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(ticker, call_date) DO UPDATE SET
                        fiscal_year=excluded.fiscal_year,
                        fiscal_quarter=excluded.fiscal_quarter,
                        earnings_call_id=excluded.earnings_call_id,

                        mpi_raw=excluded.mpi_raw, mpi_z=excluded.mpi_z,
                        dqi_raw=excluded.dqi_raw, dqi_z=excluded.dqi_z,
                        ssi_raw=excluded.ssi_raw, ssi_z=excluded.ssi_z,
                        eri_raw=excluded.eri_raw, eri_z=excluded.eri_z,
                        lri_raw=excluded.lri_raw, lri_z=excluded.lri_z,

                        pmi_raw=excluded.pmi_raw, pmi_z=excluded.pmi_z,
                        bsi_raw=excluded.bsi_raw, bsi_z=excluded.bsi_z,

                        source_file=excluded.source_file,
                        loaded_at_utc=excluded.loaded_at_utc;
                    """,
                    (
                        ticker, call_date, fy, fq, call_id,
                        col_float(r, "mpi_raw"), col_float(r, "mpi_z"),
                        col_float(r, "dqi_raw"), col_float(r, "dqi_z"),
                        col_float(r, "ssi_raw"), col_float(r, "ssi_z"),
                        col_float(r, "eri_raw"), col_float(r, "eri_z"),
                        col_float(r, "lri_raw"), col_float(r, "lri_z"),
                        col_float(r, "pmi_raw"), col_float(r, "pmi_z"),
                        col_float(r, "bsi_raw"), col_float(r, "bsi_z"),
                        source_file, loaded_at,
                    ),
                )
                n_rows += 1

    conn.close()
    print("====")
    print(f"Files processed: {n_files}")
    print(f"Rows upserted:  {n_rows}")
    print(f"Rows skipped (no earnings_call match): {n_skipped_no_match}")
    print(f"DB: {Path(DB).resolve()}")
    print("View: call_zscores_latest ready")


if __name__ == "__main__":
    main()
