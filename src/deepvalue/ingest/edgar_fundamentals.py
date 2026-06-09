"""
EDGAR XBRL -> point-in-time fundamentals for the LIVE deep-value funnel.

Purpose-built for deepvalue (NOT parley's light revenue/EPS/equity extractor): pulls the full
balance sheet + cash flow + share counts the Graham/Piotroski/Altman screen reads, and emits
the SAME `Period` shape as fundamentals_store (income/balance/cashflow dicts keyed by FMP field
names, `.get()` across all three) so quant/metrics.py + quant/trap_signals.py work unchanged.

It reuses edgar.py's XBRL machinery (companyfacts fetch+cache, point-in-time concept extraction,
annual-flow derivation). Each annual period's `filing_date` is the as-originally-reported 10-K
filing date (earliest `filed`) — the load-bearing point-in-time key (CLAUDE.md: never
period_of_report). Source for the LIVE forward path; the backtest keeps using its grab caches.
"""
from __future__ import annotations

import functools

from deepvalue.ingest.edgar import (
    _annual_flow,
    _best_revenue_rows,
    _concept_rows,
    _instant_at,
    _total_debt_at,
    fetch_company_facts,
)
from deepvalue.ingest.fundamentals_store import Period

# FMP field -> US-GAAP concept(s), priority-ordered (first present wins). Micro-cap XBRL
# tagging is inconsistent, hence the fallbacks.

# Instant (stock) balance-sheet items, valued at the fiscal-year-end date.
_INSTANT: dict[str, list[str]] = {
    "totalCurrentAssets": ["AssetsCurrent"],
    "totalCurrentLiabilities": ["LiabilitiesCurrent"],
    "totalAssets": ["Assets"],
    "totalLiabilities": ["Liabilities"],
    "totalStockholdersEquity": ["StockholdersEquity",
                                "StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest"],
    "retainedEarnings": ["RetainedEarningsAccumulatedDeficit"],
    "cashAndCashEquivalents": ["CashAndCashEquivalentsAtCarryingValue",
                               "CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalents"],
    "shortTermInvestments": ["ShortTermInvestments"],
    "inventory": ["InventoryNet"],
    "netReceivables": ["AccountsReceivableNetCurrent", "ReceivablesNetCurrent"],
    "propertyPlantEquipmentNet": ["PropertyPlantAndEquipmentNet"],
    "goodwill": ["Goodwill"],
    "intangibleAssets": ["IntangibleAssetsNetExcludingGoodwill", "FiniteLivedIntangibleAssetsNet"],
    "longTermDebt": ["LongTermDebtNoncurrent"],
    "accountPayables": ["AccountsPayableCurrent", "AccountsPayableAndAccruedLiabilitiesCurrent"],
}

# Duration (flow) income/cash-flow items, summed over the ~365-day fiscal year.
_DURATION: dict[str, list[str]] = {
    "netIncome": ["NetIncomeLoss", "ProfitLoss"],
    "grossProfit": ["GrossProfit"],
    "ebit": ["OperatingIncomeLoss"],
    "sellingGeneralAndAdministrativeExpenses": ["SellingGeneralAndAdministrativeExpense"],
    "depreciationAndAmortization": ["DepreciationDepletionAndAmortization",
                                    "DepreciationAmortizationAndAccretionNet",
                                    "DepreciationAndAmortization"],
    "operatingCashFlow": ["NetCashProvidedByUsedInOperatingActivities",
                          "NetCashProvidedByUsedInOperatingActivitiesContinuingOperations"],
}
# Share-count flows live under the 'shares' unit, not 'USD'.
_SHARES: dict[str, list[str]] = {
    "weightedAverageShsOutDil": ["WeightedAverageNumberOfDilutedSharesOutstanding"],
    "weightedAverageShsOut": ["WeightedAverageNumberOfSharesOutstandingBasic",
                              "WeightedAverageNumberOfShareOutstandingBasicAndDiluted"],
}


def _gaap(ticker: str) -> tuple[dict, str | None]:
    facts = fetch_company_facts(ticker)
    cik = facts.get("cik")
    return facts.get("facts", {}).get("us-gaap", {}), (str(cik) if cik is not None else None)


