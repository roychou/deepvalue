"""
Information Coefficient (IC) — the cross-sectional, statistically-efficient read on
whether the model's conviction predicts forward returns.

WHY THIS MODULE EXISTS (productization.md Phase 0 / forward-validation-design.md):
A backtest cannot prove this strategy's edge — the only contamination-free window is
the ~16 weekly dates after the specialist models' training cutoff (temporal.py), and a
portfolio Sharpe over 16 return points is pure noise. Sharpe collapses each date's ~100
cross-sectional bets into ONE portfolio number; it throws the cross-section away.

IC keeps the cross-section. On each clean date we rank-correlate signed conviction
against forward return across the whole universe (~100 names) — ~100 observations per
date, ~1,600 over the clean window, not 16. Mean IC and its t-stat across dates is the
disqualify-fast signal: if clean IC <= 0 with a tight CI, that's a real negative verdict
months before Sharpe could say anything (Grinold's fundamental law: IR = IC x sqrt(breadth),
breadth = names x dates).

Everything here is pure Python (no numpy/pandas) — same discipline as metrics.py, and it
keeps the module trivially testable and dependency-free.

HONEST CAVEAT baked into the API: weekly decisions with multi-week forward windows produce
OVERLAPPING returns, which autocorrelate IC and inflate the naive t-stat. `ic_summary`
reports the naive cross-date t-stat AND flags when the horizon exceeds the spacing so the
caller knows the t-stat is optimistic until horizon-matched non-overlapping dates (or a
Newey-West correction) are used. See `select_non_overlapping`.
"""
from __future__ import annotations

import math
from dataclasses import dataclass

# ==========================================
# RANK + SPEARMAN (pure Python)
# ==========================================


def _fractional_ranks(values: list[float]) -> list[float]:
    """Ranks with average-tie handling (1-based). [3, 1, 1, 2] -> [4.0, 1.5, 1.5, 3.0]."""
    order = sorted(range(len(values)), key=lambda i: values[i])
    ranks = [0.0] * len(values)
    i = 0
    while i < len(order):
        j = i
        while j + 1 < len(order) and values[order[j + 1]] == values[order[i]]:
            j += 1
        avg = (i + j) / 2 + 1  # average of the tied positions, 1-based
        for k in range(i, j + 1):
            ranks[order[k]] = avg
        i = j + 1
    return ranks


def _pearson(xs: list[float], ys: list[float]) -> float | None:
    """Pearson correlation; None if undefined (zero variance or < 2 points)."""
    n = len(xs)
    if n < 2:
        return None
    mx = sum(xs) / n
    my = sum(ys) / n
    sxy = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    sxx = sum((x - mx) ** 2 for x in xs)
    syy = sum((y - my) ** 2 for y in ys)
    if sxx == 0 or syy == 0:
        return None
    return sxy / math.sqrt(sxx * syy)


def spearman(xs: list[float], ys: list[float]) -> float | None:
    """Spearman rank correlation = Pearson on fractional ranks. None if undefined."""
    if len(xs) != len(ys):
        raise ValueError("spearman: xs and ys must be the same length")
    if len(xs) < 2:
        return None
    return _pearson(_fractional_ranks(xs), _fractional_ranks(ys))


# ==========================================
# FORWARD RETURNS (from the price cache shape: date -> {open,high,low,close,...})
# ==========================================


def forward_return(
    prices: dict[str, dict], as_of: str, horizon_days: int
) -> float | None:
    """Simple close-to-close return from the first trading day >= as_of, horizon_days
    trading days forward. None if the window runs off the end of the cache.

    Prices are split-adjusted (not div-adjusted) — fine for a rank correlation, where
    only cross-sectional ordering matters and dividend yield differences are second-order.
    """
    if horizon_days <= 0:
        raise ValueError("forward_return: horizon_days must be positive")
    dates = sorted(prices)
    start_idx = next((i for i, d in enumerate(dates) if d >= as_of), None)
    if start_idx is None or start_idx + horizon_days >= len(dates):
        return None
    p0 = prices[dates[start_idx]].get("close")
    p1 = prices[dates[start_idx + horizon_days]].get("close")
    if not p0 or not p1 or p0 <= 0:
        return None
    return p1 / p0 - 1.0


