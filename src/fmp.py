# src/fmp.py
"""
Thin FMP (Financial Modeling Prep) API client — Ultimate plan, /stable/ endpoints.
All methods return raw JSON (list/dict).  Storage logic lives in scripts/.
"""
from __future__ import annotations

import time
from typing import Any

import requests

from src.config import FMP_API_KEY, FMP_BASE_URL

_DEFAULT_PAUSE = 0.25  # seconds between calls


class FMPClient:
    def __init__(self, api_key: str | None = None, pause: float = _DEFAULT_PAUSE):
        self.api_key = api_key or FMP_API_KEY
        if not self.api_key:
            raise ValueError("FMP_API_KEY not set.  Add it to .env or pass explicitly.")
        self.pause = pause
        self._last_call = 0.0

    # ── internal ───────────────────────────────────────────────────────
    def _get(self, path: str, params: dict | None = None) -> Any:
        """Rate-limited GET → parsed JSON."""
        elapsed = time.monotonic() - self._last_call
        if elapsed < self.pause:
            time.sleep(self.pause - elapsed)

        params = params or {}
        params["apikey"] = self.api_key
        url = f"{FMP_BASE_URL}{path}"
        resp = requests.get(url, params=params, timeout=30)
        self._last_call = time.monotonic()

        resp.raise_for_status()
        return resp.json()

    # ── index constituents ─────────────────────────────────────────────
    def russell2000_constituents(self) -> list[dict]:
        """Return list of Russell 2000 constituents (paginated, max 100/page)."""
        all_rows: list[dict] = []
        page = 0
        while True:
            batch = self._get("/stable/index-constituents", {
                "index": "Russell 2000",
                "limit": 100,
                "page": page,
            })
            if not batch:
                break
            all_rows.extend(batch)
            if len(batch) < 100:
                break
            page += 1
        return all_rows

    def russell1000_constituents(self) -> list[dict]:
        """Return list of Russell 1000 constituents (paginated, max 100/page)."""
        all_rows: list[dict] = []
        page = 0
        while True:
            batch = self._get("/stable/index-constituents", {
                "index": "Russell 1000",
                "limit": 100,
                "page": page,
            })
            if not batch:
                break
            all_rows.extend(batch)
            if len(batch) < 100:
                break
            page += 1
        return all_rows

    def sp500_constituents(self) -> list[dict]:
        """Use the dedicated /stable/sp500-constituent endpoint (returns sector + subSector)."""
        return self._get("/stable/sp500-constituent", {})

    # ── company profile ────────────────────────────────────────────────
    def company_profile(self, symbol: str) -> list[dict]:
        return self._get("/stable/profile", {"symbol": symbol})

    def company_profile_batch(self, symbols: list[str]) -> list[dict]:
        """Batch profile lookup (comma-separated, max ~50)."""
        batch = ",".join(symbols[:50])
        return self._get("/stable/profile", {"symbol": batch})

    # ── earnings estimates ─────────────────────────────────────────────
    def analyst_estimates(self, symbol: str, period: str = "quarter", limit: int = 30) -> list[dict]:
        """Analyst consensus EPS / revenue estimates."""
        return self._get("/stable/analyst-estimates", {
            "symbol": symbol, "period": period, "limit": limit,
        })

    def earnings_surprises(self, symbol: str) -> list[dict]:
        """Historical actual vs estimate EPS."""
        return self._get("/stable/earnings-surprises", {"symbol": symbol})

    # ── earnings call transcripts ──────────────────────────────────────
    def transcript_dates(self, symbol: str) -> list[dict]:
        """Available transcript quarters/dates for a symbol."""
        return self._get("/stable/earning-call-transcript-dates", {"symbol": symbol})

    def transcript(self, symbol: str, year: int, quarter: int) -> list[dict]:
        """Full transcript text for a specific quarter."""
        return self._get("/stable/earning-call-transcript", {
            "symbol": symbol, "year": year, "quarter": quarter,
        })

    def transcript_latest(self, limit: int = 100, page: int = 0) -> list[dict]:
        """Latest available transcripts across all companies."""
        return self._get("/stable/earning-call-transcript-latest", {
            "limit": limit, "page": page,
        })

    # ── news ───────────────────────────────────────────────────────────
    def news_general_latest(self, page: int = 0, limit: int = 100) -> list[dict]:
        return self._get("/stable/news/general-latest", {"page": page, "limit": limit})

    def news_stock_latest(self, page: int = 0, limit: int = 100) -> list[dict]:
        return self._get("/stable/news/stock-latest", {"page": page, "limit": limit})

    def news_press_releases_latest(self, page: int = 0, limit: int = 100) -> list[dict]:
        return self._get("/stable/news/press-releases-latest", {"page": page, "limit": limit})

    def news_stock(self, symbols: str, page: int = 0, limit: int = 100) -> list[dict]:
        return self._get("/stable/news/stock", {"symbols": symbols, "page": page, "limit": limit})

    # ── financial statements (useful later) ────────────────────────────
    def income_statement(self, symbol: str, period: str = "quarter", limit: int = 20) -> list[dict]:
        return self._get("/stable/income-statement", {
            "symbol": symbol, "period": period, "limit": limit,
        })

    def key_metrics(self, symbol: str, period: str = "quarter", limit: int = 20) -> list[dict]:
        return self._get("/stable/key-metrics", {
            "symbol": symbol, "period": period, "limit": limit,
        })
