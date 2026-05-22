# scripts/ingest_pdfs.py
from __future__ import annotations

# --- ensure project root is on PYTHONPATH ---
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))
# ------------------------------------------

import os
import re
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv

from src.db import connect, init_db, filehash_exists, insert_earnings_call
from src.text_extract import sha256_file, extract_text


def canonical_industry(raw: str | None) -> str | None:
    """
    Convert a source industry label into a lowercase canonical key.
    Example: 'HOMEBUILDING' -> 'homebuilding'
    """
    if not raw:
        return None
    return raw.strip().lower().replace("&", "and").replace(" ", "_")


# --- Ticker patterns ---
TICKER_PAREN_RE = re.compile(r"\(([A-Z]{1,6})\)")
TICKER_US_RE = re.compile(r"([A-Z]{1,6})US\b")  # no leading \b

# --- Quarter/year patterns ---
QY_RE = re.compile(r"\bQ([1-4])\s*[-_ ]?\s*(20\d{2})\b", re.IGNORECASE)

# --- Date patterns ---
DATE_ISO_RE = re.compile(r"\b(20\d{2})[-/](\d{1,2})[-/](\d{1,2})\b")
DATE_DMY_RE = re.compile(r"\b(\d{1,2})[- ]([A-Za-z]{3})[- ](20\d{2})\b")
DATE_LONG_RE = re.compile(
    r"\b(\d{1,2})(January|February|March|April|May|June|July|August|September|October|November|December)(20\d{2})\b",
    re.IGNORECASE,
)

MONTH_MAP = {
    "jan": "01", "feb": "02", "mar": "03", "apr": "04",
    "may": "05", "jun": "06", "jul": "07", "aug": "08",
    "sep": "09", "oct": "10", "nov": "11", "dec": "12",
}


def parse_ticker_from_filename(stem: str) -> str:
    m = TICKER_PAREN_RE.search(stem)
    if m:
        return m.group(1).upper()
    m = TICKER_US_RE.search(stem)
    if m:
        return m.group(1).upper()
    return "UNKNOWN"


def parse_date_from_filename(stem: str) -> str | None:
    m = DATE_ISO_RE.search(stem)
    if m:
        yyyy, mm, dd = m.group(1), m.group(2).zfill(2), m.group(3).zfill(2)
        return f"{yyyy}-{mm}-{dd}"

    m = DATE_DMY_RE.search(stem)
    if m:
        dd = m.group(1).zfill(2)
        mon = MONTH_MAP.get(m.group(2).lower()[:3])
        yyyy = m.group(3)
        if mon:
            return f"{yyyy}-{mon}-{dd}"

    m = DATE_LONG_RE.search(stem)
    if m:
        dd = m.group(1).zfill(2)
        mon3 = m.group(2)[:3].lower()
        yyyy = m.group(3)
        mon = MONTH_MAP.get(mon3)
        if mon:
            return f"{yyyy}-{mon}-{dd}"

    return None


def parse_qy_from_filename(stem: str) -> tuple[int | None, int | None]:
    m = QY_RE.search(stem)
    if not m:
        return None, None
    return int(m.group(1)), int(m.group(2))


def classify_event_type(text: str | None) -> str:
    """
    Header-based event classifier.
    Looks only at the first ~1200 chars to avoid false positives from body text.
    Returns:
      - 'earnings_call'
      - 'non_earnings'
    """
    if not text:
        return "earnings_call"

    head = " ".join(text[:1200].lower().split())

    # Clear non-earnings / special events
    non_markers = [
        "investor day",
        "fireside chat",
        "annual meeting",
        "shareholder meeting",
        "special call",
        "investment conference",
        "global conference",
        "conference presentation",
        "acquisition of ",
        "announced the acquisition",
        "announced an acquisition",
        "merger agreement",
        "definitive agreement",
        "business update call",
        "strategic update",
    ]

    # Clear earnings markers
    earnings_markers = [
        "earnings call",
        "results conference call",
        "quarter results",
        "fourth quarter",
        "first quarter",
        "second quarter",
        "third quarter",
        "q1",
        "q2",
        "q3",
        "q4",
    ]

    is_earnings = any(m in head for m in earnings_markers)
    is_non = any(m in head for m in non_markers)

    # If header clearly indicates earnings, treat as earnings even if it mentions acquisitions, etc.
    if is_earnings:
        return "earnings_call"

    # If header indicates a special event and does not look like earnings, treat as non-earnings
    if is_non:
        return "non_earnings"

    # Default: earnings_call (keeps coverage; you can tighten to 'non_earnings' if you prefer)
    return "earnings_call"


def ensure_factset_columns(conn) -> None:
    """
    Ensures the FactSet enrichment columns exist on earnings_calls.
    Your DB uses:
      - sector_factset
      - industry_factset
      - mkt_value_factset
      - asof_date_factset
    """
    cols = [r[1] for r in conn.execute("PRAGMA table_info(earnings_calls)").fetchall()]
    stmts = []

    if "sector_factset" not in cols:
        stmts.append("ALTER TABLE earnings_calls ADD COLUMN sector_factset TEXT")
    if "industry_factset" not in cols:
        stmts.append("ALTER TABLE earnings_calls ADD COLUMN industry_factset TEXT")
    if "mkt_value_factset" not in cols:
        stmts.append("ALTER TABLE earnings_calls ADD COLUMN mkt_value_factset REAL")
    if "asof_date_factset" not in cols:
        stmts.append("ALTER TABLE earnings_calls ADD COLUMN asof_date_factset TEXT")

    for s in stmts:
        conn.execute(s)
    if stmts:
        conn.commit()


