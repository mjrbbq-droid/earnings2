"""
Scrape FMP news feeds for police / defund-police / law-enforcement headlines.

FMP has no keyword-search endpoint, so we paginate the three latest feeds
(general, stock-tagged, press releases) and filter client-side.

Output: data/police_news.csv  with columns:
    published, symbol, source, keyword, title, url, snippet, feed
"""
from __future__ import annotations

import csv
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.config import DATA_DIR
from src.fmp import FMPClient

# Keywords — each is a compiled word-boundary regex on (title + text).
# Word boundaries matter: bare "police" would otherwise hit "policies", "policed".
KEYWORDS: dict[str, re.Pattern] = {
    "defund police":     re.compile(r"\bdefund(ing)?\s+(the\s+)?police\b", re.I),
    "police":            re.compile(r"\bpolice\b", re.I),
    "law enforcement":   re.compile(r"\blaw\s+enforcement\b", re.I),
    "police union":      re.compile(r"\bpolice\s+union", re.I),
    "police brutality":  re.compile(r"\bpolice\s+brutality\b", re.I),
    "police officer":    re.compile(r"\bpolice\s+officers?\b", re.I),
    "officer-involved": re.compile(r"\bofficer[\s-]involved\b", re.I),
    "george floyd":      re.compile(r"\bgeorge\s+floyd\b", re.I),
    "black lives matter": re.compile(r"\bblack\s+lives\s+matter\b", re.I),
    "blue lives matter": re.compile(r"\bblue\s+lives\s+matter\b", re.I),
    "body camera":       re.compile(r"\bbody[\s-]cam(era)?s?\b", re.I),
    "public safety":     re.compile(r"\bpublic\s+safety\b", re.I),
    "retail theft":      re.compile(r"\bretail\s+theft\b|\borganized\s+retail\s+crime\b", re.I),
}

FEEDS = {
    "general":        "news_general_latest",
    "stock":          "news_stock_latest",
    "press_release":  "news_press_releases_latest",
}

PAGES_PER_FEED = 20        # 20 pages * 100 items = 2,000 items per feed
LIMIT = 100
OUT_PATH = Path(DATA_DIR) / "police_news.csv"


def find_keywords(text: str) -> list[str]:
    hits = []
    for label, pat in KEYWORDS.items():
        if pat.search(text):
            hits.append(label)
    return hits


def main() -> None:
    fmp = FMPClient()
    rows: list[dict] = []
    seen_urls: set[str] = set()
    seen_titles: set[str] = set()

    for feed_name, method_name in FEEDS.items():
        method = getattr(fmp, method_name)
        print(f"\n=== {feed_name} ===")
        for page in range(PAGES_PER_FEED):
            try:
                batch = method(page=page, limit=LIMIT)
            except Exception as e:
                print(f"  page {page}: ERROR {e}")
                break
            if not batch:
                print(f"  page {page}: empty, stopping")
                break

            page_hits = 0
            for item in batch:
                url = item.get("url", "") or ""
                title = (item.get("title") or "").strip()
                title_key = title.lower()
                if url in seen_urls or title_key in seen_titles:
                    continue
                blob = f"{title}  {item.get('text','')}"
                hits = find_keywords(blob)
                if not hits:
                    continue
                seen_urls.add(url)
                seen_titles.add(title_key)
                rows.append({
                    "published": item.get("publishedDate"),
                    "symbol":    item.get("symbol") or "",
                    "source":    item.get("site") or item.get("publisher") or "",
                    "keyword":   "; ".join(hits),
                    "title":     item.get("title", "").strip(),
                    "url":       url,
                    "snippet":   (item.get("text") or "").strip()[:300],
                    "feed":      feed_name,
                })
                page_hits += 1
            print(f"  page {page}: {len(batch):3d} items, {page_hits} hits  (total hits: {len(rows)})")

    rows.sort(key=lambda r: r["published"] or "", reverse=True)

    with open(OUT_PATH, "w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(
            f,
            fieldnames=["published", "symbol", "source", "keyword", "title", "url", "snippet", "feed"],
        )
        writer.writeheader()
        writer.writerows(rows)

    print(f"\nWrote {len(rows)} rows -> {OUT_PATH}")

    print("\n--- top 30 most recent ---")
    for r in rows[:30]:
        sym = f"[{r['symbol']}] " if r['symbol'] else ""
        print(f"{r['published']}  {sym}({r['keyword']})  {r['title'][:120]}")


if __name__ == "__main__":
    main()
