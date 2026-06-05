"""
L1 value metrics (spec §5) — deterministic, pure Python, no LLM. Computes the
"why it's cheap" numbers from a point-in-time fundamentals Period + the as-of price.

Graham/Burry value lenses:
  - net-net:   NCAV / NNWC (Graham's discounted working capital), price-to-NCAV, p_tbv
  - earnings:  EV/EBIT, EV/EBITDA (through-cycle cheapness)
Market cap uses diluted weighted shares from the same filing × the as-of close, so the
whole metric is point-in-time consistent with the fundamentals.
"""
from __future__ import annotations

import math

from deepvalue.ingest.fundamentals_store import Period


def _f(x) -> float | None:
    try:
        v = float(x)
        return None if math.isnan(v) else v
    except (TypeError, ValueError):
        return None


def _div(a: float | None, b: float | None) -> float | None:
    if a is None or b is None or b == 0:
        return None
    return a / b


def diluted_shares(p: Period) -> float | None:
    return _f(p.get("weightedAverageShsOutDil") or p.get("weightedAverageShsOut"))


def market_cap(p: Period, price: float) -> float | None:
    sh = diluted_shares(p)
    return None if sh is None else sh * price


def ncav(p: Period) -> float | None:
    """Net current asset value = total current assets − TOTAL liabilities (Graham)."""
    ca = _f(p.get("totalCurrentAssets"))
    tl = _f(p.get("totalLiabilities"))
    return None if ca is None or tl is None else ca - tl


def nnwc(p: Period) -> float | None:
    """Net-net working capital — Graham's liquidation haircut: cash at 100%, receivables
    75%, inventory 50%, minus ALL liabilities."""
    cash = _f(p.get("cashAndShortTermInvestments")) or _f(p.get("cashAndCashEquivalents"))
    rec = _f(p.get("netReceivables")) or _f(p.get("accountsReceivables")) or 0.0
    inv = _f(p.get("inventory")) or 0.0
    tl = _f(p.get("totalLiabilities"))
    if cash is None or tl is None:
        return None
    return cash + 0.75 * rec + 0.50 * inv - tl


def tangible_book(p: Period) -> float | None:
    """Stockholders' equity less goodwill and intangibles."""
    eq = _f(p.get("totalStockholdersEquity")) or _f(p.get("totalEquity"))
    if eq is None:
        return None
    gw = _f(p.get("goodwill")) or 0.0
    intang = _f(p.get("intangibleAssets")) or 0.0
    if gw == 0 and intang == 0:
        gwi = _f(p.get("goodwillAndIntangibleAssets"))
        if gwi is not None:
            gw = gwi
    return eq - gw - intang


def enterprise_value(p: Period, price: float) -> float | None:
    mc = market_cap(p, price)
    if mc is None:
        return None
    debt = _f(p.get("totalDebt")) or 0.0
    cash = _f(p.get("cashAndCashEquivalents")) or 0.0
    return mc + debt - cash


def value_metrics(p: Period, price: float) -> dict:
    """All §5 value metrics for one name as of `p.filing_date` at `price`. Missing inputs
    surface as None rather than raising — the screen treats None as 'not screenable'."""
    mc = market_cap(p, price)
    nc = ncav(p)
    nn = nnwc(p)
    tb = tangible_book(p)
    ev = enterprise_value(p, price)
    return {
        "market_cap": mc,
        "ncav": nc,
        "price_to_ncav": _div(mc, nc),          # Graham buy: <= 0.67
        "nnwc": nn,
        "price_to_nnwc": _div(mc, nn),
        "p_tbv": _div(mc, tb),                  # price-to-tangible-book; <= 1.0 is cheap
        "ev": ev,
        "ev_ebit": _div(ev, _f(p.get("ebit"))),
        "ev_ebitda": _div(ev, _f(p.get("ebitda"))),
    }
