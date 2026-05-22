# scripts/zscore_homebuilders.py
from __future__ import annotations

import sqlite3, json, csv
from statistics import mean, pstdev
from pathlib import Path

DB = "./data/earnings.db"
OUT_DIR = "./data"

DEEP_PROMPT = "hb_deep_v2"
FORENSICS_PROMPT = "hb_forensics_v1"

TREND = {"improving": 1, "stable": 0, "deteriorating": -1, "unclear": 0}
LEVEL = {"low": -1, "medium": 0, "high": 1, "unclear": 0}

PVV = {"volume_led": 1, "mixed": 0, "price_led": -1, "unclear": 0}
GUIDE = {"raised": 2, "reaffirmed": 0, "cut": -2, "withdrawn": -2, "no_guidance": 0, "unclear": 0}
TIMING = {"front_half": 1, "even": 0, "back_half": -1, "unclear": 0}
VIS = {"high": 1, "medium": 0, "low": -1, "unclear": 0}
MOUT = {"expanding": 1, "stable": 0, "contracting": -1, "unclear": 0}

def z(x, mu, sd):
    return 0.0 if sd == 0 else (x - mu) / sd

c = sqlite3.connect(DB)
c.row_factory = sqlite3.Row

tickers = [r[0] for r in c.execute("SELECT DISTINCT ticker FROM earnings_calls ORDER BY ticker;").fetchall()]

for ticker in tickers:
    rows = c.execute(
        """
        SELECT ec.id, ec.call_date, ec.fiscal_year, ec.fiscal_quarter,
               sd.signature_json AS deep_json,
               sf.signature_json AS forensics_json
        FROM earnings_calls ec
        LEFT JOIN earnings_signatures sd
          ON sd.earnings_call_id = ec.id AND sd.prompt_version = ?
        LEFT JOIN earnings_signatures sf
          ON sf.earnings_call_id = ec.id AND sf.prompt_version = ?
        WHERE ec.ticker = ?
        ORDER BY ec.call_date;
        """,
        (DEEP_PROMPT, FORENSICS_PROMPT, ticker),
    ).fetchall()

    # Need deep signatures at minimum + require call_date
    rows = [r for r in rows if r["deep_json"] and r["call_date"]]
    if not rows:
        continue

    series = []
    for r in rows:
        deep = json.loads(r["deep_json"])
        forensic = json.loads(r["forensics_json"]) if r["forensics_json"] else {}

        # Normalize call_date to YYYY-MM-DD
        call_date = str(r["call_date"])[:10]

        # Margin Pressure Index (higher = worse)
        mpi = (
            -TREND.get(deep.get("gross_margin_direction","unclear"),0) * 2
            -TREND.get(deep.get("pricing_power","unclear"),0) * 2
            -TREND.get(deep.get("incentive_intensity","unclear"),0) * 2
            -TREND.get(deep.get("buydown_intensity","unclear"),0) * 1
            +LEVEL.get(deep.get("spec_mix_level","unclear"),0) * 1
            +LEVEL.get(deep.get("inventory_pressure","unclear"),0) * 1
            +LEVEL.get(deep.get("competitive_pressure","unclear"),0) * 1
        )

        # Demand Quality Index (higher = better)
        dqi = (
            TREND.get(deep.get("demand_trend","unclear"),0) * 2
            +TREND.get(deep.get("traffic_leads","unclear"),0) * 1
            +TREND.get(deep.get("conversion","unclear"),0) * 1
            -TREND.get(deep.get("cancellations","unclear"),0) * 2
            +(1 if deep.get("backlog_quality") == "strong" else -1 if deep.get("backlog_quality") == "weak" else 0)
        )

        # Sales Signal Index (SSI) (higher = better sales reality)
        ssi = (
            TREND.get(deep.get("sales_trend","unclear"),0) * 2
            +PVV.get(deep.get("price_vs_volume","unclear"),0) * 1
            +TREND.get(deep.get("volume_trend","unclear"),0) * 1
            +TREND.get(deep.get("backlog_direction","unclear"),0) * 1
        )

        # Earnings Revision Index (ERI) (higher = better)
        eri = (
            GUIDE.get(deep.get("earnings_guidance_action","unclear"),0)
            +TIMING.get(deep.get("earnings_timing_bias","unclear"),0)
            +VIS.get(deep.get("earnings_visibility","unclear"),0)
            +MOUT.get(deep.get("margin_outlook","unclear"),0)
        )

        # Language Risk Index (higher = worse)
        lri = float(forensic.get("evasion_score", 0))
        lri += 10 * LEVEL.get(forensic.get("hedging_intensity","low"), -1)
        lri += 10 * LEVEL.get(forensic.get("deflection_intensity","low"), -1)
        lri += 10 * LEVEL.get(forensic.get("constraint_framing","low"), -1)
        if forensic.get("answer_substitution_detected"):
            lri += 10
        if forensic.get("specificity_asymmetry") == "negative":
            lri += 10

        series.append({
            "ticker": ticker,
            "call_date": call_date,
            "fiscal_year": r["fiscal_year"],
            "fiscal_quarter": r["fiscal_quarter"],
            "mpi_raw": mpi,
            "dqi_raw": dqi,
            "ssi_raw": ssi,
            "eri_raw": eri,
            "lri_raw": lri,
        })

    # Deduplicate any accidental repeats on (ticker, call_date)
    seen = set()
    series2 = []
    for x in series:
        key = (x["ticker"], x["call_date"])
        if key in seen:
            continue
        seen.add(key)
        series2.append(x)
    series = series2

    def _mu_sd(vals):
        return mean(vals), pstdev(vals)

    mpi_mu, mpi_sd = _mu_sd([x["mpi_raw"] for x in series])
    dqi_mu, dqi_sd = _mu_sd([x["dqi_raw"] for x in series])
    ssi_mu, ssi_sd = _mu_sd([x["ssi_raw"] for x in series])
    eri_mu, eri_sd = _mu_sd([x["eri_raw"] for x in series])
    lri_mu, lri_sd = _mu_sd([x["lri_raw"] for x in series])

    out_path = Path(OUT_DIR) / f"{ticker}_zscores.csv"
    with open(out_path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=[
            "ticker","call_date","fiscal_year","fiscal_quarter",
            "mpi_raw","mpi_z",
            "dqi_raw","dqi_z",
            "ssi_raw","ssi_z",
            "eri_raw","eri_z",
            "lri_raw","lri_z"
        ])
        w.writeheader()
        for x in series:
            w.writerow({
                **x,
                "mpi_z": round(z(x["mpi_raw"], mpi_mu, mpi_sd), 3),
                "dqi_z": round(z(x["dqi_raw"], dqi_mu, dqi_sd), 3),
                "ssi_z": round(z(x["ssi_raw"], ssi_mu, ssi_sd), 3),
                "eri_z": round(z(x["eri_raw"], eri_mu, eri_sd), 3),
                "lri_z": round(z(x["lri_raw"], lri_mu, lri_sd), 3),
            })

    print("Wrote:", out_path)

c.close()


