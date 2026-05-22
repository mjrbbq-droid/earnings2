"""Download all 12 months of IRS 2025 990 XML ZIPs."""
from __future__ import annotations
import requests
import sys
from pathlib import Path


def main() -> None:
    out_dir = Path('data/raw_sources/irs_zips')
    out_dir.mkdir(parents=True, exist_ok=True)

    total_bytes = 0
    for m in range(1, 13):
        fn = f'2025_TEOS_XML_{m:02d}A.zip'
        out = out_dir / fn
        url = f'https://apps.irs.gov/pub/epostcard/990/xml/2025/{fn}'
        if out.exists() and out.stat().st_size > 10_000_000:
            print(f'  cached: {fn} ({out.stat().st_size/1e6:.0f} MB)')
            total_bytes += out.stat().st_size
            continue
        print(f'  fetching {fn}...', end='', flush=True)
        r = requests.get(url, timeout=600, stream=True)
        if r.status_code != 200:
            print(f' ERROR {r.status_code}')
            continue
        n = 0
        with open(out, 'wb') as f:
            for chunk in r.iter_content(1024 * 1024):
                f.write(chunk)
                n += len(chunk)
        total_bytes += n
        print(f' {n/1e6:.0f} MB')

    print(f'\nTotal: {total_bytes/1e9:.2f} GB across 12 months')


if __name__ == '__main__':
    main()
