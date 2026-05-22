# scripts/backfill_call_date.py
from __future__ import annotations

import re
import sqlite3
from pathlib import Path

DB = "./data/earnings.db"

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


def parse_date_from_stem(stem: str) -> str | None:
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
        mon = MONTH_MAP.get(m.group(2).lower()[:3])
        yyyy = m.group(3)
        if mon:
            return f"{yyyy}-{mon}-{dd}"

    return None


def main() -> None:
    c = sqlite3.connect(DB)
    c.row_factory = sqlite3.Row

    rows = c.execute(
        "SELECT id, call_date, source_path FROM earnings_calls ORDER BY id;"
    ).fetchall()

    updated = 0
    with c:
        for r in rows:
            if r["call_date"]:
                continue
            stem = Path(r["source_path"]).stem
            d = parse_date_from_stem(stem)
            if d:
                c.execute("UPDATE earnings_calls SET call_date=? WHERE id=?;", (d, r["id"]))
                updated += 1

    print("call_date updated:", updated)
    c.close()


if __name__ == "__main__":
    main()


