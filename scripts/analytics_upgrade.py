# scripts/analytics_upgrade.py
from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from typing import Optional, Tuple, List, Dict


DB = "./data/earnings.db"

# Composite score weights (positive is better; LRI is inverted)
WEIGHTS = {
    "mpi_z": 0.30,
    "dqi_z": 0.25,
    "ssi_z": 0.20,
    "eri_z": 0.15,
    "lri_z_inverted": 0.10,  # use -lri_z
}

SCORE_VERSION = "score_v2"


def utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def col_exists(conn: sqlite3.Connection, table: str, col: str) -> bool:
    return any(r[1] == col for r in conn.execute(f"PRAGMA table_info({table});").fetchall())


def ensure_schema(conn: sqlite3.Connection) -> None:
    """
    Ensure required tables/columns exist.
    This does NOT touch your earnings_calls table.
    It ensures call_zscores has score columns, enforces one-row-per-call,
    and creates aggregate tables.
    """
    # Ensure call_zscores exists (historical PK preserved for compatibility)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS call_zscores (
            ticker TEXT NOT NULL,
            call_date TEXT,
            fiscal_year INTEGER,
            fiscal_quarter INTEGER,
            earnings_call_id INTEGER,

            mpi_raw REAL, mpi_z REAL,
            dqi_raw REAL, dqi_z REAL,
            ssi_raw REAL, ssi_z REAL,
            eri_raw REAL, eri_z REAL,
            lri_raw REAL, lri_z REAL,

            pmi_raw REAL, pmi_z REAL,
            bsi_raw REAL, bsi_z REAL,

            source_file TEXT NOT NULL,
            loaded_at_utc TEXT NOT NULL,

            composite_score REAL,
            call_stance TEXT,
            score_version TEXT,
            scored_at_utc TEXT,

            PRIMARY KEY (ticker, call_date, source_file)
        );
        """
    )

    # Enforce "one row per call"
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS ux_call_zscores_ticker_calldate "
        "ON call_zscores(ticker, call_date);"
    )

    # Helpful indexes
    conn.execute(
        "CREATE INDEX IF NOT EXISTS ix_call_zscores_ticker_date "
        "ON call_zscores(ticker, call_date);"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS ix_call_zscores_call_id "
        "ON call_zscores(earnings_call_id);"
    )

    # Add scoring columns to call_zscores if missing (safe)
    if not col_exists(conn, "call_zscores", "composite_score"):
        conn.execute("ALTER TABLE call_zscores ADD COLUMN composite_score REAL;")
    if not col_exists(conn, "call_zscores", "call_stance"):
        conn.execute("ALTER TABLE call_zscores ADD COLUMN call_stance TEXT;")
    if not col_exists(conn, "call_zscores", "score_version"):
        conn.execute("ALTER TABLE call_zscores ADD COLUMN score_version TEXT;")
    if not col_exists(conn, "call_zscores", "scored_at_utc"):
        conn.execute("ALTER TABLE call_zscores ADD COLUMN scored_at_utc TEXT;")

    # Latest view (recreate to ensure correct definition)
    conn.execute("DROP VIEW IF EXISTS call_zscores_latest;")
    conn.execute(
        """
        CREATE VIEW call_zscores_latest AS
        WITH ranked AS (
            SELECT
                *,
                ROW_NUMBER() OVER (
                    PARTITION BY ticker
                    ORDER BY
                        CASE WHEN call_date IS NULL THEN 0 ELSE 1 END DESC,
                        call_date DESC,
                        scored_at_utc DESC,
                        loaded_at_utc DESC
                ) AS rn
            FROM call_zscores
        )
        SELECT * FROM ranked WHERE rn = 1;
        """
    )

    # Aggregate tables
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS industry_zscore_dispersion (
            industry_fs TEXT NOT NULL,
            asof_utc TEXT NOT NULL,

            n_tickers INTEGER NOT NULL,

            avg_composite REAL,
            med_composite REAL,

            pct_bullish REAL,
            pct_caut_bullish REAL,
            pct_neutral REAL,
            pct_caut_bearish REAL,
            pct_bearish REAL,

            avg_mpi_z REAL,
            avg_dqi_z REAL,
            avg_ssi_z REAL,
            avg_eri_z REAL,
            avg_lri_z REAL,

            PRIMARY KEY (industry_fs, asof_utc)
        );

        CREATE TABLE IF NOT EXISTS industry_regime (
            industry_fs TEXT NOT NULL,
            asof_utc TEXT NOT NULL,
            regime_badge TEXT NOT NULL,
            rationale TEXT,
            PRIMARY KEY (industry_fs, asof_utc)
        );
        """
    )

    conn.commit()


