# src/db.py
from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Optional


SCHEMA_SQL = """
PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS earnings_calls (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker           TEXT NOT NULL,
    company          TEXT,
    industry         TEXT,
    call_date        TEXT,           -- ISO date YYYY-MM-DD if known
    fiscal_year      INTEGER,
    fiscal_quarter   INTEGER,        -- 1-4 if known
    source           TEXT NOT NULL,   -- "pdf"
    source_path      TEXT NOT NULL,   -- original filepath at ingest time
    file_hash        TEXT NOT NULL,   -- sha256
    raw_text         TEXT NOT NULL,
    text_char_count  INTEGER NOT NULL DEFAULT 0,
    created_at_utc   TEXT NOT NULL,
    event_type       TEXT NOT NULL DEFAULT 'earnings_call'
);

CREATE UNIQUE INDEX IF NOT EXISTS ux_earnings_calls_filehash
ON earnings_calls(file_hash);

-- One call per (ticker, call_date)
CREATE UNIQUE INDEX IF NOT EXISTS ux_earnings_calls_ticker_calldate
ON earnings_calls(ticker, call_date);

CREATE INDEX IF NOT EXISTS ix_earnings_calls_ticker_date
ON earnings_calls(ticker, call_date);

CREATE TABLE IF NOT EXISTS earnings_chunks (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    earnings_call_id INTEGER NOT NULL,
    chunk_index      INTEGER NOT NULL,
    section          TEXT NOT NULL,   -- "prepared" | "qa"
    speaker          TEXT,
    text             TEXT NOT NULL,
    word_count       INTEGER NOT NULL,
    created_at_utc   TEXT NOT NULL,
    FOREIGN KEY (earnings_call_id) REFERENCES earnings_calls(id) ON DELETE CASCADE
);

CREATE UNIQUE INDEX IF NOT EXISTS ux_chunks_call_index
ON earnings_chunks(earnings_call_id, chunk_index);

CREATE INDEX IF NOT EXISTS ix_chunks_call
ON earnings_chunks(earnings_call_id);

CREATE TABLE IF NOT EXISTS earnings_signatures (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    earnings_call_id INTEGER NOT NULL,
    model            TEXT NOT NULL,
    prompt_version   TEXT NOT NULL,
    signature_json   TEXT NOT NULL,
    created_at_utc   TEXT NOT NULL,
    FOREIGN KEY (earnings_call_id) REFERENCES earnings_calls(id) ON DELETE CASCADE
);

CREATE UNIQUE INDEX IF NOT EXISTS ux_signatures_call_model_prompt
ON earnings_signatures(earnings_call_id, model, prompt_version);
"""


def connect(db_path: str | Path) -> sqlite3.Connection:
    db_path = Path(db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON;")
    conn.execute("PRAGMA busy_timeout = 60000;")  # 60s
    return conn


def _col_exists(conn: sqlite3.Connection, table: str, col: str) -> bool:
    cols = [r["name"] for r in conn.execute(f"PRAGMA table_info({table});").fetchall()]
    return col in cols


def _migrate(conn: sqlite3.Connection) -> None:
    # Ensure event_type exists and backfilled
    if not _col_exists(conn, "earnings_calls", "event_type"):
        conn.execute("ALTER TABLE earnings_calls ADD COLUMN event_type TEXT;")
        conn.execute(
            "UPDATE earnings_calls SET event_type='earnings_call' "
            "WHERE event_type IS NULL OR event_type='';"
        )
        conn.commit()

    # Ensure uniqueness rule exists
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS ux_earnings_calls_ticker_calldate "
        "ON earnings_calls(ticker, call_date);"
    )
    conn.commit()


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA_SQL)
    conn.commit()
    _migrate(conn)


def filehash_exists(conn: sqlite3.Connection, file_hash: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM earnings_calls WHERE file_hash = ? LIMIT 1;",
        (file_hash,),
    ).fetchone()
    return row is not None


def _fetch_existing_call_id(
    conn: sqlite3.Connection,
    *,
    ticker: str,
    call_date: Optional[str],
    file_hash: str,
) -> Optional[int]:
    if ticker and call_date:
        r = conn.execute(
            """
            SELECT id
            FROM earnings_calls
            WHERE ticker=? AND call_date=?
            ORDER BY id DESC
            LIMIT 1;
            """,
            (ticker, call_date),
        ).fetchone()
        if r:
            return int(r["id"])

    r = conn.execute(
        """
        SELECT id
        FROM earnings_calls
        WHERE file_hash=?
        ORDER BY id DESC
        LIMIT 1;
        """,
        (file_hash,),
    ).fetchone()
    return int(r["id"]) if r else None


