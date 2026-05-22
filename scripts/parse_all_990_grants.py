"""
Phase 3: parse Schedule I / Part XV from all 20 donor foundations across
2 tax years (FY2022 + FY2023). Builds a multi-foundation Ã— multi-year view.

Uses the IRS 2023 + 2024 indices to locate OBJECT_IDs and the monthly ZIPs we
already downloaded.
"""
from __future__ import annotations

import csv
import json
import sys
import zipfile
from pathlib import Path
from xml.etree import ElementTree as ET

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.config import RISK_DB_PATH
from src.risk_db import utcnow
from src.risk_schema import connect, init_db

NS = {"irs": "http://www.irs.gov/efile"}


def _text(el, path: str) -> str | None:
    if el is None:
        return None
    sub = el.find(path, NS)
    return sub.text.strip() if sub is not None and sub.text else None


def parse_990pf(root):
    grants = []
    for g in root.iter(f"{{{NS['irs']}}}GrantOrContributionPdDurYrGrp"):
        recipient = (
            _text(g, "irs:RecipientBusinessName/irs:BusinessNameLine1Txt")
            or _text(g, "irs:RecipientPersonNm")
            or _text(g, "irs:RecipientNameNm")
        )
        amt = _text(g, "irs:Amt")
        try:
            amount = int(amt) if amt else None
        except ValueError:
            amount = None
        grants.append({
            "recipient_name": recipient or "(unknown)",
            "recipient_ein":  _text(g, "irs:RecipientEIN"),
            "grant_amount":   amount,
            "grant_purpose":  _text(g, "irs:GrantOrContributionPurposeTxt"),
        })
    return grants


def parse_990(root):
    """
    Handle both schema variants for 990 Schedule I:
      - older: <GrantsOtherAsstToOrgsInUSGrp> with <RecipientBusinessName>
      - newer (2022+): <GrantsToDomesticOrgsGrp> with <RecipientBusinessName>
    Also walk <GrantsToOrgOutsideUSGrp> for foreign grants.
    """
    grants = []
    for tag in ("GrantsOtherAsstToOrgsInUSGrp", "GrantsToDomesticOrgsGrp",
                "GrantsToOrgOutsideUSGrp", "GrantsOtherAsstToOrgsOutsideUSGrp",
                "RecipientTable"):
        for g in root.iter(f"{{{NS['irs']}}}{tag}"):
            recipient = (
                _text(g, "irs:RecipientBusinessName/irs:BusinessNameLine1Txt")
                or _text(g, "irs:RecipientNameNm")
            )
            amt = _text(g, "irs:CashGrantAmt") or _text(g, "irs:Amt")
            try:
                amount = int(amt) if amt else None
            except ValueError:
                amount = None
            grants.append({
                "recipient_name": recipient or "(unknown)",
                "recipient_ein":  _text(g, "irs:RecipientEIN"),
                "grant_amount":   amount,
                "grant_purpose":  _text(g, "irs:PurposeOfGrantTxt")
                                  or _text(g, "irs:GrantPurposeTxt"),
            })
    return grants


def load_filings_from_index(index_path: Path, target_eins: set[str]) -> list[dict]:
    """Return canonical 990 / 990PF filing per (EIN, tax_period) â€” prefer non-990T."""
    by_key = {}
    with open(index_path, encoding="utf-8") as f:
        for row in csv.DictReader(f):
            if row["EIN"] not in target_eins:
                continue
            if row["RETURN_TYPE"] == "990T":
                continue  # skip unrelated-business-income tax forms
            key = (row["EIN"], row["TAX_PERIOD"])
            # Pick the most recent if duplicates exist
            by_key.setdefault(key, row)
    return list(by_key.values())


def find_in_zips(object_id: str, zip_dir: Path) -> tuple[str, str] | None:
    """Legacy per-call linear scan. Prefer the pre-built location map."""
    for zip_path in sorted(zip_dir.glob("*.zip")):
        try:
            with zipfile.ZipFile(zip_path) as z:
                for n in z.namelist():
                    if object_id in n:
                        return str(zip_path), n
        except Exception:
            continue
    return None


def load_location_map() -> dict[str, tuple[str, str]]:
    """Load the OBJECT_ID -> (zip_path, xml_path) map built by
    build_filing_index_for_all_eins.py."""
    p = Path("data/reference/all_filing_locations.json")
    if not p.exists():
        return {}
    with open(p, encoding="utf-8") as f:
        raw = json.load(f)
    return {k: tuple(v) for k, v in raw.items()}