def stance_from_score(score: Optional[float]) -> str:
    if score is None:
        return "Neutral"
    if score >= 0.75:
        return "Bullish"
    if score >= 0.25:
        return "Cautiously Bullish"
    if score > -0.25:
        return "Neutral"
    if score > -0.75:
        return "Cautiously Bearish"
    return "Bearish"


def compute_composite(
    mpi_z: Optional[float],
    dqi_z: Optional[float],
    ssi_z: Optional[float],
    eri_z: Optional[float],
    lri_z: Optional[float],
) -> Optional[float]:
    num = 0.0
    den = 0.0

    def add(val: Optional[float], w: float):
        nonlocal num, den
        if val is None:
            return
        num += w * float(val)
        den += w

    add(mpi_z, WEIGHTS["mpi_z"])
    add(dqi_z, WEIGHTS["dqi_z"])
    add(ssi_z, WEIGHTS["ssi_z"])
    add(eri_z, WEIGHTS["eri_z"])
    if lri_z is not None:
        add(-float(lri_z), WEIGHTS["lri_z_inverted"])

    if den == 0:
        return None
    return num / den


def update_call_scores(conn: sqlite3.Connection) -> int:
    """
    Compute composite_score + call_stance for every row in call_zscores.
    Uses rowid update (fine because table is local).
    """
    now = utc_now()

    rows = conn.execute(
        """
        SELECT rowid, mpi_z, dqi_z, ssi_z, eri_z, lri_z
        FROM call_zscores;
        """
    ).fetchall()

    updated = 0
    for rowid, mpi_z, dqi_z, ssi_z, eri_z, lri_z in rows:
        score = compute_composite(mpi_z, dqi_z, ssi_z, eri_z, lri_z)
        stance = stance_from_score(score)
        conn.execute(
            """
            UPDATE call_zscores
            SET composite_score=?,
                call_stance=?,
                score_version=?,
                scored_at_utc=?
            WHERE rowid=?;
            """,
            (
                None if score is None else round(score, 6),
                stance,
                SCORE_VERSION,
                now,
                rowid,
            ),
        )
        updated += 1

    conn.commit()
    return updated


def fetch_latest_industry_by_ticker(conn: sqlite3.Connection) -> List[Tuple[str, str]]:
    """
    Returns (ticker, industry_fs) using latest call per ticker from earnings_calls.
    IMPORTANT: uses earnings_calls.industry_factset.
    Ignores NULL/blank call_date to make "latest" deterministic.
    """
    rows = conn.execute(
        """
        WITH latest_calls AS (
          SELECT ticker, MAX(call_date) AS call_date
          FROM earnings_calls
          WHERE ticker IS NOT NULL AND ticker != '' AND ticker != 'UNKNOWN'
            AND call_date IS NOT NULL AND call_date != ''
          GROUP BY ticker
        )
        SELECT e.ticker, e.industry_factset
        FROM earnings_calls e
        JOIN latest_calls lc ON lc.ticker=e.ticker AND lc.call_date=e.call_date
        WHERE e.industry_factset IS NOT NULL AND e.industry_factset != '';
        """
    ).fetchall()
    return [(str(r[0]), str(r[1])) for r in rows]


