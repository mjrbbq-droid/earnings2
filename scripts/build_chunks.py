# scripts/build_chunks.py
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
import sqlite3
from datetime import datetime, timezone

from dotenv import load_dotenv

from src.db import connect, init_db, fetch_calls_without_chunks, insert_chunk


# ---------------------------
# Transcript cleaning (FactSet / CallStreet)
# ---------------------------

# Remove common boilerplate lines that appear in headers and sometimes mid-transcript.
# Keep this conservative: only drop lines that are very unlikely to be actual call content.
JUNK_LINE_RE = re.compile(
    r"^\s*("
    r"(corrected|preliminary|final|edited)\s+transcript|"
    r"event\s+transcript|"
    r"total\s+pages(\s*:\s*\d+|\s+\d+)?|"
    r"copyright\s*Â©|"
    r"1-877-FACTSET|"
    r"www\.callstreet\.com|"
    r"factset\s+callstreet|"
    r"callstreet|"
    r"^\d+\s*$"  # standalone page number lines
    r")\b.*$",
    re.IGNORECASE,
)

# Drop lines like: "Beazer Homes USA, Inc. (BZH)" that are repeated page headers
TICKER_HEADER_RE = re.compile(r"^\s*.*\([A-Z]{1,6}\)\s*$")

# Drop repeated call header lines like: "Q1 2023 Earnings Call"
EARNINGS_CALL_HEADER_RE = re.compile(r"^\s*Q[1-4]\s+\d{4}\s+Earnings\s+Call\b.*$", re.IGNORECASE)

# Drop date header lines like: "02-Feb-2023" (keep only if they are part of a sentence)
DATE_HEADER_RE = re.compile(r"^\s*\d{1,2}-[A-Za-z]{3}-\d{4}\s*$")


def clean_transcript_text(text: str) -> str:
    """
    Remove boilerplate lines while preserving real content.
    """
    lines = text.splitlines()
    out: list[str] = []

    for ln in lines:
        s = ln.strip()
        if not s:
            out.append("")
            continue

        # obvious junk
        if JUNK_LINE_RE.match(s):
            continue

        # repeated header lines in FactSet PDFs
        if TICKER_HEADER_RE.match(s):
            continue
        if EARNINGS_CALL_HEADER_RE.match(s):
            continue
        if DATE_HEADER_RE.match(s):
            continue

        out.append(ln)

    cleaned = "\n".join(out)
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
    return cleaned.strip()


# ---------------------------
# DB safety / uniqueness
# ---------------------------

def ensure_chunk_uniqueness(conn: sqlite3.Connection) -> None:
    """
    Prevent duplicate chunks on reruns.
    Canonical chunk table is earnings_chunks (per your DB schema).
    """
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS ux_chunks_call_index "
        "ON earnings_chunks(earnings_call_id, chunk_index);"
    )
    conn.commit()


# ---------------------------
# Main
# ---------------------------

def main() -> None:
    load_dotenv()

    db_path = os.getenv("EARNINGS_DB_PATH", "./data/databases/earnings.db")
    conn = connect(db_path)
    init_db(conn)

    ensure_chunk_uniqueness(conn)

    calls = fetch_calls_without_chunks(conn)
    if not calls:
        print("No calls found without chunks. Nothing to do.")
        conn.close()
        return

    # Import here (keeps startup fast)
    from src.chunking import split_prepared_vs_qa, chunk_by_speaker_lines

    utc_now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    total_chunks = 0
    calls_chunked = 0
    calls_skipped = 0

    with conn:
        for call_id, raw_text in calls:
            if not raw_text or not str(raw_text).strip():
                calls_skipped += 1
                print(f"SKIP: call_id={call_id} (empty raw_text)")
                continue

            cleaned = clean_transcript_text(str(raw_text))
            if not cleaned:
                calls_skipped += 1
                print(f"SKIP: call_id={call_id} (all text removed as boilerplate)")
                continue

            prepared, qa = split_prepared_vs_qa(cleaned)
            chunk_index = 0

            # Prepared
            for speaker, txt in chunk_by_speaker_lines(prepared, default_speaker="UNKNOWN"):
                txt = (txt or "").strip()
                if not txt:
                    continue
                insert_chunk(
                    conn,
                    earnings_call_id=call_id,
                    chunk_index=chunk_index,
                    section="prepared",
                    speaker=speaker,
                    text=txt,
                    created_at_utc=utc_now,
                )
                chunk_index += 1
                total_chunks += 1

            # Q&A
            for speaker, txt in chunk_by_speaker_lines(qa, default_speaker="UNKNOWN"):
                txt = (txt or "").strip()
                if not txt:
                    continue
                insert_chunk(
                    conn,
                    earnings_call_id=call_id,
                    chunk_index=chunk_index,
                    section="qa",
                    speaker=speaker,
                    text=txt,
                    created_at_utc=utc_now,
                )
                chunk_index += 1
                total_chunks += 1

            calls_chunked += 1
            print(f"OK: call_id={call_id} chunks={chunk_index}")

    conn.close()

    print("====")
    print(f"Calls chunked: {calls_chunked}")
    print(f"Calls skipped (empty/boilerplate): {calls_skipped}")
    print(f"Chunks written: {total_chunks}")
    print(f"DB: {db_path}")


if __name__ == "__main__":
    main()

#                  .\.venv\Scripts\python.exe -m scripts.build_chunks