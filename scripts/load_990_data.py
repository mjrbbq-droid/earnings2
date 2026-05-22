"""
Pull 990 filing history from ProPublica Nonprofit Explorer for each donor
foundation in data/donor_foundations.csv, store filing-level totals in
foundation_filings. The headline number is total_grants_paid per tax year —
that's what answers "did this foundation keep giving or fade?"

Idempotent: UNIQUE(ein, tax_year). Re-running upserts.
"""
from __future__ import annotations

import csv
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.config import DATA_DIR, RISK_DB_PATH
from src.propublica import (
    ProPublicaClient,
    filing_year,
    total_expenses,
    total_grants_paid,
    total_revenue,
)
from src.risk_db import utcnow
from src.risk_schema import connect, init_db


def load_donor_csv(path: Path) -> list[dict]:
    with open(path, encoding="utf-8") as f:
        return list(csv.DictReader(f))


def upsert_donor(conn, row: dict) -> None:
    conn.execute(
        """
        INSERT INTO donor_foundations
            (ein, foundation_name, donor_ticker, donor_company, relationship, notes, added_at_utc)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(ein) DO UPDATE SET
            foundation_name = excluded.foundation_name,
            donor_ticker    = excluded.donor_ticker,
            donor_company   = excluded.donor_company,
            relationship    = excluded.relationship,
            notes           = excluded.notes;
        """,
        (
            row["ein"].replace("-", ""),
            row["foundation_name"],
            row["donor_ticker"],
            row.get("donor_company") or None,
            row.get("relationship") or None,
            row.get("notes") or None,
            utcnow(),
        ),
    )


def upsert_filing(conn, ein: str, filing: dict) -> None:
    year = filing_year(filing)
    if year is None:
        return
    conn.execute(
        """
        INSERT INTO foundation_filings
            (ein, tax_year, form_type, total_revenue, total_expenses,
             total_grants_paid, pdf_url, raw_json, fetched_at_utc)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(ein, tax_year) DO UPDATE SET
            form_type         = excluded.form_type,
            total_revenue     = excluded.total_revenue,
            total_expenses    = excluded.total_expenses,
            total_grants_paid = excluded.total_grants_paid,
            pdf_url           = excluded.pdf_url,
            raw_json          = excluded.raw_json,
            fetched_at_utc    = excluded.fetched_at_utc;
        """,
        (
            ein,
            year,
            filing.get("formtype"),
            total_revenue(filing),
            total_expenses(filing),
            total_grants_paid(filing),
            filing.get("pdf_url"),
            json.dumps(filing),
            utcnow(),
        ),
    )


def main() -> None:
    conn = connect(RISK_DB_PATH)
    init_db(conn)
    pp = ProPublicaClient()

    donors = load_donor_csv(Path(DATA_DIR) / "donor_foundations.csv")
    print(f"Loading {len(donors)} donor foundations.\n")

    for d in donors:
        upsert_donor(conn, d)
    conn.commit()

    n_donors = 0
    n_filings = 0
    n_errors = 0

    for d in donors:
        ein = d["ein"].replace("-", "")
        name = d["foundation_name"]
        ticker = d["donor_ticker"]
        print(f"--- {ticker:6s}  EIN {ein:10s}  {name[:50]}")
        try:
            filings = pp.filings(ein)
        except Exception as e:
            n_errors += 1
            print(f"  ERROR fetching filings: {e}")
            continue

        n_donors += 1
        years_paid: list[tuple[int, int | None]] = []
        for filing in filings:
            upsert_filing(conn, ein, filing)
            year = filing_year(filing)
            grants = total_grants_paid(filing)
            if year is not None:
                years_paid.append((year, grants))
            n_filings += 1

        conn.commit()

        # Print compact trajectory
        years_paid.sort(reverse=True)
        for year, grants in years_paid[:7]:
            g_str = f"${grants/1e6:.2f}M" if grants else "(none)"
            print(f"      {year}: grants_paid = {g_str}")

    print()
    print("=" * 80)
    print(f"Done. donors={n_donors}  filings={n_filings}  errors={n_errors}\n")

    # ── Trajectory summary across foundations ─────────────────────────
    print("=== TOTAL GRANTS PAID BY FOUNDATION × YEAR (US$M) ===\n")
    rows = conn.execute(
        """
        SELECT d.donor_ticker, d.foundation_name, f.tax_year, f.total_grants_paid
        FROM foundation_filings f
        JOIN donor_foundations d ON d.ein = f.ein
        WHERE f.tax_year >= 2018
        ORDER BY d.donor_ticker, f.tax_year;
        """
    ).fetchall()

    # Pivot: ticker -> {year: grants}
    pivot: dict[str, dict[int, int | None]] = {}
    fname: dict[str, str] = {}
    years_seen: set[int] = set()
    for r in rows:
        t = r["donor_ticker"]
        pivot.setdefault(t, {})[r["tax_year"]] = r["total_grants_paid"]
        fname[t] = r["foundation_name"]
        years_seen.add(r["tax_year"])

    years = sorted(years_seen)
    header = f"  {'TICKER':7s}  {'FOUNDATION':38s}  " + "  ".join(f"{y:>8d}" for y in years)
    print(header)
    print("  " + "-" * (len(header) - 2))
    for t in sorted(pivot.keys()):
        row = pivot[t]
        cells = []
        for y in years:
            g = row.get(y)
            cells.append(f"${g/1e6:>6.2f}M" if g else "    --   ")
        print(f"  {t:7s}  {fname[t][:38]:38s}  " + "  ".join(cells))

    conn.close()


if __name__ == "__main__":
    main()
