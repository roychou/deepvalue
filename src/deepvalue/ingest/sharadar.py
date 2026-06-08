"""
Sharadar adapter — survivorship-free prices + fundamentals from the DuckDB built by
scripts/sharadar_load.py, exposed in the SAME shapes the quant layer already consumes
(fundamentals_store.Period, the {date: {close,...}} price dict), so quant/metrics.py and
quant/trap_signals.py work unchanged.

Why Sharadar over the FMP cache: genuinely survivorship-free back to 1998 (~28 cohorts vs
FMP's clean ~10), native point-in-time key (`datekey` = when the filing became available),
stable `permaticker` (kills ticker reuse), and SIC/category/delisting in TICKERS. Each
company is bounded by its TICKERS price window so a reused ticker can't bleed across.
"""
from __future__ import annotations

import functools
from pathlib import Path

import duckdb

from deepvalue.ingest.fundamentals_store import Period

_DB = Path("data/cache/sharadar.duckdb")

# Sharadar SF1 column -> the FMP-style field name our quant code reads via Period.get().
_MAP = {
    "assetsc": "totalCurrentAssets", "liabilitiesc": "totalCurrentLiabilities",
    "assets": "totalAssets", "liabilities": "totalLiabilities",
    "equity": "totalStockholdersEquity", "retearn": "retainedEarnings",
    "netinc": "netIncome", "revenue": "revenue", "gp": "grossProfit",
    "ebit": "ebit", "ebitda": "ebitda", "cashneq": "cashAndCashEquivalents",
    "receivables": "netReceivables", "inventory": "inventory",
    "debt": "totalDebt", "debtnc": "longTermDebt",
    "intangibles": "goodwillAndIntangibleAssets",
    "shareswadil": "weightedAverageShsOutDil", "shareswa": "weightedAverageShsOut",
    "depamor": "depreciationAndAmortization", "ncfo": "operatingCashFlow",
    "capex": "capitalExpenditure", "sgna": "sellingGeneralAndAdministrativeExpenses",
    "payables": "accountPayables", "ppnenet": "propertyPlantEquipmentNet",
    "investmentsc": "_investmentsc",
}
_SF1_COLS = ["ticker", "datekey", "reportperiod", *(_MAP.keys())]


@functools.lru_cache(maxsize=1)
def _con() -> duckdb.DuckDBPyConnection:
    if not _DB.exists():
        raise FileNotFoundError(f"{_DB} not found — run scripts/sharadar_load.py first")
    return duckdb.connect(str(_DB), read_only=True)


def prices(ticker: str, start: str | None = None, end: str | None = None) -> dict[str, dict]:
    """{date: {'close': closeadj, 'volume': volume}} — split/div-adjusted close for returns.
    Optional [start,end] bounds isolate one company on a reused ticker (TICKERS window)."""
    q = "SELECT date, closeadj, volume FROM sep WHERE ticker = ?"
    params: list = [ticker]
    if start:
        q += " AND date >= ?"; params.append(start)
    if end:
        q += " AND date <= ?"; params.append(end)
    out = {}
    for d, c, v in _con().execute(q, params).fetchall():
        ds = str(d)
        out[ds] = {"close": c, "volume": v}
    return out


def periods(ticker: str, dimension: str = "ARY",
            start: str | None = None, end: str | None = None) -> list[Period]:
    """Fundamentals as Period objects (Sharadar fields remapped to FMP names), newest first.
    filing_date = datekey (point-in-time). Bounded by [start,end] on datekey for reuse safety."""
    cols = ", ".join(_SF1_COLS)
    q = f"SELECT {cols} FROM sf1 WHERE ticker = ? AND dimension = ?"
    params: list = [ticker, dimension]
    if start:
        q += " AND datekey >= ?"; params.append(start)
    if end:
        q += " AND datekey <= ?"; params.append(end)
    rows = _con().execute(q, params).fetchall()
    out = []
    for row in rows:
        rec = dict(zip(_SF1_COLS, row))
        income = {_MAP[k]: rec[k] for k in _MAP if rec.get(k) is not None}
        # NNWC wants cash + short-term investments
        cash = rec.get("cashneq")
        if cash is not None:
            income["cashAndShortTermInvestments"] = cash + (rec.get("investmentsc") or 0)
        out.append(Period(symbol=ticker, cik=None, period_end=str(rec["reportperiod"]),
                          filing_date=str(rec["datekey"]), income=income, balance={}, cashflow={}))
    out.sort(key=lambda p: p.period_end, reverse=True)
    return out


def as_of(period_list: list[Period], as_of_date: str) -> Period | None:
    elig = [p for p in period_list if p.filing_date <= as_of_date]
    return max(elig, key=lambda p: p.filing_date) if elig else None


def prior_year(period_list: list[Period], period: Period) -> Period | None:
    target = str(int(period.period_end[:4]) - 1)
    return next((p for p in period_list if p.period_end[:4] == target), None)


def common_stock_universe() -> list[dict]:
    """Survivorship-free common-stock names with sector + price window (for reuse bounding)."""
    rows = _con().execute(
        "SELECT ticker, permaticker, siccode, isdelisted, firstpricedate, lastpricedate "
        "FROM tickers WHERE category IN ('Domestic Common Stock', 'Canadian Common Stock', "
        "'ADR Common Stock') AND ticker IS NOT NULL"
    ).fetchall()
    return [{"ticker": t, "permaticker": p, "siccode": str(s) if s is not None else None,
             "isdelisted": dl, "first": str(fp) if fp else None, "last": str(lp) if lp else None}
            for t, p, s, dl, fp, lp in rows]
