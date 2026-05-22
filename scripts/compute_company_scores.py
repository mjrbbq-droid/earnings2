"""
Compute company_scores rollups from article_scores.

For each active ticker × window (default 7 / 30 / 90 days), write one row to
company_scores with:
    n_articles            — count of scored articles in window (relevance >= MIN_RELEVANCE)
    mean_severity         — signed average; + = activism direction, - = institutional
    activism_volume       — count with stance = 'activism'
    institutional_volume  — count with stance = 'positive_institutional'
    persistence_score     — unique_days / n_articles
                            (1.0 = drumbeat across many days; ~0.2 = single-day flash flood)

Idempotent: UNIQUE(ticker, as_of_date, window_days) — re-running same day replaces.
"""
from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.config import ANTHROPIC_MODEL, RISK_DB_PATH
from src.risk_db import load_active_tickers, utcnow
from src.risk_schema import connect, init_db
from src.risk_scoring import PROMPT_VERSION

WINDOWS = [7, 30, 90]
MIN_RELEVANCE = 30


def compute_for_ticker(conn, ticker: str, as_of: datetime, window_days: int) -> dict:
    cutoff = (as_of - timedelta(days=window_days)).isoformat()
    rows = conn.execute(
        """
        SELECT
            a.published,
            s.relevance,
            s.stance,
            s.severity,
            s.confidence
        FROM articles a
        JOIN article_scores s
          ON s.article_id     = a.id
         AND s.model          = ?
         AND s.prompt_version = ?
        WHERE a.ticker    = ?
          AND a.published >= ?
          AND s.relevance >= ?;
        """,
        (ANTHROPIC_MODEL, PROMPT_VERSION, ticker, cutoff, MIN_RELEVANCE),
    ).fetchall()

    n = len(rows)
    if n == 0:
        return {
            "n_articles": 0,
            "mean_severity": None,
            "activism_volume": 0,
            "institutional_volume": 0,
            "persistence_score": None,
        }

    severities = [r["severity"] for r in rows]
    activism = sum(1 for r in rows if r["stance"] == "activism")
    institutional = sum(1 for r in rows if r["stance"] == "positive_institutional")

    # persistence = unique days with coverage / total articles
    unique_days = {(r["published"] or "")[:10] for r in rows if r["published"]}
    persistence = len(unique_days) / n if n else None

    return {
        "n_articles": n,
        "mean_severity": sum(severities) / n,
        "activism_volume": activism,
        "institutional_volume": institutional,
        "persistence_score": persistence,
    }


def main() -> None:
    conn = connect(RISK_DB_PATH)
    init_db(conn)

    as_of = datetime.now(timezone.utc)
    as_of_date = as_of.date().isoformat()
    now = utcnow()

    tickers = load_active_tickers(conn)
    print(f"Computing company_scores  as_of={as_of_date}  tickers={[t for t, _ in tickers]}\n")

    for ticker, company in tickers:
        print(f"=== {ticker} ({company}) ===")
        for w in WINDOWS:
            stats = compute_for_ticker(conn, ticker, as_of, w)
            conn.execute(
                """
                INSERT INTO company_scores
                    (ticker, as_of_date, window_days,
                     n_articles, mean_severity, activism_volume, institutional_volume,
                     persistence_score, computed_at_utc)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(ticker, as_of_date, window_days) DO UPDATE SET
                    n_articles           = excluded.n_articles,
                    mean_severity        = excluded.mean_severity,
                    activism_volume      = excluded.activism_volume,
                    institutional_volume = excluded.institutional_volume,
                    persistence_score    = excluded.persistence_score,
                    computed_at_utc      = excluded.computed_at_utc;
                """,
                (
                    ticker, as_of_date, w,
                    stats["n_articles"],
                    stats["mean_severity"],
                    stats["activism_volume"],
                    stats["institutional_volume"],
                    stats["persistence_score"],
                    now,
                ),
            )
            ms = stats["mean_severity"]
            ms_str = f"{ms:+.2f}" if ms is not None else "  n/a"
            ps = stats["persistence_score"]
            ps_str = f"{ps:.2f}" if ps is not None else "n/a"
            print(
                f"  {w:3d}d: n={stats['n_articles']:3d}  "
                f"mean_sev={ms_str}  "
                f"activism={stats['activism_volume']:2d}  "
                f"institutional={stats['institutional_volume']:2d}  "
                f"persistence={ps_str}"
            )
    conn.commit()

    print("\n--- final company_scores table ---")
    for r in conn.execute(
        """
        SELECT ticker, as_of_date, window_days, n_articles,
               mean_severity, activism_volume, institutional_volume, persistence_score
        FROM company_scores
        ORDER BY ticker, window_days;
        """
    ):
        ms = r["mean_severity"]
        ms_str = f"{ms:+.2f}" if ms is not None else "  n/a"
        ps = r["persistence_score"]
        ps_str = f"{ps:.2f}" if ps is not None else "n/a"
        print(
            f"  {r['ticker']:5s}  {r['as_of_date']}  "
            f"win={r['window_days']:3d}d  n={r['n_articles']:3d}  "
            f"sev={ms_str}  act={r['activism_volume']:2d}  inst={r['institutional_volume']:2d}  "
            f"pers={ps_str}"
        )

    conn.close()


if __name__ == "__main__":
    main()