def main() -> None:
    conn = connect(RISK_DB_PATH)
    init_db(conn)

    # Load all donor EINs
    donors = {}
    with open("data/reference/donor_foundations.csv", encoding="utf-8") as f:
        for r in csv.DictReader(f):
            ein = r["ein"].replace("-", "")
            donors[ein] = (r["donor_ticker"], r["foundation_name"])

    target_eins = set(donors.keys())
    zip_dir = Path("data/raw_sources/irs_zips")
    location_map = load_location_map()
    print(f"Pre-built location map: {len(location_map)} OBJECT_IDs")

    # Pull filings from 2023, 2024, and 2025 indices
    filings_2023 = load_filings_from_index(Path("data/raw_sources/irs_index/index_2023.csv"), target_eins)
    filings_2024 = load_filings_from_index(Path("data/raw_sources/irs_index/index_2024.csv"), target_eins)
    idx_2025 = Path("data/raw_sources/irs_index/index_2025.csv")
    filings_2025 = (
        load_filings_from_index(idx_2025, target_eins) if idx_2025.exists() else []
    )

    # Wipe existing irs_xml grants for these EINs so we can re-run cleanly
    # SQLite limit on bound parameters is ~32k; we're well under
    placeholders = ",".join("?" * len(target_eins))
    conn.execute(
        f"DELETE FROM foundation_grants WHERE source='irs_xml' AND donor_ein IN ({placeholders});",
        list(target_eins),
    )
    conn.commit()

    all_filings = filings_2023 + filings_2024 + filings_2025
    print(
        f"Processing {len(all_filings)} filings "
        f"(2023:{len(filings_2023)}, 2024:{len(filings_2024)}, 2025:{len(filings_2025)}) "
        f"across {len(donors)} foundations\n"
    )

    total_grants = 0
    n_filings_parsed = 0
    n_missing = 0

    for row in all_filings:
        ein = row["EIN"]
        object_id = row["OBJECT_ID"]
        return_type = row["RETURN_TYPE"]
        # Tax year from TAX_PERIOD like "202212" or "202311"
        tax_period = row["TAX_PERIOD"]
        tax_year = int(tax_period[:4]) if tax_period[4:6] in ("01", "02") else int(tax_period[:4])
        ticker, name = donors[ein]

        location = location_map.get(object_id) or find_in_zips(object_id, zip_dir)
        if not location:
            print(f"  ! MISSING {ticker:6s} {tax_period}  oid={object_id}")
            n_missing += 1
            continue
        zp, xp = location

        try:
            with zipfile.ZipFile(zp) as z:
                with z.open(xp) as f:
                    root = ET.parse(f).getroot()
            actual_type = _text(root, "irs:ReturnHeader/irs:ReturnTypeCd")
            if actual_type == "990PF":
                grants = parse_990pf(root)
            elif actual_type in ("990", "990EZ"):
                grants = parse_990(root)
            else:
                print(f"  ? {ticker} {tax_period}: unknown return type {actual_type}")
                continue

            for g in grants:
                conn.execute(
                    """
                    INSERT INTO foundation_grants
                        (donor_ein, tax_year, recipient_name, recipient_ein,
                         grant_amount, grant_purpose, source, source_filing_url, fetched_at_utc)
                    VALUES (?, ?, ?, ?, ?, ?, 'irs_xml', ?, ?);
                    """,
                    (ein, tax_year, g["recipient_name"], g["recipient_ein"],
                     g["grant_amount"], g["grant_purpose"],
                     f"{zp}::{xp}", utcnow()),
                )
            total = sum(g["grant_amount"] or 0 for g in grants)
            n_filings_parsed += 1
            total_grants += len(grants)
            print(f"  {ticker:6s}  FY{tax_year}  {actual_type:5s}  n={len(grants):5d}  total=${total/1e6:>8.2f}M")
        except Exception as e:
            print(f"  ! ERROR {ticker} {tax_period}: {e}")
            continue

    conn.commit()
    print(f"\nParsed {n_filings_parsed} filings, {total_grants:,} grants. Missing: {n_missing}")

    conn.close()


if __name__ == "__main__":
    main()
