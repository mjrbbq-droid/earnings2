"""
Pull federal contract awards from USAspending.gov for our investigation targets.
Flags LE agency awards as is_le_agency=1.
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.config import RISK_DB_PATH
from src.risk_db import utcnow
from src.risk_schema import connect, init_db

USA_API = "https://api.usaspending.gov/api/v2/search/spending_by_award/"

# Law-enforcement sub-agencies. These appear in awarding_sub_agency in
# USAspending results when the contract is for federal law enforcement.
LE_SUB_AGENCIES = {
    "Federal Bureau of Investigation",
    "Drug Enforcement Administration",
    "U.S. Immigration and Customs Enforcement",
    "U.S. Customs and Border Protection",
    "U.S. Marshals Service",
    "Bureau of Alcohol, Tobacco, Firearms, and Explosives",
    "Bureau of Prisons / Federal Prison System",
    "Federal Prison Industries / Unicor",
    "Office of Justice Programs",
    "Office of Community Oriented Policing Services",
    "U.S. Secret Service",
    "Transportation Security Administration",
    "Cybersecurity and Infrastructure Security Agency",
    "Bureau of the Public Debt",
    "Department of Justice",
    "U.S. Postal Inspection Service",
}
LE_AGENCY_NAMES = {
    "Department of Justice",
    "Department of Homeland Security",
}
LE_KEYWORDS = ["FBI", "DOJ", "DEA", "ATF", "ICE", "CBP", "TSA", "USSS", "USMS",
               "DHS", "Federal Bureau of Investigation", "Immigration",
               "Customs and Border", "Justice", "Marshals", "Secret Service",
               "Drug Enforcement", "Alcohol Tobacco"]


def is_le(awarding_agency: str | None, awarding_sub: str | None) -> bool:
    if awarding_sub and awarding_sub in LE_SUB_AGENCIES:
        return True
    if awarding_agency and awarding_agency in LE_AGENCY_NAMES:
        return True
    blob = " ".join(filter(None, [awarding_agency, awarding_sub]))
    for kw in LE_KEYWORDS:
        if kw.lower() in blob.lower():
            return True
    return False


def fetch_contracts(company_name: str, start_date: str, end_date: str,
                    max_pages: int = 5, limit: int = 100) -> list[dict]:
    """Pull contracts where recipient name contains the search text."""
    all_results = []
    for page in range(1, max_pages + 1):
        body = {
            "filters": {
                "recipient_search_text": [company_name],
                "award_type_codes": ["A", "B", "C", "D"],     # contract awards
                "time_period": [{"start_date": start_date, "end_date": end_date}],
            },
            "fields": [
                "Award ID", "Recipient Name", "Awarding Agency",
                "Awarding Sub Agency", "Award Amount",
                "Period of Performance Start Date",
                "Period of Performance Current End Date",
                "Description",
            ],
            "page": page, "limit": limit, "sort": "Award Amount", "order": "desc",
        }
        r = requests.post(USA_API, json=body, timeout=60)
        if not r.ok:
            return all_results
        data = r.json()
        results = data.get("results") or []
        all_results.extend(results)
        if len(results) < limit:
            break
        time.sleep(0.5)
    return all_results


def upsert_contracts(conn, ticker: str, company_name: str, rows: list[dict]) -> int:
    inserted = 0
    for r in rows:
        agency = r.get("Awarding Agency")
        sub    = r.get("Awarding Sub Agency")
        le     = is_le(agency, sub)
        amt    = r.get("Award Amount")
        try:
            amount = float(amt) if amt is not None else 0
        except (TypeError, ValueError):
            amount = 0
        conn.execute(
            """
            INSERT INTO company_federal_contracts
                (ticker, company_name, recipient_name, award_id,
                 awarding_agency, awarding_sub_agency, description,
                 period_start, period_end, award_amount, is_le_agency,
                 fetched_at_utc)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
            """,
            (
                ticker, company_name, r.get("Recipient Name"),
                r.get("Award ID"), agency, sub, r.get("Description"),
                r.get("Period of Performance Start Date"),
                r.get("Period of Performance Current End Date"),
                amount, 1 if le else 0, utcnow(),
            ),
        )
        inserted += 1
    return inserted


def main() -> None:
    conn = connect(RISK_DB_PATH)
    init_db(conn)

    # Targets: all anti/mixed/pro_police_net companies
    rows = conn.execute(
        """
        SELECT ticker, company_name, net_position
        FROM company_stance_investigation
        WHERE net_position IN ('anti_police_net','mixed','pro_police_net')
        ORDER BY net_position, ticker;
        """
    ).fetchall()
    targets = [(r["ticker"], r["company_name"]) for r in rows]
    print(f"Pulling federal contracts for {len(targets)} target tickers (FY2020-FY2025)\n")

    # Fresh start — clear existing rows for these tickers
    for t, _ in targets:
        conn.execute("DELETE FROM company_federal_contracts WHERE ticker = ?;", (t,))
    conn.commit()

    total_contracts = 0
    total_le = 0

    for i, (ticker, name) in enumerate(targets, 1):
        # Search by simple short name — USAspending matches partial recipient names
        short_name = name.split(",")[0].split(" Inc")[0].split(" Corporation")[0].split(" Corp")[0].split(" Co.")[0].strip()
        if len(short_name) < 4:
            short_name = name
        print(f"  {i:3d}/{len(targets)}  {ticker:6s}  {short_name[:40]}", end="  ")
        try:
            results = fetch_contracts(short_name, "2020-01-01", "2025-05-22", max_pages=3)
        except Exception as e:
            print(f"  ERROR: {e}")
            continue
        n = upsert_contracts(conn, ticker, name, results)
        n_le = sum(1 for r in results if is_le(r.get("Awarding Agency"), r.get("Awarding Sub Agency")))
        total_contracts += n
        total_le += n_le
        print(f"contracts={n:>4d}  le_contracts={n_le}")
        conn.commit()

    print(f"\nLoaded {total_contracts:,} contracts; {total_le:,} LE-agency contracts\n")

    # Summary by ticker
    print("=== Federal LE-contract totals by ticker (FY2020-2025) ===")
    for r in conn.execute(
        """
        SELECT ticker, COUNT(*) AS n_le, SUM(award_amount) AS total_le_award
        FROM company_federal_contracts
        WHERE is_le_agency = 1
        GROUP BY ticker
        HAVING SUM(award_amount) > 0
        ORDER BY total_le_award DESC LIMIT 30;
        """
    ):
        amt = r["total_le_award"] or 0
        print(f"  {r['ticker']:6s}  n={r['n_le']:4d}  ${amt:>15,.0f}")

    conn.close()


if __name__ == "__main__":
    main()
