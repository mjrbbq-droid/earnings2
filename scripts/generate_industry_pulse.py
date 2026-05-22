# scripts/generate_industry_pulse.py
from __future__ import annotations

import sys
import os
import json
import sqlite3
import time
import random
from pathlib import Path
from datetime import datetime, timezone
from typing import Optional, Any, Tuple, List

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from dotenv import load_dotenv
from openai import OpenAI, APITimeoutError, APIConnectionError, RateLimitError

DB = "./data/databases/earnings.db"

FORENSICS_PROMPT = "hb_forensics_v1"

SIZE_PRESETS = {
    "all":   (None, None),
    "small": (250_000_000, 8_000_000_000),
    "smid":  (1_000_000_000, 50_000_000_000),
    "large": (10_000_000_000, None),
}

TOP_N = 30

MAX_ATTEMPTS = 6
BASE_SLEEP = 5.0

# If your DB has normalized event_type, keep this strict.
# If not, expand the set or normalize upstream.
EARNINGS_EVENT_TYPES = {
    "earnings_call",
    "earnings",
    "quarterly",
    "q",
}


def slug(s: str) -> str:
    return "".join(ch.lower() if ch.isalnum() else "_" for ch in s).strip("_")


def norm_label(s: str) -> str:
    return "".join(s.strip().upper().split())


def call_with_retries(fn) -> Any:
    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            return fn()
        except (APITimeoutError, APIConnectionError, RateLimitError) as e:
            if attempt == MAX_ATTEMPTS:
                raise
            sleep_s = BASE_SLEEP * (2 ** (attempt - 1)) + random.uniform(0, 2.0)
            print(
                f"Transient API error attempt {attempt}/{MAX_ATTEMPTS}: "
                f"{type(e).__name__}. Retrying in {sleep_s:.1f}s..."
            )
            time.sleep(sleep_s)


def fetch_tickers_for_industry(
    conn: sqlite3.Connection,
    industry_factset: str,
    size: str,
) -> List[Tuple[str, Optional[float], str, str]]:
    """
    Membership comes from earnings_calls FactSet-enriched columns:
      - sector_factset
      - industry_factset
      - mkt_value_factset

    IMPORTANT: membership is based on each ticker's latest EARNINGS call, not latest transcript.
    Returns: [(ticker, mkt_value_factset, sector_factset, industry_factset), ...]
    """
    mv_min, mv_max = SIZE_PRESETS[size]
    ind_norm = norm_label(industry_factset)

    # Pull latest EARNINGS call per ticker by id (avoids same-date duplicates).
    # If your event_type is already normalized, you can replace norm_event_type calls
    # with a simple lower(coalesce(event_type,'earnings_call')) IN (...)
    sql = """
    WITH eligible AS (
      SELECT
        id,
        ticker,
        call_date,
        mkt_value_factset,
        sector_factset,
        industry_factset,
        event_type,
        event_type_clean
      FROM earnings_calls
      WHERE ticker IS NOT NULL AND ticker != '' AND ticker != 'UNKNOWN'
        AND call_date IS NOT NULL AND call_date != ''
        AND industry_factset IS NOT NULL AND industry_factset != ''
    ),
    eligible_earnings AS (
      SELECT *
      FROM eligible
      WHERE COALESCE(event_type_clean, event_type, 'earnings_call') = 'earnings_call'
    ),
    latest_earnings AS (
      SELECT
        ticker,
        MAX(id) AS max_id
      FROM eligible_earnings
      GROUP BY ticker
    )
    SELECT
      e.ticker,
      e.mkt_value_factset,
      e.sector_factset,
      e.industry_factset,
      e.event_type
    FROM eligible_earnings e
    JOIN latest_earnings le
      ON le.ticker = e.ticker AND le.max_id = e.id
    WHERE upper(replace(e.industry_factset,' ','')) = ?
      AND (? IS NULL OR e.mkt_value_factset >= ?)
      AND (? IS NULL OR e.mkt_value_factset <= ?)
    ORDER BY e.mkt_value_factset DESC;
    """

    rows = conn.execute(sql, (ind_norm, mv_min, mv_min, mv_max, mv_max)).fetchall()

    out: List[Tuple[str, Optional[float], str, str]] = []
    for r in rows:
        ticker = str(r[0])
        mv = float(r[1]) if r[1] is not None else None
        sec = str(r[2]) if r[2] else ""
        ind = str(r[3]) if r[3] else ""
        out.append((ticker, mv, sec, ind))

    return out


