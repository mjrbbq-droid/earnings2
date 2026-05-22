# scripts/run_pipeline.py
from __future__ import annotations

import argparse
import sqlite3
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DB = str(PROJECT_ROOT / "data" / "earnings.db")


def run_step(py: Path, module: str, args: list[str], *, label: str) -> None:
    cmd = [str(py), "-m", module] + args
    print("\n" + "-" * 60)
    print(f"STEP: {label}")
    print("CMD:", " ".join(cmd))
    subprocess.check_call(cmd, cwd=str(PROJECT_ROOT))


def fetch_active_industries() -> list[str]:
    conn = sqlite3.connect(DB)
    rows = conn.execute(
        "SELECT industry_fs FROM industry_prompt_registry WHERE is_active = 1 ORDER BY industry_fs"
    ).fetchall()
    conn.close()
    return [r[0] for r in rows]


def fetch_industries_with_new_pulses(since_utc: str) -> list[str]:
    """Return industries that had company pulses generated since `since_utc`."""
    conn = sqlite3.connect(DB)
    rows = conn.execute(
        """
        SELECT DISTINCT ec.industry_factset
        FROM pulse_log pl
        JOIN earnings_calls ec ON ec.id = pl.call_id
        WHERE pl.pulse_type = 'company'
          AND pl.generated_at >= ?
          AND ec.industry_factset IS NOT NULL
          AND ec.industry_factset != ''
        ORDER BY ec.industry_factset;
        """,
        (since_utc,),
    ).fetchall()
    conn.close()
    return [r[0] for r in rows]


def main(argv: Optional[list[str]] = None) -> None:
    p = argparse.ArgumentParser(description="Run full earnings2 pipeline.")
    p.add_argument("--industry", type=str, default=None, help="Limit to a single FactSet industry label.")
    p.add_argument("--tickers", type=str, default=None, help="Comma-separated tickers to limit pulse generation.")
    p.add_argument("--skip-gpt", action="store_true", help="Skip GPT steps (deep/forensics/pulses). Keep deterministic steps.")
    p.add_argument("--no-pulse", action="store_true", help="Do not generate any pulse markdown.")
    p.add_argument("--force-pulse", action="store_true", help="Force-regenerate all pulses (ignore pulse_log).")
    p.add_argument("--only-ingest", action="store_true", help="Only ingest PDFs.")
    p.add_argument("--only-chunk", action="store_true", help="Only chunk calls that have no chunks.")
    p.add_argument("--only-score", action="store_true", help="Only run zscore computation.")
    p.add_argument("--only-pulse", action="store_true", help="Only run pulse generation (company + industry).")
    args = p.parse_args(argv)

    py = Path(sys.executable)

    print("\n" + "=" * 60)
    print("RUNNING EARNINGS2 PIPELINE")
    print("=" * 60)
    print("Project:", PROJECT_ROOT)
    print("Python :", py)

    # -----------------------
    # Single-stage shortcuts
    # -----------------------
    if args.only_ingest:
        run_step(py, "scripts.ingest_pdfs", [], label="INGEST PDFs")
        return

    if args.only_chunk:
        run_step(py, "scripts.build_chunks", [], label="BUILD CHUNKS")
        return

    if args.only_score:
        run_step(py, "scripts.zscore_calls", [], label="COMPUTE Z-SCORES")
        return

    if args.only_pulse:
        _run_pulses(py, args)
        return

    # -----------------------
    # Full pipeline
    # -----------------------
    # 1) INGEST
    run_step(py, "scripts.ingest_pdfs", [], label="INGEST PDFs")

    # 2) CHUNK
    run_step(py, "scripts.build_chunks", [], label="BUILD CHUNKS")

    # 3) GPT extraction
    if not args.skip_gpt:
        router_args: list[str] = []
        if args.industry:
            router_args += ["--industry", args.industry.strip()]

        run_step(py, "scripts.extract_deep_signatures_router", router_args, label="DEEP SIGNATURES (industry-routed)")
        run_step(py, "scripts.extract_forensics_gpt", [], label="FORENSICS SIGNATURES")

    # 4) Z-scores (deterministic, always run)
    run_step(py, "scripts.zscore_calls", [], label="COMPUTE Z-SCORES")

    # 5) Pulses
    if (not args.no_pulse) and (not args.skip_gpt):
        _run_pulses(py, args)

    print("\n" + "=" * 60)
    print("PIPELINE COMPLETE")
    print("=" * 60)


def _run_pulses(py: Path, args) -> None:
    run_start_utc = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    # Company pulses
    pulse_args: list[str] = []
    if args.tickers:
        pulse_args.append(args.tickers.strip())
    if args.force_pulse:
        pulse_args.append("--force")
    run_step(py, "scripts.generate_company_pulses", pulse_args, label="GENERATE COMPANY PULSES")

    # Industry pulses — only for industries that got new company pulses
    if args.industry:
        industries = [args.industry.strip()]
    elif args.force_pulse:
        industries = fetch_active_industries()
    else:
        industries = fetch_industries_with_new_pulses(run_start_utc)
        if not industries:
            print("\nNo industries had new company pulses — skipping industry pulse generation.")
            return

    for ind in industries:
        run_step(
            py, "scripts.generate_industry_pulse", [ind],
            label=f"GENERATE INDUSTRY PULSE: {ind}",
        )


if __name__ == "__main__":
    main()


# Usage examples:
# Run everything:                                .\.venv\Scripts\python.exe -m scripts.run_pipeline
# Run everything for one industry:               .\.venv\Scripts\python.exe -m scripts.run_pipeline --industry "ENERGY EQUIPMENT & SERVICES"
# Run everything, pulses for specific tickers:   .\.venv\Scripts\python.exe -m scripts.run_pipeline --tickers "SLB,HAL,BKR,WFRD"
# Skip GPT (just recompute scores):              .\.venv\Scripts\python.exe -m scripts.run_pipeline --skip-gpt
# Only regenerate pulses:                        .\.venv\Scripts\python.exe -m scripts.run_pipeline --only-pulse
# Only compute zscores:                          .\.venv\Scripts\python.exe -m scripts.run_pipeline --only-score