# ==========================================
# CROSS-SECTIONAL IC
# ==========================================


@dataclass(frozen=True)
class ICResult:
    """IC for a single decision date."""

    as_of: str
    ic: float | None      # Spearman(conviction, forward_return); None if < 2 usable names
    n: int                # number of names that entered the correlation


def cross_sectional_ic(
    as_of: str,
    convictions: dict[str, float],
    forward_returns: dict[str, float | None],
) -> ICResult:
    """Rank-correlate signed conviction vs forward return across names present in BOTH,
    dropping any name whose conviction or forward return is missing/None.

    convictions: ticker -> signed conviction (e.g. confidence x sign(direction), where
                 BUY=+1, SELL=-1, HOLD=0). Abstentions must be ABSENT, not 0 — a name the
                 model declined to score is missing data, not a neutral bet.
    """
    names = [
        t for t in convictions
        if t in forward_returns
        and forward_returns[t] is not None
        and convictions[t] is not None
    ]
    if len(names) < 2:
        return ICResult(as_of=as_of, ic=None, n=len(names))
    xs = [convictions[t] for t in names]
    ys = [forward_returns[t] for t in names]  # type: ignore[misc]
    return ICResult(as_of=as_of, ic=spearman(xs, ys), n=len(names))


# ==========================================
# AGGREGATION ACROSS DATES
# ==========================================


@dataclass(frozen=True)
class ICSummary:
    n_dates: int          # dates with a defined IC
    total_obs: int        # sum of per-date name counts (the real breadth)
    mean_ic: float | None
    std_ic: float | None  # sample std of per-date ICs
    ic_ir: float | None   # mean_ic / std_ic  (information ratio of the IC series)
    t_stat: float | None  # ic_ir * sqrt(n_dates) — NAIVE; see overlap_warning
    overlap_warning: bool # True when forward windows overlap -> t_stat is optimistic


def ic_summary(
    results: list[ICResult], horizon_days: int, spacing_days: int
) -> ICSummary:
    """Aggregate per-date ICs into mean IC, IC-IR, and a cross-date t-stat.

    horizon_days vs spacing_days: if the forward-return horizon exceeds the spacing
    between decision dates, the windows OVERLAP, the per-date ICs autocorrelate, and the
    naive t-stat (which assumes independent dates) is inflated. We still report it — it's
    the right point estimate — but flag overlap so the number isn't over-read. The honest
    fix is `select_non_overlapping` or a Newey-West SE (future work).
    """
    ics = [r.ic for r in results if r.ic is not None]
    total_obs = sum(r.n for r in results if r.ic is not None)
    n = len(ics)
    if n == 0:
        return ICSummary(0, total_obs, None, None, None, None, horizon_days > spacing_days)
    mean_ic = sum(ics) / n
    if n < 2:
        std = None
        ic_ir = None
        t_stat = None
    else:
        var = sum((x - mean_ic) ** 2 for x in ics) / (n - 1)
        std = math.sqrt(var)
        ic_ir = mean_ic / std if std > 0 else None
        t_stat = ic_ir * math.sqrt(n) if ic_ir is not None else None
    return ICSummary(
        n_dates=n,
        total_obs=total_obs,
        mean_ic=mean_ic,
        std_ic=std,
        ic_ir=ic_ir,
        t_stat=t_stat,
        overlap_warning=horizon_days > spacing_days,
    )


def select_non_overlapping(dates: list[str], horizon_days: int, spacing_days: int) -> list[str]:
    """Thin a sorted date list so consecutive kept dates are >= horizon_days apart (in
    trading-day terms, approximated by spacing_days per step), removing the overlap that
    inflates the IC t-stat. Trades breadth for an honest, independent-sample t-stat.
    """
    if not dates:
        return []
    step = max(1, math.ceil(horizon_days / max(1, spacing_days)))
    return sorted(dates)[::step]
