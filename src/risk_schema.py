# src/risk_schema.py
"""
Schema + connection for institutional_risk.db.

Domain: news + earnings-call signals about activism, public-safety/police
discourse, governance, and other reputational-risk vectors at the company level.

CSV is the ingestion layer; this DB is the master store.
Excel is for review/reporting, not core storage.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path


SCHEMA_SQL = """
PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

-- ── companies ─────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS companies (
    ticker          TEXT PRIMARY KEY,
    company         TEXT NOT NULL,
    sector          TEXT,
    status          TEXT NOT NULL DEFAULT 'active',   -- active | paused | dropped
    created_at_utc  TEXT NOT NULL,
    updated_at_utc  TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS ix_companies_status ON companies(status);

-- ── query taxonomy (the ontology — core IP) ───────────────────────────
CREATE TABLE IF NOT EXISTS query_taxonomy (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    category        TEXT NOT NULL,                    -- police | activism | governance | public_safety | incident | ...
    keyword         TEXT NOT NULL,                    -- canonical phrase
    severity        INTEGER NOT NULL,                 -- 1-5 base weight
    stance          TEXT,                             -- activism | positive_institutional | neutral
    notes           TEXT,
    active          INTEGER NOT NULL DEFAULT 1,
    created_at_utc  TEXT NOT NULL,
    UNIQUE(category, keyword)
);

CREATE INDEX IF NOT EXISTS ix_taxonomy_category ON query_taxonomy(category);
CREATE INDEX IF NOT EXISTS ix_taxonomy_active   ON query_taxonomy(active);

-- ── articles (raw ingestion landing) ──────────────────────────────────
CREATE TABLE IF NOT EXISTS articles (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker           TEXT,                            -- nullable; some sources don't tag
    company          TEXT,
    published        TEXT,                            -- ISO timestamp
    title            TEXT NOT NULL,
    url              TEXT,
    domain           TEXT,
    source           TEXT,                            -- publisher
    snippet          TEXT,
    source_country   TEXT,
    query_type       TEXT,                            -- which query produced this row
    source_api       TEXT NOT NULL,                   -- fmp_general | fmp_stock | fmp_press | gdelt | newsapi | ...
    raw_path         TEXT,                            -- pointer to source CSV in data/raw_articles/
    url_hash         TEXT,                            -- sha256(url) for dedup
    title_hash       TEXT,                            -- sha256(title.lower())
    ingested_at_utc  TEXT NOT NULL
    -- No FK on ticker: articles may reference tickers outside company_master.
    -- company_master is the watchlist, not the universe of valid tickers.
);

CREATE UNIQUE INDEX IF NOT EXISTS ux_articles_url_hash
    ON articles(url_hash) WHERE url_hash IS NOT NULL;
CREATE INDEX IF NOT EXISTS ix_articles_ticker_pub
    ON articles(ticker, published);
CREATE INDEX IF NOT EXISTS ix_articles_published
    ON articles(published);
CREATE INDEX IF NOT EXISTS ix_articles_source_api
    ON articles(source_api);

-- ── keyword hits (links articles ↔ taxonomy) ──────────────────────────
CREATE TABLE IF NOT EXISTS article_keyword_hits (
    article_id      INTEGER NOT NULL,
    taxonomy_id     INTEGER NOT NULL,
    matched_text    TEXT,
    PRIMARY KEY (article_id, taxonomy_id),
    FOREIGN KEY (article_id)  REFERENCES articles(id)         ON DELETE CASCADE,
    FOREIGN KEY (taxonomy_id) REFERENCES query_taxonomy(id)   ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS ix_hits_taxonomy ON article_keyword_hits(taxonomy_id);

-- ── fetched full text (one row per article) ──────────────────────────
CREATE TABLE IF NOT EXISTS article_text (
    article_id        INTEGER PRIMARY KEY,
    url               TEXT NOT NULL,
    fetched_at_utc    TEXT NOT NULL,
    http_status       INTEGER,
    fetch_status      TEXT NOT NULL,        -- success | paywall_or_short | unsupported | error | timeout
    error_msg         TEXT,
    raw_html_size     INTEGER,
    extracted_chars   INTEGER,
    extracted_text    TEXT,
    extractor         TEXT,                 -- 'trafilatura', ...
    extractor_version TEXT,
    fetch_source      TEXT NOT NULL DEFAULT 'origin',  -- 'origin' | 'wayback'
    FOREIGN KEY (article_id) REFERENCES articles(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS ix_article_text_status ON article_text(fetch_status);

-- ── machine scoring (one row per article × model × prompt) ────────────
CREATE TABLE IF NOT EXISTS article_scores (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    article_id          INTEGER NOT NULL,
    model               TEXT NOT NULL,
    prompt_version      TEXT NOT NULL,
    relevance           INTEGER,                       -- 0-100
    stance              TEXT,                          -- activism | positive_institutional | neutral
    severity            INTEGER,                       -- -5 .. +5  (recency-decayed)
    severity_raw        INTEGER,                       -- -5 .. +5  (pre-decay)
    confidence          REAL,                          -- 0-1
    rationale           TEXT,
    -- v3 extraction (populated when Python computes score + Claude writes rationale)
    event_date          TEXT,                          -- ISO date YYYY-MM-DD of underlying event
    infraction_type     TEXT,
    infraction_summary  TEXT,
    context_note        TEXT,
    disagrees_with_score INTEGER,                      -- 0/1 — Claude disagrees with Python
    disagreement_note   TEXT,
    age_days            INTEGER,                       -- days from the date that drove decay (event preferred, else publish)
    recency_factor      REAL,                          -- 0.30 - 1.00 — Python's recency multiplier
    recency_source      TEXT,                          -- 'event' | 'publish' | 'none' — which date drove the decay
    scored_at_utc       TEXT NOT NULL,
    FOREIGN KEY (article_id) REFERENCES articles(id) ON DELETE CASCADE,
    UNIQUE(article_id, model, prompt_version)
);

CREATE INDEX IF NOT EXISTS ix_article_scores_article ON article_scores(article_id);

-- ── rolled-up company scores (snapshots over a window) ────────────────
CREATE TABLE IF NOT EXISTS company_scores (
    id                    INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker                TEXT NOT NULL,
    as_of_date            TEXT NOT NULL,              -- ISO date
    window_days           INTEGER NOT NULL,
    n_articles            INTEGER NOT NULL DEFAULT 0,
    mean_severity         REAL,
    activism_volume       INTEGER NOT NULL DEFAULT 0,
    institutional_volume  INTEGER NOT NULL DEFAULT 0,
    persistence_score     REAL,                       -- how sustained the coverage is
    computed_at_utc       TEXT NOT NULL,
    UNIQUE(ticker, as_of_date, window_days),
    FOREIGN KEY (ticker) REFERENCES companies(ticker) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS ix_company_scores_ticker_date
    ON company_scores(ticker, as_of_date);

-- ── review queue (articles flagged for analyst review) ────────────────
CREATE TABLE IF NOT EXISTS review_queue (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    article_id      INTEGER NOT NULL UNIQUE,
    reason          TEXT,                              -- low_confidence | high_severity | ambiguous_stance | ...
    priority        INTEGER NOT NULL DEFAULT 3,        -- 1 (high) .. 5 (low)
    status          TEXT NOT NULL DEFAULT 'pending',   -- pending | in_review | done | skipped
    assigned_to     TEXT,
    created_at_utc  TEXT NOT NULL,
    FOREIGN KEY (article_id) REFERENCES articles(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS ix_review_queue_status_priority
    ON review_queue(status, priority);

-- ── company-level BALANCED police-stance investigation ───────────────
-- Captures BOTH directions per company:
--   anti_police_*  — defund advocacy, refusing to sell to police, donations
--                    to police-reform NGOs, walked-back partnerships
--   pro_police_*   — selling weapons / FR / body cams / radios to police,
--                    hosting police data, marketing partnerships, donations
--                    to police foundations or unions
-- net_position synthesizes the two.
CREATE TABLE IF NOT EXISTS company_stance_investigation (
    ticker                            TEXT PRIMARY KEY,
    company_name                      TEXT,
    sector                            TEXT,
    investigated_at_utc               TEXT NOT NULL,

    -- ANTI-police
    anti_police_action                INTEGER NOT NULL DEFAULT 0,
    anti_police_type                  TEXT,
    anti_police_first_date            TEXT,
    anti_police_first_year            INTEGER,
    anti_police_last_known_date       TEXT,
    anti_police_summary               TEXT,
    anti_police_current_status        TEXT,
    anti_police_evidence_url          TEXT,
    anti_police_evidence_quote        TEXT,

    -- PRO-police
    pro_police_action                 INTEGER NOT NULL DEFAULT 0,
    pro_police_type                   TEXT,
    pro_police_first_date             TEXT,
    pro_police_first_year             INTEGER,
    pro_police_last_known_date        TEXT,
    pro_police_summary                TEXT,
    pro_police_current_status         TEXT,
    pro_police_evidence_url           TEXT,
    pro_police_evidence_quote         TEXT,

    -- Net synthesis (Option A labels — v4)
    net_position                      TEXT,    -- reform_leaning | enforcement_leaning | cross_exposure | no_material_exposure | unknown
    net_summary                       TEXT,
    confidence                        REAL,
    notes                             TEXT,
    n_search_results                  INTEGER,
    policy_stance_score               REAL     -- v4: signed -5.0..+5.0 (+ = enforcement-leaning, − = reform-leaning)
);

CREATE INDEX IF NOT EXISTS ix_csi_anti  ON company_stance_investigation(anti_police_action);
CREATE INDEX IF NOT EXISTS ix_csi_pro   ON company_stance_investigation(pro_police_action);
CREATE INDEX IF NOT EXISTS ix_csi_net   ON company_stance_investigation(net_position);

-- ── donor foundations (ticker -> 501c3 EIN map) ──────────────────────
CREATE TABLE IF NOT EXISTS donor_foundations (
    ein                TEXT PRIMARY KEY,
    foundation_name    TEXT NOT NULL,
    donor_ticker       TEXT NOT NULL,
    donor_company      TEXT,
    relationship       TEXT,                    -- 'corporate_foundation' | 'separate_charitable_arm' | 'fiscal_sponsor' | 'no_separate_entity'
    notes              TEXT,
    added_at_utc       TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS ix_donor_foundations_ticker ON donor_foundations(donor_ticker);

-- ── 990 filings — one row per foundation × tax year ──────────────────
CREATE TABLE IF NOT EXISTS foundation_filings (
    id                       INTEGER PRIMARY KEY AUTOINCREMENT,
    ein                      TEXT NOT NULL,
    tax_year                 INTEGER NOT NULL,     -- fiscal year end (tax_prd_yr)
    form_type                INTEGER,              -- 0=990, 1=990-EZ, 2=990-PF
    total_revenue            INTEGER,
    total_expenses           INTEGER,
    total_grants_paid        INTEGER,              -- THE headline number for 'still active?'
    pdf_url                  TEXT,
    raw_json                 TEXT,                  -- full ProPublica row for audit
    fetched_at_utc           TEXT NOT NULL,
    UNIQUE(ein, tax_year),
    FOREIGN KEY (ein) REFERENCES donor_foundations(ein) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS ix_foundation_filings_ein_year
    ON foundation_filings(ein, tax_year);

-- ── Schedule I grant detail — populated later from raw 990 XML ───────
CREATE TABLE IF NOT EXISTS foundation_grants (
    id                       INTEGER PRIMARY KEY AUTOINCREMENT,
    donor_ein                TEXT NOT NULL,
    tax_year                 INTEGER NOT NULL,
    recipient_name           TEXT NOT NULL,
    recipient_ein            TEXT,
    grant_amount             INTEGER,
    grant_purpose            TEXT,
    grantee_classification   TEXT,                  -- 'anti_police_adversarial' | 'collaborative_reform' | 'reentry' | 'innocence' | 'broad_cj_reform' | 'unrelated'
    source                   TEXT,                  -- 'propublica' | 'irs_xml' | 'manual'
    source_filing_url        TEXT,
    fetched_at_utc           TEXT NOT NULL,
    FOREIGN KEY (donor_ein) REFERENCES donor_foundations(ein) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS ix_foundation_grants_donor_year
    ON foundation_grants(donor_ein, tax_year);
CREATE INDEX IF NOT EXISTS ix_foundation_grants_classification
    ON foundation_grants(grantee_classification);

-- ── federal contracts (USAspending.gov) ───────────────────────────────
CREATE TABLE IF NOT EXISTS company_federal_contracts (
    id                      INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker                  TEXT,
    company_name            TEXT,
    recipient_name          TEXT,
    award_id                TEXT,
    awarding_agency         TEXT,
    awarding_sub_agency     TEXT,
    description             TEXT,
    period_start            TEXT,
    period_end              TEXT,
    award_amount            REAL,
    is_le_agency            INTEGER NOT NULL DEFAULT 0,    -- 1 = DOJ/FBI/DEA/ATF/USMS/DHS/ICE/CBP/TSA/USSS/BOP
    fetched_at_utc          TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS ix_fed_contracts_ticker ON company_federal_contracts(ticker);
CREATE INDEX IF NOT EXISTS ix_fed_contracts_le     ON company_federal_contracts(is_le_agency);

-- ── SEC EDGAR filings detection ───────────────────────────────────────
CREATE TABLE IF NOT EXISTS company_sec_signals (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker            TEXT NOT NULL,
    cik               TEXT,
    form_type         TEXT,                    -- '10-K', 'DEF 14A', '8-K'
    filing_date       TEXT,
    accession_number  TEXT,
    signal_type       TEXT,                    -- 'legal_proceedings_hit', 'shareholder_proposal_hit', etc.
    keyword_hit       TEXT,
    excerpt           TEXT,
    filing_url        TEXT,
    fetched_at_utc    TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS ix_sec_signals_ticker ON company_sec_signals(ticker);

-- ── GDELT news mentions ───────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS gdelt_news (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker          TEXT NOT NULL,
    company_name    TEXT,
    query_used      TEXT,
    title           TEXT,
    url             TEXT,
    domain          TEXT,
    seen_date       TEXT,
    language        TEXT,
    sourcecountry   TEXT,
    tone            REAL,
    url_hash        TEXT,
    fetched_at_utc  TEXT NOT NULL,
    UNIQUE(url_hash)
);

CREATE INDEX IF NOT EXISTS ix_gdelt_ticker     ON gdelt_news(ticker);
CREATE INDEX IF NOT EXISTS ix_gdelt_seen_date  ON gdelt_news(seen_date);

-- ── manual reviews (analyst overrides) ────────────────────────────────
CREATE TABLE IF NOT EXISTS manual_reviews (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    article_id      INTEGER NOT NULL,
    reviewer        TEXT,
    relevance       INTEGER,
    stance          TEXT,
    severity        INTEGER,
    notes           TEXT,
    reviewed_at_utc TEXT NOT NULL,
    FOREIGN KEY (article_id) REFERENCES articles(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS ix_manual_reviews_article ON manual_reviews(article_id);
"""


def connect(db_path: str | Path) -> sqlite3.Connection:
    db_path = Path(db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON;")
    conn.execute("PRAGMA busy_timeout = 60000;")
    return conn


def _col_exists(conn: sqlite3.Connection, table: str, col: str) -> bool:
    cols = [r["name"] for r in conn.execute(f"PRAGMA table_info({table});").fetchall()]
    return col in cols


def _migrate(conn: sqlite3.Connection) -> None:
    """Idempotent schema migrations for additions made after first deploy."""
    if not _col_exists(conn, "article_text", "fetch_source"):
        conn.execute(
            "ALTER TABLE article_text ADD COLUMN fetch_source TEXT NOT NULL DEFAULT 'origin';"
        )
        conn.execute(
            "UPDATE article_text SET fetch_source = 'origin' "
            "WHERE fetch_source IS NULL OR fetch_source = '';"
        )
        conn.commit()

    # v4: policy_stance_score on company_stance_investigation (Option A labels)
    if not _col_exists(conn, "company_stance_investigation", "policy_stance_score"):
        conn.execute("ALTER TABLE company_stance_investigation ADD COLUMN policy_stance_score REAL;")
        conn.commit()

    # v3 extraction columns on article_scores
    v3_columns = [
        ("severity_raw",         "INTEGER"),
        ("event_date",           "TEXT"),
        ("infraction_type",      "TEXT"),
        ("infraction_summary",   "TEXT"),
        ("context_note",         "TEXT"),
        ("disagrees_with_score", "INTEGER"),
        ("disagreement_note",    "TEXT"),
        ("age_days",             "INTEGER"),
        ("recency_factor",       "REAL"),
        ("recency_source",       "TEXT"),
    ]
    added = False
    for col, sqltype in v3_columns:
        if not _col_exists(conn, "article_scores", col):
            conn.execute(f"ALTER TABLE article_scores ADD COLUMN {col} {sqltype};")
            added = True
    if added:
        conn.commit()


def init_db(conn: sqlite3.Connection) -> None:
    # Pre-migration: drop legacy company_stance_investigation v1 (had `took_stance` column)
    # so the CREATE INDEX statements in SCHEMA_SQL succeed against the new schema.
    cols = [
        r["name"] for r in
        conn.execute("PRAGMA table_info(company_stance_investigation);").fetchall()
    ]
    if "took_stance" in cols:
        conn.execute("DROP TABLE company_stance_investigation;")
        conn.commit()

    conn.executescript(SCHEMA_SQL)
    conn.commit()
    _migrate(conn)