def rebuild_industry_aggregates(conn: sqlite3.Connection) -> Tuple[int, str]:
    """
    Build industry dispersion + regime badges using latest zscore per ticker.
    Industry membership comes from earnings_calls latest call per ticker.
    """
    now = utc_now()

    # Latest industry per ticker
    ti = fetch_latest_industry_by_ticker(conn)
    if not ti:
        return 0, now

    # Latest zscores per ticker
    zs = conn.execute(
        """
        SELECT ticker, composite_score, call_stance, mpi_z, dqi_z, ssi_z, eri_z, lri_z
        FROM call_zscores_latest;
        """
    ).fetchall()
    zs_map = {str(r[0]): r[1:] for r in zs}

    from collections import defaultdict
    import statistics as stats

    by_ind: Dict[str, List[Tuple]] = defaultdict(list)
    for ticker, industry_fs in ti:
        if ticker in zs_map:
            by_ind[industry_fs].append((ticker, *zs_map[ticker]))

    if not by_ind:
        return 0, now

    conn.execute("DELETE FROM industry_regime")

    for industry_fs, rows in by_ind.items():
        n = len(rows)
        comps = [r[1] for r in rows if r[1] is not None]
        avg_comp = (sum(comps) / len(comps)) if comps else None
        med_comp = stats.median(comps) if comps else None

        def pct(label: str) -> float:
            return round(sum(1 for r in rows if r[2] == label) / n, 6) if n else 0.0

        def avg_metric(idx: int) -> Optional[float]:
            vals = [r[idx] for r in rows if r[idx] is not None]
            return round(sum(vals) / len(vals), 6) if vals else None

        # (ticker, composite_score, call_stance, mpi_z, dqi_z, ssi_z, eri_z, lri_z)
        avg_mpi = avg_metric(3)
        avg_dqi = avg_metric(4)
        avg_ssi = avg_metric(5)
        avg_eri = avg_metric(6)
        avg_lri = avg_metric(7)

        conn.execute(
            """
            INSERT OR REPLACE INTO industry_zscore_dispersion (
              industry_fs, asof_utc, n_tickers,
              avg_composite, med_composite,
              pct_bullish, pct_caut_bullish, pct_neutral, pct_caut_bearish, pct_bearish,
              avg_mpi_z, avg_dqi_z, avg_ssi_z, avg_eri_z, avg_lri_z
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
            """,
            (
                industry_fs, now, n,
                None if avg_comp is None else round(avg_comp, 6),
                None if med_comp is None else round(med_comp, 6),
                pct("Bullish"),
                pct("Cautiously Bullish"),
                pct("Neutral"),
                pct("Cautiously Bearish"),
                pct("Bearish"),
                avg_mpi, avg_dqi, avg_ssi, avg_eri, avg_lri,
            ),
        )

        bull_share = pct("Bullish") + pct("Cautiously Bullish")
        bear_share = pct("Bearish") + pct("Cautiously Bearish")

        if med_comp is None:
            badge = "Neutral"
            rationale = "No composite scores available."
        elif med_comp >= 0.25 and bull_share >= bear_share:
            badge = "Risk-On / Improving"
            rationale = f"Median composite {med_comp:.2f}; bullish share {bull_share:.2f} >= bearish {bear_share:.2f}."
        elif med_comp <= -0.25 and bear_share > bull_share:
            badge = "Risk-Off / Deteriorating"
            rationale = f"Median composite {med_comp:.2f}; bearish share {bear_share:.2f} > bullish {bull_share:.2f}."
        else:
            badge = "Mixed / Transitional"
            rationale = f"Median composite {med_comp:.2f}; bull {bull_share:.2f} vs bear {bear_share:.2f}."

        conn.execute(
            """
            INSERT OR REPLACE INTO industry_regime (industry_fs, asof_utc, regime_badge, rationale)
            VALUES (?, ?, ?, ?);
            """,
            (industry_fs, now, badge, rationale),
        )

    conn.commit()
    return len(by_ind), now


def main() -> None:
    conn = sqlite3.connect(DB)
    ensure_schema(conn)

    n = update_call_scores(conn)
    print(f"OK: updated composite_score + call_stance for {n} call_zscores rows")

    n_ind, asof = rebuild_industry_aggregates(conn)
    print(f"OK: wrote industry dispersion + regime for {n_ind} industries (asof_utc={asof})")

    conn.close()
    print("DONE")


if __name__ == "__main__":
    main()


