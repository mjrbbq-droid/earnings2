"""
Phase 5: Apply Option A labels + compute policy_stance_score (-5.0..+5.0).

Sign convention:  + = enforcement-leaning, − = reform-leaning, 0 = no material.

Score model (additive, clipped to [-5, +5]):
  reform side contribution:       REFORM_WEIGHTS[anti_type] × STATUS_MOD[anti_status]
  enforcement side contribution:  ENFORCE_WEIGHTS[pro_type] × STATUS_MOD[pro_status]
  fed LE bonus (enforcement-side): +1.5 if fed_le ≥ $1B; +1.0 if ≥ $100M; +0.5 if ≥ $10M

Label remap is in-DB UPDATE — no re-investigation needed.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.config import RISK_DB_PATH
from src.risk_schema import connect, init_db


# Reform-side type weights (negative contribution to score)
REFORM_WEIGHTS = {
    "defund_police_explicit":                  -5,
    "exit_facial_recognition_police":          -4,
    "refuse_facial_recognition_police":        -4,
    "moratorium_facial_recognition_police":    -3,
    "severed_police_partnership":              -3,
    "donations_anti_police_adversarial":       -3,
    "boycott_police_products":                 -3,
    "donations_broad_criminal_justice_reform": -2,
    "donations_collaborative_reform":          -1,
    "donations_reentry_employment":            -1,
    "donations_innocence_wrongful_conviction": -1,
    "statement_only":                          -1,
    "none":                                     0,
    "unknown":                                  0,
}

# Enforcement-side type weights (positive contribution to score)
ENFORCE_WEIGHTS = {
    "manufactures_police_weapons":              5,
    "sells_data_analytics_to_police":           4,
    "sells_facial_recognition_to_police":       4,
    "sells_surveillance_tech_to_police":        4,
    "hosts_police_data_infrastructure":         3,
    "sells_body_cameras_to_police":             3,
    "sells_radios_comms_to_police":             3,
    "donates_to_police_foundations":            3,
    "donates_to_police_unions":                 3,
    "marketing_partnership_first_responders":   2,
    "lobbies_for_police_funding":               2,
    "public_statement_supporting_police":       1,
    "none":                                     0,
    "unknown":                                  0,
}

STATUS_MOD = {
    "reinforced":  1.2,
    "expanded":    1.1,
    "maintained":  1.0,
    "active":      1.0,
    "eroded":      0.7,
    "unknown":     0.7,
    "suppressed":  0.7,  # parent suppressing it, but position still publicly held
    "faded":       0.5,  # PR went silent, may or may not still be acting
    "walked_back": 0.2,  # explicit reversal
}

# Option A label map
NET_POSITION_RENAMES = {
    "anti_police_net": "reform_leaning",
    "pro_police_net":  "enforcement_leaning",
    "mixed":           "cross_exposure",
    "neutral":         "no_material_exposure",
    "unknown":         "unknown",
}

# Anti-type rename: only the most loaded one
ANTI_TYPE_RENAMES = {
    "donations_anti_police_adversarial": "reform_advocacy_grants",
    # the others (defund_police_explicit, exit_facial_recognition_police, etc.)
    # are descriptive, not loaded — keep as is
}


def fed_le_bonus(amount: float) -> float:
    if amount is None:
        return 0
    if amount >= 1_000_000_000:
        return 1.5
    if amount >= 100_000_000:
        return 1.0
    if amount >= 10_000_000:
        return 0.5
    return 0


def compute_score(row: dict) -> float:
    anti_type = row.get("anti_police_type")
    pro_type  = row.get("pro_police_type")
    anti_status = row.get("anti_police_current_status") or "unknown"
    pro_status  = row.get("pro_police_current_status")  or "unknown"

    # Apply status modifier on absolute value, preserve sign
    reform_pts = 0.0
    if row.get("anti_police_action") and anti_type:
        w = REFORM_WEIGHTS.get(anti_type, 0)
        m = STATUS_MOD.get(anti_status, 0.7)
        reform_pts = w * m   # already negative

    enforce_pts = 0.0
    if row.get("pro_police_action") and pro_type:
        w = ENFORCE_WEIGHTS.get(pro_type, 0)
        m = STATUS_MOD.get(pro_status, 0.7)
        enforce_pts = w * m  # positive

    bonus = fed_le_bonus(row.get("fed_le_total") or 0)

    score = reform_pts + enforce_pts + bonus
    return max(-5.0, min(5.0, score))


def main() -> None:
    conn = connect(RISK_DB_PATH)
    init_db(conn)

    # 1. Remap net_position values
    print("Remapping net_position labels (Option A)...")
    for old, new in NET_POSITION_RENAMES.items():
        n = conn.execute(
            "UPDATE company_stance_investigation SET net_position = ? WHERE net_position = ?;",
            (new, old),
        ).rowcount
        if n:
            print(f"  {old:22s} -> {new:24s}  ({n} rows)")
    conn.commit()

    # 2. Remap anti_police_type loaded labels
    print("\nRemapping anti_police_type loaded labels...")
    for old, new in ANTI_TYPE_RENAMES.items():
        n = conn.execute(
            "UPDATE company_stance_investigation SET anti_police_type = ? WHERE anti_police_type = ?;",
            (new, old),
        ).rowcount
        if n:
            print(f"  {old:38s} -> {new:24s}  ({n} rows)")
        # Also remap in foundation_grants classifications
        n2 = conn.execute(
            "UPDATE foundation_grants SET grantee_classification = ? WHERE grantee_classification = 'anti_police_adversarial';",
            ("reform_advocacy_grants",),
        ).rowcount
        if n2:
            print(f"  (grants)  anti_police_adversarial -> reform_advocacy_grants  ({n2} rows)")
        break  # only the one remap
    conn.commit()

    # 3. Compute policy_stance_score for each row
    print("\nComputing policy_stance_score for 254 investigations...")
    # Pull fed_le_total per ticker
    fed_le = {
        r["ticker"]: r["total"] or 0
        for r in conn.execute(
            """
            SELECT ticker, SUM(award_amount) AS total
            FROM company_federal_contracts
            WHERE is_le_agency = 1
            GROUP BY ticker;
            """
        )
    }

    n_scored = 0
    for r in conn.execute(
        """
        SELECT ticker,
               anti_police_action, anti_police_type, anti_police_current_status,
               pro_police_action,  pro_police_type,  pro_police_current_status
        FROM company_stance_investigation;
        """
    ):
        row = dict(r)
        row["fed_le_total"] = fed_le.get(r["ticker"], 0)
        # Note: anti_type may have been remapped — adjust REFORM_WEIGHTS lookup
        anti = row["anti_police_type"]
        if anti == "reform_advocacy_grants":
            row["anti_police_type"] = "donations_anti_police_adversarial"  # use orig weight key
        score = round(compute_score(row), 2)
        conn.execute(
            "UPDATE company_stance_investigation SET policy_stance_score = ? WHERE ticker = ?;",
            (score, r["ticker"]),
        )
        n_scored += 1
    conn.commit()
    print(f"  Scored {n_scored} rows.\n")

    # 4. Report extremes
    print("=== TOP 15 ENFORCEMENT-LEANING (score >= +3) ===")
    for r in conn.execute(
        """
        SELECT ticker, company_name, policy_stance_score, net_position
        FROM company_stance_investigation
        WHERE policy_stance_score >= 3.0
        ORDER BY policy_stance_score DESC, ticker LIMIT 15;
        """
    ):
        print(f"  {r['ticker']:6s}  score=+{r['policy_stance_score']:.2f}  "
              f"net={r['net_position']:24s}  {r['company_name'][:38]}")

    print("\n=== TOP 15 REFORM-LEANING (score <= -2) ===")
    for r in conn.execute(
        """
        SELECT ticker, company_name, policy_stance_score, net_position
        FROM company_stance_investigation
        WHERE policy_stance_score <= -2.0
        ORDER BY policy_stance_score ASC, ticker LIMIT 15;
        """
    ):
        print(f"  {r['ticker']:6s}  score={r['policy_stance_score']:+.2f}  "
              f"net={r['net_position']:24s}  {r['company_name'][:38]}")

    print("\n=== Cross-exposure (mixed) companies + their scores ===")
    for r in conn.execute(
        """
        SELECT ticker, company_name, policy_stance_score, anti_police_type, pro_police_type
        FROM company_stance_investigation
        WHERE net_position = 'cross_exposure'
        ORDER BY policy_stance_score, ticker;
        """
    ):
        print(f"  {r['ticker']:6s}  score={r['policy_stance_score']:+.2f}  "
              f"anti={(r['anti_police_type'] or '')[:30]:30s}  "
              f"pro={(r['pro_police_type'] or '')[:30]}")

    print("\n=== Score distribution buckets ===")
    for r in conn.execute(
        """
        SELECT
          CASE
            WHEN policy_stance_score >= 3   THEN 'a) +3 to +5  (strong enforcement)'
            WHEN policy_stance_score >= 1   THEN 'b) +1 to +3  (lean enforcement)'
            WHEN policy_stance_score > -1   THEN 'c) -1 to +1  (no material posture)'
            WHEN policy_stance_score > -3   THEN 'd) -1 to -3  (lean reform)'
            WHEN policy_stance_score <= -3  THEN 'e) -3 to -5  (strong reform)'
          END AS bucket,
          COUNT(*) AS n
        FROM company_stance_investigation
        WHERE policy_stance_score IS NOT NULL
        GROUP BY bucket ORDER BY bucket;
        """
    ):
        print(f"  {r['bucket']:38s}  n={r['n']}")

    conn.close()


if __name__ == "__main__":
    main()
