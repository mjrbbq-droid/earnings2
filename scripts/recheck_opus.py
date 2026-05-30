"""Opus deep re-check of the high-value subset of the Russell-1000 sweep.

Target set ("do it right"):
  - reform_leaning + cross_exposure   (the screen pool — must be airtight)
  - unknown                            (model didn't conclude)
  - enforcement_leaning, confidence<0.75  (firm up disputable pro-police claims)
  - a reproducible random sample of no_material_exposure  (false-negative audit)

Runs as an Opus batch (50% off) + a synchronous pause_turn-continuation recovery
for the ~1% the batch leaves paused. Overwrites the row and stamps
investigation_model so provenance (sonnet sweep vs opus re-check) is auditable.

  python scripts/recheck_opus.py --limit 5      # probe
  python scripts/recheck_opus.py                # full remaining target set
  python scripts/recheck_opus.py --batch-id ..  # resume an existing batch
"""
from __future__ import annotations

import argparse
import json
import random
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from src.config import RISK_DB_PATH
from src.risk_schema import connect, init_db
from src.stance_investigation import SYSTEM_PROMPT, StanceInvestigation, build_user_msg, get_client
from investigate_universe import upsert_investigation
from investigate_universe_batch import parse_stance_json, sanitize_id, strictify

MODEL = "claude-opus-4-7"
MAX_USES = 12
NO_MATERIAL_SAMPLE = 50
SAMPLE_SEED = 7
BATCH_ID_FILE = Path("data/reference/last_recheck_batch_id.txt")

# Opus 4.7 BATCH pricing (50% of standard). Web search not discounted.
PRICE = {"in": 7.5/1e6, "out": 37.5/1e6, "cache_read": 0.75/1e6, "cache_write": 9.375/1e6}
WEB_SEARCH = 10/1000


def strict_schema():
    return strictify(StanceInvestigation.model_json_schema())


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


def ensure_model_col(conn):
    cols = [r[1] for r in conn.execute("PRAGMA table_info(company_stance_investigation)")]
    if "investigation_model" not in cols:
        conn.execute("ALTER TABLE company_stance_investigation ADD COLUMN investigation_model TEXT")
        conn.commit()


def target_tickers(conn) -> list[str]:
    """Deterministic target set restricted to R1000 universe members."""
    def q(where):
        return [r[0] for r in conn.execute(
            f"SELECT i.ticker FROM company_stance_investigation i "
            f"JOIN universe u ON u.ticker=i.ticker WHERE {where}")]
    screen = q("i.net_position IN ('reform_leaning','cross_exposure')")
    unknown = q("i.net_position='unknown'")
    lowconf_pos = q("i.net_position='enforcement_leaning' AND COALESCE(i.confidence,0)<0.75")
    nomat = q("i.net_position='no_material_exposure'")
    sample = random.Random(SAMPLE_SEED).sample(sorted(nomat), min(NO_MATERIAL_SAMPLE, len(nomat)))
    ordered = list(dict.fromkeys(screen + unknown + lowconf_pos + sample))  # dedupe, keep order
    return ordered


def already_rechecked(conn, ticker) -> bool:
    r = conn.execute("SELECT investigation_model FROM company_stance_investigation WHERE ticker=?",
                     (ticker,)).fetchone()
    return bool(r and r[0] == MODEL)


def build_requests(conn, tickers):
    schema = strict_schema()
    meta, reqs = {}, []
    for tk in tickers:
        if already_rechecked(conn, tk):
            continue
        row = conn.execute("SELECT company_name, sector FROM company_stance_investigation WHERE ticker=?",
                           (tk,)).fetchone()
        nm = row["company_name"] if row else tk
        sec = (row["sector"] if row else "") or ""
        cid = sanitize_id(tk)
        meta[cid] = (tk, nm, sec)
        reqs.append({"custom_id": cid, "params": {
            "model": MODEL, "max_tokens": 12000, "thinking": {"type": "adaptive"},
            "system": [{"type": "text", "text": SYSTEM_PROMPT, "cache_control": {"type": "ephemeral"}}],
            "messages": [{"role": "user", "content": build_user_msg(tk, nm, sec)}],
            "tools": [{"type": "web_search_20260209", "name": "web_search", "max_uses": MAX_USES}],
            "output_config": {"format": {"type": "json_schema", "schema": schema}},
        }})
    return reqs, meta


def stamp(conn, ticker):
    conn.execute("UPDATE company_stance_investigation SET investigation_model=? WHERE ticker=?",
                 (MODEL, ticker))


def validate(obj):
    obj["confidence"] = max(0.0, min(1.0, float(obj.get("confidence") or 0)))
    return StanceInvestigation(**obj)


