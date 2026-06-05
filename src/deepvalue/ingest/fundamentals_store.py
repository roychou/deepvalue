"""
Point-in-time fundamentals accessor over the FMP grab cache (data/cache/fundamentals/).

The grab stored per-name annual income + balance statements, each carrying `filingDate`
(when it hit EDGAR). This module merges them into period records and exposes an `as_of`
selector that returns ONLY statements whose filingDate <= as_of — the load-bearing
point-in-time rule (CLAUDE.md: use filing_date, NEVER period_of_report). A backtest that
reads a statement before it was filed is lookahead bias; this is where that's enforced.

No cash-flow statements were grabbed, so cash-flow items are absent here; trap signals
that need operating cash flow approximate it (see quant/trap_signals.py).
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path

FUND_DIR = Path("data/cache/fundamentals")


@dataclass(frozen=True)
class Period:
    """One fiscal period's merged income + balance statement, with its filing date."""
    symbol: str
    cik: str | None
    period_end: str          # period_of_report — for labeling ONLY, never for as-of cuts
    filing_date: str         # the point-in-time key
    income: dict
    balance: dict

    def get(self, field: str, default=None):
        """Read a line item from income or balance (income wins on the rare overlap)."""
        v = self.income.get(field, self.balance.get(field, default))
        return v if v is not None else default


def _safe(symbol: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]", "_", symbol)


def _file_for(symbol: str, cache_dir: Path) -> Path | None:
    # fundamentals were keyed by the same symbol+status keys as prices; try active then any
    direct = cache_dir / f"{_safe(symbol)}__active.json"
    if direct.exists():
        return direct
    hits = sorted(cache_dir.glob(f"{_safe(symbol)}__*.json"))
    return hits[0] if hits else None


def load_periods(symbol: str, cache_dir: Path | None = None) -> list[Period]:
    """All fiscal periods for a name, NEWEST FIRST, merging income+balance by period date."""
    base = cache_dir if cache_dir is not None else FUND_DIR
    path = _file_for(symbol, base)
    if path is None:
        return []
    data = json.loads(path.read_text())
    bal_by_date = {r.get("date"): r for r in data.get("balance", []) if r.get("date")}
    periods = []
    for inc in data.get("income", []):
        d = inc.get("date")
        filed = inc.get("filingDate") or inc.get("acceptedDate")
        if not d or not filed:
            continue
        periods.append(Period(
            symbol=symbol, cik=data.get("cik"), period_end=d, filing_date=filed[:10],
            income=inc, balance=bal_by_date.get(d, {}),
        ))
    periods.sort(key=lambda p: p.period_end, reverse=True)
    return periods


def as_of(symbol: str, as_of_date: str, cache_dir: Path | None = None) -> Period | None:
    """Most recent period whose FILING date is on/before as_of_date (point-in-time).
    Returns None if nothing was filed yet as of that date."""
    eligible = [p for p in load_periods(symbol, cache_dir) if p.filing_date <= as_of_date]
    return max(eligible, key=lambda p: p.filing_date) if eligible else None


def prior_year(symbol: str, period: Period, cache_dir: Path | None = None) -> Period | None:
    """The fiscal period one year before `period` (for YoY trap-signal deltas)."""
    target = str(int(period.period_end[:4]) - 1)
    for p in load_periods(symbol, cache_dir):
        if p.period_end[:4] == target:
            return p
    return None
