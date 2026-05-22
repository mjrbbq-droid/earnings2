# scripts/extract_forensics_gpt.py
from __future__ import annotations

import sys
import os
import json
import time
import random
from pathlib import Path
from datetime import datetime, timezone
from typing import Any, Optional, List, Literal

# --- ensure project root is on PYTHONPATH ---
PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))
# ------------------------------------------

from dotenv import load_dotenv
from openai import OpenAI, APITimeoutError, APIConnectionError, RateLimitError
from pydantic import BaseModel, Field
from pydantic import TypeAdapter

from src.db import connect, init_db, fetch_call_with_chunks, insert_signature

PROMPT_VERSION = "hb_forensics_v1"
Level = Literal["low", "medium", "high"]


class ForensicsQuote(BaseModel):
    quote: str = Field(..., description="Exact short quote (<=25 words) showing the pattern.")
    speaker: Optional[str] = None
    section: Optional[Literal["prepared", "qa"]] = None
    pattern: str = Field(
        ...,
        description="hedging|deflection|answer_substitution|specificity_asymmetry|constraint_framing|temporal_deferral|other",
    )


class HomebuildersForensics(BaseModel):
    ticker: str
    call_date: Optional[str] = None
    fiscal_year: Optional[int] = None
    fiscal_quarter: Optional[int] = None

    evasion_score: int = Field(..., ge=0, le=100)
    hedging_intensity: Level
    deflection_intensity: Level
    constraint_framing: Level
    specificity_asymmetry: Literal["positive", "neutral", "negative"]
    answer_substitution_detected: bool
    narrative_control_level: Level

    likely_sensitive_topics: List[str] = Field(default_factory=list)
    implicit_admissions: List[str] = Field(default_factory=list)
    what_they_are_not_saying: List[str] = Field(default_factory=list)

    evidence_quotes: List[ForensicsQuote] = Field(default_factory=list)
    analyst_summary: str = Field(..., description="5-8 sentences, forensic read of management language this quarter.")


def enforce_openai_strict_schema(schema: Any) -> Any:
    """
    OpenAI strict json_schema likes:
      - additionalProperties: false on objects
      - required: all properties (explicit)
    """
    if isinstance(schema, dict):
        if schema.get("type") == "object":
            schema.setdefault("additionalProperties", False)
            props = schema.get("properties")
            if isinstance(props, dict):
                schema["required"] = list(props.keys())
                for v in props.values():
                    enforce_openai_strict_schema(v)

        for key in ("$defs", "definitions"):
            if key in schema and isinstance(schema[key], dict):
                for v in schema[key].values():
                    enforce_openai_strict_schema(v)

        for key in ("anyOf", "oneOf", "allOf"):
            if key in schema and isinstance(schema[key], list):
                for v in schema[key]:
                    enforce_openai_strict_schema(v)

        if "items" in schema:
            enforce_openai_strict_schema(schema["items"])

        ap = schema.get("additionalProperties")
        if isinstance(ap, dict):
            enforce_openai_strict_schema(ap)

    elif isinstance(schema, list):
        for item in schema:
            enforce_openai_strict_schema(item)

    return schema


def build_input(call: dict) -> str:
    c = call["call"]
    chunks = call["chunks"]

    header = {
        "ticker": c.get("ticker"),
        "call_date": c.get("call_date"),
        "fiscal_year": c.get("fiscal_year"),
        "fiscal_quarter": c.get("fiscal_quarter"),
    }

    lines = []
    for ch in chunks:
        speaker = ch.get("speaker") or "UNKNOWN"
        section = ch.get("section") or "unknown"
        text = ch.get("text") or ""
        lines.append(f"[{section.upper()}] {speaker}: {text}")

    return "CALL_METADATA:\n" + json.dumps(header) + "\n\nTRANSCRIPT:\n" + "\n".join(lines)


def call_with_retries(fn, *, call_id: int, max_attempts: int = 6, base_sleep: float = 5.0):
    for attempt in range(1, max_attempts + 1):
        try:
            return fn()
        except (APITimeoutError, APIConnectionError, RateLimitError) as e:
            if attempt == max_attempts:
                raise
            sleep_s = base_sleep * (2 ** (attempt - 1)) + random.uniform(0, 2.0)
            print(
                f"Transient API error on call_id={call_id} attempt {attempt}/{max_attempts}: "
                f"{type(e).__name__}. Retrying in {sleep_s:.1f}s..."
            )
            time.sleep(sleep_s)


