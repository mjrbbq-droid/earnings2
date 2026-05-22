"""
Run the anti-police stance investigation across a configurable universe.

Default: priority sectors of S&P 500 + manually-named extras (UL, LEVI) that
aren't in S&P 500 but are known watchlist candidates.

Usage:
    python scripts/investigate_universe.py                 # priority sectors + extras
    python scripts/investigate_universe.py --limit 20      # first 20 tickers
    python scripts/investigate_universe.py --tickers MSFT,IBM,AMZN
    python scripts/investigate_universe.py --sector Technology
    python scripts/investigate_universe.py --all           # full S&P 500

Idempotent: skips tickers already in company_stance_investigation.
"""
from __future__ import annotations

import argparse
import csv
import sys
import traceback
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.config import DATA_DIR, RISK_DB_PATH
from src.risk_db import utcnow
from src.risk_schema import connect, init_db
from src.stance_investigation import StanceInvestigation, get_client, investigate_company

PRIORITY_SECTORS = {
    "Technology",
    "Communication Services",
    "Consumer Cyclical",
    "Consumer Discretionary",
    "Consumer Defensive",
    "Consumer Staples",
    "Healthcare",
    "Financial Services",
}

# Companies that aren't in S&P 500 but we want investigated (known candidates)
EXTRAS: list[tuple[str, str, str]] = [
    ("UL",   "Unilever (Ben & Jerry's parent)", "Consumer Defensive"),
    ("LEVI", "Levi Strauss & Co.",              "Consumer Cyclical"),
    ("VFC",  "VF Corporation (North Face parent)", "Consumer Cyclical"),
]


def load_universe(csv_path: Path) -> list[dict]:
    rows = []
    with open(csv_path, encoding="utf-8") as f:
        for r in csv.DictReader(f):
            rows.append(r)
    return rows


def already_done(conn, ticker: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM company_stance_investigation WHERE ticker = ? LIMIT 1;",
        (ticker,),
    ).fetchone()
    return row is not None


