# scripts/zscore_calls.py
from __future__ import annotations

import sqlite3
import json
from statistics import mean, pstdev
from datetime import datetime, timezone
from typing import Optional

DB = "./data/earnings.db"

# Mappings
TREND = {"improving": 1, "stable": 0, "deteriorating": -1, "unclear": 0}
LEVEL = {"low": -1, "medium": 0, "high": 1, "unclear": 0}
PVV = {"volume_led": 1, "mixed": 0, "price_led": -1, "unclear": 0}
GUIDE = {"raised": 2, "reaffirmed": 0, "cut": -2, "withdrawn": -2, "no_guidance": 0, "unclear": 0}
TIMING = {"front_half": 1, "even": 0, "back_half": -1, "unclear": 0}
VIS = {"high": 1, "medium": 0, "low": -1, "unclear": 0}
MOUT = {"expanding": 1, "stable": 0, "contracting": -1, "unclear": 0}

FORENSICS_PROMPT = "hb_forensics_v1"

# bump this whenever scoring logic changes
SCORE_VERSION = "zscore_calls_v3"


def z(x: float, mu: float, sd: float) -> float:
    return 0.0 if sd == 0 else (x - mu) / sd


def ensure_call_zscores(conn: sqlite3.Connection) -> None:
    """
    Ensure the call_zscores table exists and is protected against duplicates.

    Hard rule: one row per (ticker, call_date).
    """
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS call_zscores (
            ticker TEXT NOT NULL,
            call_date TEXT,                 -- YYYY-MM-DD
            fiscal_year INTEGER,
            fiscal_quarter INTEGER,
            earnings_call_id INTEGER,

            mpi_raw REAL, mpi_z REAL,
            dqi_raw REAL, dqi_z REAL,
            ssi_raw REAL, ssi_z REAL,
            eri_raw REAL, eri_z REAL,
            lri_raw REAL, lri_z REAL,

            pmi_raw REAL, pmi_z REAL,
            bsi_raw REAL, bsi_z REAL,

            source_file TEXT NOT NULL,
            loaded_at_utc TEXT NOT NULL,

            composite_score REAL,
            call_stance TEXT,
            score_version TEXT,
            scored_at_utc TEXT,

            -- historical PK kept for compatibility; uniqueness is enforced separately
            PRIMARY KEY (ticker, call_date, source_file)
        );

        -- HARD RULE: prevent duplicates for the same call across re-runs/source_file changes
        CREATE UNIQUE INDEX IF NOT EXISTS ux_call_zscores_ticker_calldate
            ON call_zscores(ticker, call_date);

        CREATE INDEX IF NOT EXISTS ix_call_zscores_ticker_date
            ON call_zscores(ticker, call_date);

        CREATE INDEX IF NOT EXISTS ix_call_zscores_call_id
            ON call_zscores(earnings_call_id);

        -- Latest zscores per ticker (by call_date, then scored_at, then loaded_at)
        CREATE VIEW IF NOT EXISTS call_zscores_latest AS
        WITH ranked AS (
            SELECT
                *,
                ROW_NUMBER() OVER (
                    PARTITION BY ticker
                    ORDER BY
                        CASE WHEN call_date IS NULL THEN 0 ELSE 1 END DESC,
                        call_date DESC,
                        scored_at_utc DESC,
                        loaded_at_utc DESC
                ) AS rn
            FROM call_zscores
        )
        SELECT * FROM ranked WHERE rn = 1;
        """
    )
    conn.commit()


def deep_prompt_for_industry(conn: sqlite3.Connection, industry_fs: str) -> Optional[str]:
    r = conn.execute(
        "SELECT deep_prompt_version FROM industry_prompt_registry WHERE industry_fs=? AND is_active=1 LIMIT 1",
        (industry_fs,),
    ).fetchone()
    return str(r[0]) if r else None


def fetch_latest_industry_by_ticker(conn: sqlite3.Connection) -> dict[str, str]:
    rows = conn.execute(
        """
        WITH latest_calls AS (
          SELECT ticker, MAX(call_date) AS call_date
          FROM earnings_calls
          WHERE ticker IS NOT NULL AND ticker!='' AND ticker!='UNKNOWN'
            AND call_date IS NOT NULL AND call_date!=''
          GROUP BY ticker
        )
        SELECT e.ticker, e.industry_factset
        FROM earnings_calls e
        JOIN latest_calls lc ON lc.ticker=e.ticker AND lc.call_date=e.call_date
        WHERE e.industry_factset IS NOT NULL AND e.industry_factset!='';
        """
    ).fetchall()
    return {r[0]: r[1] for r in rows}


def fetch_deep_series(conn: sqlite3.Connection, ticker: str, deep_prompt: str):
    return conn.execute(
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
          AND ec.call_date IS NOT NULL AND ec.call_date!=''
        ORDER BY ec.call_date;
        """,
        (deep_prompt, FORENSICS_PROMPT, ticker),
    ).fetchall()