def latest_call_ids_missing_forensics(conn, model: str) -> list[int]:
    """
    Returns the latest earnings_calls.id per ticker that does NOT yet have hb_forensics_v1 for this model.
    Latest is determined by call_date then id.
    Ignores NULL/blank call_date to avoid "latest" ambiguity.
    """
    rows = conn.execute(
        """
        WITH latest AS (
          SELECT ec.ticker, MAX(ec.call_date) AS max_date
          FROM earnings_calls ec
          WHERE ec.ticker IS NOT NULL AND ec.ticker != '' AND ec.ticker != 'UNKNOWN'
            AND ec.call_date IS NOT NULL AND ec.call_date != ''
          GROUP BY ec.ticker
        ),
        latest_ids AS (
          SELECT ec.id
          FROM earnings_calls ec
          JOIN latest l
            ON l.ticker = ec.ticker AND l.max_date = ec.call_date
        )
        SELECT li.id
        FROM latest_ids li
        WHERE NOT EXISTS (
          SELECT 1
          FROM earnings_signatures es
          WHERE es.earnings_call_id = li.id
            AND es.prompt_version = ?
            AND es.model = ?
        )
        ORDER BY li.id;
        """,
        (PROMPT_VERSION, model),
    ).fetchall()
    return [int(r[0]) for r in rows]


def ensure_unique_index(conn):
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS ux_signatures_call_model_prompt "
        "ON earnings_signatures(earnings_call_id, model, prompt_version);"
    )
    conn.commit()


def log_failure(msg: str) -> None:
    try:
        log_dir = PROJECT_ROOT / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        path = log_dir / "forensics_failures_latest.log"
        with open(path, "a", encoding="utf-8") as f:
            f.write(msg.rstrip() + "\n")
    except Exception:
        pass


def main() -> None:
    load_dotenv()

    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key:
        raise SystemExit("Missing OPENAI_API_KEY in your .env")

    model = os.getenv("OPENAI_MODEL", "gpt-5.2")
    db_path = os.getenv("EARNINGS_DB_PATH", "./data/earnings.db")

    client = OpenAI(api_key=api_key, timeout=180.0)

    conn = connect(db_path)
    init_db(conn)
    ensure_unique_index(conn)

    call_ids = latest_call_ids_missing_forensics(conn, model=model)
    if not call_ids:
        print("No latest calls pending forensics extraction. Nothing to do.")
        return

    utc_now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    schema = TypeAdapter(HomebuildersForensics).json_schema()
    schema = enforce_openai_strict_schema(schema)

    system = (
        "You are a linguistic forensics analyst (intelligence + legal testimony). "
        "Analyze HOW management uses language: hedging, deflection, constraint framing, specificity asymmetry, "
        "answer substitution, and narrative control. "
        "Do not accuse of lying. Provide cautious, evidence-based observations and quotes. "
        "Keep quotes short (<=25 words) and only include quotes that clearly demonstrate the named pattern."
    )

    print(f"Pending LATEST-call forensics: {len(call_ids)}")
    ok = 0
    fail = 0

    for call_id in call_ids:
        call = fetch_call_with_chunks(conn, call_id)
        payload = build_input(call)

        print(f"\nStarting forensics (latest) call_id={call_id} ...")

        def _do_request():
            return client.responses.create(
                model=model,
                input=[
                    {"role": "system", "content": system},
                    {"role": "user", "content": payload},
                ],
                text={
                    "format": {
                        "type": "json_schema",
                        "name": "homebuilders_forensics",
                        "schema": schema,
                        "strict": True,
                    }
                },
            )

        try:
            resp = call_with_retries(_do_request, call_id=call_id)
            obj = json.loads(resp.output_text)
            sig_json = json.dumps(obj, ensure_ascii=False)

            sig_id = insert_signature(
                conn,
                earnings_call_id=call_id,
                model=model,
                prompt_version=PROMPT_VERSION,
                signature_json=sig_json,
                created_at_utc=utc_now,
            )

            conn.commit()
            ok += 1
            print(f"OK: call_id={call_id} forensics_id={sig_id}")

        except Exception as e:
            conn.commit()
            fail += 1
            msg = f"{utc_now} FAIL call_id={call_id} {type(e).__name__}: {e}"
            print(msg)
            log_failure(msg)
            continue

    print("\n====")
    print(f"Forensics OK: {ok}")
    print(f"Forensics FAIL: {fail}")
    print(f"DB: {db_path}")
    print(f"Model: {model} | Prompt: {PROMPT_VERSION}")


if __name__ == "__main__":
    main()



###            .\.venv\Scripts\python.exe -m scripts.extract_forensics_gpt