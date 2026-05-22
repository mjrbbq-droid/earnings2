# scripts/extract_deep_signatures_router.py
from __future__ import annotations

import sys
import json
import os
import time
import random
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from dotenv import load_dotenv
from openai import OpenAI, APITimeoutError, APIConnectionError, RateLimitError
from pydantic import TypeAdapter

from src.db import (
    connect,
    init_db,
    fetch_call_with_chunks,
    insert_signature,
)

# ---- Import all deep schemas here ----
from src.signatures_schema_deep_v2 import HomebuildersDeepSignatureV2
from src.signatures_schema_energy_services_v1 import EnergyServicesDeepSignatureV1
from src.signatures_schema_construction_engineering_v1 import ConstructionEngineeringDeepSignatureV1

DB = "./data/earnings.db"

MAX_ATTEMPTS = 6
BASE_SLEEP = 5.0


# ---------------------------
# Strict schema enforcement
# ---------------------------
def enforce_openai_strict_schema(schema: Any) -> Any:
    """
    OpenAI strict json_schema requires:
      - additionalProperties=false on objects
      - required includes all properties on objects
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

    return schema


# ---------------------------
# Schema resolver
# ---------------------------
def resolve_schema(schema_name: str):
    name = (schema_name or "").strip()

    if name == "HomebuildersDeepSignatureV2":
        return HomebuildersDeepSignatureV2

    if name == "EnergyServicesDeepSignatureV1":
        return EnergyServicesDeepSignatureV1

    if name == "ConstructionEngineeringDeepSignatureV1":
        return ConstructionEngineeringDeepSignatureV1

    raise RuntimeError(f"Unknown deep_schema_name: {schema_name}")


# ---------------------------
# Build transcript payload
# ---------------------------
def build_input(call: dict) -> str:
    c = call["call"]
    chunks = call["chunks"]

    header = {
        "ticker": c.get("ticker"),
        "call_date": c.get("call_date"),
        "fiscal_year": c.get("fiscal_year"),
        "fiscal_quarter": c.get("fiscal_quarter"),
    }

    prepared: list[str] = []
    qa: list[str] = []

    for ch in chunks:
        speaker = ch.get("speaker") or "UNKNOWN"
        text = ch.get("text") or ""
        line = f"{speaker}: {text}"
        if ch.get("section") == "prepared":
            prepared.append(line)
        else:
            qa.append(line)

    return (
        "CALL_METADATA:\n"
        + json.dumps(header)
        + "\n\nPREPARED_REMARKS:\n"
        + "\n".join(prepared)
        + "\n\nQ_AND_A:\n"
        + "\n".join(qa)
    )


def call_with_retries(fn, *, call_id: int) -> Any:
    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            return fn()
        except (APITimeoutError, APIConnectionError, RateLimitError) as e:
            if attempt == MAX_ATTEMPTS:
                raise
            sleep_s = BASE_SLEEP * (2 ** (attempt - 1)) + random.uniform(0, 2.0)
            print(
                f"Transient API error call_id={call_id} attempt {attempt}/{MAX_ATTEMPTS}: "
                f"{type(e).__name__}. Retrying in {sleep_s:.1f}s..."
            )
            time.sleep(sleep_s)


# ---------------------------
# Fetch industry prompt config (per-call)
# ---------------------------
def fetch_industry_prompt_config(conn, call_id: int) -> Optional[dict]:
    """
    Returns the ACTIVE deep config for the call's industry_factset.
    """
    row = conn.execute(
        """
        SELECT ec.industry_factset,
               ip.deep_prompt_version,
               ip.deep_system_prompt,
               ip.deep_schema_name
        FROM earnings_calls ec
        JOIN industry_prompt_registry ip
          ON ip.industry_fs = ec.industry_factset
         AND ip.is_active = 1
        WHERE ec.id = ?
          AND ec.industry_factset IS NOT NULL
          AND ec.industry_factset != ''
        """,
        (call_id,),
    ).fetchone()

    if not row:
        return None

    return {
        "industry": row[0],
        "prompt_version": row[1],
        "system_prompt": row[2],
        "schema_name": row[3],
    }


def has_signature(conn, call_id: int, prompt_version: str, model: str) -> bool:
    r = conn.execute(
        """
        SELECT 1
        FROM earnings_signatures
        WHERE earnings_call_id = ?
          AND prompt_version = ?
          AND model = ?
        LIMIT 1;
        """,
        (call_id, prompt_version, model),
    ).fetchone()
    return r is not None


# ---------------------------
# Determine pending calls correctly (per industry)
# ---------------------------
def fetch_pending_call_ids(conn, *, model: str, industry_filter: Optional[str], limit: Optional[int]) -> list[int]:
    """
    Correct logic:
      For each call with an industry_factset, look up its industry's active deep_prompt_version.
      Only include the call if that exact prompt_version has NOT been written for this call+model.

    IMPORTANT: Ignores calls with NULL/blank call_date to avoid "floating" events.
    """
    params: list[Any] = [model]

    sql = """
    SELECT ec.id
    FROM earnings_calls ec
    JOIN industry_prompt_registry ip
      ON ip.industry_fs = ec.industry_factset
     AND ip.is_active = 1
    LEFT JOIN earnings_signatures es
      ON es.earnings_call_id = ec.id
     AND es.prompt_version = ip.deep_prompt_version
     AND es.model = ?
    WHERE es.id IS NULL
      AND ec.call_date IS NOT NULL
      AND ec.call_date != ''
      AND ec.ticker IS NOT NULL
      AND ec.ticker != ''
      AND ec.ticker != 'UNKNOWN'
    """
    if industry_filter:
        sql += " AND ec.industry_factset = ? "
        params.append(industry_filter)

    sql += " ORDER BY ec.id "

    if limit:
        sql += " LIMIT ? "
        params.append(int(limit))

    rows = conn.execute(sql, tuple(params)).fetchall()
    return [int(r[0]) for r in rows]


# ---------------------------
# Main
# ---------------------------
def main():
    import argparse

    load_dotenv()

    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key:
        raise SystemExit("Missing OPENAI_API_KEY")

    model = os.getenv("OPENAI_MODEL", "gpt-5.2")
    db_path = os.getenv("EARNINGS_DB_PATH", DB)

    p = argparse.ArgumentParser(description="Industry-routed deep signature extraction (PDF-only pipeline).")
    p.add_argument("--industry", default=None, help="Optional industry_factset filter (exact match).")
    p.add_argument("--limit", type=int, default=None, help="Optional limit on number of calls to process.")
    args = p.parse_args()

    conn = connect(db_path)
    init_db(conn)

    call_ids = fetch_pending_call_ids(conn, model=model, industry_filter=args.industry, limit=args.limit)

    if not call_ids:
        print("No calls pending deep extraction.")
        conn.close()
        return

    client = OpenAI(api_key=api_key, timeout=180.0)
    utc_now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    print(f"Pending deep extractions: {len(call_ids)}")

    for call_id in call_ids:
        config = fetch_industry_prompt_config(conn, call_id)
        if not config:
            print(f"SKIP call_id={call_id} (no industry prompt config)")
            continue

        # Safety: if somehow already exists, skip
        if has_signature(conn, call_id, config["prompt_version"], model):
            print(f"SKIP call_id={call_id} (already has {config['prompt_version']})")
            continue

        schema_model = resolve_schema(config["schema_name"])
        schema = TypeAdapter(schema_model).json_schema()
        schema = enforce_openai_strict_schema(schema)

        call = fetch_call_with_chunks(conn, call_id)
        payload = build_input(call)

        def _do_request():
            return client.responses.create(
                model=model,
                input=[
                    {"role": "system", "content": config["system_prompt"]},
                    {"role": "user", "content": payload},
                ],
                text={
                    "format": {
                        "type": "json_schema",
                        "name": config["schema_name"],
                        "schema": schema,
                        "strict": True,
                    }
                },
            )

        try:
            resp = call_with_retries(_do_request, call_id=call_id)
            obj = json.loads(resp.output_text)
            sig_json = json.dumps(obj, ensure_ascii=False)

            insert_signature(
                conn,
                earnings_call_id=call_id,
                model=model,
                prompt_version=config["prompt_version"],
                signature_json=sig_json,
                created_at_utc=utc_now,
            )
            conn.commit()

            print(
                f"OK: call_id={call_id} industry={config['industry']} "
                f"prompt_version={config['prompt_version']} schema={config['schema_name']}"
            )

        except Exception as e:
            print(f"FAIL call_id={call_id} {type(e).__name__}: {e}")
            continue

    conn.close()
    print("DONE")


if __name__ == "__main__":
    main()






#     .\.venv\Scripts\python.exe -m scripts.extract_deep_signatures_router
