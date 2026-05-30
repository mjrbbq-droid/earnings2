"""Apply XML-sourced grants-paid fills produced by audit_grants_paid_from_xml.py.

Scope (default): the tracked cohort only —
  * UPDATE existing rows whose total_grants_paid is null.
  * INSERT latest-year rows ONLY for foundations that already have >=1 filing row.
Foundations with zero existing rows (the 203-foundation discovery backlog) are
listed but NOT inserted unless --include-new is passed.

Never overwrites a non-null value (audit shows 0 DIFF; existing data is correct).
Records provenance in raw_json._grants_paid_basis.
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import zipfile
from xml.etree import ElementTree as ET

DB = "data/institutional_risk.db"
AUDIT = "data/reference/grants_paid_xml_audit.json"
NS = "{http://www.irs.gov/efile}"
RTYPE_CODE = {"990": 0, "990EZ": 1, "990-EZ": 1, "990PF": 2}


def utcnow():
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--include-new", action="store_true",
                    help="also insert rows for foundations that have zero filing rows")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    res = json.load(open(AUDIT))
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row

    existing_rows = set((r["ein"], r["tax_year"]) for r in
                        conn.execute("select ein, tax_year from foundation_filings"))
    foundations_with_rows = set(r["ein"] for r in
                                conn.execute("select distinct ein from foundation_filings"))

    fills = [r for r in res if r["status"] == "FILL" and r.get("xml") is not None]

    n_update = n_insert = n_skip_new = 0
    for r in fills:
        ein, ty, val, rtype = r["ein"], r["ty"], r["xml"], r["rtype"]
        basis = f"efile_xml_{rtype}_partIX_or_partI"
        if (ein, ty) in existing_rows:
            # UPDATE existing null row
            row = conn.execute(
                "select id, raw_json, total_grants_paid from foundation_filings where ein=? and tax_year=?",
                (ein, ty)).fetchone()
            if row["total_grants_paid"] is not None:
                continue  # never overwrite
            raw = json.loads(row["raw_json"]) if row["raw_json"] else {}
            raw["_grants_paid_basis"] = basis
            raw["_grants_paid_value"] = val
            if not args.dry_run:
                conn.execute("update foundation_filings set total_grants_paid=?, raw_json=? where id=?",
                             (val, json.dumps(raw), row["id"]))
            n_update += 1
        else:
            # INSERT new row
            if ein not in foundations_with_rows and not args.include_new:
                n_skip_new += 1
                continue
            raw = {"_grants_paid_basis": basis, "_grants_paid_value": val,
                   "_source_zip": r.get("zip"), "_source_xml": r.get("xp")}
            if not args.dry_run:
                conn.execute(
                    """insert into foundation_filings
                       (ein, tax_year, form_type, total_revenue, total_expenses,
                        total_grants_paid, pdf_url, raw_json, fetched_at_utc)
                       values (?,?,?,?,?,?,?,?,?)""",
                    (ein, ty, RTYPE_CODE.get(rtype), None, None, val, None,
                     json.dumps(raw), utcnow()))
            n_insert += 1

    if not args.dry_run:
        conn.commit()
    print(f"UPDATE existing null rows : {n_update}")
    print(f"INSERT (foundations w/ rows): {n_insert}")
    print(f"SKIPPED new-foundation inserts (use --include-new): {n_skip_new}")
    if args.dry_run:
        print("(dry run — no writes)")
    conn.close()


if __name__ == "__main__":
    main()
