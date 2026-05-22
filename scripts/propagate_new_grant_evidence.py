"""
Propagate newly-classified grant evidence (from the 203 newly-added foundation EINs)
into the company_stance_investigation table.

Logic:
  - Aggregate per-ticker totals across reform-side and pro-police-side classifications
  - For tickers ALREADY in company_stance_investigation:
      update anti_police_action / pro_police_action flags + types + current_status
      ONLY if the existing flags are 0 and the new evidence is material (>= $25K)
  - For tickers NOT YET in company_stance_investigation:
      INSERT new rows with the grant-derived flags
  - The rest of the column data is best-effort populated from the grant data;
    Claude-derived rationale is NOT generated here (that's a separate audit pass)

Materiality thresholds:
  reform side material:  >= $50K total reform-side grants in last 3 years
  pro side material:     >= $50K total pro-police grants in last 3 years
  small-but-recurring:   >= $25K AND >= 3 grants
"""
from __future__ import annotations

import csv
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.config import RISK_DB_PATH
from src.risk_schema import connect

REFORM_CLASSES = (
    'reform_advocacy_grants',
    'anti_police_adversarial',
    'broad_cj_reform',
    'collaborative_reform',
    'reentry_employment',
    'innocence_wrongful_conviction',
)
PRO_CLASSES = ('pro_police',)

# Map classification to anti_police_type (matches scoring weights)
ANTI_TYPE_MAP = {
    'anti_police_adversarial':      'reform_advocacy_grants',
    'reform_advocacy_grants':       'reform_advocacy_grants',
    'broad_cj_reform':              'donations_broad_criminal_justice_reform',
    'collaborative_reform':         'donations_collaborative_reform',
    'reentry_employment':           'donations_reentry_employment',
    'innocence_wrongful_conviction':'donations_innocence_wrongful_conviction',
}