def enrich_call_from_factset(conn, call_id: int, ticker: str) -> None:
    """
    Updates earnings_calls row with sector/industry/mkt_value from latest FactSet snapshot.
    Also sets earnings_calls.industry to a lowercase canonical key derived from FactSet industry.
    If snapshot not present or ticker not found, silently does nothing.
    """
    exists = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='universe_factset_snapshot' LIMIT 1;"
    ).fetchone()
    if not exists:
        return

    snap_date = conn.execute("SELECT MAX(asof_date) FROM universe_factset_snapshot").fetchone()[0]
    if not snap_date:
        return

    row = conn.execute(
        """
        SELECT sector, industry, mkt_value
        FROM universe_factset_snapshot
        WHERE asof_date=? AND ticker=?
        """,
        (snap_date, ticker),
    ).fetchone()
    if not row:
        return

    sector_raw, industry_raw, mkt_value = row
    industry_key = canonical_industry(industry_raw)

    conn.execute(
        """
        UPDATE earnings_calls
        SET
            sector_factset=?,
            industry_factset=?,
            industry=?,
            mkt_value_factset=?,
            asof_date_factset=?
        WHERE id=?;
        """,
        (sector_raw, industry_raw, industry_key, mkt_value, snap_date, call_id),
    )


def event_exists(conn, ticker: str, call_date: str | None, event_type: str) -> bool:
    """
    Event dedupe check based on (ticker, call_date, event_type).
    If call_date is missing, we can't event-dedupe safely here (file_hash still dedupes).
    """
    if not ticker or ticker == "UNKNOWN" or not call_date:
        return False
    r = conn.execute(
        """
        SELECT 1
        FROM earnings_calls
        WHERE ticker=? AND call_date=? AND event_type=?
        LIMIT 1;
        """,
        (ticker, call_date, event_type),
    ).fetchone()
    return r is not None


def main() -> None:
    load_dotenv()

    db_path = os.getenv("EARNINGS_DB_PATH", "./data/earnings.db")
    raw_dir = Path(os.getenv("RAW_PDF_DIR", "./data/raw_pdfs"))
    processed_dir = Path(os.getenv("PROCESSED_DIR", "./data/processed"))

    default_industry = os.getenv("DEFAULT_INDUSTRY", "unclassified").strip().lower()

    raw_dir.mkdir(parents=True, exist_ok=True)
    processed_dir.mkdir(parents=True, exist_ok=True)

    conn = connect(db_path)
    init_db(conn)

    ensure_factset_columns(conn)

    pdfs = sorted(raw_dir.glob("*.pdf"))
    if not pdfs:
        print(f"No PDFs found in: {raw_dir}")
        return

    utc_now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    ingested = skipped_hash = skipped_event = errors = 0

    with conn:
        for pdf in pdfs:
            try:
                file_hash = sha256_file(pdf)

                # 1) Fast dedupe: identical file already ingested
                if filehash_exists(conn, file_hash):
                    skipped_hash += 1
                    pdf.replace(processed_dir / pdf.name)
                    print(f"SKIP (duplicate filehash): {pdf.name}")
                    continue

                stem = pdf.stem
                ticker = parse_ticker_from_filename(stem)
                call_date = parse_date_from_filename(stem)
                fiscal_quarter, fiscal_year = parse_qy_from_filename(stem)

                text = extract_text(pdf)
                text_char_count = len(text or "")

                event_type = classify_event_type(text)

                # 2) Event dedupe: same ticker/date/type already in DB (even if file differs)
                if event_exists(conn, ticker, call_date, event_type=event_type):
                    skipped_event += 1
                    pdf.replace(processed_dir / pdf.name)
                    print(
                        f"SKIP (duplicate event): {pdf.name} "
                        f"ticker={ticker} date={call_date} type={event_type}"
                    )
                    continue

                call_id = insert_earnings_call(
                    conn,
                    ticker=ticker,
                    company=None,
                    industry=default_industry,
                    call_date=call_date,
                    fiscal_year=fiscal_year,
                    fiscal_quarter=fiscal_quarter,
                    source="pdf",
                    source_path=str(pdf),
                    file_hash=file_hash,
                    raw_text=text,
                    created_at_utc=utc_now,
                    event_type=event_type,
                )

                # Ensure text_char_count is set
                conn.execute(
                    "UPDATE earnings_calls SET text_char_count=? WHERE id=?",
                    (text_char_count, call_id),
                )

                # Enrich with FactSet
                if ticker != "UNKNOWN":
                    enrich_call_from_factset(conn, call_id, ticker)

                pdf.replace(processed_dir / pdf.name)
                ingested += 1
                print(
                    f"OK: {pdf.name} -> earnings_calls.id={call_id} "
                    f"ticker={ticker} date={call_date} type={event_type}"
                )

            except Exception as e:
                errors += 1
                print(f"ERROR processing {pdf.name}: {e}")

    print("====")
    print(f"Ingested: {ingested}")
    print(f"Skipped (duplicate filehash): {skipped_hash}")
    print(f"Skipped (duplicate event): {skipped_event}")
    print(f"Errors: {errors}")
    print(f"DB: {db_path}")


if __name__ == "__main__":
    main()

    
# Run:
#   .\.venv\Scripts\python.exe -m scripts.ingest_pdfs
