# data/

Organized into clear buckets. **Everything except `reference/` is gitignored** — re-buildable from scripts and source APIs.

```
data/
├── reference/        ← analyst-curated lookups (TRACKED IN GIT)
│   ├── sp500_universe.csv
│   ├── donor_foundations.csv / .json
│   ├── foundation_candidates_v2.csv          ← current candidate list
│   ├── foundation_candidates_tier_b_review.csv  ← workflow file for review
│   ├── company_master.csv
│   ├── industry_key.csv
│   ├── query_taxonomy.csv
│   ├── anti_police_companies.csv
│   ├── police_news.csv
│   ├── v2_anti_tickers.txt / v2_anti_snapshot.json
│   ├── all_filing_locations.json / filing_locations.json / daf_filing_locations.json
│   ├── bzh_signatures.csv
│   └── r2kdata.xlsx                          ← Russell 2000 universe
│
├── databases/        ← live SQLite (gitignored)
│   └── earnings.db
│   (institutional_risk.db is at data/ root — was locked during reorg, move
│    it here when no process is using it)
│
├── raw_sources/      ← bulk source data, gitignored, re-downloadable
│   ├── irs_zips/             ← raw IRS 990 ZIP files (~10 GB)
│   ├── irs_index/            ← per-year filing indices
│   ├── raw_articles/         ← FMP news dumps
│   ├── raw_pdfs/             ← earnings call PDFs (raw)
│   └── raw_pdf_slides/       ← investor day PDFs
│
├── processed/        ← PDF transcripts after ingestion (gitignored, 94 MB)
│
├── processed_articles/  ← article processing output (gitignored, empty placeholder)
│
├── outputs/          ← final deliverables (gitignored, regenerable)
│   └── police_policy_stance_*.xlsx
│
└── REVIEW_LATER/     ← things to triage, then move or delete
    ├── old_zscore_csvs/                  ← 19 per-ticker debug outputs
    ├── dated_snapshots/                  ← 4 old Excel snapshots
    ├── duplicate_foundation_candidates/  ← older versions of foundation_candidates_v2
    ├── db_backups/                       ← earnings_backup_before_cleanup.db (105 MB)
    ├── unclear_artifacts/                ← Indutry_tool_for_transcripts.xlsx, .sqbpro session files
    ├── raw/                              ← old data/raw/ folder (now empty)
    └── needs_review/                     ← was empty placeholder
```

## Where paths come from

Most scripts use `src.config.DB_PATH`, `RISK_DB_PATH`, `RAW_ARTICLES_DIR`, etc. — see [src/config.py](../src/config.py). A few scripts still hardcode `./data/databases/earnings.db` and similar; those have been updated to the new locations.

## When to put a file in `reference/`

- Small (< 1 MB)
- Analyst-curated (not auto-generated)
- Inputs to scripts, not outputs
- Worth versioning in git (you want to see how it changes over time)

## When to put a file in `REVIEW_LATER/`

- You're not sure if it's still in use
- It might be a duplicate of something newer
- You moved files during cleanup and need to decide later

After review, either move it to the right bucket OR delete it.
