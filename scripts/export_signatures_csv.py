# scripts/export_signatures_csv.py
from __future__ import annotations

import json
import csv
import sqlite3


def clean_text(s: str) -> str:
    if not s:
        return ""
    # Common mojibake from PDF extraction
    return (
        s.replace("Ã¢â‚¬Å“", "â€œ")
         .replace("Ã¢â‚¬Â", "â€")
         .replace("Ã¢â‚¬â„¢", "â€™")
         .replace("Ã¢â‚¬â€œ", "â€“")
         .replace("Ã¢â‚¬â€", "â€”")
         .replace("Ã¢â‚¬Â¦", "â€¦")
         .replace("ÃƒÂ©", "Ã©")
    )


def main() -> None:
    db = "./data/databases/earnings.db"
    out = "./data/reference/bzh_signatures.csv"

    c = sqlite3.connect(db)
    c.row_factory = sqlite3.Row

    rows = c.execute(
        """
        SELECT ec.id, ec.ticker, ec.call_date, ec.fiscal_year, ec.fiscal_quarter, es.signature_json
        FROM earnings_calls ec
        JOIN earnings_signatures es ON es.earnings_call_id = ec.id
        ORDER BY
          COALESCE(ec.call_date, '') ASC,
          COALESCE(ec.fiscal_year, 0) ASC,
          COALESCE(ec.fiscal_quarter, 0) ASC,
          ec.id ASC;
        """
    ).fetchall()

    cols = [
        "call_id", "ticker", "call_date", "fiscal_year", "fiscal_quarter",
        "regime_label", "demand_trend", "pricing_power", "incentives", "cancellations",
        "backlog", "traffic_leads", "affordability", "build_cycle_times",
        "labor_material_costs", "land_lots", "guidance_bias", "visibility_confidence",
        "key_changes", "supporting_quotes"
    ]

    with open(out, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=cols)
        w.writeheader()

        for r in rows:
            s = json.loads(r["signature_json"])

            # Clean quotes
            quotes = s.get("supporting_quotes", [])
            for q in quotes:
                q["quote"] = clean_text(q.get("quote", ""))

            w.writerow({
                "call_id": r["id"],
                "ticker": r["ticker"],
                "call_date": r["call_date"],
                "fiscal_year": r["fiscal_year"],
                "fiscal_quarter": r["fiscal_quarter"],
                "regime_label": s.get("regime_label"),
                "demand_trend": s.get("demand_trend"),
                "pricing_power": s.get("pricing_power"),
                "incentives": s.get("incentives"),
                "cancellations": s.get("cancellations"),
                "backlog": s.get("backlog"),
                "traffic_leads": s.get("traffic_leads"),
                "affordability": s.get("affordability"),
                "build_cycle_times": s.get("build_cycle_times"),
                "labor_material_costs": s.get("labor_material_costs"),
                "land_lots": s.get("land_lots"),
                "guidance_bias": s.get("guidance_bias"),
                "visibility_confidence": s.get("visibility_confidence"),
                "key_changes": json.dumps(s.get("key_changes", []), ensure_ascii=False),
                "supporting_quotes": json.dumps(quotes, ensure_ascii=False),
            })

    print("Wrote:", out)


if __name__ == "__main__":
    main()
