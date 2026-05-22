"""
Merge Tier A foundation candidates into the live donor_foundations.csv
and the SQLite donor_foundations table.

Tier A = high-confidence corporate foundation matches from IRS index scan.
Tier B = keep flagged for manual review (saved separately).
"""
from __future__ import annotations
import csv
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.config import RISK_DB_PATH
from src.risk_schema import connect


def main() -> None:
    # Source of truth for "already present" is SQLite (the CSV may have
    # uncommitted entries from a previous failed run).
    conn0 = connect(RISK_DB_PATH)
    db_eins = {r['ein'] for r in conn0.execute('SELECT ein FROM donor_foundations;')}
    conn0.close()
    print(f'SQLite donor_foundations: {len(db_eins)} EINs')

    # Load existing CSV for field structure & to keep older rows
    existing = []
    existing_eins = set()
    with open('data/reference/donor_foundations.csv', encoding='utf-8') as f:
        reader = csv.DictReader(f)
        fieldnames = reader.fieldnames
        for r in reader:
            existing.append(r)
            existing_eins.add(r['ein'].replace('-', ''))

    print(f'CSV donor_foundations:    {len(existing)} rows')
    print(f'Combined known EINs:      {len(db_eins | existing_eins)}\n')
    print(f'Fieldnames: {fieldnames}\n')

    # "Already present" = in SQLite (the live system of record)
    existing_eins = db_eins

    # Load Tier A candidates
    tier_a = []
    tier_b = []
    with open('data/reference/foundation_candidates_v2.csv', encoding='utf-8') as f:
        for r in csv.DictReader(f):
            if r['tier'] in ('A1', 'A2', 'A3'):
                tier_a.append(r)
            elif r['tier'] in ('B1', 'B2'):
                tier_b.append(r)

    print(f'Tier A candidates to merge: {len(tier_a)}')
    print(f'Tier B candidates (deferred): {len(tier_b)}\n')

    # Map candidate fields -> donor_foundations schema
    # Sample existing row to confirm field order
    if existing:
        print('Existing row sample:', existing[0])
        print()

    # Build new rows, skipping any EINs already present
    new_rows = []
    skipped_dupes = 0
    for c in tier_a:
        ein_no_dash = c['ein_dashed'].replace('-', '')
        if ein_no_dash in existing_eins:
            skipped_dupes += 1
            continue
        new_row = {
            'ein': c['ein_dashed'],
            'foundation_name': c['foundation_name'],
            'donor_ticker': c['donor_ticker'],
            'donor_company': c['donor_company'],
            'relationship': c['relationship'],
            'notes': c['notes'],
        }
        # Pad missing fields with empty
        for fn in fieldnames:
            if fn not in new_row:
                new_row[fn] = ''
        new_rows.append(new_row)

    print(f'New rows to add: {len(new_rows)}')
    print(f'Skipped (already present): {skipped_dupes}\n')

    # Write merged CSV
    all_rows = existing + new_rows
    out_path = Path('data/reference/donor_foundations.csv')
    with open(out_path, 'w', encoding='utf-8', newline='') as f:
        w = csv.DictWriter(f, fieldnames=fieldnames, lineterminator='\n')
        w.writeheader()
        for r in all_rows:
            w.writerow(r)
    print(f'Wrote merged CSV: {out_path}  ({len(all_rows)} total rows)\n')

    # Save Tier B separately for manual review
    if tier_b:
        tier_b_path = Path('data/reference/foundation_candidates_tier_b_review.csv')
        with open(tier_b_path, 'w', encoding='utf-8', newline='') as f:
            w = csv.writer(f, lineterminator='\n')
            w.writerow(['ein_dashed','foundation_name','donor_ticker','donor_company','tier','why','notes'])
            for c in tier_b:
                w.writerow([c['ein_dashed'], c['foundation_name'], c['donor_ticker'],
                            c['donor_company'], c['tier'], c['why'], c['notes']])
        print(f'Wrote Tier B review file: {tier_b_path}  ({len(tier_b)} rows)\n')

    # Sync to SQLite donor_foundations table
    conn = connect(RISK_DB_PATH)
    n_before = conn.execute('SELECT COUNT(*) AS n FROM donor_foundations;').fetchone()['n']
    print(f'SQLite donor_foundations BEFORE: {n_before} rows')

    cols = [r['name'] for r in conn.execute('PRAGMA table_info(donor_foundations);')]
    print(f'SQLite columns: {cols}\n')

    # Insert new rows
    now_utc = datetime.now(timezone.utc).isoformat(timespec='seconds')
    inserted = 0
    for r in new_rows:
        vals = {}
        for col in cols:
            if col == 'ein':
                vals[col] = r['ein'].replace('-', '')  # SQLite stores no-dash
            elif col == 'added_at_utc':
                vals[col] = now_utc
            elif col in r:
                vals[col] = r[col]
            else:
                vals[col] = None
        placeholders = ', '.join('?' for _ in cols)
        colnames = ', '.join(cols)
        try:
            cur = conn.execute(
                f'INSERT OR IGNORE INTO donor_foundations ({colnames}) VALUES ({placeholders});',
                tuple(vals[c] for c in cols),
            )
            if cur.rowcount > 0:
                inserted += 1
        except Exception as e:
            print(f'  Error inserting {r["ein"]}: {e}')
    conn.commit()
    n_after = conn.execute('SELECT COUNT(*) AS n FROM donor_foundations;').fetchone()['n']
    print(f'SQLite donor_foundations AFTER:  {n_after} rows (+{n_after - n_before})\n')
    conn.close()

    print(f'>>> MERGE COMPLETE: {len(existing)} existing -> {len(all_rows)} total foundations')


if __name__ == '__main__':
    main()