def mu_sd(vals):
    vals = [v for v in vals if v is not None]
    if not vals:
        return 0.0, 0.0
    return mean(vals), pstdev(vals)


def stance_from_composite(c: Optional[float]) -> Optional[str]:
    """
    Optional: simple stance mapping. Keep conservative.
    You can change thresholds later.
    """
    if c is None:
        return None
    if c >= 0.5:
        return "bullish"
    if c <= -0.5:
        return "bearish"
    return "neutral"


def main():
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    ensure_call_zscores(conn)

    industry_by_ticker = fetch_latest_industry_by_ticker(conn)
    tickers = sorted(industry_by_ticker.keys())

    utc_now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    source_file = "zscore_calls.py"
    scored_at = utc_now
    loaded_at = utc_now

    n_written = 0
    n_skipped = 0

    for ticker in tickers:
        industry_fs = industry_by_ticker.get(ticker)
        deep_prompt = deep_prompt_for_industry(conn, industry_fs) if industry_fs else None
        if not deep_prompt:
            n_skipped += 1
            continue

        rows = fetch_deep_series(conn, ticker, deep_prompt)
        rows = [r for r in rows if r["deep_json"]]
        if not rows:
            n_skipped += 1
            continue

        series = []
        for r in rows:
            deep = json.loads(r["deep_json"])
            forensic = json.loads(r["forensics_json"]) if r["forensics_json"] else {}

            # -------------------------
            # Universal indices
            # -------------------------
            ssi = (
                TREND.get(deep.get("sales_trend", "unclear"), 0) * 2
                + TREND.get(deep.get("volume_trend", "unclear"), 0) * 1
                + PVV.get(deep.get("price_vs_volume", "unclear"), 0) * 1
                + TREND.get(deep.get("demand_trend", "unclear"), 0) * 1
            )

            # Fallback: use activity_trend (energy) or revenue_trend (C&E) when sales_trend isn't present
            if deep.get("sales_trend") is None:
                if "activity_trend" in deep:
                    ssi = TREND.get(deep.get("activity_trend", "unclear"), 0) * 2
                elif "revenue_trend" in deep:
                    ssi = TREND.get(deep.get("revenue_trend", "unclear"), 0) * 2

            eri = (
                GUIDE.get(deep.get("earnings_guidance_action", "unclear"), 0)
                + TIMING.get(deep.get("earnings_timing_bias", "unclear"), 0)
                + VIS.get(deep.get("earnings_visibility", "unclear"), 0)
            )

            mout = deep.get("gross_margin_outlook") or deep.get("operating_margin_outlook") or deep.get("margin_outlook") or "unclear"
            pmi = MOUT.get(mout, 0)

            bsi = LEVEL.get(deep.get("balance_sheet_stress", "unclear"), 0) * -1
            bsi += TREND.get(deep.get("liquidity_trend", "unclear"), 0) * 1
            bsi += TREND.get(deep.get("leverage_trend", "unclear"), 0) * -1

            # Language risk index
            lri = float(forensic.get("evasion_score", 0))
            lri += 10 * LEVEL.get(forensic.get("hedging_intensity", "low"), -1)
            lri += 10 * LEVEL.get(forensic.get("deflection_intensity", "low"), -1)
            lri += 10 * LEVEL.get(forensic.get("constraint_framing", "low"), -1)
            if forensic.get("answer_substitution_detected"):
                lri += 10
            if forensic.get("specificity_asymmetry") == "negative":
                lri += 10

            # -------------------------
            # Industry-aware MPI/DQI
            # -------------------------
            mpi = None
            dqi = None

            # CONSTRUCTION & ENGINEERING MPI/DQI
            if ("book_to_burn" in deep) or ("new_awards_trend" in deep) or ("bid_pipeline_trend" in deep):
                b2b = deep.get("book_to_burn", "unclear")
                b2b_score = 1 if b2b == "above_1" else -1 if b2b == "below_1" else 0

                dqi = (
                    TREND.get(deep.get("new_awards_trend", "unclear"), 0) * 2
                    + TREND.get(deep.get("backlog_trend", "unclear"), 0) * 2
                    + TREND.get(deep.get("bid_pipeline_trend", "unclear"), 0) * 1
                    + b2b_score * 1
                    + TREND.get(deep.get("revenue_trend", "unclear"), 0) * 1
                    - LEVEL.get(deep.get("project_pushouts", "unclear"), 0) * 1
                )

                mpi = (
                    -TREND.get(deep.get("pricing_power", "unclear"), 0) * 2
                    -TREND.get(deep.get("pass_through_success", "unclear"), 0) * 2
                    + TREND.get(deep.get("cost_pressure", "unclear"), 0) * 2
                    + TREND.get(deep.get("labor_cost_trend", "unclear"), 0) * 1
                    + TREND.get(deep.get("materials_cost_trend", "unclear"), 0) * 1
                    + LEVEL.get(deep.get("competitive_pressure", "unclear"), 0) * 1
                    -TREND.get(deep.get("project_execution_quality", "unclear"), 0) * 1
                    + LEVEL.get(deep.get("tariff_or_supply_chain_impact", "unclear"), 0) * 1
                )

            # ENERGY EQUIPMENT & SERVICES MPI/DQI
            elif ("utilization_trend" in deep) or ("dayrate_trend" in deep) or ("book_to_bill" in deep):
                b2b = deep.get("book_to_bill", "unclear")
                b2b_score = 1 if b2b == "above_1" else -1 if b2b == "below_1" else 0

                dur = deep.get("backlog_duration", "unclear")
                dur_score = 1 if dur == "long_cycle" else 0

                dqi = (
                    TREND.get(deep.get("activity_trend", "unclear"), 0) * 2
                    + TREND.get(deep.get("backlog_trend", "unclear"), 0) * 1
                    + TREND.get(deep.get("bookings_trend", "unclear"), 0) * 1
                    + b2b_score * 1
                    + dur_score * 1
                    - LEVEL.get(deep.get("project_pushouts", "unclear"), 0) * 1
                )

                mpi = (
                    -TREND.get(deep.get("utilization_trend", "unclear"), 0) * 1
                    -TREND.get(deep.get("dayrate_trend", "unclear"), 0) * 2
                    -TREND.get(deep.get("pricing_power", "unclear"), 0) * 2
                    + TREND.get(deep.get("cost_pressure", "unclear"), 0) * 2
                    -TREND.get(deep.get("pass_through_success", "unclear"), 0) * 2
                    + LEVEL.get(deep.get("competitive_pressure", "unclear"), 0) * 1
                    + LEVEL.get(deep.get("tariff_or_supply_chain_impact", "unclear"), 0) * 1
                )

            # HOMEBUILDERS MPI/DQI
            elif ("buydown_intensity" in deep) or ("incentive_intensity" in deep) or ("spec_mix_level" in deep):
                mpi = (
                    -TREND.get(deep.get("gross_margin_direction", "unclear"), 0) * 2
                    -TREND.get(deep.get("pricing_power", "unclear"), 0) * 2
                    -TREND.get(deep.get("incentive_intensity", "unclear"), 0) * 2
                    -TREND.get(deep.get("buydown_intensity", "unclear"), 0) * 1
                    + LEVEL.get(deep.get("spec_mix_level", "unclear"), 0) * 1
                    + LEVEL.get(deep.get("inventory_pressure", "unclear"), 0) * 1
                    + LEVEL.get(deep.get("competitive_pressure", "unclear"), 0) * 1
                )
                dqi = (
                    TREND.get(deep.get("demand_trend", "unclear"), 0) * 2
                    + TREND.get(deep.get("traffic_leads", "unclear"), 0) * 1
                    + TREND.get(deep.get("conversion", "unclear"), 0) * 1
                    - TREND.get(deep.get("cancellations", "unclear"), 0) * 2
                    + (1 if deep.get("backlog_quality") == "strong" else -1 if deep.get("backlog_quality") == "weak" else 0)
                )

            series.append(
                {
                    "earnings_call_id": int(r["id"]),
                    "call_date": r["call_date"],
                    "fiscal_year": r["fiscal_year"],
                    "fiscal_quarter": r["fiscal_quarter"],
                    "mpi_raw": mpi,
                    "dqi_raw": dqi,
                    "ssi_raw": ssi,
                    "eri_raw": eri,
                    "pmi_raw": pmi,
                    "bsi_raw": bsi,
                    "lri_raw": lri,
                }
            )

        # Per-ticker mu/sd
        mpi_mu, mpi_sd = mu_sd([x["mpi_raw"] for x in series])
        dqi_mu, dqi_sd = mu_sd([x["dqi_raw"] for x in series])
        ssi_mu, ssi_sd = mu_sd([x["ssi_raw"] for x in series])
        eri_mu, eri_sd = mu_sd([x["eri_raw"] for x in series])
        pmi_mu, pmi_sd = mu_sd([x["pmi_raw"] for x in series])
        bsi_mu, bsi_sd = mu_sd([x["bsi_raw"] for x in series])
        lri_mu, lri_sd = mu_sd([x["lri_raw"] for x in series])

        with conn:
            for x in series:
                mpi_z = None if x["mpi_raw"] is None else round(z(x["mpi_raw"], mpi_mu, mpi_sd), 6)
                dqi_z = None if x["dqi_raw"] is None else round(z(x["dqi_raw"], dqi_mu, dqi_sd), 6)

                ssi_z = round(z(x["ssi_raw"], ssi_mu, ssi_sd), 6) if x["ssi_raw"] is not None else None
                eri_z = round(z(x["eri_raw"], eri_mu, eri_sd), 6) if x["eri_raw"] is not None else None
                pmi_z = round(z(x["pmi_raw"], pmi_mu, pmi_sd), 6) if x["pmi_raw"] is not None else None
                bsi_z = round(z(x["bsi_raw"], bsi_mu, bsi_sd), 6) if x["bsi_raw"] is not None else None
                lri_z = round(z(x["lri_raw"], lri_mu, lri_sd), 6) if x["lri_raw"] is not None else None

                # Simple composite (optional; adjust later)
                comps = [v for v in [mpi_z, dqi_z, ssi_z, eri_z, pmi_z, bsi_z] if v is not None]
                composite = round(sum(comps) / len(comps), 6) if comps else None
                stance = stance_from_composite(composite)

                conn.execute(
                    """
                    INSERT INTO call_zscores (
                      ticker, call_date, fiscal_year, fiscal_quarter, earnings_call_id,
                      mpi_raw, mpi_z, dqi_raw, dqi_z,
                      ssi_raw, ssi_z, eri_raw, eri_z,
                      pmi_raw, pmi_z, bsi_raw, bsi_z,
                      lri_raw, lri_z,
                      source_file, loaded_at_utc,
                      composite_score, call_stance, score_version, scored_at_utc
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(ticker, call_date) DO UPDATE SET
                      fiscal_year=excluded.fiscal_year,
                      fiscal_quarter=excluded.fiscal_quarter,
                      earnings_call_id=excluded.earnings_call_id,

                      mpi_raw=excluded.mpi_raw, mpi_z=excluded.mpi_z,
                      dqi_raw=excluded.dqi_raw, dqi_z=excluded.dqi_z,
                      ssi_raw=excluded.ssi_raw, ssi_z=excluded.ssi_z,
                      eri_raw=excluded.eri_raw, eri_z=excluded.eri_z,
                      pmi_raw=excluded.pmi_raw, pmi_z=excluded.pmi_z,
                      bsi_raw=excluded.bsi_raw, bsi_z=excluded.bsi_z,
                      lri_raw=excluded.lri_raw, lri_z=excluded.lri_z,

                      source_file=excluded.source_file,
                      loaded_at_utc=excluded.loaded_at_utc,
                      composite_score=excluded.composite_score,
                      call_stance=excluded.call_stance,
                      score_version=excluded.score_version,
                      scored_at_utc=excluded.scored_at_utc;
                    """,
                    (
                        ticker, x["call_date"], x["fiscal_year"], x["fiscal_quarter"], x["earnings_call_id"],
                        x["mpi_raw"], mpi_z,
                        x["dqi_raw"], dqi_z,
                        x["ssi_raw"], ssi_z,
                        x["eri_raw"], eri_z,
                        x["pmi_raw"], pmi_z,
                        x["bsi_raw"], bsi_z,
                        x["lri_raw"], lri_z,
                        source_file, loaded_at,
                        composite, stance, SCORE_VERSION, scored_at,
                    ),
                )
                n_written += 1

    conn.close()
    print("====")
    print(f"Rows written: {n_written}")
    print(f"Tickers skipped (no registry/deep): {n_skipped}")
    print("DONE")


if __name__ == "__main__":
    main()