def upsert_investigation(
    conn, *, ticker: str, company_name: str, sector: str | None,
    result: StanceInvestigation, n_search_results: int = 0,
) -> None:
    conn.execute(
        """
        INSERT INTO company_stance_investigation
            (ticker, company_name, sector, investigated_at_utc,
             anti_police_action, anti_police_type, anti_police_first_date,
             anti_police_first_year, anti_police_last_known_date,
             anti_police_summary, anti_police_current_status,
             anti_police_evidence_url, anti_police_evidence_quote,
             pro_police_action, pro_police_type, pro_police_first_date,
             pro_police_first_year, pro_police_last_known_date,
             pro_police_summary, pro_police_current_status,
             pro_police_evidence_url, pro_police_evidence_quote,
             net_position, net_summary, confidence, notes, n_search_results)
        VALUES (?, ?, ?, ?,
                ?, ?, ?, ?, ?, ?, ?, ?, ?,
                ?, ?, ?, ?, ?, ?, ?, ?, ?,
                ?, ?, ?, ?, ?)
        ON CONFLICT(ticker) DO UPDATE SET
            company_name                  = excluded.company_name,
            sector                        = excluded.sector,
            investigated_at_utc           = excluded.investigated_at_utc,
            anti_police_action            = excluded.anti_police_action,
            anti_police_type              = excluded.anti_police_type,
            anti_police_first_date        = excluded.anti_police_first_date,
            anti_police_first_year        = excluded.anti_police_first_year,
            anti_police_last_known_date   = excluded.anti_police_last_known_date,
            anti_police_summary           = excluded.anti_police_summary,
            anti_police_current_status    = excluded.anti_police_current_status,
            anti_police_evidence_url      = excluded.anti_police_evidence_url,
            anti_police_evidence_quote    = excluded.anti_police_evidence_quote,
            pro_police_action             = excluded.pro_police_action,
            pro_police_type               = excluded.pro_police_type,
            pro_police_first_date         = excluded.pro_police_first_date,
            pro_police_first_year         = excluded.pro_police_first_year,
            pro_police_last_known_date    = excluded.pro_police_last_known_date,
            pro_police_summary            = excluded.pro_police_summary,
            pro_police_current_status     = excluded.pro_police_current_status,
            pro_police_evidence_url       = excluded.pro_police_evidence_url,
            pro_police_evidence_quote     = excluded.pro_police_evidence_quote,
            net_position                  = excluded.net_position,
            net_summary                   = excluded.net_summary,
            confidence                    = excluded.confidence,
            notes                         = excluded.notes,
            n_search_results              = excluded.n_search_results;
        """,
        (
            ticker, company_name, sector, utcnow(),
            1 if result.anti_police_action else 0, result.anti_police_type,
            result.anti_police_first_date, result.anti_police_first_year,
            result.anti_police_last_known_date, result.anti_police_summary,
            result.anti_police_current_status, result.anti_police_evidence_url,
            result.anti_police_evidence_quote,
            1 if result.pro_police_action else 0, result.pro_police_type,
            result.pro_police_first_date, result.pro_police_first_year,
            result.pro_police_last_known_date, result.pro_police_summary,
            result.pro_police_current_status, result.pro_police_evidence_url,
            result.pro_police_evidence_quote,
            result.net_position, result.net_summary, result.confidence,
            result.notes, n_search_results,
        ),
    )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--all", action="store_true", help="Investigate ALL S&P 500 (not just priority sectors)")
    parser.add_argument("--limit", type=int, default=None, help="Process only first N tickers")
    parser.add_argument("--sector", type=str, default=None, help="Filter to one sector exactly")
    parser.add_argument("--tickers", type=str, default=None, help="Comma-separated explicit ticker list")
    parser.add_argument("--include-extras", action="store_true", default=True, help="Include UL/LEVI/VFC")
    parser.add_argument("--skip-extras", action="store_true", help="Do not include UL/LEVI/VFC")
    args = parser.parse_args()

    conn = connect(RISK_DB_PATH)
    init_db(conn)
    client = get_client()

    universe_csv = Path(DATA_DIR) / "sp500_universe.csv"
    if not universe_csv.exists():
        print(f"Missing {universe_csv} — run scripts/load_sp500_universe.py first.")
        return
    sp500 = load_universe(universe_csv)

    # Build candidate list
    candidates: list[tuple[str, str, str]] = []
    if args.tickers:
        wanted = {t.strip().upper() for t in args.tickers.split(",") if t.strip()}
        by_ticker = {r["ticker"]: r for r in sp500}
        for t in wanted:
            if t in by_ticker:
                r = by_ticker[t]
                candidates.append((t, r["name"], r["sector"]))
            else:
                extra = next((e for e in EXTRAS if e[0] == t), None)
                candidates.append(extra if extra else (t, t, ""))
    else:
        for r in sp500:
            sector = r.get("sector") or ""
            if args.sector:
                if sector == args.sector:
                    candidates.append((r["ticker"], r["name"], sector))
            elif args.all or sector in PRIORITY_SECTORS:
                candidates.append((r["ticker"], r["name"], sector))
        if args.include_extras and not args.skip_extras:
            for t, n, s in EXTRAS:
                if args.sector and s != args.sector:
                    continue
                candidates.append((t, n, s))

    # Cap by --limit (AFTER filtering)
    if args.limit is not None:
        candidates = candidates[: args.limit]

    from src.config import ANTHROPIC_MODEL
    print(f"Universe: {len(candidates)} ticker(s).  Model: {ANTHROPIC_MODEL}\n")

    n_skipped = 0
    n_done = 0
    n_positives = 0
    n_errors = 0
    started_at = datetime.now(timezone.utc)

    for i, (ticker, name, sector) in enumerate(candidates, 1):
        if already_done(conn, ticker):
            n_skipped += 1
            continue

        print(f"--- {i}/{len(candidates)}  {ticker:6s}  ({sector or '?'})  {name[:60]} ---")
        try:
            result = investigate_company(client, ticker=ticker, company_name=name, sector=sector)
        except Exception as e:
            n_errors += 1
            print(f"  ERROR: {type(e).__name__}: {e}")
            traceback.print_exc()
            print()
            continue

        upsert_investigation(conn, ticker=ticker, company_name=name, sector=sector, result=result)
        conn.commit()
        n_done += 1
        if result.anti_police_action or result.pro_police_action:
            n_positives += 1

        anti_flag = "A" if result.anti_police_action else "-"
        pro_flag  = "P" if result.pro_police_action  else "-"
        anti_date = result.anti_police_first_date or "         "
        pro_date  = result.pro_police_first_date  or "         "
        print(f"  [{anti_flag}{pro_flag}]  net={result.net_position:18s}  conf={result.confidence:.2f}")
        if result.anti_police_action:
            print(f"      ANTI {anti_date}  {result.anti_police_type}  ({result.anti_police_current_status})")
            print(f"           {result.anti_police_summary or ''}")
        if result.pro_police_action:
            print(f"      PRO  {pro_date}  {result.pro_police_type}  ({result.pro_police_current_status})")
            print(f"           {result.pro_police_summary or ''}")
        print(f"      NET  {result.net_summary}")
        print()

    elapsed = (datetime.now(timezone.utc) - started_at).total_seconds()
    print("=" * 80)
    print(f"Done in {elapsed:.0f}s.  skipped={n_skipped}  done={n_done}  positives={n_positives}  errors={n_errors}\n")

    # ── Summary by net position ─────────────────────────────────────────
    print("--- net positions across investigated companies ---")
    for r in conn.execute(
        """
        SELECT net_position, COUNT(*) AS n
        FROM company_stance_investigation
        GROUP BY net_position ORDER BY n DESC;
        """
    ):
        print(f"  {r['net_position']:18s} {r['n']}")

    print("\n--- ANTI-police actions (sorted by date) ---")
    for r in conn.execute(
        """
        SELECT ticker, company_name, anti_police_type, anti_police_first_date,
               anti_police_current_status, net_position, confidence
        FROM company_stance_investigation
        WHERE anti_police_action = 1
        ORDER BY COALESCE(anti_police_first_date, '9999') ASC, ticker;
        """
    ):
        date_str = r["anti_police_first_date"] or "(no date)"
        print(
            f"  {r['ticker']:6s}  {date_str:12s}  "
            f"{r['anti_police_type']:36s}  "
            f"current={r['anti_police_current_status']:11s}  "
            f"net={r['net_position']:18s}  {r['company_name'][:40]}"
        )

    print("\n--- PRO-police actions (sorted by date) ---")
    for r in conn.execute(
        """
        SELECT ticker, company_name, pro_police_type, pro_police_first_date,
               pro_police_current_status, net_position, confidence
        FROM company_stance_investigation
        WHERE pro_police_action = 1
        ORDER BY COALESCE(pro_police_first_date, '9999') ASC, ticker;
        """
    ):
        date_str = r["pro_police_first_date"] or "(no date)"
        print(
            f"  {r['ticker']:6s}  {date_str:12s}  "
            f"{r['pro_police_type']:38s}  "
            f"current={r['pro_police_current_status']:11s}  "
            f"net={r['net_position']:18s}  {r['company_name'][:40]}"
        )

    print("\n--- MIXED companies (both anti and pro) ---")
    for r in conn.execute(
        """
        SELECT ticker, company_name, net_summary
        FROM company_stance_investigation
        WHERE net_position = 'mixed'
        ORDER BY ticker;
        """
    ):
        print(f"  {r['ticker']:6s}  {r['company_name']}")
        print(f"          {r['net_summary']}")

    conn.close()


if __name__ == "__main__":
    main()
