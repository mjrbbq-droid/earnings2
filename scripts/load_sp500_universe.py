"""
Fetch S&P 500 constituents from FMP, save to data/sp500_universe.csv, and print
sector breakdown. Used as practical proxy for 'large-cap US universe' since
FMP doesn't expose Russell 1000 directly.

Columns: ticker, name, sector, subSector, headQuarter, dateFirstAdded, cik, founded
"""
from __future__ import annotations

import csv
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.config import DATA_DIR
from src.fmp import FMPClient

OUT_PATH = Path(DATA_DIR) / "sp500_universe.csv"

FIELDS = ["ticker", "name", "sector", "subSector", "headQuarter", "dateFirstAdded", "cik", "founded"]


def main() -> None:
    fmp = FMPClient()
    rows = fmp.sp500_constituents()
    print(f"Fetched {len(rows)} S&P 500 constituents.\n")

    with open(OUT_PATH, "w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS)
        w.writeheader()
        for r in rows:
            w.writerow({
                "ticker":         r.get("symbol", ""),
                "name":           r.get("name", ""),
                "sector":         r.get("sector", ""),
                "subSector":      r.get("subSector", ""),
                "headQuarter":    r.get("headQuarter", ""),
                "dateFirstAdded": r.get("dateFirstAdded", ""),
                "cik":            r.get("cik", ""),
                "founded":        r.get("founded", ""),
            })
    print(f"Saved -> {OUT_PATH}\n")

    sectors = Counter(r.get("sector") or "(unknown)" for r in rows)
    print("Sector breakdown:")
    for sector, n in sectors.most_common():
        print(f"  {sector:40s}  {n:3d}")

    # Highlight sectors most likely to have anti-police stances
    priority_sectors = {
        "Technology",
        "Consumer Discretionary",
        "Consumer Cyclical",
        "Communication Services",
        "Consumer Staples",
        "Consumer Defensive",
        "Healthcare",
        "Financial Services",
    }
    n_priority = sum(n for s, n in sectors.items() if s in priority_sectors)
    print(f"\nPriority sectors (consumer/tech/comms/healthcare/finance): {n_priority} tickers")
    print(f"Other (energy/industrials/REITs/materials/utilities):       {len(rows) - n_priority} tickers")


if __name__ == "__main__":
    main()
