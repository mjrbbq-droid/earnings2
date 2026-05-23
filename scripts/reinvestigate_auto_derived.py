"""
Re-investigate the 17 tickers whose company_stance_investigation rows were
auto-derived from corporate-foundation 990 grant data (no Claude rationale yet).

For each ticker:
  1. Pull its top 990-grant evidence from foundation_grants (top 10 by amount)
  2. Format as a "KNOWN EVIDENCE" block
  3. Call investigate_company(known_evidence=...) — Claude verifies via web_search
     and adds WHO/WHY/WHEN/STATUS narrative + primary-source URLs
  4. Upsert the result back into company_stance_investigation
  5. After all done, rerun policy_stance_score via apply_option_a_and_score
"""
from __future__ import annotations

import sys
import traceback
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.config import RISK_DB_PATH
from src.risk_db import utcnow
from src.risk_schema import connect, init_db
from src.stance_investigation import get_client, investigate_company

AUTO_NOTE_FRAGMENTS = (
    'Auto-inserted from corporate foundation 990',
    'Auto from 990 grants',
)


def build_evidence_block(conn, ticker: str) -> str:
    """Build a structured block of the company's classified 990 grants."""
    rows = conn.execute(
        """
        SELECT g.tax_year, g.grantee_classification, g.recipient_name,
               g.grant_amount, g.grant_purpose, df.foundation_name
        FROM foundation_grants g
        JOIN donor_foundations df ON df.ein = g.donor_ein
        WHERE df.donor_ticker = ?
          AND g.grantee_classification IN ('pro_police', 'collaborative_reform',
              'broad_cj_reform', 'reentry_employment', 'innocence_wrongful_conviction',
              'reform_advocacy_grants', 'anti_police_adversarial')
          AND g.grant_amount > 0
        ORDER BY g.grant_amount DESC
        LIMIT 15;
        """,
        (ticker,),
    ).fetchall()
    if not rows:
        return ""

    fdn_name = rows[0]['foundation_name']
    lines = [f"From {fdn_name} (IRS Form 990 / 990-PF Schedule I):"]
    for r in rows:
        amt = r['grant_amount'] or 0
        purp = (r['grant_purpose'] or '')[:80]
        lines.append(
            f"  FY{r['tax_year']}  {r['grantee_classification']:30s} "
            f"${amt:>10,d}  -> {r['recipient_name'][:50]}"
            + (f"  ({purp})" if purp else "")
        )
    return "\n".join(lines)


def find_auto_derived_tickers(conn) -> list[str]:
    rows = conn.execute(
        f"""
        SELECT ticker FROM company_stance_investigation
        WHERE notes LIKE '%{AUTO_NOTE_FRAGMENTS[0]}%'
           OR notes LIKE '%{AUTO_NOTE_FRAGMENTS[1]}%'
        ORDER BY ticker;
        """
    ).fetchall()
    return [r['ticker'] for r in rows]