def main() -> None:
    conn = connect(RISK_DB_PATH)

    # Load the 203 newly-added EINs
    new_eins = set()
    with open('data/reference/foundation_candidates_v2.csv', encoding='utf-8') as f:
        for r in csv.DictReader(f):
            if r['tier'] in ('A1', 'A2', 'A3'):
                new_eins.add(r['ein_dashed'].replace('-', ''))
    print(f'Newly-added EINs: {len(new_eins)}')

    # Aggregate per-ticker grant totals from those EINs
    print('\nAggregating new grant evidence by ticker...')
    agg = defaultdict(lambda: {
        'reform_total': 0.0,
        'reform_n': 0,
        'pro_total': 0.0,
        'pro_n': 0,
        'top_reform_cls': defaultdict(float),
        'top_pro_cls': defaultdict(float),
        'reform_recipients': set(),
        'pro_recipients': set(),
        'years': set(),
        'foundation_name': '',
        'company_name': '',
    })

    rows = conn.execute(f"""
        SELECT df.donor_ticker AS ticker, df.foundation_name, df.donor_company,
               g.grantee_classification, g.recipient_name, g.grant_amount, g.tax_year
        FROM foundation_grants g JOIN donor_foundations df ON df.ein = g.donor_ein
        WHERE g.donor_ein IN ({','.join('?' * len(new_eins))})
          AND g.grantee_classification IN ({','.join('?' * len(REFORM_CLASSES + PRO_CLASSES))})
          AND g.grant_amount > 0;
    """, list(new_eins) + list(REFORM_CLASSES + PRO_CLASSES)).fetchall()

    for r in rows:
        t = r['ticker']
        if t.startswith('DAF_'):
            continue
        agg[t]['foundation_name'] = r['foundation_name']
        agg[t]['company_name'] = r['donor_company']
        agg[t]['years'].add(r['tax_year'])
        amt = r['grant_amount']
        cls = r['grantee_classification']
        if cls in REFORM_CLASSES:
            agg[t]['reform_total'] += amt
            agg[t]['reform_n'] += 1
            agg[t]['top_reform_cls'][cls] += amt
            agg[t]['reform_recipients'].add(r['recipient_name'])
        elif cls in PRO_CLASSES:
            agg[t]['pro_total'] += amt
            agg[t]['pro_n'] += 1
            agg[t]['top_pro_cls'][cls] += amt
            agg[t]['pro_recipients'].add(r['recipient_name'])

    print(f'Tickers with new evidence: {len(agg)}')

    # Existing investigated tickers
    existing = {r['ticker']: dict(r) for r in conn.execute(
        'SELECT * FROM company_stance_investigation;')}
    print(f'Existing investigations: {len(existing)}\n')

    # Look up sector / sub-industry from sp500_universe
    sectors = {}
    with open('data/reference/sp500_universe.csv', encoding='utf-8') as f:
        for r in csv.DictReader(f):
            sectors[r['ticker']] = (r.get('sector', ''), r.get('industry', ''))

    # Get the full column list of company_stance_investigation
    cols = [r['name'] for r in conn.execute('PRAGMA table_info(company_stance_investigation);')]
    print(f'company_stance_investigation columns ({len(cols)}): {cols}\n')

    n_updated = 0
    n_inserted = 0
    n_skipped_small = 0
    now_utc = datetime.now(timezone.utc).isoformat(timespec='seconds')

    for ticker, data in sorted(agg.items()):
        # Materiality test
        reform_total = data['reform_total']
        pro_total = data['pro_total']
        reform_material = reform_total >= 50_000 or (reform_total >= 25_000 and data['reform_n'] >= 3)
        pro_material    = pro_total    >= 50_000 or (pro_total    >= 25_000 and data['pro_n']    >= 3)

        if not reform_material and not pro_material:
            n_skipped_small += 1
            continue

        # Pick dominant types
        dom_reform = max(data['top_reform_cls'].items(), key=lambda x: x[1])[0] if data['top_reform_cls'] else None
        dom_pro    = max(data['top_pro_cls'].items(),    key=lambda x: x[1])[0] if data['top_pro_cls']    else None

        anti_type = ANTI_TYPE_MAP.get(dom_reform) if dom_reform and reform_material else None
        pro_type  = 'donates_to_police_foundations'  if dom_pro and pro_material   else None

        # Build evidence summary strings
        reform_evidence = (
            f"Corporate foundation grants ({sorted(data['years'])[-1] if data['years'] else 'recent'}): "
            f"${reform_total/1e3:.1f}K across {data['reform_n']} grants to "
            + ', '.join(sorted(data['reform_recipients'])[:3])
            + (f' (+{len(data["reform_recipients"])-3} more)' if len(data['reform_recipients']) > 3 else '')
        ) if reform_material else None

        pro_evidence = (
            f"Corporate foundation grants ({sorted(data['years'])[-1] if data['years'] else 'recent'}): "
            f"${pro_total/1e3:.1f}K across {data['pro_n']} grants to "
            + ', '.join(sorted(data['pro_recipients'])[:3])
        ) if pro_material else None

        # Net position
        if reform_material and pro_material:
            net = 'cross_exposure'
        elif reform_material:
            net = 'reform_leaning'
        elif pro_material:
            net = 'enforcement_leaning'
        else:
            net = 'no_material_exposure'

        # Status: assume 'maintained' if grants present in 2023 or later, else 'eroded'
        max_yr = max(data['years']) if data['years'] else 0
        status = 'maintained' if max_yr >= 2023 else 'eroded'

        if ticker in existing:
            inv = existing[ticker]
            # Only update if the row currently has no police flags
            if inv['anti_police_action'] == 0 and inv['pro_police_action'] == 0:
                auto_note = (f'[Auto from 990 grants @ {now_utc}] '
                             f'reform=${round(reform_total/1000)}K, pro=${round(pro_total/1000)}K')
                conn.execute("""
                    UPDATE company_stance_investigation
                    SET anti_police_action = ?,
                        anti_police_type = ?,
                        anti_police_current_status = ?,
                        anti_police_summary = COALESCE(NULLIF(anti_police_summary, ''), ?),
                        pro_police_action = ?,
                        pro_police_type = ?,
                        pro_police_current_status = ?,
                        pro_police_summary = COALESCE(NULLIF(pro_police_summary, ''), ?),
                        net_position = ?,
                        notes = COALESCE(notes, '') || char(10) || ?
                    WHERE ticker = ?;
                """, (
                    1 if reform_material else 0,
                    anti_type,
                    status if reform_material else None,
                    reform_evidence,
                    1 if pro_material else 0,
                    pro_type,
                    status if pro_material else None,
                    pro_evidence,
                    net,
                    auto_note,
                    ticker,
                ))
                n_updated += 1
                print(f"  UPDATE {ticker:6s}  net={net:20s}  reform=${reform_total/1e3:>6.1f}K  pro=${pro_total/1e3:>6.1f}K")
        else:
            # Insert new row
            sector, industry = sectors.get(ticker, ('Unknown', 'Unknown'))
            company_name = data['company_name']
            yrs = sorted(data['years'])
            year_range = f"FY{yrs[0]}-FY{yrs[-1]}" if yrs else None
            insert_vals = {
                'ticker': ticker,
                'company_name': company_name,
                'sector': sector,
                'investigated_at_utc': now_utc,
                'anti_police_action':         1 if reform_material else 0,
                'anti_police_type':           anti_type,
                'anti_police_first_year':     yrs[0] if reform_material and yrs else None,
                'anti_police_last_known_date': f"FY{yrs[-1]}" if reform_material and yrs else None,
                'anti_police_summary':        reform_evidence,
                'anti_police_current_status': status if reform_material else None,
                'pro_police_action':          1 if pro_material else 0,
                'pro_police_type':            pro_type,
                'pro_police_first_year':      yrs[0] if pro_material and yrs else None,
                'pro_police_last_known_date': f"FY{yrs[-1]}" if pro_material and yrs else None,
                'pro_police_summary':         pro_evidence,
                'pro_police_current_status':  status if pro_material else None,
                'net_position':               net,
                'net_summary':                f"Auto-derived from {sum(1 for x in [reform_material, pro_material] if x)} side(s) of corporate foundation 990 grant data ({year_range}).",
                'confidence':                 'medium',
                'notes':                      f"[{now_utc}] Auto-inserted from corporate foundation 990 grant evidence. No Claude investigation yet.",
                'n_search_results':           0,
                'policy_stance_score':        None,
            }
            # Build INSERT statement using only columns that exist in the table
            row_cols = [c for c in cols if c in insert_vals]
            placeholders = ','.join('?' * len(row_cols))
            colnames = ','.join(row_cols)
            try:
                conn.execute(
                    f"INSERT INTO company_stance_investigation ({colnames}) VALUES ({placeholders});",
                    tuple(insert_vals[c] for c in row_cols)
                )
                n_inserted += 1
                print(f"  INSERT {ticker:6s}  net={net:20s}  reform=${reform_total/1e3:>6.1f}K  pro=${pro_total/1e3:>6.1f}K  ({company_name})")
            except Exception as e:
                print(f"  ! ERROR inserting {ticker}: {e}")

    conn.commit()

    print(f'\n=== PROPAGATION COMPLETE ===')
    print(f'  Updated existing rows:  {n_updated}')
    print(f'  Inserted new rows:      {n_inserted}')
    print(f'  Skipped (immaterial):   {n_skipped_small}')

    # Final count
    total = conn.execute('SELECT COUNT(*) AS n FROM company_stance_investigation;').fetchone()['n']
    print(f'  company_stance_investigation rows: {total}')

    conn.close()


if __name__ == '__main__':
    main()
