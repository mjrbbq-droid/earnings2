# src/config.py
from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

# ── Paths ──────────────────────────────────────────────────────────────
PROJECT_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = PROJECT_ROOT / "data"
DB_PATH = os.getenv("EARNINGS_DB_PATH", str(DATA_DIR / "earnings.db"))
RISK_DB_PATH = os.getenv("RISK_DB_PATH", str(DATA_DIR / "institutional_risk.db"))
RAW_ARTICLES_DIR = DATA_DIR / "raw_articles"
PROCESSED_ARTICLES_DIR = DATA_DIR / "processed_articles"

# ── FMP ────────────────────────────────────────────────────────────────
FMP_API_KEY = os.getenv("FMP_API_KEY", "")
FMP_BASE_URL = "https://financialmodelingprep.com"

# ── OpenAI ─────────────────────────────────────────────────────────────
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-5.2")

# ── Anthropic ──────────────────────────────────────────────────────────
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
ANTHROPIC_MODEL = os.getenv("ANTHROPIC_MODEL", "claude-opus-4-7")