def poll(client, bid, interval=60):
    t0 = time.time()
    while True:
        b = client.messages.batches.retrieve(bid)
        rc = b.request_counts
        print(f"  [{time.time()-t0:6.0f}s] {b.processing_status} ok={rc.succeeded} err={rc.errored} proc={rc.processing}", flush=True)
        if b.processing_status == "ended":
            return
        time.sleep(interval)


def write_batch(client, conn, bid, meta):
    ok = cost = 0.0
    n_ok = 0
    paused = []
    for res in client.messages.batches.results(bid):
        tk, nm, sec = meta.get(res.custom_id, (res.custom_id, res.custom_id, ""))
        if res.result.type != "succeeded":
            paused.append((tk, nm, sec)); continue
        cost += cost_of(res.result.message.usage)
        obj = parse_stance_json(res.result.message)
        if obj is None:
            paused.append((tk, nm, sec)); continue
        try:
            result = validate(obj)
        except Exception:
            paused.append((tk, nm, sec)); continue
        upsert_investigation(conn, ticker=tk, company_name=nm, sector=sec, result=result, n_search_results=0)
        stamp(conn, tk); conn.commit(); n_ok += 1
    print(f"  batch wrote {n_ok}; {len(paused)} need sync recovery")
    return n_ok, cost, paused


def investigate_sync(client, tk, nm, sec):
    schema = json.dumps(StanceInvestigation.model_json_schema())
    content = build_user_msg(tk, nm, sec) + (
        "\n\nReturn ONLY a JSON object (no prose, no fence) matching this schema:\n" + schema)
    msgs = [{"role": "user", "content": content}]
    resp = None
    for _ in range(12):
        resp = client.messages.create(
            model=MODEL, max_tokens=12000, thinking={"type": "adaptive"},
            system=[{"type": "text", "text": SYSTEM_PROMPT, "cache_control": {"type": "ephemeral"}}],
            messages=msgs,
            tools=[{"type": "web_search_20260209", "name": "web_search", "max_uses": MAX_USES}])
        if resp.stop_reason == "pause_turn":
            msgs.append({"role": "assistant", "content": resp.content}); continue
        break
    return resp


def recover_sync(client, conn, paused):
    cost = 0.0
    for tk, nm, sec in paused:
        try:
            resp = investigate_sync(client, tk, nm, sec)
            cost += cost_of(resp.usage) * 2  # sync = full price (PRICE is batch/half)
            obj = parse_stance_json(resp)
            if obj is None:
                print(f"  {tk:6} STILL FAILED stop={resp.stop_reason}"); continue
            result = validate(obj)
            upsert_investigation(conn, ticker=tk, company_name=nm, sector=sec, result=result, n_search_results=0)
            stamp(conn, tk); conn.commit()
            print(f"  {tk:6} sync OK net={result.net_position} conf={result.confidence}")
        except Exception as e:
            print(f"  {tk:6} ERR {type(e).__name__}: {str(e)[:100]}")
    return cost


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--batch-id", default=None)
    ap.add_argument("--poll-interval", type=int, default=60)
    args = ap.parse_args()

    conn = connect(RISK_DB_PATH); init_db(conn); ensure_model_col(conn)
    client = get_client()

    if args.batch_id:
        rows = conn.execute("SELECT ticker, company_name, sector FROM company_stance_investigation").fetchall()
        meta = {sanitize_id(r["ticker"]): (r["ticker"], r["company_name"], r["sector"] or "") for r in rows}
        poll(client, args.batch_id, args.poll_interval)
        _, cost, paused = write_batch(client, conn, args.batch_id, meta)
        cost += recover_sync(client, conn, paused)
        print(f"\nEST COST (resume): ${cost:.2f}")
        return

    targets = target_tickers(conn)
    reqs, meta = build_requests(conn, targets if args.limit is None else targets[:args.limit])
    print(f"Target set: {len(targets)} tickers | submitting {len(reqs)} (rest already re-checked)")
    if not reqs:
        print("Nothing to do."); return
    batch = client.messages.batches.create(requests=reqs)
    BATCH_ID_FILE.write_text(batch.id, encoding="utf-8")
    print(f"Opus re-check batch: {batch.id}\n")
    poll(client, batch.id, args.poll_interval)
    n_ok, cost, paused = write_batch(client, conn, batch.id, meta)
    cost += recover_sync(client, conn, paused)
    n = n_ok + (len(paused))
    print(f"\nRe-checked {n} tickers.  EST COST: ${cost:.2f}", flush=True)
    if n:
        print(f"  per-ticker: ${cost/n:.3f}")
    conn.close()


if __name__ == "__main__":
    main()
