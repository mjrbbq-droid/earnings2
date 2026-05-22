# scripts/extract_signatures_gpt.py
from __future__ import annotations

# --- ensure project root is on PYTHONPATH ---
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))
# ------------------------------------------

import json
import os
from datetime import datetime, timezone
from typing import Any

from dotenv import load_dotenv
from openai import OpenAI
from pydantic import TypeAdapter

from src.db import (
    connect,
    init_db,
    fetch_calls_for_signatures,
    fetch_call_with_chunks,
    insert_signature,
)
from src.signatures_schema import HomebuildersSignature


PROMPT_VERSION = "hb_sig_v1"


def enforce_openai_strict_schema(schema: Any) -> Any:
    """
    OpenAI strict json_schema requirements:
      - For every object: additionalProperties must be false
      - For every object: required must be present and include EVERY key in properties
    This recursively patches a Pydantic-generated JSON schema to comply.
    """
    if isinstance(schema, dict):
        if schema.get("type") == "object":
            schema.setdefault("additionalProperties", False)

            props = schema.get("properties")
            if isinstance(props, dict):
                # required must include all property keys
                schema["required"] = list(props.keys())

                # recurse into each property schema
                for subschema in props.values():
                    enforce_openai_strict_schema(subschema)

        # recurse through schema containers
        for key in ("$defs", "definitions"):
            if key in schema and isinstance(schema[key], dict):
                for subschema in schema[key].values():
                    enforce_openai_strict_schema(subschema)

        # recurse through combinators and arrays
        for key in ("anyOf", "oneOf", "allOf"):
            if key in schema and isinstance(schema[key], list):
                for subschema in schema[key]:
                    enforce_openai_strict_schema(subschema)

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

    prepared, qa = [], []

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


def main() -> None:
    load_dotenv()

    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key:
        raise SystemExit("Missing OPENAI_API_KEY in your .env")

    model = os.getenv("OPENAI_MODEL", "gpt-5.2")
    db_path = os.getenv("EARNINGS_DB_PATH", "./data/databases/earnings.db")

    client = OpenAI(api_key=api_key)

    conn = connect(db_path)
    init_db(conn)

    call_ids = fetch_calls_for_signatures(conn, prompt_version=PROMPT_VERSION, model=model)
    if not call_ids:
        print("No calls pending signature extraction. Nothing to do.")
        return

    utc_now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    # Build json_schema from pydantic model and patch for OpenAI strict mode
    schema = TypeAdapter(HomebuildersSignature).json_schema()
    schema = enforce_openai_strict_schema(schema)

    with conn:
        for call_id in call_ids:
            call = fetch_call_with_chunks(conn, call_id)
            payload = build_input(call)

            resp = client.responses.create(
                model=model,
                input=[
                    {
                        "role": "system",
                        "content": (
                            "You are an expert equity analyst. "
                            "Extract a structured homebuilders regime signature from this earnings call. "
                            "Be conservative; use 'unclear' if not supported. "
                            "Provide short supporting quotes (<=25 words)."
                        ),
                    },
                    {"role": "user", "content": payload},
                ],
                text={
                    "format": {
                        "type": "json_schema",
                        "name": "homebuilders_signature",
                        "schema": schema,
                        "strict": True,
                    }
                },
            )

            sig_obj = json.loads(resp.output_text)
            sig_json = json.dumps(sig_obj, ensure_ascii=False)

            sig_id = insert_signature(
                conn,
                earnings_call_id=call_id,
                model=model,
                prompt_version=PROMPT_VERSION,
                signature_json=sig_json,
                created_at_utc=utc_now,
            )

            print(f"OK: call_id={call_id} signature_id={sig_id}")

    print("====")
    print(f"Signatures written: {len(call_ids)}")
    print(f"DB: {db_path}")
    print(f"Model: {model} | Prompt: {PROMPT_VERSION}")


if __name__ == "__main__":
    main()



#       python -m scripts.extract_signatures_gpt