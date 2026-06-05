"""
Price access for deepvalue — reads the FMP survivorship grab cache on disk.

PORT-ADAPT (done): parley's fundamentals.py called an IBKR/EDGAR price cache via
`get_prices(ticker, period) -> {date: {"close": ..., ...}}`. deepvalue keeps that
contract but sources from the one-time FMP grab (`scripts/fmp_grab.py`), which wrote
survivorship-free daily history (incl. delisted names, clipped at delisting) to
`data/cache/prices/<key>.json`. No network here — the grab already happened.

A company can have more than one cache file: `<TICKER>__active.json` and/or
`<TICKER>__delisted_<YYYYMMDD>.json` (e.g. an acquired name the screener still flags
active). We union all of a ticker's files by date, so the caller sees the full series
regardless of which key holds it. Callers enforce point-in-time discipline themselves
by filtering to dates `<= as_of` (NEVER returns future-of-as_of filtering here — that's
the caller's job; this layer just serves what was grabbed). Paths are cwd-relative
(`data/cache/`, run from repo root), matching the other ingest modules.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

PRICES_DIR = Path("data/cache/prices")

# Numeric OHLCV fields to coerce to float so callers can do arithmetic directly.
_NUMERIC = ("open", "high", "low", "close", "volume", "vwap")


def _safe(ticker: str) -> str:
    """Same symbol sanitization the grab uses when forming cache keys."""
    return re.sub(r"[^A-Za-z0-9._-]", "_", ticker)


def get_prices(ticker: str, period: str = "max", cache_dir: Path | None = None) -> dict:
    """Daily history for `ticker` as `{date: {"open","high","low","close","volume","vwap"}}`.

    Returns the full grabbed series (newest dates included); `period` is accepted for
    backward-compat with the old IBKR signature but is advisory — the cache already holds
    max depth and callers slice by as-of date. Returns `{}` if the ticker isn't in the
    grabbed universe (a legitimate miss the replay loop treats as "skip"). Raises if the
    cache itself is missing (a setup error, not a per-ticker miss).
    """
    base = cache_dir if cache_dir is not None else PRICES_DIR
    if not base.exists():
        raise FileNotFoundError(
            f"price cache not found at {base} — run scripts/fmp_grab.py (from repo root) first"
        )

    out: dict[str, dict] = {}
    for path in sorted(base.glob(f"{_safe(ticker)}__*.json")):
        rows = json.loads(path.read_text()).get("rows", [])
        for row in rows:
            date = row.get("date")
            if not date:
                continue
            out[date] = {k: _coerce(row.get(k)) for k in _NUMERIC}
    return out


def _coerce(value):
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None
