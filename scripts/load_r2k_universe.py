# scripts/load_r2k_universe.py
"""
Load Russell 2000 universe from r2kdata.xlsx into fmp_constituents table.
The Excel has a hierarchical layout: Sector → Industry → Company (with ticker).

Usage:
    python -m scripts.load_r2k_universe
    python -m scripts.load_r2k_universe --file data/raw/r2kdata.xlsx
    python -m scripts.load_r2k_universe --replace   # clear existing R2K rows first
"""
from __future__ import annotations

import argparse
import json
import sqlite3

import pandas as pd

from datetime import datetime, timezone
from pathlib import Path

from src.config import DB_PATH
from src.fmp_schema import init_fmp_schema

SKIP_SECTORS = {"[Cash]", "[Unassigned]"}

DEFAULT_FILE = Path("data/raw/r2kdata.xlsx")


def parse_r2k_excel(filepath: Path) -> list[dict]:
    """
    Parse the hierarchical Sector > Industry > Company layout.

    Layout (3 columns, starting at row 2):
      - Sector rows:   mixed case name, no ticker  (e.g. "Communication Services")
      - Industry rows: ALL CAPS name, no ticker     (e.g. "ENTERTAINMENT")
      - Company rows:  company name + ticker in col 1
    """
    df = pd.read_excel(filepath, header=None)

    current_sector = None
    current_industry = None
    companies: list[dict] = []

    for i in range(2, len(df)):  # data starts at row 2
        name = str(df.iloc[i, 0]).strip() if pd.notna(df.iloc[i, 0]) else ""
        ticker = str(df.iloc[i, 1]).strip() if pd.notna(df.iloc[i, 1]) else ""

        if not name or name == "nan" or name.startswith("Total"):
            continue

        # Read weight from col 2
        weight = None
        if pd.notna(df.iloc[i, 2]):
            try:
                weight = float(df.iloc[i, 2])
            except (ValueError, TypeError):
                pass

        if ticker:
            # Company row
            companies.append({
                "ticker": ticker.upper(),
                "company_name": name,
                "sector": current_sector,
                "industry": current_industry,
                "weight": weight,
            })
        elif name == name.upper() and len(name) > 1:
            # ALL CAPS = custom industry
            current_industry = name
        else:
            # Mixed case = sector
            if name in SKIP_SECTORS:
                current_sector = None
                current_industry = None
            else:
                current_sector = name
                current_industry = None

    return companies


def main():
    parser = argparse.ArgumentParser(description="Load R2K universe from Excel into SQLite.")
    parser.add_argument("--file", default=str(DEFAULT_FILE), help="Path to r2kdata.xlsx")
    parser.add_argument("--replace", action="store_true", help="Clear existing russell2000 rows before loading")
    args = parser.parse_args()

    filepath = Path(args.file)
    if not filepath.exists():
        raise FileNotFoundError(f"File not found: {filepath}")

    companies = parse_r2k_excel(filepath)
    print(f"Parsed {len(companies)} companies from {filepath.name}")
    print(f"  Sectors:    {len(set(c['sector'] for c in companies if c['sector']))}")
    print(f"  Industries: {len(set(c['industry'] for c in companies if c['industry']))}")

    now_utc = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    conn = sqlite3.connect(DB_PATH)
    init_fmp_schema(conn)

    with conn:
        if args.replace:
            conn.execute("DELETE FROM fmp_constituents WHERE index_name = 'russell2000';")
            print("Cleared existing russell2000 rows.")

        inserted = 0
        for c in companies:
            conn.execute(
                """
                INSERT INTO fmp_constituents
                (ticker, company_name, sector, sub_sector, industry, market_cap,
                 weight, index_name, cik, founded, asof_date, updated_at_utc, raw_json)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(index_name, ticker, asof_date) DO UPDATE SET
                  company_name=excluded.company_name,
                  sector=excluded.sector,
                  industry=excluded.industry,
                  weight=excluded.weight,
                  updated_at_utc=excluded.updated_at_utc,
                  raw_json=excluded.raw_json;
                """,
                (
                    c["ticker"],
                    c["company_name"],
                    c["sector"],
                    None,           # sub_sector
                    c["industry"],
                    None,           # market_cap — enrich later via FMP profile
                    c["weight"],
                    "russell2000",
                    None,           # cik
                    None,           # founded
                    today,
                    now_utc,
                    json.dumps(c, ensure_ascii=False),
                ),
            )
            inserted += 1

    conn.close()
    print(f"\nLoaded {inserted} constituents into fmp_constituents (index=russell2000, asof={today}).")
    print(f"DB: {DB_PATH}")


if __name__ == "__main__":
    main()
