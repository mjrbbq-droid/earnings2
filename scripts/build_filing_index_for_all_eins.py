"""
Build a JSON map of OBJECT_ID -> (zip_path, xml_path) for ALL filings
of every EIN listed in donor_foundations.csv across the IRS 2023/2024/2025
indices. Saves data/all_filing_locations.json to be used by the parser.

This is much faster than the previous per-filing linear ZIP scan.
"""
from __future__ import annotations
import csv
import json
import sys
import zipfile
from pathlib import Path


def main() -> None:
    # Collect ALL EINs we care about
    target_eins = set()
    with open('data/reference/donor_foundations.csv', encoding='utf-8') as f:
        for r in csv.DictReader(f):
            target_eins.add(r['ein'].replace('-', ''))
    print(f'Target EINs: {len(target_eins)}')

    # Collect all OBJECT_IDs for those EINs across indices
    target_oids = set()
    for idx_path in ['data/raw_sources/irs_index/index_2023.csv',
                     'data/raw_sources/irs_index/index_2024.csv',
                     'data/raw_sources/irs_index/index_2025.csv']:
        if not Path(idx_path).exists():
            continue
        print(f'  Reading {idx_path}...')
        with open(idx_path, encoding='utf-8') as f:
            for row in csv.DictReader(f):
                if row['EIN'] in target_eins and row['RETURN_TYPE'] != '990T':
                    target_oids.add(row['OBJECT_ID'])
    print(f'Target OBJECT_IDs to locate: {len(target_oids)}')

    # Single pass through every ZIP, recording matches
    locations: dict[str, list[str]] = {}  # oid -> [zip_path, xml_path]
    zip_dir = Path('data/raw_sources/irs_zips')
    zips = sorted(zip_dir.glob('*.zip'))
    print(f'Scanning {len(zips)} ZIPs...')

    for i, zp in enumerate(zips, 1):
        try:
            with zipfile.ZipFile(zp) as z:
                names = z.namelist()
        except Exception as e:
            print(f'  ! {zp.name}: {e}')
            continue
        hits = 0
        for n in names:
            # Filename format: 202413199349100610_public.xml
            # Extract just the OBJECT_ID prefix
            stem = Path(n).stem  # e.g., 202413199349100610_public
            oid = stem.split('_')[0] if '_' in stem else stem
            if oid in target_oids:
                locations[oid] = [str(zp), n]
                hits += 1
        if hits:
            print(f'  [{i:2d}/{len(zips)}] {zp.name}: {hits} hits')

    out_path = Path('data/reference/all_filing_locations.json')
    with open(out_path, 'w', encoding='utf-8') as f:
        json.dump(locations, f, indent=2)
    print(f'\nWrote {out_path}: {len(locations)} OBJECT_IDs mapped')
    print(f'  Missing: {len(target_oids) - len(locations)} (likely not in our local ZIPs)')


if __name__ == '__main__':
    main()
