"""
Parse Schedule I / Part XV grant-line detail from IRS 990 XML and load into
foundation_grants. Run after scripts/load_990_data.py.

Handles:
  - 990-PF: <GrantOrContributionPdDurYrGrp> elements in <SupplementaryInformationGrp>
  - 990:    <GrantsOtherAsstToOrgsInUSGrp> in <IRS990ScheduleI>

For each grant: recipient_name, recipient_ein (if present), grant_amount,
grant_purpose.
"""
from __future__ import annotations

import sys
import zipfile
from pathlib import Path
from xml.etree import ElementTree as ET

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.config import RISK_DB_PATH
from src.risk_db import utcnow
from src.risk_schema import connect, init_db

# IRS efile XML namespace
NS = {"irs": "http://www.irs.gov/efile"}


# Known filing locations (object_id -> (zip_path, xml_path_inside_zip))
FILING_LOCATIONS = {
    "202312869349100516": ("data/irs_zips/2023_TEOS_XML_10A.zip",
                            "2023_TEOS_XML_10A/202312869349100516_public.xml",
                            "94-6064702", "Levi Strauss Foundation", 2022),
    "202321019349102627": ("data/irs_zips/2023_TEOS_XML_04A.zip",
                            "2023_TEOS_XML_04A/202321019349102627_public.xml",
                            "93-1159948", "Nike Foundation", 2022),
    "202322839349100212": ("data/irs_zips/2023_TEOS_XML_10A.zip",
                            "2023_TEOS_XML_10A/202322839349100212_public.xml",
                            "23-7049738", "JPMorgan Chase Foundation", 2022),
    "202343199349103794": ("data/irs_zips/2023_TEOS_XML_11A.zip",
                            "2023_TEOS_XML_11A/202343199349103794_public.xml",
                            "26-2456949", "Chubb Charitable Foundation", 2022),
}


def _text(el, path: str) -> str | None:
    """Get text from a namespaced sub-element."""
    if el is None:
        return None
    sub = el.find(path, NS)
    return sub.text.strip() if sub is not None and sub.text else None


def parse_990pf_grants(root: ET.Element) -> list[dict]:
    """Pull grants from a 990-PF return."""
    grants = []
    for grant_el in root.iter(f"{{{NS['irs']}}}GrantOrContributionPdDurYrGrp"):
        # Recipient name (may be person or business)
        recipient = (
            _text(grant_el, "irs:RecipientBusinessName/irs:BusinessNameLine1Txt")
            or _text(grant_el, "irs:RecipientPersonNm")
            or _text(grant_el, "irs:RecipientNameNm")
        )
        amt_txt = _text(grant_el, "irs:Amt")
        try:
            amount = int(amt_txt) if amt_txt else None
        except ValueError:
            amount = None
        purpose = _text(grant_el, "irs:GrantOrContributionPurposeTxt")
        recipient_ein = _text(grant_el, "irs:RecipientEIN")
        grants.append({
            "recipient_name": recipient or "(unknown)",
            "recipient_ein": recipient_ein,
            "grant_amount": amount,
            "grant_purpose": purpose,
        })
    return grants


def parse_990_scheduleI_grants(root: ET.Element) -> list[dict]:
    """Pull grants from a 990 Schedule I (public charity)."""
    grants = []
    for grant_el in root.iter(f"{{{NS['irs']}}}GrantsOtherAsstToOrgsInUSGrp"):
        recipient = (
            _text(grant_el, "irs:RecipientBusinessName/irs:BusinessNameLine1Txt")
            or _text(grant_el, "irs:RecipientNameNm")
        )
        amt_txt = _text(grant_el, "irs:CashGrantAmt")
        try:
            amount = int(amt_txt) if amt_txt else None
        except ValueError:
            amount = None
        purpose = _text(grant_el, "irs:PurposeOfGrantTxt")
        recipient_ein = _text(grant_el, "irs:RecipientEIN")
        grants.append({
            "recipient_name": recipient or "(unknown)",
            "recipient_ein": recipient_ein,
            "grant_amount": amount,
            "grant_purpose": purpose,
        })
    return grants


def load_xml(zip_path: str, xml_path: str) -> ET.Element:
    with zipfile.ZipFile(zip_path) as z:
        with z.open(xml_path) as f:
            return ET.parse(f).getroot()


def upsert_grant(conn, donor_ein: str, tax_year: int, grant: dict,
                 source_url: str | None) -> None:
    conn.execute(
        """
        INSERT INTO foundation_grants
            (donor_ein, tax_year, recipient_name, recipient_ein,
             grant_amount, grant_purpose, source, source_filing_url, fetched_at_utc)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?);
        """,
        (
            donor_ein.replace("-", ""),
            tax_year,
            grant["recipient_name"],
            grant["recipient_ein"],
            grant["grant_amount"],
            grant["grant_purpose"],
            "irs_xml",
            source_url,
            utcnow(),
        ),
    )


def main() -> None:
    conn = connect(RISK_DB_PATH)
    init_db(conn)

    # Clear existing irs_xml grants for these foundations so we can re-run cleanly
    eins = [e.replace("-", "") for _, _, e, _, _ in FILING_LOCATIONS.values()]
    placeholders = ",".join("?" * len(eins))
    conn.execute(
        f"DELETE FROM foundation_grants WHERE source='irs_xml' AND donor_ein IN ({placeholders});",
        eins,
    )
    conn.commit()

    total_grants = 0
    total_amount = 0

    for object_id, (zp, xp, ein, name, tax_year) in FILING_LOCATIONS.items():
        print(f"\n--- {name}  EIN {ein}  tax_year {tax_year}  object_id {object_id}")
        root = load_xml(zp, xp)
        return_type = _text(root, "irs:ReturnHeader/irs:ReturnTypeCd")
        print(f"    return_type = {return_type}")

        if return_type == "990PF":
            grants = parse_990pf_grants(root)
        elif return_type in ("990", "990EZ"):
            grants = parse_990_scheduleI_grants(root)
        else:
            print(f"    Unknown return type, skipping")
            continue

        for g in grants:
            upsert_grant(conn, ein, tax_year, g, source_url=f"{zp}::{xp}")

        total = sum(g["grant_amount"] or 0 for g in grants)
        print(f"    Parsed {len(grants)} grants  total = ${total/1e6:.2f}M")
        total_grants += len(grants)
        total_amount += total

    conn.commit()
    print(f"\n=== Loaded {total_grants} grants  total ${total_amount/1e6:.2f}M ===")

    # Quick top-recipients per foundation
    print()
    for object_id, (_, _, ein, name, _) in FILING_LOCATIONS.items():
        ein_clean = ein.replace("-", "")
        print(f"\n--- Top 8 recipients: {name} ---")
        for r in conn.execute(
            """
            SELECT recipient_name, grant_amount
            FROM foundation_grants
            WHERE donor_ein = ? AND source = 'irs_xml'
            ORDER BY grant_amount DESC NULLS LAST
            LIMIT 8;
            """,
            (ein_clean,),
        ):
            amt = r['grant_amount'] or 0
            print(f"    ${amt:>12,d}  {r['recipient_name']}")

    conn.close()


if __name__ == "__main__":
    main()
