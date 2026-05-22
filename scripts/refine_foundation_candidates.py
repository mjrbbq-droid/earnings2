"""
Stricter re-scan of IRS indices to find corporate foundations for S&P 500 companies.
Filters out:
  - Family foundations (DD Chichester duPont, DHR Danaher Lynch, HSY Hershey Family)
  - Geographic false positives (AAPL "Apple Valley", HIG "Hartford Marathon")
  - Wrong-company matches (GLW "Owens Corning", HPQ "HP Baptist School")
  - Patient assistance / employee assistance funds (these are real but not the
    primary corporate foundation we want; we'll add them as secondary EINs later)
"""
from __future__ import annotations
import csv
import re
from pathlib import Path
from collections import defaultdict


def short_name(name: str) -> str:
    n = name
    for s in [' Inc.', ' Inc', ' Corporation', ' Corp.', ' Corp', ' Co.',
              ' Companies', ' Company', ' Holdings', ' Group', ' (The)',
              ' Plc', ' Ltd', ' & Co']:
        n = n.replace(s, '')
    return n.strip().upper()


SKIP_NAMES = {'AMERICAN', 'NATIONAL', 'NEW', 'FIRST', 'UNITED', 'GLOBAL', 'GENERAL', 'TRUE'}
FOUNDATION_KW = ['FOUNDATION', 'CHARITABLE', 'GIVING', 'CARES', 'PHILANTHROP',
                 'IMPACT FUND', 'COMMUNITY FUND']

FALSE_POSITIVE_MARKERS = [
    'FAMILY FOUNDATION',
    'PATIENT ASSISTANCE', 'EMPLOYEE ASSISTANCE', 'EMPLOYEE RELIEF',
    'CATASTROPHIC ASSISTANCE', 'EMPLOYEE CATASTROPHIC',
    'SCHOLARSHIP FOUNDATION', 'BAPTIST', 'METHODIST', 'CATHOLIC',
    'MARATHON FOUNDATION', 'LIBRARY FOUNDATION', 'MUSEUM FOUNDATION',
    'PUBLIC SCHOOL', 'SCHOOL FOUNDATION', 'SCHOOL SCHOLARSHIP',
    'CHILDHOOD CANCER FOUNDATION',
    'DISASTER RELIEF FUND',
    'HERITAGE FOUNDATION',
    'VALLEY FOUNDATION',
    'AGRICULTURE FOUNDATION',
    'WARRIORS', 'QUIET WATERS',
    'TEAMSTER',
    'TARGET RANGE',
    'POPOCATEPETL',
    'C/O CIENA HEALTHCARE',
    'PROGRESSIVE AGRICULTURE',
    'CASA ADOBE',
    'HP BAPTIST',
    'OWENS CORNING',
    'TOMMY AND TRUDY',
    'PERRY & DONNA',
    'CHICHESTER DUPONT',
    'DANAHER LYNCH',
    'HERSHEY FAMILY',
    'WEYERHAEUSER FAMILY',
    'BLACHFORD-COOPER',
    'EVEREST EDWIN',
    'JOSEPH AND ANNA GARTNER',
    'AES HAWAII',
    'MAY KAY HOUCK',
    'AFLAC CHILDHOOD',
    'BERKSHIRE HATHAWAY ENERGY',
    'CINTAS DELIVERS',
    'DELIVERS THE BEST',
    # Additional second-pass false positives
    'APA SHERPA',                            # mountaineering, not APA Corp
    'AES CRESTWOOD',                         # MD school district
    'CISCO CENTER',                          # CA theater, not Cisco Systems
    'HARTFORD FOUNDATION FOR PUBLIC',        # CT community fdn, not Hartford Ins
    'POOL CHARITABLE TRUST UMA',             # personal trust
    'FRANCIS POOL',                          # personal trust
    'ALFRED I DUPONT',                       # personal trust (Jacksonville)
    'HISTORICAL FOUNDATION',                 # historical society, not corp
    'EATON LEADERSHIP',                      # leadership prog, not Eaton Corp
    'GE AEROSPACE FOUNDATION',               # need to verify — this is a sub-brand
    'OLD DOMINION FOUNDATION INCORPORATED',  # ODFL is freight, this looks unclear
    'PILOTS CHARITABLE FUND',                # employee-only
]


def score_match(short: str, name: str) -> tuple[str, int, str]:
    name_u = name.upper()
    for fp in FALSE_POSITIVE_MARKERS:
        if fp in name_u:
            return ('REJECT', 0, f'reject:{fp}')
    if re.fullmatch(rf'(THE\s+)?{re.escape(short)}\s+FOUNDATION\s*(INC|INCORPORATED)?\.?', name_u):
        return ('A1', 100, 'exact match')
    if re.match(rf'(THE\s+)?{re.escape(short)}\s+\w*\s*FOUNDATION', name_u):
        return ('A2', 90, 'company + foundation')
    if re.search(rf'\b{re.escape(short)}\s+(CHARITABLE|CHARITY)\s+(TRUST|FOUNDATION)', name_u):
        return ('A3', 85, 'charitable trust/foundation')
    if re.search(rf'\b{re.escape(short)}\s+(GROUP\s+)?(CARES|IMPACT|COMMUNITY)\s+(FUND|FOUNDATION)', name_u):
        return ('B1', 70, 'cares/impact fund')
    if name_u.startswith(short + ' '):
        return ('B2', 60, 'starts with name')
    return ('C', 30, 'weak match')