def upsert(conn, *, ticker, company_name, sector, result, evidence_block: str, n_search_results: int = 0):
    notes_extra = (
        f"\n[{utcnow()}] Re-investigated with Claude after auto-derivation from 990 grants. "
        f"Evidence hint provided: yes ({len(evidence_block)} chars)."
    )
    conn.execute(
        """
        INSERT INTO company_stance_investigation
            (ticker, company_name, sector, investigated_at_utc,
             anti_police_action, anti_police_type, anti_police_first_date,
             anti_police_first_year, anti_police_last_known_date,
             anti_police_summary, anti_police_current_status,
             anti_police_evidence_url, anti_police_evidence_quote,
             pro_police_action, pro_police_type, pro_police_first_date,
             pro_police_first_year, pro_police_last_known_date,
             pro_police_summary, pro_police_current_status,
             pro_police_evidence_url, pro_police_evidence_quote,
             net_position, net_summary, confidence, notes, n_search_results)
        VALUES (?, ?, ?, ?,
                ?, ?, ?, ?, ?, ?, ?, ?, ?,
                ?, ?, ?, ?, ?, ?, ?, ?, ?,
                ?, ?, ?, ?, ?)
        ON CONFLICT(ticker) DO UPDATE SET
            company_name                  = excluded.company_name,
            sector                        = excluded.sector,
            investigated_at_utc           = excluded.investigated_at_utc,
            anti_police_action            = excluded.anti_police_action,
            anti_police_type              = excluded.anti_police_type,
            anti_police_first_date        = excluded.anti_police_first_date,
            anti_police_first_year        = excluded.anti_police_first_year,
            anti_police_last_known_date   = excluded.anti_police_last_known_date,
            anti_police_summary           = excluded.anti_police_summary,
            anti_police_current_status    = excluded.anti_police_current_status,
            anti_police_evidence_url      = excluded.anti_police_evidence_url,
            anti_police_evidence_quote    = excluded.anti_police_evidence_quote,
            pro_police_action             = excluded.pro_police_action,
            pro_police_type               = excluded.pro_police_type,
            pro_police_first_date         = excluded.pro_police_first_date,
            pro_police_first_year         = excluded.pro_police_first_year,
            pro_police_last_known_date    = excluded.pro_police_last_known_date,
            pro_police_summary            = excluded.pro_police_summary,
            pro_police_current_status     = excluded.pro_police_current_status,
            pro_police_evidence_url       = excluded.pro_police_evidence_url,
            pro_police_evidence_quote     = excluded.pro_police_evidence_quote,
            net_position                  = excluded.net_position,
            net_summary                   = excluded.net_summary,
            confidence                    = excluded.confidence,
            notes                         = excluded.notes,
            n_search_results              = excluded.n_search_results;
        """,
        (
            ticker, company_name, sector, utcnow(),
            1 if result.anti_police_action else 0, result.anti_police_type,
            result.anti_police_first_date, result.anti_police_first_year,
            result.anti_police_last_known_date, result.anti_police_summary,
            result.anti_police_current_status, result.anti_police_evidence_url,
            result.anti_police_evidence_quote,
            1 if result.pro_police_action else 0, result.pro_police_type,
            result.pro_police_first_date, result.pro_police_first_year,
            result.pro_police_last_known_date, result.pro_police_summary,
            result.pro_police_current_status, result.pro_police_evidence_url,
            result.pro_police_evidence_quote,
            result.net_position, result.net_summary, result.confidence,
            (result.notes or '') + notes_extra, n_search_results,
        ),
    )


def main() -> None:
    conn = connect(RISK_DB_PATH)
    init_db(conn)
    client = get_client()

    tickers = find_auto_derived_tickers(conn)
    print(f"Found {len(tickers)} auto-derived tickers to re-investigate:")
    for t in tickers:
        print(f"  {t}")
    print()

    started = datetime.now(timezone.utc)

    for i, ticker in enumerate(tickers, 1):
        existing = conn.execute(
            "SELECT company_name, sector FROM company_stance_investigation WHERE ticker = ?;",
            (ticker,),
        ).fetchone()
        if not existing:
            print(f"  [{i:2d}/{len(tickers)}] {ticker}: row vanished — skipping")
            continue
        company_name = existing['company_name']
        sector = existing['sector']
        evidence_block = build_evidence_block(conn, ticker)
        print(f"--- [{i:2d}/{len(tickers)}] {ticker:6s}  {company_name[:45]:47s} ({sector or '?'}) ---")
        print(f"  Evidence block size: {len(evidence_block)} chars")

        try:
            result = investigate_company(
                client,
                ticker=ticker,
                company_name=company_name,
                sector=sector,
                known_evidence=evidence_block,
            )
        except Exception as e:
            print(f"  ! ERROR {ticker}: {type(e).__name__}: {e}")
            traceback.print_exc()
            print()
            continue

        upsert(conn, ticker=ticker, company_name=company_name, sector=sector,
               result=result, evidence_block=evidence_block)
        conn.commit()

        anti_flag = "A" if result.anti_police_action else "-"
        pro_flag  = "P" if result.pro_police_action  else "-"
        print(f"  [{anti_flag}{pro_flag}] net={result.net_position:22s}  conf={result.confidence:.2f}")
        if result.anti_police_action:
            print(f"      ANTI {result.anti_police_first_date or '???'}  {result.anti_police_type}  ({result.anti_police_current_status})")
            print(f"           {(result.anti_police_summary or '')[:120]}")
            print(f"           src: {result.anti_police_evidence_url}")
        if result.pro_police_action:
            print(f"      PRO  {result.pro_police_first_date or '???'}  {result.pro_police_type}  ({result.pro_police_current_status})")
            print(f"           {(result.pro_police_summary or '')[:120]}")
            print(f"           src: {result.pro_police_evidence_url}")
        print(f"      NET  {result.net_summary[:120] if result.net_summary else ''}")
        print()

    elapsed = (datetime.now(timezone.utc) - started).total_seconds()
    print(f"\nDone in {elapsed:.0f}s.\n")

    conn.close()


if __name__ == "__main__":
    main()
