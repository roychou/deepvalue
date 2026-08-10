"""
Per-name EDGAR failures must not cost the week — the 2026-08-03 outage post-mortem.

That Monday the whole weekly run died on ONE company: SEC serves 404/NoSuchKey for filers that
have never filed XBRL, `fetch_company_facts` raised, and nothing between it and `main()` caught
it. These tests pin both halves of the fix: a 404 is a fact about one filer (skip it), any other
EDGAR error is a fact about EDGAR (stay loud) — and the tolerance for the latter is bounded, so
an outage can never masquerade as a quiet week with no candidates.
"""
from __future__ import annotations

import pytest

from deepvalue.forward import run as fwd
from deepvalue.ingest import edgar_fundamentals as ef
from deepvalue.ingest.edgar import EdgarError, EdgarNotFound


@pytest.fixture(autouse=True)
def _clear_period_cache():
    """load_periods is lru_cached — isolate tests from each other's fake tickers."""
    ef.load_periods.cache_clear()
    yield
    ef.load_periods.cache_clear()


# --- the 404 contract ----------------------------------------------------------------------

def test_no_xbrl_filer_reads_as_no_data_not_an_error(monkeypatch):
    """CIK 0000071508's exact failure: 404 must become 'not screenable', not an exception."""
    def _404(ticker):
        raise EdgarNotFound("EDGAR has no resource at .../companyfacts/CIK0000071508.json")

    monkeypatch.setattr(ef, "fetch_company_facts", _404)
    assert ef.load_periods("NOXBRL") == []
    assert ef.as_of("NOXBRL", "2026-08-03") is None


def test_edgar_outage_still_raises(monkeypatch):
    """403/429/5xx are EDGAR failing, not the company — these must NOT be swallowed."""
    def _throttled(ticker):
        raise EdgarError("EDGAR returned 429 for ...: rate limited")

    monkeypatch.setattr(ef, "fetch_company_facts", _throttled)
    with pytest.raises(EdgarError):
        ef.load_periods("BLOCKED")


def test_not_found_is_an_edgar_error_subclass():
    """Existing `except EdgarError` handlers must keep catching 404s."""
    assert issubclass(EdgarNotFound, EdgarError)


# --- the prefilter's bounded tolerance -------------------------------------------------------

def _universe(n: int) -> list[tuple[str, str]]:
    return [(f"T{i}", str(1000 + i)) for i in range(n)]


def test_one_bad_filer_no_longer_kills_the_run(monkeypatch):
    """The regression itself: a single unreadable name among many must not abort."""
    def _as_of(ticker, as_of):
        if ticker == "T3":
            raise EdgarNotFound("no companyfacts")
        return None  # everything else: screenable but not passing — irrelevant here

    monkeypatch.setattr(fwd.ef, "as_of", _as_of)
    assert fwd.fundamental_prefilter(_universe(20), "2026-08-03", min_f=5) == []


def test_mass_failure_aborts_loudly(monkeypatch):
    """An EDGAR-wide outage must NOT quietly produce an empty book that reads as a quiet week."""
    monkeypatch.setattr(fwd.ef, "as_of",
                        lambda t, a: (_ for _ in ()).throw(EdgarError("EDGAR returned 503")))
    with pytest.raises(EdgarError, match="EDGAR looks unavailable"):
        fwd.fundamental_prefilter(_universe(200), "2026-08-03", min_f=5)


def test_tolerance_scales_with_universe_size():
    assert fwd._max_edgar_failures(10) == 10      # floor: small universes get absolute slack
    assert fwd._max_edgar_failures(1000) == 150   # 15% of a large one
