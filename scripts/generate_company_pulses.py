# scripts/generate_company_pulses.py
from __future__ import annotations

import argparse
import sys
import os
import json
import sqlite3
import time
import random
from pathlib import Path
from datetime import datetime, timezone
from typing import Optional, Any

# --- ensure project root is on PYTHONPATH ---
PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))
# ------------------------------------------

from dotenv import load_dotenv
from openai import OpenAI, APITimeoutError, APIConnectionError, RateLimitError

DB = "./data/earnings.db"
OUT_DIR = PROJECT_ROOT / "pulses" / "company"
OUT_DIR.mkdir(parents=True, exist_ok=True)

FORENSICS_PROMPT = "hb_forensics_v1"  # forensics is universal

MAX_ATTEMPTS = 6
BASE_SLEEP = 5.0


SYSTEM_PROMPT_TEMPLATE = """You are a senior buy-side analyst writing a COMPANY PULSE for portfolio decision-making.

Context:
- Industry: {industry_fs}
- Industry mechanics profile (how to interpret demand/pricing/margins/backlog for this industry):
{industry_mechanics}

Rules:
- Do NOT restate the same fact in multiple sections.
- Use quotes sparingly: at most 2 short anchor quotes TOTAL, only where language itself matters.
- Prefer synthesis over narration.
- Assume the reader knows basic earnings-call context.
- Focus on WHAT CHANGED, WHAT MATTERS, and WHAT BREAKS the thesis.
- Use the provided scorecard and call stance; do not invent scores.

Structure exactly as follows:

TOP (mandatory)
- Call Stance: Bullish / Cautiously Bullish / Neutral / Cautiously Bearish / Bearish
- Composite Score (latest): number
- Scoreboard (latest call): MPI_z, DQI_z, SSI_z, ERI_z, LRI_z

1) Executive Take (include guidance/revision read-through here)
2) What Changed This Quarter (Delta Only)
3) Demand & Pricing Reality
4) Margin Mechanics
5) Bull vs Bear + What Confirms or Breaks the Thesis
"""


def call_with_retries(fn, *, ticker: str) -> Any:
    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            return fn()
        except (APITimeoutError, APIConnectionError, RateLimitError) as e:
            if attempt == MAX_ATTEMPTS:
                raise
            sleep_s = BASE_SLEEP * (2 ** (attempt - 1)) + random.uniform(0, 2.0)
            print(
                f"Transient API error for {ticker} attempt {attempt}/{MAX_ATTEMPTS}: "
                f"{type(e).__name__}. Retrying in {sleep_s:.1f}s..."
            )
            time.sleep(sleep_s)