def main() -> None:
    sp500 = {}
    with open('data/sp500_universe.csv', encoding='utf-8') as f:
        for r in csv.DictReader(f):
            sp500[r['ticker']] = {'name': r['name'], 'sector': r.get('sector', '')}

    have_tickers = set()
    with open('data/donor_foundations.csv', encoding='utf-8') as f:
        for r in csv.DictReader(f):
            have_tickers.add(r['donor_ticker'])

    all_candidates = defaultdict(list)
    for idx in ['data/irs_index/index_2023.csv',
                'data/irs_index/index_2024.csv',
                'data/irs_index/index_2025.csv']:
        if not Path(idx).exists():
            continue
        print(f'Scanning {idx}...')
        with open(idx, encoding='utf-8') as f:
            for row in csv.DictReader(f):
                if row['RETURN_TYPE'] == '990T':
                    continue
                tname = (row['TAXPAYER_NAME'] or '').upper()
                if not any(kw in tname for kw in FOUNDATION_KW):
                    continue
                for ticker, info in sp500.items():
                    if ticker in have_tickers:
                        continue
                    short = short_name(info['name'])
                    if short in SKIP_NAMES or len(short) < 3:
                        continue
                    if not re.search(r'\b' + re.escape(short) + r'\b', tname):
                        continue
                    key = (row['EIN'], tname)
                    if key in [(c['ein'], c['name']) for c in all_candidates[ticker]]:
                        continue
                    tier, score, why = score_match(short, tname)
                    all_candidates[ticker].append({
                        'ein': row['EIN'], 'name': tname,
                        'state': row.get('STATE', ''),
                        'period': row['TAX_PERIOD'], 'rtype': row['RETURN_TYPE'],
                        'tier': tier, 'score': score, 'why': why,
                    })

    final = {}
    for ticker, cands in all_candidates.items():
        non_reject = [c for c in cands if c['tier'] != 'REJECT']
        if not non_reject:
            continue
        best = max(non_reject, key=lambda x: x['score'])
        final[ticker] = best

    tier_counts = defaultdict(int)
    for c in final.values():
        tier_counts[c['tier']] += 1
    print(f'\nFinal accepted: {len(final)} tickers')
    for t in sorted(tier_counts):
        print(f'  Tier {t}: {tier_counts[t]}')

    out_path = Path('data/foundation_candidates_v2.csv')
    with open(out_path, 'w', encoding='utf-8', newline='') as f:
        w = csv.writer(f, lineterminator='\n')
        w.writerow(['ein_dashed', 'foundation_name', 'donor_ticker', 'donor_company',
                    'sector', 'relationship', 'tier', 'score', 'why', 'state', 'notes'])
        for ticker, c in sorted(final.items()):
            comp = sp500[ticker]
            ein_d = f"{c['ein'][:2]}-{c['ein'][2:]}"
            w.writerow([ein_d, c['name'].title(), ticker, comp['name'], comp['sector'],
                        'corporate_foundation', c['tier'], c['score'], c['why'],
                        c['state'], 'auto-flagged from IRS scan'])
    print(f'Saved -> {out_path}\n')

    print('=== TIER A1 (exact pattern) ===')
    for ticker in sorted(t for t, c in final.items() if c['tier'] == 'A1'):
        c = final[ticker]
        print(f"  {ticker:6s}  {sp500[ticker]['name'][:28]:30s} -> EIN {c['ein']}  {c['name'][:50]}")

    print(f'\n=== TIER A2 ===')
    for ticker in sorted(t for t, c in final.items() if c['tier'] == 'A2'):
        c = final[ticker]
        print(f"  {ticker:6s}  {sp500[ticker]['name'][:28]:30s} -> EIN {c['ein']}  {c['name'][:50]}")

    print(f'\n=== TIER A3 ===')
    for ticker in sorted(t for t, c in final.items() if c['tier'] == 'A3'):
        c = final[ticker]
        print(f"  {ticker:6s}  {sp500[ticker]['name'][:28]:30s} -> EIN {c['ein']}  {c['name'][:50]}")

    print(f'\n=== TIER B (needs review) ===')
    for ticker in sorted(t for t, c in final.items() if c['tier'] in ('B1', 'B2')):
        c = final[ticker]
        print(f"  [{c['tier']}] {ticker:6s}  {sp500[ticker]['name'][:28]:30s} -> EIN {c['ein']}  {c['name'][:50]}")

    n_auto = sum(1 for c in final.values() if c['tier'] in ('A1', 'A2', 'A3'))
    n_review = sum(1 for c in final.values() if c['tier'] in ('B1', 'B2'))
    print(f'\n>>> RESULT: {n_auto} tickers auto-addable, {n_review} need manual review')


if __name__ == '__main__':
    main()
