"""
Thin REST client for Financial Modeling Prep (FMP).

Why a shared client: both fundamentals.py and fetch_prices.py hit FMP. A small
shared module avoids duplicating the API key handling, base URL, and error
shape across two files.

Why direct requests over fmpsdk: FMP exposes only three endpoints we need
(income-statement, balance-sheet-statement, historical-price-full). A library
adds a dependency layer that hides the contract. Direct requests keeps the
HTTP shape visible at the call site.

Uses the FMP "stable" API (post-Aug 2025 restructure). The legacy v3 API is
no longer accessible for new subscriptions.

Free tier limits:
- 250 requests per day
- US stocks only
- ~5 years historical depth
- Daily quotes (not intraday)

The existing on-disk cache absorbs the daily call limit — same ticker on the
same day is fetched once.
"""
from __future__ import annotations

import logging
import os
from typing import Any

import httpx

logger = logging.getLogger(__name__)

BASE_URL = "https://financialmodelingprep.com/stable"
TIMEOUT_SECONDS = 30


class FMPError(RuntimeError):
    """Raised when FMP returns an error or unexpected shape."""


def _api_key() -> str:
    key = os.environ.get("FMP_API_KEY")
    if not key:
        raise FMPError("FMP_API_KEY is not set. Add it to .env and reload the environment.")
    return key


def _get(path: str, params: dict[str, Any] | None = None) -> Any:
    """GET an FMP endpoint, return parsed JSON. Raises FMPError on failure."""
    params = dict(params or {})
    params["apikey"] = _api_key()
    url = f"{BASE_URL}/{path.lstrip('/')}"
    try:
        response = httpx.get(url, params=params, timeout=TIMEOUT_SECONDS)
    except httpx.RequestError as e:
        raise FMPError(f"FMP request failed: {e}") from e

    if response.status_code != 200:
        raise FMPError(
            f"FMP returned {response.status_code} for {path}: {response.text[:200]}"
        )

    data = response.json()
    # FMP returns either a list (normal) or an object with "Error Message" (failure)
    if isinstance(data, dict) and "Error Message" in data:
        raise FMPError(f"FMP error for {path}: {data['Error Message']}")
    return data


def get_stock_news(
    ticker: str, from_date: str, to_date: str, limit: int = 50
) -> list[dict[str, Any]]:
    """Recent news for a ticker over [from_date, to_date] (FMP stable `news/stock`).
    Rows include publishedDate, title, text, site, url. Empty list on failure / if the
    endpoint is not on the current plan — news is optional, never fatal."""
    try:
        data = _get(
            "news/stock",
            params={"symbols": ticker, "from": from_date, "to": to_date, "limit": limit},
        )
    except FMPError as e:
        logger.warning(f"FMP news fetch failed for {ticker}: {e}")
        return []
    return data if isinstance(data, list) else []


def get_bulk_csv(path: str, params: dict[str, Any] | None = None) -> str:
    """GET a bulk endpoint and return raw CSV text. FMP's `*-bulk` endpoints
    (Premium) return CSV (all companies per period), not JSON. Raises FMPError on
    non-200 (e.g. 402 if the endpoint isn't on the current tier)."""
    params = dict(params or {})
    params["apikey"] = _api_key()
    url = f"{BASE_URL}/{path.lstrip('/')}"
    try:
        response = httpx.get(url, params=params, timeout=TIMEOUT_SECONDS)
    except httpx.RequestError as e:
        raise FMPError(f"FMP bulk request failed: {e}") from e
    if response.status_code != 200:
        raise FMPError(
            f"FMP bulk returned {response.status_code} for {path}: {response.text[:200]}"
        )
    return response.text


def get_income_statement(ticker: str, limit: int = 5) -> list[dict[str, Any]]:
    """Annual income statement, most recent first.

    Each entry includes acceptedDate (filing date).
    """
    data = _get("income-statement", params={"symbol": ticker, "limit": limit})
    if not isinstance(data, list) or not data:
        raise FMPError(f"No income statement data returned for {ticker}")
    return data


def get_balance_sheet(ticker: str, limit: int = 5) -> list[dict[str, Any]]:
    """Annual balance sheet, most recent first. Each entry includes acceptedDate (filing date)."""
    data = _get("balance-sheet-statement", params={"symbol": ticker, "limit": limit})
    if not isinstance(data, list) or not data:
        raise FMPError(f"No balance sheet data returned for {ticker}")
    return data


def get_historical_prices(
    ticker: str, from_date: str | None = None, to_date: str | None = None
) -> list[dict[str, Any]]:
    """Daily OHLCV history. Returns list ordered from most recent to oldest.

    Each entry: {symbol, date, open, high, low, close, volume, change, changePercent, vwap}.
    Free tier provides 5 years of history; pass from_date/to_date to bound the window.
    """
    params: dict[str, Any] = {"symbol": ticker}
    if from_date:
        params["from"] = from_date
    if to_date:
        params["to"] = to_date
    data = _get("historical-price-eod/full", params=params)
    if not isinstance(data, list) or not data:
        raise FMPError(f"No historical price data returned for {ticker}")
    return data
