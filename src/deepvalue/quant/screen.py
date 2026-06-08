"""
L1 screen (spec §5) — apply a screen profile to a universe as-of a date, emit ranked
QuantScreenResult records. Deterministic, no LLM: cheap reduction of thousands → a few
hundred candidates, each tagged with WHY it's cheap and HOW likely it's dying.

Point-in-time throughout: fundamentals via fundamentals_store.as_of (filing_date cut),
price via the grab cache at/just before that filing. A name with no statement filed yet,
no price, or missing inputs is simply not screenable (passed=False, excluded from rank).
"""
from __future__ import annotations

from datetime import date
from pathlib import Path

import yaml

from deepvalue.contracts.models import QuantScreenResult
from deepvalue.ingest.fundamentals_store import as_of, prior_year
from deepvalue.ingest.prices import get_prices
from deepvalue.quant.metrics import value_metrics
from deepvalue.quant.trap_signals import trap_signals

_PROFILES_PATH = Path("config/screen_profiles.yaml")


def load_profiles(path: Path | None = None) -> dict:
    return yaml.safe_load((path or _PROFILES_PATH).read_text())


def _price_asof(ticker: str, on_or_before: str) -> float | None:
    px = get_prices(ticker)
    elig = [d for d in px if d <= on_or_before]
    return px[max(elig)].get("close") if elig else None


def _passes(profile: str, vm: dict, thresholds: dict) -> bool:
    """Profile membership test. Conservative: a required metric being None = not a pass."""
    if profile == "net_net":
        ptn, ptbv = vm.get("price_to_ncav"), vm.get("p_tbv")
        return (ptn is not None and 0 < ptn <= thresholds.get("price_to_ncav_max", 0.67)
                and ptbv is not None and ptbv <= thresholds.get("p_tbv_max", 1.0))
    if profile == "normalized_earnings":
        ee = vm.get("ev_ebit")
        return ee is not None and 0 < ee <= thresholds.get("ev_ebit_max", 8.0)
    if profile == "hidden_assets":
        # needs non-operating asset value (real estate/securities/NOLs) — not derivable
        # from summary statements alone; deferred to a richer extractor.
        return False
    return False


def _rank_key(profile: str, vm: dict) -> float:
    """Lower = cheaper = better rank. Cheapest metric per profile."""
    key = {"net_net": "price_to_ncav", "normalized_earnings": "ev_ebit"}.get(profile)
    v = vm.get(key) if key else None
    return v if v is not None else float("inf")


def screen_name(ticker: str, cik: str, as_of_date: str, profile: str,
                profiles: dict) -> QuantScreenResult | None:
    """One name, one profile, point-in-time. None if no statement was filed by as_of_date
    or no price is available."""
    p = as_of(ticker, as_of_date)
    if p is None:
        return None
    price = _price_asof(ticker, p.filing_date)
    if price is None:
        return None
    vm = value_metrics(p, price)
    ts = trap_signals(p, prior_year(ticker, p), price)
    thresholds = profiles.get(profile, {}).get("thresholds", {})
    return QuantScreenResult(
        ticker=ticker, cik=str(cik or p.cik or ""), as_of=date.fromisoformat(as_of_date),
        screen_profile=profile, value_metrics=vm, trap_signals=ts,
        passed=_passes(profile, vm, thresholds), rank=0,
    )


def run_screen(universe: list[tuple[str, str]], as_of_date: str, profile: str,
               profiles: dict | None = None) -> list[QuantScreenResult]:
    """Screen a `(ticker, cik)` universe as-of a date; return PASSING names ranked cheapest
    first (rank 1 = cheapest). Non-passing / non-screenable names are dropped."""
    profs = profiles or load_profiles()
    results = [r for ticker, cik in universe
               if (r := screen_name(ticker, cik, as_of_date, profile, profs)) is not None
               and r.passed]
    results.sort(key=lambda r: _rank_key(profile, r.value_metrics))
    return [r.model_copy(update={"rank": i + 1}) for i, r in enumerate(results)]
