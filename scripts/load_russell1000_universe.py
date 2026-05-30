"""Load the Russell 1000 constituent universe from
data/reference/russell1000.xlsx into the `universe` table, and report
assessment coverage (which tickers already have a stance investigation vs
which are still UNRATED).

Design: universe membership != assessment. A ticker's status is DERIVED:
    unrated  = in universe, no row in company_stance_investigation
    assessed = has an investigation row
So "no material position" (middle) is only ever reached by an actual
assessment, never by being swept into the universe.

Idempotent: re-running upserts. Also writes a normalized
data/reference/russell1000_universe.csv mirror.
"""
from __future__ import annotations

import csv
import sqlite3
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

import openpyxl

SRC = Path("data/reference/russell1000.xlsx")
CSV_OUT = Path("data/reference/russell1000_universe.csv")
DB = "data/institutional_risk.db"


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def read_rows() -> list[dict]:
    wb = openpyxl.load_workbook(SRC, read_only=True, data_only=True)
    ws = wb["Company Overview"]
    rows = list(ws.iter_rows(values_only=True))
    wb.close()
    header = [str(h).strip() if h else "" for h in rows[0]]
    out = []
    for r in rows[1:]:
        if not r or not r[0]:
            continue  # skip blank separator / empty rows
        rec = dict(zip(header, r))
        sym = str(rec.get("Symbol", "")).strip()
        if not sym:
            continue
        out.append({
            "ticker": sym,
            "company": (rec.get("Name") or "").strip(),
            "exchange": (rec.get("Stock Exchange") or "").strip(),
            "sector": (rec.get("RBICS Economy") or "").strip(),
            "market_value_musd": rec.get("Market Value"),
            "sales_musd": rec.get("Sales"),
        })
    return out


def ensure_table(conn) -> None:
    conn.execute("""
        CREATE TABLE IF NOT EXISTS universe (
            ticker            TEXT PRIMARY KEY,
            company           TEXT,
            exchange          TEXT,
            sector            TEXT,
            market_value_musd REAL,
            sales_musd        REAL,
            index_membership  TEXT,
            added_at_utc      TEXT
        );
    """)


def main() -> None:
    rows = read_rows()
    print(f"Parsed {len(rows)} Russell 1000 constituents from {SRC.name}")

    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    ensure_table(conn)

    for r in rows:
        conn.execute("""
            INSERT INTO universe
                (ticker, company, exchange, sector, market_value_musd, sales_musd,
                 index_membership, added_at_utc)
            VALUES (?,?,?,?,?,?,?,?)
            ON CONFLICT(ticker) DO UPDATE SET
                company=excluded.company, exchange=excluded.exchange,
                sector=excluded.sector, market_value_musd=excluded.market_value_musd,
                sales_musd=excluded.sales_musd, index_membership=excluded.index_membership;
        """, (r["ticker"], r["company"], r["exchange"], r["sector"],
              r["market_value_musd"], r["sales_musd"], "russell1000", utcnow()))
    conn.commit()

    # mirror to CSV
    with open(CSV_OUT, "w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=[
            "ticker", "company", "exchange", "sector", "market_value_musd", "sales_musd"])
        w.writeheader()
        for r in rows:
            w.writerow({k: r[k] for k in w.fieldnames})

    # ── coverage report (derived status) ────────────────────────────────
    assessed = set(x[0] for x in conn.execute(
        "select ticker from company_stance_investigation"))
    uni = set(x[0] for x in conn.execute("select ticker from universe"))

    in_both = uni & assessed
    unrated = uni - assessed
    assessed_not_in_r1000 = assessed - uni

    print(f"\n=== COVERAGE ===")
    print(f"Universe (Russell 1000): {len(uni)}")
    print(f"  assessed   : {len(in_both)}  ({100*len(in_both)/len(uni):.1f}%)")
    print(f"  unrated    : {len(unrated)}")
    print(f"Investigated but NOT in this R1000 list: {len(assessed_not_in_r1000)}")
    if assessed_not_in_r1000:
        print("   ", ", ".join(sorted(assessed_not_in_r1000)))

    print("\nUnrated by sector (what a full assessment run would cover):")
    sec = Counter(r["sector"] for r in conn.execute(
        "select u.sector from universe u "
        "where u.ticker not in (select ticker from company_stance_investigation)"))
    for s, n in sec.most_common():
        print(f"  {s or '(blank)':28} {n}")

    conn.close()
    print(f"\nWrote {CSV_OUT}")


if __name__ == "__main__":
    main()
