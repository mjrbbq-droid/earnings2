"""Batch the Russell-1000 stance sweep through the Anthropic Batch API.

Confirmed-working recipe (see probe):
  - output_config.format with a STRICT json schema (additionalProperties:false,
    all properties required, numeric/string constraints stripped)
  - web_search server tool with max_uses cap
  - adaptive thinking
  - 50% token discount vs the synchronous path

Submits one batch over all UNRATED universe tickers (skipping the 4 share-class
duplicates already assessed under a sibling), persists the batch id for
resumability, polls until ended, then streams results into
company_stance_investigation with live cost tracking.

Resume a submitted batch without re-submitting:
    python scripts/investigate_universe_batch.py --batch-id msgbatch_xxx
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))  # sibling import

from src.config import RISK_DB_PATH
from src.risk_schema import connect, init_db
from src.stance_investigation import (
    SYSTEM_PROMPT, StanceInvestigation, build_user_msg, get_client,
)
from investigate_universe import CLASS_DUPE_SIBLING, already_done, upsert_investigation

MODEL = "claude-sonnet-4-6"
BATCH_ID_FILE = Path("data/reference/last_sweep_batch_id.txt")

# Sonnet 4.6 BATCH pricing ($/token = 50% of standard). Web search not discounted.
PRICE = {"in": 1.5/1e6, "out": 7.5/1e6, "cache_read": 0.15/1e6, "cache_write": 1.875/1e6}
WEB_SEARCH = 10/1000

_UNSUPPORTED = {"minimum", "maximum", "exclusiveMinimum", "exclusiveMaximum",
                "multipleOf", "pattern", "format", "minLength", "maxLength",
                "minItems", "maxItems"}


def strictify(node):
    if isinstance(node, dict):
        for k in list(node.keys()):
            if k in _UNSUPPORTED:
                node.pop(k)
        if node.get("type") == "object" or "properties" in node:
            node["additionalProperties"] = False
            if "properties" in node:
                node["required"] = list(node["properties"].keys())
        for v in node.values():
            strictify(v)
    elif isinstance(node, list):
        for v in node:
            strictify(v)
    return node


def strict_schema():
    return strictify(StanceInvestigation.model_json_schema())


def sanitize_id(t: str) -> str:
    """custom_id must match ^[a-zA-Z0-9_-]{1,64}$ — tickers like BRK.B have dots."""
    return re.sub(r"[^a-zA-Z0-9_-]", "_", t)[:64]


def parse_stance_json(message):
    """Robustly pull the structured-output JSON object from a message.
    Prefers the last text block; falls back to first-brace..last-brace span.
    Returns dict or None (e.g. pause_turn with no final text)."""
    blocks = [getattr(b, "text", "") for b in message.content
              if getattr(b, "type", "") == "text" and getattr(b, "text", "")]
    for txt in reversed(blocks):  # final JSON is usually the last text block
        for candidate in (txt, txt[txt.find("{"):txt.rfind("}") + 1] if "{" in txt else ""):
            if not candidate.strip():
                continue
            try:
                obj = json.loads(candidate)
                if isinstance(obj, dict) and "net_position" in obj:
                    return obj
            except Exception:
                continue
    return None


def cost_of(usage) -> float:
    if usage is None:
        return 0.0
    cr = getattr(usage, "cache_read_input_tokens", 0) or 0
    cw = getattr(usage, "cache_creation_input_tokens", 0) or 0
    it = getattr(usage, "input_tokens", 0) or 0
    ot = getattr(usage, "output_tokens", 0) or 0
    ws = 0
    stu = getattr(usage, "server_tool_use", None)
    if stu is not None:
        ws = getattr(stu, "web_search_requests", 0) or 0
    return it*PRICE["in"] + ot*PRICE["out"] + cr*PRICE["cache_read"] + cw*PRICE["cache_write"] + ws*WEB_SEARCH


def build_requests(conn, model, max_uses, limit=None):
    schema = strict_schema()
    rows = conn.execute(
        "SELECT ticker, company, sector FROM universe ORDER BY market_value_musd DESC NULLS LAST"
    ).fetchall()
    reqs, meta = [], {}
    for r in rows:
        tk = r["ticker"]
        if already_done(conn, tk) or already_done(conn, CLASS_DUPE_SIBLING.get(tk, tk)):
            continue
        nm, sec = r["company"], r["sector"] or ""
        cid = sanitize_id(tk)
        meta[cid] = (tk, nm, sec)
        reqs.append({
            "custom_id": cid,
            "params": {
                "model": model,
                "max_tokens": 12000,
                "thinking": {"type": "adaptive"},
                "system": [{"type": "text", "text": SYSTEM_PROMPT,
                            "cache_control": {"type": "ephemeral"}}],
                "messages": [{"role": "user", "content": build_user_msg(tk, nm, sec)}],
                "tools": [{"type": "web_search_20260209", "name": "web_search", "max_uses": max_uses}],
                "output_config": {"format": {"type": "json_schema", "schema": schema}},
            },
        })
        if limit and len(reqs) >= limit:
            break
    return reqs, meta


def write_results(client, conn, batch_id, meta):
    """Stream batch results into the DB. meta maps custom_id -> (ticker, name, sector)."""
    n_ok = n_err = n_pos = 0
    total_cost = 0.0
    errors = []
    for res in client.messages.batches.results(batch_id):
        cid = res.custom_id
        tk = meta.get(cid, (cid, cid, ""))[0]
        if res.result.type != "succeeded":
            n_err += 1
            errors.append((tk, str(getattr(res.result, "error", res.result.type))[:160]))
            continue
        m = res.result.message
        total_cost += cost_of(m.usage)
        obj = parse_stance_json(m)
        if obj is None:
            n_err += 1
            reason = "pause_turn (no final output)" if m.stop_reason == "pause_turn" else "no parseable JSON"
            errors.append((tk, f"{reason} [stop={m.stop_reason}]"))
            continue
        try:
            if obj.get("confidence") is not None:
                obj["confidence"] = max(0.0, min(1.0, float(obj["confidence"])))
            result = StanceInvestigation(**obj)
        except Exception as e:
            n_err += 1
            errors.append((tk, f"validate: {type(e).__name__}: {str(e)[:120]}"))
            continue
        _tk, nm, sec = meta.get(cid, (cid, cid, ""))
        upsert_investigation(conn, ticker=tk, company_name=nm, sector=sec,
                             result=result, n_search_results=0)
        conn.commit()
        n_ok += 1
        if result.anti_police_action or result.pro_police_action:
            n_pos += 1
    print(f"\nWrote {n_ok} investigations.  errored={n_err}  positives={n_pos}")
    print(f"EST. COST (batch, {MODEL}): ${total_cost:.2f}")
    if errors:
        print(f"\n{len(errors)} errors (first 15):")
        for tk, e in errors[:15]:
            print(f"  {tk:6} {e}")


def poll(client, batch_id, interval):
    t0 = time.time()
    while True:
        b = client.messages.batches.retrieve(batch_id)
        rc = b.request_counts
        print(f"  [{time.time()-t0:6.0f}s] {b.processing_status}  "
              f"proc={rc.processing} ok={rc.succeeded} err={rc.errored}", flush=True)
        if b.processing_status == "ended":
            return
        time.sleep(interval)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", default=MODEL)
    ap.add_argument("--max-uses", type=int, default=12)
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--poll-interval", type=int, default=60)
    ap.add_argument("--batch-id", default=None, help="Resume: fetch+write an existing batch")
    args = ap.parse_args()

    conn = connect(RISK_DB_PATH)
    init_db(conn)
    client = get_client()

    if args.batch_id:
        # Resume mode: rebuild meta from universe so we can label rows.
        rows = conn.execute("SELECT ticker, company, sector FROM universe").fetchall()
        meta = {sanitize_id(r["ticker"]): (r["ticker"], r["company"], r["sector"] or "") for r in rows}
        print(f"Resuming batch {args.batch_id} ...")
        poll(client, args.batch_id, args.poll_interval)
        write_results(client, conn, args.batch_id, meta)
        return

    reqs, meta = build_requests(conn, args.model, args.max_uses, args.limit)
    print(f"Submitting {len(reqs)} requests  (model={args.model}, web_search max_uses={args.max_uses})")
    batch = client.messages.batches.create(requests=reqs)
    BATCH_ID_FILE.write_text(batch.id, encoding="utf-8")
    print(f"Batch id: {batch.id}  (saved to {BATCH_ID_FILE})")
    print(f"Submitted at {datetime.now(timezone.utc).isoformat()}\n")

    poll(client, batch.id, args.poll_interval)
    write_results(client, conn, batch.id, meta)

    # coverage after
    uni = conn.execute("SELECT COUNT(*) FROM universe").fetchone()[0]
    assessed = conn.execute(
        "SELECT COUNT(*) FROM universe WHERE ticker IN (SELECT ticker FROM company_stance_investigation)"
    ).fetchone()[0]
    print(f"\nCoverage now: {assessed}/{uni} ({100*assessed/uni:.1f}%) of Russell 1000 assessed.")
    conn.close()


if __name__ == "__main__":
    main()
