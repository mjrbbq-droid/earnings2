"""
Two fixes for foundation_grants:

1. Reclassify EIN 52-0906599 grants from pro_police -> collaborative_reform.
   That EIN is the National Police Foundation (now National Policing Institute),
   a DC-based research / think-tank that works WITH police on reform — not a
   local police-support foundation.

2. De-duplicate DAF grants that appear in BOTH the 2023 and 2024 IRS indices
   (Fidelity Charitable, Tides Foundation, Tides Center, etc. amended their
   FY2022 990s, so the same filing got loaded twice). Keep the most recent
   source_filing_url's copy of each (donor_ein, tax_year) pair.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.config import RISK_DB_PATH
from src.risk_schema import connect


def main() -> None:
    conn = connect(RISK_DB_PATH)

    # ── Fix 1: reclassify NPF/NPI EIN 52-0906599 ───────────────────────
    print("Fix 1: Reclassifying National Police Foundation / NPI (EIN 52-0906599)")
    before = conn.execute(
        "SELECT COUNT(*) AS n, SUM(grant_amount) AS t FROM foundation_grants "
        "WHERE recipient_ein = '520906599'"
    ).fetchone()
    print(f"  Grants targeting EIN 52-0906599: n={before['n']}  total=${before['t'] or 0:,}")

    n_updated = conn.execute(
        """
        UPDATE foundation_grants
        SET grantee_classification = 'collaborative_reform'
        WHERE recipient_ein = '520906599'
          AND grantee_classification = 'pro_police';
        """
    ).rowcount
    print(f"  Reclassified {n_updated} grants from pro_police -> collaborative_reform.")

    # Also catch the upper-case 'POLICE FOUNDATION' bare name where the EIN matches NPI
    # (some filings spell out the EIN slightly differently)
    conn.commit()

    # ── Fix 2: de-duplicate DAF filings appearing in BOTH 2023 and 2024 indices ─
    print("\nFix 2: De-duplicating DAF grants from amended/refiled 990s")

    # Find (donor_ein, tax_year) pairs with grants from multiple source_filing_urls
    dupe_keys = conn.execute(
        """
        SELECT donor_ein, tax_year, COUNT(DISTINCT source_filing_url) AS n_urls
        FROM foundation_grants
        WHERE source = 'irs_xml'
        GROUP BY donor_ein, tax_year
        HAVING n_urls > 1;
        """
    ).fetchall()
    print(f"  Found {len(dupe_keys)} (donor_ein, tax_year) pairs loaded from MULTIPLE source files:")
    for r in dupe_keys:
        # Resolve to a readable foundation name
        fname = conn.execute(
            "SELECT foundation_name FROM donor_foundations WHERE ein = ?;",
            (r["donor_ein"],),
        ).fetchone()
        print(f"    {r['donor_ein']:11s} FY{r['tax_year']}  {fname['foundation_name']:40s}  "
              f"({r['n_urls']} source files)")

    if not dupe_keys:
        print("  No duplicates detected.")
    else:
        # Strategy: for each (donor_ein, tax_year) with multiple URLs, keep the
        # newest source_filing_url (sorted lexically — 2024 zips sort after 2023)
        # and delete the rest.
        total_deleted = 0
        for r in dupe_keys:
            ein = r["donor_ein"]
            year = r["tax_year"]
            urls = conn.execute(
                """
                SELECT DISTINCT source_filing_url FROM foundation_grants
                WHERE donor_ein = ? AND tax_year = ? AND source = 'irs_xml'
                ORDER BY source_filing_url;
                """,
                (ein, year),
            ).fetchall()
            url_list = [r["source_filing_url"] for r in urls]
            keep_url = url_list[-1]   # newest (last lexically)
            for drop_url in url_list[:-1]:
                cur = conn.execute(
                    """
                    DELETE FROM foundation_grants
                    WHERE donor_ein = ? AND tax_year = ? AND source_filing_url = ?;
                    """,
                    (ein, year, drop_url),
                )
                total_deleted += cur.rowcount
                print(f"  Deleted {cur.rowcount} dupe rows for EIN {ein} FY{year} from {drop_url[:60]}")
        conn.commit()
        print(f"  TOTAL deleted: {total_deleted} duplicate grant rows.")

    # ── Post-fix totals ──────────────────────────────────────────────────
    print("\n=== UPDATED grand totals by classification ===")
    for r in conn.execute(
        """
        SELECT grantee_classification, COUNT(*) AS n, SUM(grant_amount) AS total
        FROM foundation_grants
        GROUP BY grantee_classification
        ORDER BY total DESC NULLS LAST;
        """
    ):
        amt = r["total"] or 0
        print(f"  {r['grantee_classification']:38s}  n={r['n']:>7,d}  ${amt:>14,d}")

    print("\n=== Updated pro-police flows (top 25, post-dedupe) ===")
    for r in conn.execute(
        """
        SELECT df.donor_ticker, df.foundation_name, g.tax_year,
               g.recipient_name, g.grant_amount
        FROM foundation_grants g
        JOIN donor_foundations df ON df.ein = g.donor_ein
        WHERE g.grantee_classification = 'pro_police'
        ORDER BY g.grant_amount DESC LIMIT 25;
        """
    ):
        amt = r["grant_amount"] or 0
        print(f"  {r['donor_ticker']:13s} FY{r['tax_year']}  ${amt:>10,d}  "
              f"({r['foundation_name'][:30]}) -> {r['recipient_name'][:40]}")

    print("\n=== Updated collaborative_reform top flows ===")
    for r in conn.execute(
        """
        SELECT df.donor_ticker, df.foundation_name, g.tax_year,
               g.recipient_name, g.grant_amount
        FROM foundation_grants g
        JOIN donor_foundations df ON df.ein = g.donor_ein
        WHERE g.grantee_classification = 'collaborative_reform'
        ORDER BY g.grant_amount DESC LIMIT 15;
        """
    ):
        amt = r["grant_amount"] or 0
        print(f"  {r['donor_ticker']:13s} FY{r['tax_year']}  ${amt:>10,d}  "
              f"({r['foundation_name'][:30]}) -> {r['recipient_name'][:40]}")

    conn.close()


if __name__ == "__main__":
    main()