def insert_earnings_call(
    conn: sqlite3.Connection,
    *,
    ticker: str,
    company: Optional[str],
    industry: Optional[str],
    call_date: Optional[str],
    fiscal_year: Optional[int],
    fiscal_quarter: Optional[int],
    source: str,
    source_path: str,
    file_hash: str,
    raw_text: str,
    created_at_utc: str,
    event_type: str = "earnings_call",
) -> int:
    ticker = (ticker or "").strip().upper()
    event_type = (event_type or "earnings_call").strip().lower()
    text_char_count = len(raw_text or "")

    cur = conn.execute(
        """
        INSERT OR IGNORE INTO earnings_calls
        (ticker, company, industry, call_date, fiscal_year, fiscal_quarter,
         source, source_path, file_hash, raw_text, text_char_count, created_at_utc, event_type)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
        """,
        (
            ticker,
            company,
            industry,
            call_date,
            fiscal_year,
            fiscal_quarter,
            source,
            source_path,
            file_hash,
            raw_text,
            text_char_count,
            created_at_utc,
            event_type,
        ),
    )

    if cur.lastrowid:
        conn.commit()
        return int(cur.lastrowid)

    existing_id = _fetch_existing_call_id(conn, ticker=ticker, call_date=call_date, file_hash=file_hash)
    if existing_id is None:
        raise RuntimeError("Insert ignored but could not find existing earnings_call row.")
    return existing_id


def fetch_calls_without_chunks(conn: sqlite3.Connection) -> list[tuple[int, str]]:
    rows = conn.execute(
        """
        SELECT c.id, c.raw_text
        FROM earnings_calls c
        LEFT JOIN earnings_chunks ch
          ON ch.earnings_call_id = c.id
        WHERE ch.earnings_call_id IS NULL
          AND c.raw_text IS NOT NULL
          AND trim(c.raw_text) != ''
          AND COALESCE(c.event_type, 'earnings_call') = 'earnings_call'
        ORDER BY c.id;
        """
    ).fetchall()
    return [(int(r["id"]), str(r["raw_text"])) for r in rows]


def insert_chunk(
    conn: sqlite3.Connection,
    *,
    earnings_call_id: int,
    chunk_index: int,
    section: str,
    speaker: Optional[str],
    text: str,
    created_at_utc: str,
) -> int:
    txt = (text or "").strip()
    word_count = len(txt.split())
    cur = conn.execute(
        """
        INSERT INTO earnings_chunks
        (earnings_call_id, chunk_index, section, speaker, text, word_count, created_at_utc)
        VALUES (?, ?, ?, ?, ?, ?, ?);
        """,
        (earnings_call_id, chunk_index, section, speaker, txt, word_count, created_at_utc),
    )
    return int(cur.lastrowid)


def fetch_call_with_chunks(conn: sqlite3.Connection, call_id: int) -> dict:
    c = conn.execute("SELECT * FROM earnings_calls WHERE id = ?;", (call_id,)).fetchone()
    if not c:
        raise ValueError(f"Call id not found: {call_id}")

    chunks = conn.execute(
        """
        SELECT chunk_index, section, speaker, text, word_count
        FROM earnings_chunks
        WHERE earnings_call_id = ?
        ORDER BY chunk_index;
        """,
        (call_id,),
    ).fetchall()

    return {"call": dict(c), "chunks": [dict(r) for r in chunks]}


def insert_signature(
    conn: sqlite3.Connection,
    *,
    earnings_call_id: int,
    model: str,
    prompt_version: str,
    signature_json: str,
    created_at_utc: str,
) -> int:
    cur = conn.execute(
        """
        INSERT OR REPLACE INTO earnings_signatures
        (earnings_call_id, model, prompt_version, signature_json, created_at_utc)
        VALUES (?, ?, ?, ?, ?);
        """,
        (earnings_call_id, model, prompt_version, signature_json, created_at_utc),
    )
    return int(cur.lastrowid)