def fetch_latest_earnings_call_id(
    conn: sqlite3.Connection,
    ticker: str
) -> Optional[int]:
    """
    Return the id of the latest earnings call for a ticker.
    Relies on event_type_clean = 'earnings_call' set in SQL.
    """

    r = conn.execute(
        """
        SELECT id
        FROM earnings_calls
        WHERE ticker = ?
          AND call_date IS NOT NULL
          AND call_date != ''
          AND event_type_clean = 'earnings_call'
        ORDER BY call_date DESC, id DESC
        LIMIT 1;
        """,
        (ticker,),
    ).fetchone()

    return int(r[0]) if r else None


def fetch_signature(conn: sqlite3.Connection, call_id: int, prompt_version: str) -> Optional[dict]:
    r = conn.execute(
        """
        SELECT signature_json
        FROM earnings_signatures
        WHERE earnings_call_id = ?
          AND prompt_version = ?
        ORDER BY id DESC
        LIMIT 1;
        """,
        (call_id, prompt_version),
    ).fetchone()
    if not r or not r[0]:
        return None
    try:
        return json.loads(r[0])
    except Exception:
        return None


def get_deep_prompt_version_for_industry(conn: sqlite3.Connection, industry_fs: str) -> Optional[str]:
    r = conn.execute(
        """
        SELECT deep_prompt_version
        FROM industry_prompt_registry
        WHERE industry_fs = ?
          AND is_active = 1
        LIMIT 1;
        """,
        (industry_fs,),
    ).fetchone()
    return str(r[0]) if r else None


def load_zscores_for_call(conn: sqlite3.Connection, ticker: str, call_date: str) -> Optional[dict]:
    r = conn.execute(
        """
        SELECT
          mpi_z, dqi_z, ssi_z, eri_z, lri_z,
          composite_score, call_stance,
          score_version, scored_at_utc
        FROM call_zscores
        WHERE ticker = ? AND call_date = ?
        ORDER BY scored_at_utc DESC, loaded_at_utc DESC
        LIMIT 1;
        """,
        (ticker, call_date),
    ).fetchone()

    if not r:
        return None

    return {
        "mpi_z": r[0],
        "dqi_z": r[1],
        "ssi_z": r[2],
        "eri_z": r[3],
        "lri_z": r[4],
        "composite_score": r[5],
        "call_stance": r[6],
        "score_version": r[7],
        "scored_at_utc": r[8],
    }


def build_company_payload(
    conn: sqlite3.Connection,
    ticker: str,
    mkt_value: Optional[float],
    sector_fs: str,
    industry_fs: str,
) -> Optional[dict]:
    call_id = fetch_latest_earnings_call_id(conn, ticker)
    if not call_id:
        return None

    call = conn.execute(
        """
        SELECT id, call_date, fiscal_year, fiscal_quarter,
               sector_factset, industry_factset, mkt_value_factset, asof_date_factset,
               event_type
        FROM earnings_calls
        WHERE id = ?;
        """,
        (call_id,),
    ).fetchone()
    if not call:
        return None

    call_date = call[1]
    if not call_date:
        return None

    # Verify it is an earnings call
    et = (call[8] or "earnings_call").strip().lower()
    if et not in ("earnings_call", "earnings", "quarterly", "q"):
        return None

    deep_prompt = get_deep_prompt_version_for_industry(conn, industry_fs) if industry_fs else None
    if not deep_prompt:
        return None

    deep = fetch_signature(conn, call_id, deep_prompt)
    if not deep:
        return None

    forensic = fetch_signature(conn, call_id, FORENSICS_PROMPT)
    zrow = load_zscores_for_call(conn, ticker, call_date)

    keep = [
        "demand_trend", "traffic_leads", "conversion", "cancellations",
        "orders_trend", "backlog_direction", "backlog_quality",
        "pricing_power", "incentive_intensity", "buydown_intensity", "discounting_mode",
        "gross_margin_direction", "gross_margin_outlook", "operating_margin_outlook", "margin_driver_primary",
        "spec_mix_level", "spec_mix_direction",
        "competitive_pressure", "inventory_pressure",
        "build_cycle_times", "labor_costs", "materials_costs", "cost_actions",
        "affordability", "mortgage_rate_sensitivity",
        "sales_trend", "volume_trend", "price_vs_volume",
        "earnings_guidance_action", "earnings_timing_bias", "earnings_visibility", "guidance_confidence",
        "balance_sheet_stress", "leverage_trend", "liquidity_trend", "capital_return_posture",
        "tailwinds", "headwinds", "key_changes",
    ]
    filtered = {k: deep.get(k) for k in keep if k in deep}

    return {
        "ticker": ticker,
        "mkt_value_usd": mkt_value,
        "sector_factset": sector_fs,
        "industry_factset": industry_fs,
        "factset_asof_date": call[7],
        "latest_call": {
            "call_id": int(call[0]),
            "call_date": call_date,
            "fiscal_year": call[2],
            "fiscal_quarter": call[3],
        },
        "deep_prompt_version_used": deep_prompt,
        "deep_signature": filtered,
        "forensics": forensic,
        "zscores": zrow,
    }


