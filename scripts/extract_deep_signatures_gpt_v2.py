# scripts/extract_deep_signatures_gpt_v2.py
from __future__ import annotations

import sys
import json
import os
import time
import random
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# --- ensure project root is on PYTHONPATH ---
PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))
# ------------------------------------------

from dotenv import load_dotenv
from openai import OpenAI, APITimeoutError, APIConnectionError, RateLimitError
from pydantic import TypeAdapter

from src.db import (
    connect,
    init_db,
    fetch_calls_for_signatures,
    fetch_call_with_chunks,
    insert_signature,
)
from src.signatures_schema_deep_v2 import HomebuildersDeepSignatureV2

PROMPT_VERSION = "hb_deep_v2"

MAX_ATTEMPTS = 6
BASE_SLEEP = 5.0


def enforce_openai_strict_schema(schema: Any) -> Any:
    """
    OpenAI strict json_schema requirements:
      - every object: additionalProperties=false
      - every object: required includes every key in properties
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

    prepared = []
    qa = []
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


def call_with_retries(fn, *, call_id: int):
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


def main() -> None:
    load_dotenv()

    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key:
        raise SystemExit("Missing OPENAI_API_KEY in your .env")

    model = os.getenv("OPENAI_MODEL", "gpt-5.2")
    db_path = os.getenv("EARNINGS_DB_PATH", "./data/databases/earnings.db")

    client = OpenAI(api_key=api_key, timeout=180.0)

    conn = connect(db_path)
    init_db(conn)

    # idempotent: only runs calls missing hb_deep_v2 for this model
    call_ids = fetch_calls_for_signatures(conn, prompt_version=PROMPT_VERSION, model=model)
    if not call_ids:
        print("No calls pending deep v2 signature extraction. Nothing to do.")
        return

    # Keep downstream "latest" deterministic: skip calls missing call_date
    rows = conn.execute(
        f"""
        SELECT id
        FROM earnings_calls
        WHERE id IN ({",".join(["?"] * len(call_ids))})
          AND call_date IS NOT NULL
          AND call_date != '';
        """,
        tuple(call_ids),
    ).fetchall()
    call_ids = [int(r[0]) for r in rows]

    if not call_ids:
        print("Calls pending exist, but all are missing call_date. Nothing to do.")
        return

    utc_now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    schema = TypeAdapter(HomebuildersDeepSignatureV2).json_schema()
    schema = enforce_openai_strict_schema(schema)

    system_text = (
        "You are a senior homebuilders analyst. Extract a deep, structured signature. "
        "This is NOT sentiment; it is management's business reality. "
        "Use only the transcript. Be conservative and use 'unclear' if not supported. "
        "Focus on: sales/orders/backlog, pricing power vs incentives/buydowns, margin mechanics and outlook "
        "(gross and operating), cost/cycle time, inventory/spec mix/competition, land/community pipeline, "
        "guidance action (raised/cut/reaffirmed/withdrawn/no guidance), timing bias (front/back-half), "
        "visibility, and balance sheet/liquidity/leverage/capital return posture. "
        "Provide short supporting quotes (<=25 words)."
    )

    ok = 0
    fail = 0

    with conn:
        for call_id in call_ids:
            call = fetch_call_with_chunks(conn, call_id)
            payload = build_input(call)

            def _do_request():
                return client.responses.create(
                    model=model,
                    input=[
                        {"role": "system", "content": system_text},
                        {"role": "user", "content": payload},
                    ],
                    text={
                        "format": {
                            "type": "json_schema",
                            "name": "homebuilders_deep_signature_v2",
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

                ok += 1
                print(f"OK: call_id={call_id} deep_v2_signature_id={sig_id}")

            except Exception as e:
                fail += 1
                print(f"FAIL: call_id={call_id} {type(e).__name__}: {e}")
                continue

    print("====")
    print(f"Deep v2 signatures OK: {ok}")
    print(f"Deep v2 signatures FAIL: {fail}")
    print(f"DB: {db_path}")
    print(f"Model: {model} | Prompt: {PROMPT_VERSION}")


if __name__ == "__main__":
    main()


