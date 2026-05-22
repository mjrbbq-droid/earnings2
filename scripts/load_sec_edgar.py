"""
Lightweight SEC EDGAR scan — for each ticker, find filings containing
police / EEOC / FCC / racial-justice keywords via the efts.sec.gov
full-text search. Save hits with link to filing.

Free, no API key. EDGAR rate limit: 10 req/sec (we use 5).
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.config import RISK_DB_PATH
from src.risk_db import utcnow
from src.risk_schema import connect, init_db

EFTS = "https://efts.sec.gov/LATEST/search-index"
COMPANY_TICKERS = "https://www.sec.gov/files/company_tickers.json"
HEADERS = {"User-Agent": "EarningsRisk research@example.com"}


def load_cik_map() -> dict[str, str]:
    """Ticker -> 10-digit CIK string."""
    import requests as _req
    r = _req.get(COMPANY_TICKERS, headers=HEADERS, timeout=30)
    data = r.json()
    out = {}
    for v in data.values():
        out[v["ticker"].upper()] = f"{int(v['cik_str']):010d}"
    return out

KEYWORDS = [
    ("defund police",          "defund_police"),
    ("FCC license review",     "fcc_license_review"),
    ("EEOC investigation",     "eeoc_investigation"),
    ("police reform",          "police_reform"),
    ("racial equity",          "racial_equity"),
    ("Black Lives Matter",     "blm_mention"),
    ("criminal justice reform", "cj_reform"),
]


def edgar_search(cik: str, term: str, form_types: list[str] | None = None) -> list[dict]:
    """Full-text search across SEC filings, filtered to a specific CIK."""
    forms = ",".join(form_types or ["10-K", "10-Q", "DEF 14A", "8-K"])
    params = {
        "q":     f'"{term}"',
        "dateRange": "custom",
        "startdt": "2020-01-01",
        "enddt":   "2026-12-31",
        "forms":   forms,
        "ciks":    cik,  # EDGAR honors `ciks` reliably; `ticker` was being ignored
    }
    r = requests.get(EFTS, params=params, headers=HEADERS, timeout=30)
    if not r.ok:
        return []
    try:
        data = r.json()
    except Exception:
        return []
    return (data.get("hits") or {}).get("hits") or []


def main() -> None:
    conn = connect(RISK_DB_PATH)
    init_db(conn)

    targets = conn.execute(
        """
        SELECT ticker, company_name
        FROM company_stance_investigation
        WHERE net_position IN ('anti_police_net','mixed','pro_police_net')
        ORDER BY net_position, ticker;
        """
    ).fetchall()
    print(f"Scanning SEC EDGAR for {len(targets)} tickers × {len(KEYWORDS)} keywords\n")

    # Load ticker->CIK map (EDGAR rejects ticker filter; needs CIK)
    print("Loading SEC ticker->CIK map...")
    ticker_to_cik = load_cik_map()
    print(f"  Loaded {len(ticker_to_cik)} ticker mappings\n")

    # Clear existing signals
    for t in targets:
        conn.execute("DELETE FROM company_sec_signals WHERE ticker = ?;", (t["ticker"],))
    conn.commit()

    n_hits_total = 0
    n_missing_cik = 0
    for i, r in enumerate(targets, 1):
        ticker = r["ticker"]
        # EDGAR uses "BRK-B" as "BRK.B" in some places — try a few variants
        cik = (
            ticker_to_cik.get(ticker.upper())
            or ticker_to_cik.get(ticker.replace("-", ".").upper())
            or ticker_to_cik.get(ticker.replace("-", "").upper())
        )
        if not cik:
            n_missing_cik += 1
            continue

        n_ticker_hits = 0
        for term, term_key in KEYWORDS:
            try:
                hits = edgar_search(cik, term)
            except Exception:
                continue
            for h in hits[:5]:  # top 5 per term per ticker
                src = h.get("_source") or {}
                accession = src.get("adsh", "")
                # Build URL
                accession_clean = accession.replace("-", "")
                form_url = (
                    f"https://www.sec.gov/Archives/edgar/data/{cik}/{accession_clean}/"
                    f"{accession}-index.htm"
                ) if cik and accession else ""
                conn.execute(
                    """
                    INSERT INTO company_sec_signals
                        (ticker, cik, form_type, filing_date, accession_number,
                         signal_type, keyword_hit, excerpt, filing_url, fetched_at_utc)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
                    """,
                    (
                        ticker, cik, (src.get("forms") or [""])[0],
                        src.get("file_date"), accession,
                        term_key, term,
                        (src.get("display_names") or [""])[0],
                        form_url, utcnow(),
                    ),
                )
                n_ticker_hits += 1
                n_hits_total += 1
            time.sleep(0.2)  # 5 req/sec — well within EDGAR's 10 req/sec limit
        conn.commit()
        if n_ticker_hits:
            print(f"  {i:3d}/{len(targets)}  {ticker:6s}  hits={n_ticker_hits}")

    print(f"\nTotal SEC EDGAR hits: {n_hits_total}\n")

    # Summary
    print("=== Top tickers by SEC mentions ===")
    for r in conn.execute(
        """
        SELECT ticker, COUNT(*) AS n
        FROM company_sec_signals
        GROUP BY ticker
        HAVING COUNT(*) > 0
        ORDER BY n DESC LIMIT 30;
        """
    ):
        print(f"  {r['ticker']:6s}  n={r['n']}")

    conn.close()


if __name__ == "__main__":
    main()