def parse_args(argv: list[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Generate company pulse markdown.")
    p.add_argument("tickers", nargs="?", default=None,
                   help="Comma-separated tickers (e.g. KBH,LEN). Omit for incremental mode.")
    p.add_argument("--force", action="store_true",
                   help="Regenerate all tickers, ignoring pulse_log.")
    return p.parse_args(argv[1:])


def ensure_pulse_log_table(conn: sqlite3.Connection) -> None:
    conn.execute("""
        CREATE TABLE IF NOT EXISTS pulse_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ticker TEXT NOT NULL,
            call_id INTEGER NOT NULL,
            call_date TEXT,
            pulse_type TEXT NOT NULL DEFAULT 'company',
            generated_at TEXT NOT NULL,
            UNIQUE(ticker, call_id, pulse_type)
        );
    """)
    conn.commit()


def record_pulse(conn: sqlite3.Connection, ticker: str, call_id: int,
                 call_date: Optional[str], utc_now: str,
                 pulse_type: str = "company") -> None:
    conn.execute(
        """INSERT OR REPLACE INTO pulse_log (ticker, call_id, call_date, pulse_type, generated_at)
           VALUES (?, ?, ?, ?, ?);""",
        (ticker, call_id, call_date, pulse_type, utc_now),
    )
    conn.commit()


def fetch_tickers_needing_pulse(conn: sqlite3.Connection) -> list[str]:
    """Return tickers whose latest call has no company pulse_log entry."""
    rows = conn.execute("""
        SELECT ec.ticker
        FROM earnings_calls ec
        INNER JOIN (
            SELECT ticker, MAX(call_date) AS max_date
            FROM earnings_calls
            WHERE ticker IS NOT NULL AND ticker != '' AND ticker != 'UNKNOWN'
              AND call_date IS NOT NULL AND call_date != ''
            GROUP BY ticker
        ) latest ON ec.ticker = latest.ticker AND ec.call_date = latest.max_date
        LEFT JOIN pulse_log pl
            ON pl.ticker = ec.ticker AND pl.call_id = ec.id AND pl.pulse_type = 'company'
        WHERE pl.id IS NULL
        ORDER BY ec.ticker;
    """).fetchall()
    return [r[0] for r in rows]


def fetch_all_tickers(conn: sqlite3.Connection) -> list[str]:
    rows = conn.execute(
        """
        SELECT DISTINCT ticker
        FROM earnings_calls
        WHERE ticker IS NOT NULL AND ticker != '' AND ticker != 'UNKNOWN'
        ORDER BY ticker;
        """
    ).fetchall()
    return [str(r[0]) for r in rows]


def fetch_latest_call_id(conn: sqlite3.Connection, ticker: str) -> Optional[int]:
    """
    Latest call by call_date (ignoring NULL/blank call_date).
    """
    r = conn.execute(
        """
        SELECT id
        FROM earnings_calls
        WHERE ticker = ?
          AND call_date IS NOT NULL
          AND call_date != ''
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
    if not r:
        return None
    try:
        return json.loads(r[0])
    except Exception:
        return None


def get_factset_meta_for_call(conn: sqlite3.Connection, call_id: int) -> dict:
    """
    IMPORTANT: earnings_calls uses *_factset columns.
    """
    r = conn.execute(
        """
        SELECT ticker, call_date, fiscal_year, fiscal_quarter,
               sector_factset, industry_factset, mkt_value_factset, asof_date_factset
        FROM earnings_calls
        WHERE id = ?;
        """,
        (call_id,),
    ).fetchone()

    return {
        "ticker": r[0],
        "call_date": r[1],
        "fiscal_year": r[2],
        "fiscal_quarter": r[3],
        "sector_factset": r[4],
        "industry_factset": r[5],
        "mkt_value_factset": r[6],
        "asof_date_factset": r[7],
    }


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


def get_industry_mechanics(conn: sqlite3.Connection, industry_fs: str) -> str:
    r = conn.execute(
        """
        SELECT mechanics_json
        FROM industry_profiles
        WHERE industry_fs = ?
        LIMIT 1;
        """,
        (industry_fs,),
    ).fetchone()

    if r and r[0]:
        try:
            obj = json.loads(r[0])
            return json.dumps(obj, indent=2)
        except Exception:
            return str(r[0])

    # fallback: universal mechanics
    return json.dumps(
        {
            "primary_levers": ["demand", "pricing power", "margins", "cost actions", "earnings guidance", "balance sheet"],
            "interpretation_rules": [
                "Treat 'incentives' as industry-specific concessions (discounts, terms, pass-through, etc).",
                "Treat 'backlog' as any visibility proxy (orders, bookings, contracted work, pipeline).",
                "Focus on what changed vs last quarter and what drives margins now.",
            ],
        },
        indent=2,
    )


def normalize_stance(x: Optional[str]) -> str:
    """
    Normalize stance to a title-case label.
    Keeps 'Cautiously Bullish'/'Cautiously Bearish' intact if present.
    """
    if not x:
        return "Neutral"
    s = str(x).strip()
    if not s:
        return "Neutral"
    s_low = s.lower()
    if "caut" in s_low and "bull" in s_low:
        return "Cautiously Bullish"
    if "caut" in s_low and "bear" in s_low:
        return "Cautiously Bearish"
    if "bull" in s_low:
        return "Bullish"
    if "bear" in s_low:
        return "Bearish"
    return "Neutral"


def load_zscores_for_call(conn: sqlite3.Connection, ticker: str, call_date: str) -> Optional[dict]:
    """
    Pull scores for the specific call_date to keep pulse internally consistent with the latest call.
    """
    r = conn.execute(
        """
        SELECT mpi_z, dqi_z, ssi_z, eri_z, lri_z, composite_score, call_stance
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
    }


def build_company_payload(conn: sqlite3.Connection, ticker: str) -> Optional[dict]:
    call_id = fetch_latest_call_id(conn, ticker)
    if not call_id:
        return None

    meta = get_factset_meta_for_call(conn, call_id)
    industry_fs = meta.get("industry_factset") or "UNKNOWN"

    deep_prompt = get_deep_prompt_version_for_industry(conn, industry_fs)
    if not deep_prompt:
        return None

    deep = fetch_signature(conn, call_id, deep_prompt)
    if not deep:
        return None

    forensic = fetch_signature(conn, call_id, FORENSICS_PROMPT)

    call_date = meta.get("call_date")
    zrow = load_zscores_for_call(conn, ticker, call_date) if call_date else None

    scoreboard = {
        "mpi_z": zrow.get("mpi_z") if zrow else None,
        "dqi_z": zrow.get("dqi_z") if zrow else None,
        "ssi_z": zrow.get("ssi_z") if zrow else None,
        "eri_z": zrow.get("eri_z") if zrow else None,
        "lri_z": zrow.get("lri_z") if zrow else None,
        "composite_score": zrow.get("composite_score") if zrow else None,
        "call_stance": normalize_stance(zrow.get("call_stance") if zrow else None),
    }

    industry_mechanics = get_industry_mechanics(conn, industry_fs)

    return {
        "ticker": ticker,
        "industry_fs": industry_fs,
        # keep payload keys stable; values sourced from *_factset columns
        "sector_fs": meta.get("sector_factset"),
        "mkt_value_fs": meta.get("mkt_value_factset"),
        "factset_asof_date": meta.get("asof_date_factset"),
        "latest_call": {
            "call_id": call_id,
            "call_date": meta.get("call_date"),
            "fiscal_year": meta.get("fiscal_year"),
            "fiscal_quarter": meta.get("fiscal_quarter"),
        },
        "scoreboard": scoreboard,
        "deep_prompt_version_used": deep_prompt,
        "deep_signature": deep,
        "forensics": forensic,
        "industry_mechanics": json.loads(industry_mechanics) if industry_mechanics.strip().startswith("{") else industry_mechanics,
    }


def main() -> None:
    load_dotenv(override=True)
    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key:
        raise SystemExit("Missing OPENAI_API_KEY in .env")

    model = os.getenv("OPENAI_MODEL", "gpt-5.2")
    db_path = os.getenv("EARNINGS_DB_PATH", DB)

    client = OpenAI(api_key=api_key, timeout=180.0)

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row

    cli = parse_args(sys.argv)
    ensure_pulse_log_table(conn)

    # Determine ticker list
    explicit_tickers = None
    if cli.tickers:
        explicit_tickers = [t.strip().upper() for t in cli.tickers.split(",") if t.strip()]

    if explicit_tickers:
        # Explicit tickers always run (no incremental check)
        tickers = explicit_tickers
    elif cli.force:
        tickers = fetch_all_tickers(conn)
    else:
        # Incremental: only tickers whose latest call lacks a pulse_log entry
        tickers = fetch_tickers_needing_pulse(conn)

    if not tickers:
        print("No tickers to process. Nothing to do.")
        return

    utc_now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    mode = "explicit" if explicit_tickers else ("force" if cli.force else "incremental")
    print(f"Generating company pulses for {len(tickers)} ticker(s) [{mode}]. generated_utc={utc_now}")

    for ticker in tickers:
        payload = build_company_payload(conn, ticker)
        if not payload:
            print(f"SKIP: {ticker} (missing deep signature or registry entry)")
            continue

        industry_fs = payload.get("industry_fs") or "UNKNOWN"
        mechanics = get_industry_mechanics(conn, industry_fs)

        system_prompt = SYSTEM_PROMPT_TEMPLATE.format(
            industry_fs=industry_fs,
            industry_mechanics=mechanics,
        )

        def _do_request():
            return client.responses.create(
                model=model,
                input=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
                ],
            )

        try:
            resp = call_with_retries(_do_request, ticker=ticker)
            text = resp.output_text.strip()
            header = f"<!-- generated_utc: {utc_now} -->\n\n"

            call_date = payload["latest_call"]["call_date"] or "unknown_date"
            call_id = payload["latest_call"]["call_id"]
            out_path = OUT_DIR / f"{ticker}_{call_date}_pulse.md"
            out_path.write_text(header + text + "\n", encoding="utf-8")

            record_pulse(conn, ticker, call_id, call_date, utc_now)
            print(f"OK: {ticker} -> {out_path}")

        except Exception as e:
            print(f"FAIL: {ticker} {type(e).__name__}: {e}")
            continue

    conn.close()
    print("DONE")


if __name__ == "__main__":
    main()


