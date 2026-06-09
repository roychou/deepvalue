"""
L7 — calibration metrics: the live DECAY ALARM.

The founding risk is that the Tedium Premium edge erodes as filing-reading commoditizes. These
functions check, across the accumulated weekly books, whether the MD&A Deterioration Lead still
predicts forward underperformance OUT-OF-SAMPLE: the forward rank-IC of -deterioration vs realized
return (positive = deteriorating names underperform = edge holds), and the BUY-vs-WATCH spread.

Reuses eval.ic.spearman. v1 — a quick pooled check, not cohort-weighted; reports None until
forward data accrues. The deeper per-agent precision/recall/Brier and the feedback->agent-weight
loop (feedback.py) stay greenfield.
"""
from __future__ import annotations

from statistics import mean

from deepvalue.calibration.outcomes import forward_return
from deepvalue.eval.ic import spearman


def deterioration_ic(books: list[dict], prices_by_ticker: dict[str, dict],
                     horizon_days: int = 126) -> dict:
    """Forward rank-IC of -deterioration vs realized return, pooled across books. Positive IC =>
    deteriorating-language names underperformed => the live edge is intact."""
    dets, rets = [], []
    for book in books:
        as_of = book.get("as_of")
        for c in book.get("book", []):
            d = c.get("deterioration")
            if d is None:
                continue
            r = forward_return(prices_by_ticker.get(c["ticker"], {}), as_of, horizon_days)
            if r is None:
                continue
            dets.append(-float(d))   # negate so a POSITIVE IC means the edge holds
            rets.append(r)
    ic = spearman(dets, rets) if len(dets) >= 3 else None
    return {"horizon_days": horizon_days, "n": len(dets), "ic_neg_deterioration": ic}


def verdict_spread(books: list[dict], prices_by_ticker: dict[str, dict],
                   horizon_days: int = 126) -> dict:
    """Mean forward return of BUY vs WATCH names — does the verdict tier separate outcomes?"""
    buy, watch = [], []
    for book in books:
        as_of = book.get("as_of")
        for c in book.get("book", []):
            r = forward_return(prices_by_ticker.get(c["ticker"], {}), as_of, horizon_days)
            if r is None:
                continue
            (buy if c.get("verdict") == "BUY" else watch).append(r)
    return {"horizon_days": horizon_days, "n_buy": len(buy), "n_watch": len(watch),
            "buy_mean": mean(buy) if buy else None, "watch_mean": mean(watch) if watch else None,
            "spread": (mean(buy) - mean(watch)) if (buy and watch) else None}