def main() -> None:
    load_dotenv()
    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key:
        raise SystemExit("Missing OPENAI_API_KEY in .env")

    model = os.getenv("OPENAI_MODEL", "gpt-5.2")
    db_path = os.getenv("EARNINGS_DB_PATH", DB)

    if len(sys.argv) < 2:
        raise SystemExit(
            'Usage: python scripts/generate_industry_pulse.py "INDUSTRY_FACTSET" '
            "[--size all|small|smid|large]"
        )

    industry = sys.argv[1].strip()
    size = "all"
    if "--size" in sys.argv:
        i = sys.argv.index("--size")
        size = sys.argv[i + 1].strip().lower()
    if size not in SIZE_PRESETS:
        raise SystemExit(f"Unknown size '{size}'. Choose from: {list(SIZE_PRESETS.keys())}")

    conn = sqlite3.connect(db_path)

    tickers = fetch_tickers_for_industry(conn, industry, size)
    if not tickers:
        raise SystemExit(
            f"No earnings-call tickers found in earnings_calls with industry_factset='{industry}' size='{size}'"
        )

    companies: List[dict] = []
    for t, mv, sec, ind in tickers:
        payload = build_company_payload(conn, t, mv, sec, ind)
        if payload:
            companies.append(payload)

    if not companies:
        raise SystemExit(
            "No companies had deep signatures available for latest earnings calls "
            "(run deep extraction + scoring first, and ensure industry_prompt_registry is active)."
        )

    companies = companies[:TOP_N]

    utc_now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    system = (
        "You are a senior industry analyst. Write an INDUSTRY PULSE using only the latest EARNINGS call per company. "
        "Inputs include: deep signatures (industry-routed), z-scores when present (from SQLite), and forensics when present. "
        "Do not invent facts. Describe the industry's CURRENT regime and dispersion. "
        "Include: (1) regime framing, (2) bullish themes, (3) bearish themes, "
        "(4) winners/laggards based on z-scores + deep_signature evidence, "
        "(5) what to watch next quarter. "
        "Do not reference z-scores explicitly in the narrative; translate them into plain English."
    )

    user = {
        "industry_factset": industry,
        "size_filter": {"name": size, "mv_min": SIZE_PRESETS[size][0], "mv_max": SIZE_PRESETS[size][1]},
        "asof_utc": utc_now,
        "companies": companies,
    }

    client = OpenAI(api_key=api_key, timeout=180.0)

    def _do_request():
        return client.responses.create(
            model=model,
            input=[
                {"role": "system", "content": system},
                {"role": "user", "content": json.dumps(user, ensure_ascii=False)},
            ],
        )

    resp = call_with_retries(_do_request)
    out_text = resp.output_text.strip() + "\n"

    out_dir = PROJECT_ROOT / "pulses" / "industry"
    out_dir.mkdir(parents=True, exist_ok=True)

    out_path = out_dir / f"{slug(industry)}_{size}_pulse.md"
    out_path.write_text(out_text, encoding="utf-8")

    print("Wrote:", out_path)
    print(f"Included companies: {len(companies)} (top {TOP_N} by market cap within filter)")
    conn.close()


if __name__ == "__main__":
    main()







##        python scripts/generate_industry_pulse.py "Homebuilders" --size all
##        python scripts/generate_industry_pulse.py "Homebuilders" --size small
##        python scripts/generate_industry_pulse.py "Homebuilders" --size smid
##        python scripts/generate_industry_pulse.py "Homebuilders" --size large
