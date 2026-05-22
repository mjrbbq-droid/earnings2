# scripts/load_universe_factset_snapshot.py
from __future__ import annotations

import json
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

DB = "./data/databases/earnings.db"
DATA_DIR = Path("./data")

# Match:
#  - 20260130f.xlsx
#  - 20260130.xlsx
#  - 20260130_anything.xlsx (optional, if you ever do 20260130_2.xlsx)
FILE_PATTERN = re.compile(r"^(\d{8})(?:[a-zA-Z]|_[^.]*)?\.xlsx$", re.IGNORECASE)

REQUIRED_COLS = ["ticker", "company_name", "date", "mkt_value", "sector", "industry"]

MKT_VALUE_MULTIPLIER = 1_000_000  # FactSet export is in millions â†’ store USD


def parse_date_from_filename(name: str) -> str:
    m = FILE_PATTERN.match(name)
    if not m:
        raise ValueError(f"Filename does not match expected pattern YYYYMMDD*.xlsx: {name}")
    yyyymmdd = m.group(1)
    dt = datetime.strptime(yyyymmdd, "%Y%m%d")
    return dt.strftime("%Y-%m-%d")


def find_latest_file(data_dir: Path) -> Path:
    candidates = []
    for p in data_dir.iterdir():
        if not p.is_file():
            continue
        m = FILE_PATTERN.match(p.name)
        if not m:
            continue
        yyyymmdd = m.group(1)
        try:
            dt = datetime.strptime(yyyymmdd, "%Y%m%d")
        except ValueError:
            continue
        candidates.append((dt, p))

    if not candidates:
        raise FileNotFoundError(f"No FactSet Excel files found in {data_dir} matching YYYYMMDD*.xlsx")

    # latest by date; if multiple same date, pick latest modified time among those
    latest_dt = max(candidates, key=lambda x: x[0])[0]
    same_day = [p for (dt, p) in candidates if dt == latest_dt]
    latest_file = max(same_day, key=lambda p: p.stat().st_mtime)
    return latest_file


def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS universe_factset_snapshot (
            asof_date TEXT NOT NULL,
            ticker TEXT NOT NULL,
            company_name TEXT,
            sector TEXT,
            industry TEXT,
            mkt_value REAL,          -- USD
            source_file TEXT,
            updated_at_utc TEXT,
            raw_json TEXT,

            PRIMARY KEY (asof_date, ticker)
        );

        CREATE INDEX IF NOT EXISTS ix_univ_snap_ticker ON universe_factset_snapshot(ticker);
        CREATE INDEX IF NOT EXISTS ix_univ_snap_asof ON universe_factset_snapshot(asof_date);
        CREATE INDEX IF NOT EXISTS ix_univ_snap_sector ON universe_factset_snapshot(sector);
        CREATE INDEX IF NOT EXISTS ix_univ_snap_industry ON universe_factset_snapshot(industry);
        CREATE INDEX IF NOT EXISTS ix_univ_snap_mkt_value ON universe_factset_snapshot(mkt_value);

        -- Convenience view: latest asof_date snapshot
        CREATE VIEW IF NOT EXISTS universe_factset_latest AS
        SELECT s.*
        FROM universe_factset_snapshot s
        WHERE s.asof_date = (SELECT MAX(asof_date) FROM universe_factset_snapshot);
        """
    )
    conn.commit()


def main():
    import argparse

    p = argparse.ArgumentParser(description="Load FactSet universe screen into SQLite snapshots.")
    p.add_argument("--file", default=None, help="Optional: explicit filename in ./data (otherwise auto-detect latest)")
    p.add_argument("--replace-date", action="store_true", help="Replace the entire snapshot for that date before loading")
    args = p.parse_args()

    # pick file
    if args.file:
        in_file = DATA_DIR / args.file
        if not in_file.exists():
            raise FileNotFoundError(f"File not found: {in_file}")
    else:
        in_file = find_latest_file(DATA_DIR)

    asof_date = parse_date_from_filename(in_file.name)
    source_file = in_file.name

    print(f"Using file: {in_file.name}")
    print(f"Snapshot date (asof_date): {asof_date}")
    print(f"Replace-date: {args.replace_date}")

    df = pd.read_excel(in_file)
    df.columns = [str(c).strip().lower() for c in df.columns]

    missing = [c for c in REQUIRED_COLS if c not in df.columns]
    if missing:
        raise ValueError(f"Missing required columns: {missing}\nFound columns: {list(df.columns)}")

    # Ignore factset_mv if it appears
    if "factset_mv" in df.columns:
        print("NOTE: factset_mv present but ignored by design.")

    df["ticker"] = df["ticker"].astype(str).str.upper().str.strip()
    df = df[df["ticker"].notna() & (df["ticker"] != "") & (df["ticker"] != "NAN")]

    updated_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    conn = sqlite3.connect(DB)
    ensure_schema(conn)
    cur = conn.cursor()

    with conn:
        if args.replace_date:
            cur.execute("DELETE FROM universe_factset_snapshot WHERE asof_date = ?", (asof_date,))
            print(f"Cleared existing rows for {asof_date}")

        n = 0
        for _, r in df.iterrows():
            ticker = r["ticker"]

            mv_usd = None
            if not pd.isna(r["mkt_value"]):
                mv_usd = float(r["mkt_value"]) * MKT_VALUE_MULTIPLIER

            raw = r.to_dict()
            # Remove explicitly modeled fields + anything you want excluded
            for k in ["ticker", "company_name", "date", "sector", "industry", "mkt_value"]:
                raw.pop(k, None)
            raw.pop("factset_mv", None)

            cur.execute(
                """
                INSERT INTO universe_factset_snapshot
                (asof_date, ticker, company_name, sector, industry, mkt_value, source_file, updated_at_utc, raw_json)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(asof_date, ticker) DO UPDATE SET
                  company_name=excluded.company_name,
                  sector=excluded.sector,
                  industry=excluded.industry,
                  mkt_value=excluded.mkt_value,
                  source_file=excluded.source_file,
                  updated_at_utc=excluded.updated_at_utc,
                  raw_json=excluded.raw_json;
                """,
                (
                    asof_date,
                    ticker,
                    r["company_name"],
                    str(r["sector"]).strip() if not pd.isna(r["sector"]) else None,
                    str(r["industry"]).strip() if not pd.isna(r["industry"]) else None,
                    mv_usd,
                    source_file,
                    updated_at,
                    json.dumps(raw, ensure_ascii=False),
                ),
            )
            n += 1

    conn.close()
    print(f"Loaded/updated {n} rows into universe_factset_snapshot for {asof_date}")
    print(f"DB: {Path(DB).resolve()}")


if __name__ == "__main__":
    main()


#     python scripts/load_universe_factset_raw.py