# src/fmp_schema.py
"""
FMP-specific SQLite tables.
Kept separate from db.py so original pipeline tables stay untouched.
"""
from __future__ import annotations

import sqlite3

FMP_SCHEMA_SQL = """
-- ── Russell 2000 (or any index) constituents ──────────────────────────
CREATE TABLE IF NOT EXISTS fmp_constituents (
    ticker          TEXT NOT NULL,
    company_name    TEXT,
    sector          TEXT,
    sub_sector      TEXT,
    industry        TEXT,
    market_cap      REAL,
    weight          REAL,
    index_name      TEXT NOT NULL DEFAULT 'russell2000',
    cik             TEXT,
    founded         TEXT,
    asof_date       TEXT NOT NULL,          -- date we fetched
    updated_at_utc  TEXT NOT NULL,
    raw_json        TEXT,

    PRIMARY KEY (index_name, ticker, asof_date)
);

CREATE INDEX IF NOT EXISTS ix_fmp_const_ticker   ON fmp_constituents(ticker);
CREATE INDEX IF NOT EXISTS ix_fmp_const_sector   ON fmp_constituents(sector);
CREATE INDEX IF NOT EXISTS ix_fmp_const_industry ON fmp_constituents(industry);

-- convenience: latest snapshot
CREATE VIEW IF NOT EXISTS fmp_constituents_latest AS
SELECT c.*
FROM fmp_constituents c
WHERE c.asof_date = (
    SELECT MAX(asof_date) FROM fmp_constituents WHERE index_name = c.index_name
);


-- ── Earnings estimates (analyst consensus) ────────────────────────────
CREATE TABLE IF NOT EXISTS fmp_earnings_estimates (
    ticker              TEXT NOT NULL,
    date                TEXT NOT NULL,          -- period end date
    fiscal_year         INTEGER,
    fiscal_quarter      INTEGER,
    estimated_revenue   REAL,
    actual_revenue      REAL,
    revenue_surprise    REAL,
    estimated_eps       REAL,
    actual_eps          REAL,
    eps_surprise        REAL,
    number_analysts_est INTEGER,
    updated_at_utc      TEXT NOT NULL,
    raw_json            TEXT,

    PRIMARY KEY (ticker, date)
);

CREATE INDEX IF NOT EXISTS ix_fmp_est_ticker ON fmp_earnings_estimates(ticker);
CREATE INDEX IF NOT EXISTS ix_fmp_est_fy_fq  ON fmp_earnings_estimates(fiscal_year, fiscal_quarter);


-- ── Earnings surprises (actual vs estimate, beat/miss) ────────────────
CREATE TABLE IF NOT EXISTS fmp_earnings_surprises (
    ticker              TEXT NOT NULL,
    date                TEXT NOT NULL,          -- announcement date
    fiscal_ending       TEXT,
    estimated_eps       REAL,
    actual_eps          REAL,
    surprise_abs        REAL,                   -- actual - estimated
    surprise_pct        REAL,                   -- (actual - est) / |est| * 100
    beat_miss           TEXT,                   -- 'beat', 'miss', 'inline'
    updated_at_utc      TEXT NOT NULL,
    raw_json            TEXT,

    PRIMARY KEY (ticker, date)
);

CREATE INDEX IF NOT EXISTS ix_fmp_surp_ticker ON fmp_earnings_surprises(ticker);
CREATE INDEX IF NOT EXISTS ix_fmp_surp_bm     ON fmp_earnings_surprises(beat_miss);


-- ── Transcripts fetched from FMP ──────────────────────────────────────
CREATE TABLE IF NOT EXISTS fmp_transcripts (
    ticker          TEXT NOT NULL,
    call_date       TEXT NOT NULL,
    fiscal_year     INTEGER,
    fiscal_quarter  INTEGER,
    content         TEXT NOT NULL,
    char_count      INTEGER NOT NULL DEFAULT 0,
    updated_at_utc  TEXT NOT NULL,

    PRIMARY KEY (ticker, call_date)
);

CREATE INDEX IF NOT EXISTS ix_fmp_tx_ticker ON fmp_transcripts(ticker);
CREATE INDEX IF NOT EXISTS ix_fmp_tx_fyfq   ON fmp_transcripts(fiscal_year, fiscal_quarter);
""";


def init_fmp_schema(conn: sqlite3.Connection) -> None:
    """Create all FMP tables (idempotent)."""
    conn.executescript(FMP_SCHEMA_SQL)
    conn.commit()
