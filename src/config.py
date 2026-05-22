# src/config.py
from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

# ── Paths ──────────────────────────────────────────────────────────────
# Data is organized into subfolders (see data/README.md):
#   reference/     - small analyst-curated lookups (in git)
#   databases/     - SQLite databases (gitignored)
#   raw_sources/   - bulk source data (gitignored): IRS zips, raw articles/PDFs
#   processed/     - intermediate parquets/PDFs (gitignored)
#   outputs/       - final deliverables (gitignored)
#   REVIEW_LATER/  - things you should look at and decide what to do with
PROJECT_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = PROJECT_ROOT / "data"
REFERENCE_DIR = DATA_DIR / "reference"
DATABASES_DIR = DATA_DIR / "databases"
RAW_SOURCES_DIR = DATA_DIR / "raw_sources"
OUTPUTS_DIR = DATA_DIR / "outputs"

DB_PATH = os.getenv("EARNINGS_DB_PATH", str(DATABASES_DIR / "earnings.db"))

# institutional_risk.db: prefer new location; fall back to legacy root if not yet moved
_risk_new = DATABASES_DIR / "institutional_risk.db"
_risk_legacy = DATA_DIR / "institutional_risk.db"
RISK_DB_PATH = os.getenv("RISK_DB_PATH", str(_risk_new if _risk_new.exists() else _risk_legacy))

RAW_ARTICLES_DIR = RAW_SOURCES_DIR / "raw_articles"
PROCESSED_ARTICLES_DIR = DATA_DIR / "processed_articles"

# ── FMP ────────────────────────────────────────────────────────────────
FMP_API_KEY = os.getenv("FMP_API_KEY", "")
FMP_BASE_URL = "https://financialmodelingprep.com"

# ── OpenAI ─────────────────────────────────────────────────────────────
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-5.2")

# ── Anthropic ──────────────────────────────────────────────────────────
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
ANTHROPIC_MODEL = os.getenv("ANTHROPIC_MODEL", "claude-opus-4-7")