@functools.lru_cache(maxsize=2048)
def load_periods(ticker: str) -> list[Period]:
    """All annual fiscal periods for `ticker` from EDGAR XBRL, NEWEST FIRST. Each Period
    carries the full FMP-named field set the funnel reads, with the 10-K filing_date as the
    point-in-time key. Returns [] for non-US-GAAP/foreign filers (20-F/IFRS) — the live
    universe is restricted to domestic common-stock 10-K filers anyway."""
    gaap, cik = _gaap(ticker)
    if not gaap:
        return []

    # Anchor the annual periods on revenue + net income annual flows (union of FY-ends),
    # so a filer missing one concept still resolves on the other.
    rev = _annual_flow(_best_revenue_rows(gaap))
    ni = _annual_flow(_concept_rows(gaap, _DURATION["netIncome"]))
    ends = sorted(set(rev) | set(ni), reverse=True)
    if not ends:
        return []

    # Pre-compute annual flows per duration/share concept once.
    dur_flows = {f: _annual_flow(_concept_rows(gaap, names)) for f, names in _DURATION.items()}
    dur_flows["revenue"] = rev
    shr_flows = {f: _annual_flow(_concept_rows(gaap, names, unit="shares"))
                 for f, names in _SHARES.items()}

    periods: list[Period] = []
    for end in ends:
        anchor = rev.get(end) or ni.get(end)
        filed = (anchor or {}).get("filed")
        if not filed:
            continue
        fields: dict[str, float] = {}
        for f, flows in {**dur_flows, **shr_flows}.items():
            rec = flows.get(end)
            if rec is not None and rec.get("val") is not None:
                fields[f] = float(rec["val"])
        for f, names in _INSTANT.items():
            v = _instant_at(_concept_rows(gaap, names), end)
            if v is not None:
                fields[f] = v
        debt = _total_debt_at(gaap, end)
        if debt == debt:  # not NaN
            fields["totalDebt"] = debt
        # totalLiabilities fallback — many micro-caps don't tag `Liabilities` directly.
        # Accounting identity (Assets - Equity) is exact; else current + noncurrent.
        if "totalLiabilities" not in fields:
            la, eq = fields.get("totalAssets"), fields.get("totalStockholdersEquity")
            lnc = _instant_at(_concept_rows(gaap, ["LiabilitiesNoncurrent"]), end)
            if la is not None and eq is not None:
                fields["totalLiabilities"] = la - eq
            elif fields.get("totalCurrentLiabilities") is not None and lnc is not None:
                fields["totalLiabilities"] = fields["totalCurrentLiabilities"] + lnc
        # Share-count fallback — dilution (our strongest signal) needs a consistent count.
        # Diluted -> basic -> instant shares outstanding.
        if "weightedAverageShsOutDil" not in fields and "weightedAverageShsOut" in fields:
            fields["weightedAverageShsOutDil"] = fields["weightedAverageShsOut"]
        if "weightedAverageShsOut" not in fields and "weightedAverageShsOutDil" not in fields:
            so = _instant_at(_concept_rows(gaap, ["CommonStockSharesOutstanding",
                                                  "CommonStockSharesIssued"]), end)
            if so is not None:
                fields["weightedAverageShsOut"] = fields["weightedAverageShsOutDil"] = so
        # Derived composites the funnel also reads.
        if "ebit" in fields and "depreciationAndAmortization" in fields:
            fields["ebitda"] = fields["ebit"] + fields["depreciationAndAmortization"]
        gw, intang = fields.get("goodwill"), fields.get("intangibleAssets")
        if gw is not None or intang is not None:
            fields["goodwillAndIntangibleAssets"] = (gw or 0.0) + (intang or 0.0)
        if "cashAndCashEquivalents" in fields:
            fields["cashAndShortTermInvestments"] = (fields["cashAndCashEquivalents"]
                                                     + fields.get("shortTermInvestments", 0.0))
        if "operatingCashFlow" in fields:  # the funnel reads both names
            fields["netCashProvidedByOperatingActivities"] = fields["operatingCashFlow"]
        # totalEquity alias (some call sites use it interchangeably with stockholders' equity)
        if "totalStockholdersEquity" in fields:
            fields["totalEquity"] = fields["totalStockholdersEquity"]

        periods.append(Period(symbol=ticker, cik=cik, period_end=end, filing_date=filed[:10],
                              income=fields, balance={}, cashflow={}))
    periods.sort(key=lambda p: p.period_end, reverse=True)
    return periods


def as_of(ticker: str, as_of_date: str) -> Period | None:
    """Most recent annual period whose 10-K FILING date is on/before as_of_date (point-in-time)."""
    elig = [p for p in load_periods(ticker) if p.filing_date <= as_of_date]
    return max(elig, key=lambda p: p.filing_date) if elig else None


def prior_year(ticker: str, period: Period) -> Period | None:
    """The fiscal year one before `period` (for YoY trap-signal deltas)."""
    target = str(int(period.period_end[:4]) - 1)
    for p in load_periods(ticker):
        if p.period_end[:4] == target:
            return p
    return None
