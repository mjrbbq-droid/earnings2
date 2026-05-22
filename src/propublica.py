# src/propublica.py
"""
Thin client for ProPublica Nonprofit Explorer API.

Public, unauthenticated API. Returns structured 990 filing data including
total grants paid per year (key field: `contrpdpbks` for 990-PF, similar
fields for 990 / 990-EZ).

Reference: https://projects.propublica.org/nonprofits/api
"""
from __future__ import annotations

import time
from typing import Any

import requests

_BASE = "https://projects.propublica.org/nonprofits/api/v2"
_PAUSE = 0.4   # be polite to a free API


class ProPublicaClient:
    def __init__(self, pause: float = _PAUSE):
        self.pause = pause
        self._last_call = 0.0

    def _get(self, path: str, params: dict | None = None) -> Any:
        elapsed = time.monotonic() - self._last_call
        if elapsed < self.pause:
            time.sleep(self.pause - elapsed)
        url = f"{_BASE}{path}"
        resp = requests.get(url, params=params or {}, timeout=30)
        self._last_call = time.monotonic()
        if resp.status_code == 404:
            # ProPublica returns 404 for "no results" — surface as None so callers can branch
            return None
        resp.raise_for_status()
        return resp.json()

    # ── search ─────────────────────────────────────────────────────────
    def search(self, query: str, state: str | None = None) -> list[dict]:
        """Search by name. Returns list of organizations with EIN."""
        params: dict[str, Any] = {"q": query}
        if state:
            params["state[id]"] = state
        data = self._get("/search.json", params)
        if data is None:
            return []
        return data.get("organizations") or []

    # ── organization detail + filings ─────────────────────────────────
    def organization(self, ein: str) -> dict:
        """Pull full org detail including filings_with_data."""
        ein_clean = ein.replace("-", "").strip()
        return self._get(f"/organizations/{ein_clean}.json")

    def filings(self, ein: str) -> list[dict]:
        """Return all filings_with_data (the structured ones). Filings without
        structured data are skipped — those tend to be older or paper-filed."""
        data = self.organization(ein)
        return data.get("filings_with_data") or []


# ── Field map by form type ──────────────────────────────────────────────
# 990-PF (private foundation) uses different field names than 990 / 990-EZ.
# Map the common "total grants paid this year" field per form.
def total_grants_paid(filing: dict) -> int | None:
    """Best-effort extraction of total grants paid in this filing year."""
    formtype = filing.get("formtype")
    # formtype: 0 = 990, 1 = 990-EZ, 2 = 990-PF
    if formtype == 2:
        # 990-PF: contributions paid per books
        return filing.get("contrpdpbks")
    if formtype in (0, 1):
        # 990: totfuncexpns includes a grants line; the cleanest is line 13 = grntstoindiv + grntstogovt
        # but the parsed field varies. Best available: grntstogovt + grntstoindiv if both present.
        g_gov = filing.get("grntstogovt") or 0
        g_ind = filing.get("grntstoindiv") or 0
        if g_gov or g_ind:
            return int(g_gov + g_ind)
        # fallback: grants paid total field
        return filing.get("totgftgrntrcvd_a") or filing.get("totalgrants")
    return None


def total_revenue(filing: dict) -> int | None:
    return filing.get("totrevenue")


def total_expenses(filing: dict) -> int | None:
    return filing.get("totfuncexpns")


def filing_year(filing: dict) -> int | None:
    """Fiscal year end. Prefer tax_prd_yr; fall back to deriving from tax_prd."""
    if filing.get("tax_prd_yr"):
        return int(filing["tax_prd_yr"])
    tp = filing.get("tax_prd")
    if isinstance(tp, int) and tp > 190000:
        return int(str(tp)[:4])
    return None
