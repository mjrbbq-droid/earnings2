"""
Classify foundation_grants recipients against the v3 taxonomy:
  - anti_police_adversarial   (BLM, ArchCity, Reclaim the Block, Min Freedom Fund, etc.)
  - collaborative_reform      (Policing Project, Equal Justice USA, Center for Policing Equity)
  - reentry_employment        (Anti-Recidivism, Defy Ventures, CEO, Second Chance)
  - innocence_wrongful_conviction (Innocence Project family)
  - broad_cj_reform           (Vera Institute, NAACP LDF, ACLU CJ, Brennan Center, etc.)
  - pro_police                (Atlanta PF, NYC PF, PBA, FOP — flag these for the donor!)
  - unrelated                 (everything else — most grants)

Rule-based pattern matching. Each rule is a (regex, classification, note) tuple.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.config import RISK_DB_PATH
from src.risk_schema import connect, init_db


# ─── Pattern → classification map ────────────────────────────────────────
# Order matters — more specific patterns first.
RULES: list[tuple[re.Pattern, str, str]] = [
    # ── Anti-police adversarial ───────────────────────────────────────
    (re.compile(r"\bARCHCITY\b", re.I),                         "anti_police_adversarial", "ArchCity Defenders — litigates against police"),
    (re.compile(r"\bMINNESOTA FREEDOM FUND\b", re.I),           "anti_police_adversarial", "Cash-bail abolition (Minneapolis)"),
    (re.compile(r"\bRECLAIM THE BLOCK\b", re.I),                "anti_police_adversarial", "Explicit defund-police (Minneapolis)"),
    (re.compile(r"\bMOVEMENT FOR BLACK LIVES\b", re.I),         "anti_police_adversarial", "M4BL — defund-police coalition"),
    (re.compile(r"\bCRITICAL RESISTANCE\b", re.I),              "anti_police_adversarial", "Prison-industrial-complex abolition"),
    (re.compile(r"\bBLACK LIVES MATTER\b", re.I),               "anti_police_adversarial", "BLM Foundation / Global Network"),
    (re.compile(r"\bBLM (FOUNDATION|GLOBAL|NETWORK)\b", re.I),  "anti_police_adversarial", "BLM Foundation / Global Network"),
    (re.compile(r"\bCOLOR OF CHANGE\b", re.I),                  "anti_police_adversarial", "Police accountability campaigns"),
    (re.compile(r"\bDREAM DEFENDERS\b", re.I),                  "anti_police_adversarial", "Anti-mass-criminalization advocacy"),
    (re.compile(r"\bCAMPAIGN ZERO\b", re.I),                    "anti_police_adversarial", "8 Can't Wait, defund-adjacent"),

    # ── Innocence projects ────────────────────────────────────────────
    (re.compile(r"\bINNOCENCE PROJECT\b", re.I),                "innocence_wrongful_conviction", ""),
    (re.compile(r"\bCENTURION (MINISTRIES|INC)\b", re.I),       "innocence_wrongful_conviction", ""),
    (re.compile(r"\bEXONERATION\b", re.I),                      "innocence_wrongful_conviction", ""),

    # ── Re-entry / second chance ──────────────────────────────────────
    (re.compile(r"\bANTI[-\s]?RECIDIVISM\b", re.I),             "reentry_employment", "Anti-Recidivism Coalition"),
    (re.compile(r"\bDEFY VENTURES\b", re.I),                    "reentry_employment", ""),
    (re.compile(r"\bCENTER FOR EMPLOYMENT OPPORTUNIT", re.I),   "reentry_employment", "CEO"),
    (re.compile(r"\bSECOND CHANCE\b", re.I),                    "reentry_employment", "Re-entry employment"),
    (re.compile(r"\bRE[-\s]?ENTRY\b", re.I),                    "reentry_employment", ""),
    (re.compile(r"\bFORTUNE SOCIETY\b", re.I),                  "reentry_employment", ""),

    # ── Collaborative reform (works WITH police) ──────────────────────
    (re.compile(r"\bPOLICING PROJECT\b", re.I),                 "collaborative_reform", "Policing Project @ NYU"),
    (re.compile(r"\bCENTER FOR POLICING EQUITY\b", re.I),       "collaborative_reform", ""),
    (re.compile(r"\bEQUAL JUSTICE USA\b", re.I),                "collaborative_reform", "Trauma to Trust"),
    (re.compile(r"\bPOLICE EXECUTIVE RESEARCH FORUM\b", re.I),  "collaborative_reform", "PERF"),
    (re.compile(r"\bCOMMUNITY POLICING\b", re.I),               "collaborative_reform", ""),
    (re.compile(r"\bNATIONAL POLICING INSTITUTE\b", re.I),      "collaborative_reform",
        "Formerly National Police Foundation; research/think-tank"),
    # Special-case the bare name "POLICE FOUNDATION" when it's THE Police Foundation (EIN 52-0906599),
    # the DC research org. Local police foundations (NYC, LA, Atlanta, etc.) keep their distinct names.

    # ── Pro-police (flag if a donor funds these — usually opposite direction) ─
    (re.compile(r"\bATLANTA POLICE FOUNDATION\b", re.I),        "pro_police",            "APF — funds Cop City"),
    (re.compile(r"\bNYC?\s?POLICE FOUNDATION\b|NEW YORK CITY POLICE FOUNDATION", re.I),  "pro_police", ""),
    (re.compile(r"\bLOS ANGELES POLICE FOUNDATION\b|\bLAPF\b", re.I), "pro_police",      ""),
    (re.compile(r"\bPOLICE FOUNDATION\b", re.I),                "pro_police",            "Generic police foundation"),
    (re.compile(r"\bPOLICE BENEVOLENT\b", re.I),                "pro_police",            "Police union"),
    (re.compile(r"\bFRATERNAL ORDER OF POLICE\b|\bFOP\b", re.I), "pro_police",           "Police union"),
    (re.compile(r"\bPOLICE ATHLETIC LEAGUE\b", re.I),           "pro_police",            "PAL — youth-and-cops program"),
    (re.compile(r"\bSURVIVING WIDOWS\b|\bC\.O\.P\.S\.\b", re.I), "pro_police",           "Concerns of Police Survivors"),

    # ── DAF intermediaries — the black box. Foundation $ goes IN here; trail
    # vanishes. The original donor's intent shows up in the PURPOSE field but
    # the actual recipient is opaque in the foundation's 990.
    (re.compile(r"\bNATIONAL PHILANTHROPIC TRUST\b", re.I),     "daf_intermediary",      "NPT — major DAF host"),
    (re.compile(r"\bAMERICAN ONLINE GIVING FOUNDATION\b", re.I),"daf_intermediary",      "AOGF — DAF / fiscal sponsor"),
    (re.compile(r"\bUK ONLINE GIVING FOUNDATION\b", re.I),      "daf_intermediary",      ""),
    (re.compile(r"\bSCHWAB CHARITABLE\b", re.I),                "daf_intermediary",      ""),
    (re.compile(r"\bFIDELITY CHARITABLE\b", re.I),              "daf_intermediary",      ""),
    (re.compile(r"\bVANGUARD CHARITABLE\b", re.I),              "daf_intermediary",      ""),
    (re.compile(r"\bCAF AMERICA\b|CHARITIES AID FOUNDATION OF AMERICA", re.I), "daf_intermediary", ""),
    (re.compile(r"\bTIDES FOUNDATION\b|\bTIDES CENTER\b", re.I),"daf_intermediary",      ""),
    (re.compile(r"\bROCKEFELLER PHILANTHROPY ADVISORS\b", re.I),"daf_intermediary",      ""),
    (re.compile(r"\bSILICON VALLEY COMMUNITY FOUNDATION\b", re.I),"daf_intermediary",    ""),
    (re.compile(r"\bGOFUNDME\.ORG\b|\bGOFUNDME ORG\b", re.I),   "daf_intermediary",      ""),

    # ── Broad criminal justice reform (not clearly anti or collab) ────
    (re.compile(r"\bVERA INSTITUTE\b", re.I),                   "broad_cj_reform",        "Vera Institute of Justice"),
    (re.compile(r"\bNAACP LEGAL DEFENSE\b|\bNAACP LDF\b", re.I),"broad_cj_reform",        "NAACP LDF — police accountability litigation"),
    (re.compile(r"\bBRENNAN CENTER\b", re.I),                   "broad_cj_reform",        ""),
    (re.compile(r"\bSENTENCING PROJECT\b", re.I),               "broad_cj_reform",        ""),
    (re.compile(r"\bEQUAL JUSTICE INITIATIVE\b|\bEJI\b", re.I), "broad_cj_reform",        "Bryan Stevenson — anti-death-penalty / mass incarceration"),
    (re.compile(r"\bLAWYERS.{0,5}COMMITTEE.{0,15}CIVIL RIGHTS\b", re.I), "broad_cj_reform", "Police accountability litigation"),
    (re.compile(r"\bACLU\b", re.I),                             "broad_cj_reform",        "ACLU — broad civil-liberties"),
    (re.compile(r"\bLIVE FREE\b", re.I),                        "broad_cj_reform",        "Faith-based anti-violence"),
    (re.compile(r"\bELLA BAKER CENTER\b", re.I),                "broad_cj_reform",        "Anti-incarceration (Bay Area)"),
    (re.compile(r"\bFAIR AND JUST PROSECUTION\b", re.I),        "broad_cj_reform",        ""),
    (re.compile(r"\bPROSECUTORIAL REFORM\b", re.I),             "broad_cj_reform",        ""),
]


def classify(name: str | None, purpose: str | None) -> tuple[str, str]:
    """Return (classification, note). Searches name + purpose."""
    if not name:
        return "unrelated", ""
    blob = (name or "") + " " + (purpose or "")
    for pat, cls, note in RULES:
        if pat.search(blob):
            return cls, note
    return "unrelated", ""


def main() -> None:
    conn = connect(RISK_DB_PATH)
    init_db(conn)

    rows = conn.execute(
        """
        SELECT id, recipient_name, grant_purpose
        FROM foundation_grants
        WHERE source = 'irs_xml';
        """
    ).fetchall()
    print(f"Classifying {len(rows)} grants...")

    counts: dict[str, int] = {}
    for r in rows:
        cls, _note = classify(r["recipient_name"], r["grant_purpose"])
        conn.execute(
            "UPDATE foundation_grants SET grantee_classification = ? WHERE id = ?;",
            (cls, r["id"]),
        )
        counts[cls] = counts.get(cls, 0) + 1
    conn.commit()

    print("\nClassification distribution:")
    for cls, n in sorted(counts.items(), key=lambda x: -x[1]):
        print(f"  {cls:35s} {n:5d}")

    # ── Per-foundation breakdown ────────────────────────────────────────
    print("\n=== PER-FOUNDATION BREAKDOWN (US$, FY2022) ===\n")
    for r in conn.execute(
        """
        SELECT df.donor_ticker, df.foundation_name,
               g.grantee_classification,
               COUNT(*) AS n_grants,
               COALESCE(SUM(g.grant_amount), 0) AS total_amount
        FROM foundation_grants g
        JOIN donor_foundations df ON df.ein = g.donor_ein
        WHERE g.source = 'irs_xml'
        GROUP BY df.donor_ticker, g.grantee_classification
        ORDER BY df.donor_ticker, total_amount DESC;
        """
    ):
        t = r["donor_ticker"]
        cls = r["grantee_classification"] or "unclassified"
        n = r["n_grants"]
        amt = r["total_amount"]
        print(f"  {t:6s}  {cls:35s} n={n:5d}  ${amt:>14,d}")

    # ── Specifically flagged grants (anti/collab/reentry/pro/innocence/cj) ─
    print("\n=== ALL POLICE-ADJACENT GRANTS (non-unrelated) ===\n")
    for r in conn.execute(
        """
        SELECT df.donor_ticker, g.tax_year, g.grantee_classification,
               g.recipient_name, g.grant_amount, g.grant_purpose
        FROM foundation_grants g
        JOIN donor_foundations df ON df.ein = g.donor_ein
        WHERE g.source = 'irs_xml'
          AND g.grantee_classification != 'unrelated'
        ORDER BY
            CASE g.grantee_classification
                WHEN 'anti_police_adversarial'      THEN 1
                WHEN 'pro_police'                   THEN 2
                WHEN 'collaborative_reform'         THEN 3
                WHEN 'broad_cj_reform'              THEN 4
                WHEN 'reentry_employment'           THEN 5
                WHEN 'innocence_wrongful_conviction' THEN 6
                ELSE 9
            END,
            g.grant_amount DESC;
        """
    ):
        amt = r["grant_amount"] or 0
        cls = r["grantee_classification"] or ""
        print(f"  {r['donor_ticker']:6s}  {r['tax_year']}  {cls:30s}  ${amt:>10,d}  {r['recipient_name'][:50]}")

    conn.close()


if __name__ == "__main__":
    main()
