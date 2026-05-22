"""
Initialize institutional_risk.db and load the two seed CSVs:
    data/company_master.csv  -> companies
    data/query_taxonomy.csv  -> query_taxonomy

Idempotent: re-running upserts by primary key / unique key.
"""
from __future__ import annotations

import csv
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.config import DATA_DIR, RISK_DB_PATH
from src.risk_schema import connect, init_db


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def load_companies(conn, csv_path: Path) -> int:
    now = _utcnow()
    n = 0
    with open(csv_path, encoding="utf-8") as f:
        for row in csv.DictReader(f):
            ticker = (row["ticker"] or "").strip().upper()
            if not ticker:
                continue
            conn.execute(
                """
                INSERT INTO companies (ticker, company, sector, status, created_at_utc, updated_at_utc)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(ticker) DO UPDATE SET
                    company        = excluded.company,
                    sector         = excluded.sector,
                    status         = excluded.status,
                    updated_at_utc = excluded.updated_at_utc;
                """,
                (
                    ticker,
                    (row["company"] or "").strip(),
                    (row.get("sector") or "").strip() or None,
                    (row.get("status") or "active").strip(),
                    now,
                    now,
                ),
            )
            n += 1
    conn.commit()
    return n


def load_taxonomy(conn, csv_path: Path) -> tuple[int, int]:
    """
    CSV is source of truth. Upserts rows present in the CSV, and marks any
    taxonomy row whose (category, keyword) is missing from the CSV as active=0.

    Returns (n_upserted, n_deactivated).
    """
    now = _utcnow()
    csv_keys: set[tuple[str, str]] = set()
    n = 0

    with open(csv_path, encoding="utf-8") as f:
        for row in csv.DictReader(f):
            category = (row["category"] or "").strip().lower()
            keyword  = (row["keyword"]  or "").strip()
            if not category or not keyword:
                continue
            csv_keys.add((category, keyword))
            conn.execute(
                """
                INSERT INTO query_taxonomy
                    (category, keyword, severity, stance, notes, active, created_at_utc)
                VALUES (?, ?, ?, ?, ?, 1, ?)
                ON CONFLICT(category, keyword) DO UPDATE SET
                    severity = excluded.severity,
                    stance   = excluded.stance,
                    notes    = excluded.notes,
                    active   = 1;
                """,
                (
                    category,
                    keyword,
                    int(row["severity"]),
                    (row.get("stance") or "").strip() or None,
                    (row.get("notes")  or "").strip() or None,
                    now,
                ),
            )
            n += 1

    # Deactivate rows not in CSV
    n_deact = 0
    existing = conn.execute(
        "SELECT id, category, keyword FROM query_taxonomy WHERE active = 1;"
    ).fetchall()
    for r in existing:
        if (r["category"], r["keyword"]) not in csv_keys:
            conn.execute("UPDATE query_taxonomy SET active = 0 WHERE id = ?;", (r["id"],))
            n_deact += 1

    conn.commit()
    return n, n_deact


def main() -> None:
    db_path = Path(RISK_DB_PATH)
    print(f"DB: {db_path}")
    conn = connect(db_path)
    init_db(conn)
    print("Schema initialized.")

    company_csv  = Path(DATA_DIR) / "company_master.csv"
    taxonomy_csv = Path(DATA_DIR) / "query_taxonomy.csv"

    n_co  = load_companies(conn, company_csv)
    n_tax, n_deact = load_taxonomy(conn, taxonomy_csv)
    print(f"Loaded {n_co} companies, {n_tax} taxonomy rows  (deactivated {n_deact} not in CSV).")

    # Clean stale article_keyword_hits pointing to inactive taxonomy
    cur = conn.execute(
        """
        DELETE FROM article_keyword_hits
        WHERE taxonomy_id IN (SELECT id FROM query_taxonomy WHERE active = 0);
        """
    )
    if cur.rowcount:
        conn.commit()
        print(f"Deleted {cur.rowcount} stale keyword hits tied to inactive taxonomy.")

    # Quick summary
    print("\n--- companies ---")
    for r in conn.execute("SELECT ticker, company, sector, status FROM companies ORDER BY ticker;"):
        print(f"  {r['ticker']:6s}  {r['company']:30s}  {r['sector'] or '':30s}  {r['status']}")

    print("\n--- taxonomy by category ---")
    for r in conn.execute(
        "SELECT category, COUNT(*) AS n, MIN(severity) AS min_s, MAX(severity) AS max_s "
        "FROM query_taxonomy WHERE active = 1 GROUP BY category ORDER BY category;"
    ):
        print(f"  {r['category']:15s}  n={r['n']:3d}  severity {r['min_s']}-{r['max_s']}")

    conn.close()


if __name__ == "__main__":
    main()
