"""
L7 — outcome tracking (spec §11). Link each weekly book's verdicts to subsequent FORWARD
RETURNS, so the live edge can be measured and the decay alarm can sound.

v1 = the data layer: read the emitted candidate books (data/forward/book_<as_of>.json), pair each
name with its forward return from a price source, and build the §12 OutcomeRecord list. The
calibration -> agent-weight FEEDBACK loop (feedback.py) stays greenfield. Returns are None where
forward data isn't in yet (the live rig only just started), so this is ready to accrue over time.
"""
from __future__ import annotations

import json
from datetime import date, timedelta
from pathlib import Path

from deepvalue.contracts.models import OutcomeRecord

FORWARD_DIR = Path("data/forward")


def load_books(forward_dir: Path = FORWARD_DIR) -> list[dict]:
    """All emitted candidate books, oldest first (each: {as_of, universe, candidates, book})."""
    books: list[dict] = []
    for p in sorted(forward_dir.glob("book_*.json")):
        try:
            books.append(json.loads(p.read_text()))
        except Exception:  # noqa: BLE001 — a corrupt artifact shouldn't break the rest
            continue
    return books


def forward_return(prices: dict[str, dict], start: str, horizon_days: int) -> float | None:
    """Total return from the close on/just before `start` to the first close on/after
    start+horizon_days. None if either endpoint is missing (forward data not in yet)."""
    ds = sorted(prices)
    s = max((d for d in ds if d <= start), default=None)
    if s is None:
        return None
    target = (date.fromisoformat(s) + timedelta(days=horizon_days)).isoformat()
    e = min((d for d in ds if d >= target), default=None)
    if e is None:
        return None
    p0, p1 = prices[s].get("close"), prices[e].get("close")
    return (p1 / p0 - 1.0) if (p0 and p1 and p0 > 0) else None


def score_outcomes(books: list[dict], prices_by_ticker: dict[str, dict],
                   horizons: tuple[int, ...] = (63, 126, 252)) -> list[OutcomeRecord]:
    """One OutcomeRecord per (book, name): the verdict, its flags, and forward returns at each
    horizon (None until the data accrues). `prices_by_ticker` = {ticker: {date: {close,...}}}."""
    recs: list[OutcomeRecord] = []
    for book in books:
        as_of = book.get("as_of")
        if not as_of:
            continue
        for c in book.get("book", []):
            px = prices_by_ticker.get(c["ticker"], {})
            fwd = {f"{h}d": forward_return(px, as_of, h) for h in horizons}
            recs.append(OutcomeRecord(
                verdict_id=f"{as_of}:{c['ticker']}", ticker=c["ticker"],
                decision=c.get("verdict", "WATCH"), conviction=float(c.get("composite") or 0.0),
                as_of=date.fromisoformat(as_of), outcome_events=c.get("flags", []),
                forward_returns=fwd, realized=any(v is not None for v in fwd.values())))
    return recs
