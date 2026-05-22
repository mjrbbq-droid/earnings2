"""
Parse Schedule I grants from major DAF 990s. These are LARGE filings —
Fidelity Charitable alone disburses 2M+ grants/year. Strategy: parse all
grants, but only WRITE rows that match our anti/pro/collab/reentry/innocence
classifier. The "unrelated" grants stay unsaved (millions of rows otherwise).
"""
from __future__ import annotations

import json
import sys
import zipfile
from pathlib import Path
from xml.etree import ElementTree as ET

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.config import RISK_DB_PATH
from src.risk_db import utcnow
from src.risk_schema import connect, init_db
from scripts.classify_grants import classify
from scripts.parse_all_990_grants import parse_990, parse_990pf

NS = {"irs": "http://www.irs.gov/efile"}


def _text(el, path: str) -> str | None:
    if el is None:
        return None
    sub = el.find(path, NS)
    return sub.text.strip() if sub is not None and sub.text else None


def main() -> None:
    conn = connect(RISK_DB_PATH)
    init_db(conn)

    locations = json.loads(Path("data/daf_filing_locations.json").read_text())

    # Map DAF display-names to canonical EINs (using the IRS schema's full org name)
    # daf_short_to_ein maps the short label used in daf_filing_locations.json
    daf_short_to_ein = {
        "Tides Foundation":     "510198509",
        "Tides Center":         "943213100",
        "AOGF":                 "810739440",
        "NPT":                  "237825575",
        "Fidelity Charitable":  "110303001",
        "Schwab Charitable":    "261997839",
        "RPA":                  "133615533",
        "Silicon Valley CF":    "205205488",
        "Vanguard Charitable":  "232888152",
    }

    # Clear existing DAF grants
    placeholders = ",".join("?" * len(daf_short_to_ein))
    conn.execute(
        f"DELETE FROM foundation_grants WHERE source='irs_xml' AND donor_ein IN ({placeholders});",
        list(daf_short_to_ein.values()),
    )
    conn.commit()

    total_grants_seen = 0
    total_grants_kept = 0

    for object_id, info in locations.items():
        zp = info["zip_path"]
        xp = info["xml_path"]
        name = info["name"]
        ein = daf_short_to_ein.get(name)
        if not ein:
            print(f"  ! no EIN map for {name}, skipping")
            continue

        with zipfile.ZipFile(zp) as z:
            with z.open(xp) as f:
                root = ET.parse(f).getroot()
        actual_type = _text(root, "irs:ReturnHeader/irs:ReturnTypeCd")
        # Tax period derive
        period = _text(root, "irs:ReturnHeader/irs:TaxPeriodEndDt")
        tax_year = int(period[:4]) if period else 2022

        if actual_type == "990PF":
            grants = parse_990pf(root)
        elif actual_type in ("990", "990EZ"):
            grants = parse_990(root)
        else:
            print(f"  ? {name} {info['year']}: unknown {actual_type}")
            continue

        seen = len(grants)
        kept = 0
        for g in grants:
            cls, _ = classify(g["recipient_name"], g["grant_purpose"])
            if cls == "unrelated":
                continue
            conn.execute(
                """
                INSERT INTO foundation_grants
                    (donor_ein, tax_year, recipient_name, recipient_ein,
                     grant_amount, grant_purpose, grantee_classification,
                     source, source_filing_url, fetched_at_utc)
                VALUES (?, ?, ?, ?, ?, ?, ?, 'irs_xml', ?, ?);
                """,
                (ein, tax_year, g["recipient_name"], g["recipient_ein"],
                 g["grant_amount"], g["grant_purpose"], cls,
                 f"{zp}::{xp}", utcnow()),
            )
            kept += 1
        conn.commit()
        total_grants_seen += seen
        total_grants_kept += kept
        print(f"  {name:35s}  FY{tax_year}  {actual_type:5s}  seen={seen:>7,}  kept={kept:>4}")

    print(f"\nTotal: seen {total_grants_seen:,} grants, kept {total_grants_kept} that match classifier\n")

    # Summary by classification across all DAFs
    print("=== Classified DAF grants (recipient hit our taxonomy) ===")
    for r in conn.execute(
        """
        SELECT df.donor_ticker, df.foundation_name, g.grantee_classification,
               COUNT(*) AS n, SUM(g.grant_amount) AS total
        FROM foundation_grants g
        JOIN donor_foundations df ON df.ein = g.donor_ein
        WHERE df.relationship = 'daf' AND g.source = 'irs_xml'
        GROUP BY df.donor_ticker, g.grantee_classification
        ORDER BY df.donor_ticker, total DESC;
        """
    ):
        print(f"  {r['donor_ticker']:14s}  {r['grantee_classification']:36s}  "
              f"n={r['n']:4d}  ${r['total']:>14,d}")

    # Top anti_police_adversarial recipients across DAFs
    print("\n=== TOP anti_police_adversarial grants from DAFs ===")
    for r in conn.execute(
        """
        SELECT df.foundation_name, g.tax_year, g.recipient_name, g.grant_amount, g.grant_purpose
        FROM foundation_grants g
        JOIN donor_foundations df ON df.ein = g.donor_ein
        WHERE df.relationship = 'daf'
          AND g.source = 'irs_xml'
          AND g.grantee_classification = 'anti_police_adversarial'
        ORDER BY g.grant_amount DESC LIMIT 30;
        """
    ):
        amt = r["grant_amount"] or 0
        print(f"  {r['foundation_name'][:30]:30s}  FY{r['tax_year']}  ${amt:>10,d}  {r['recipient_name'][:50]}")

    # Top broad_cj_reform from DAFs
    print("\n=== TOP broad_cj_reform grants from DAFs ===")
    for r in conn.execute(
        """
        SELECT df.foundation_name, g.tax_year, g.recipient_name, g.grant_amount, g.grant_purpose
        FROM foundation_grants g
        JOIN donor_foundations df ON df.ein = g.donor_ein
        WHERE df.relationship = 'daf'
          AND g.source = 'irs_xml'
          AND g.grantee_classification IN ('broad_cj_reform', 'collaborative_reform', 'reentry_employment', 'innocence_wrongful_conviction')
        ORDER BY g.grant_amount DESC LIMIT 30;
        """
    ):
        amt = r["grant_amount"] or 0
        recipient = (r['recipient_name'] or "(unknown)")[:40]
        cls = r['grantee_classification'] or "?"
        print(f"  {r['foundation_name'][:30]:30s}  FY{r['tax_year']}  ${amt:>10,d}  "
              f"({cls})  {recipient}")

    conn.close()


if __name__ == "__main__":
    main()
