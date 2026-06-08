"""
L3 — the MD&A Deterioration Lead, wired for the live forward session.

The validated edge: quiet year-over-year deterioration in 10-K MD&A language LEADS the hard
distress events (~3-6 months), so a candidate whose language is softening is a trap-in-waiting
even if the quant screen looks clean. This module scores that for the book shortlist.

Reuses the proven backtest path (scripts/l1l3_sharadar_backtest.py): fetch ONLY the two needed
10-Ks (current + prior, point-in-time), isolate the changed MD&A spans (diff/align), and score
deterioration with the Sonnet materiality reader (diff/materiality) under a HARD spend cap.

SPENDS LLM (~$0.01/name on the changed spans). Caller passes max_llm_usd; this stops at the cap.
"""
from __future__ import annotations

import logging

from deepvalue.diff.align import changed_text
from deepvalue.diff.materiality import MaterialityResult, score_materiality
from deepvalue.ingest.edgar import filings_by_cik
from deepvalue.ingest.edgar_filings import fetch_filing_document_by_cik
from deepvalue.ingest.segmentation import extract_mdna

log = logging.getLogger("tedium.forward.l3")


def _mdna(cik: str, filing: dict, cache: dict) -> str | None:
    """Extracted MD&A for one 10-K, cached per accession; fetches only what's needed."""
    key = filing["accession"]
    if key not in cache:
        try:
            html = fetch_filing_document_by_cik(cik, filing["accession"], filing["primary_document"])
            cache[key] = extract_mdna(html, "10-K")  # robust segmenter (over-capture-guarded)
        except Exception as e:  # noqa: BLE001 — a bad fetch drops the name, never the run
            log.warning("MD&A fetch failed (cik=%s acc=%s): %s", cik, key, type(e).__name__)
            cache[key] = None
    return cache[key]


def changed_mdna(cik: str, as_of: str, cache: dict) -> str | None:
    """Changed MD&A spans for the 10-K filed at/just before as_of vs its prior — point-in-time,
    fetching ONLY those two documents."""
    fils = filings_by_cik(cik, forms=("10-K",))  # newest-first; cached submissions
    idx = next((i for i, f in enumerate(fils) if f["filed"] <= as_of), None)
    if idx is None or idx + 1 >= len(fils):
        return None
    cur, pri = _mdna(cik, fils[idx], cache), _mdna(cik, fils[idx + 1], cache)
    return (changed_text(cur, pri) or None) if (cur and pri) else None


def score_deterioration(book: list[dict], as_of: str, client, *,
                        max_llm_usd: float) -> tuple[dict[str, MaterialityResult], float]:
    """Score MD&A deterioration for each book candidate (current vs prior 10-K), respecting the
    HARD spend cap. Returns ({ticker: MaterialityResult}, total_spend_usd). The Deterioration Lead.

    Stops cleanly at the cap — partial coverage is logged, never a silent overspend. Names with
    no usable 10-K pair (first-year filers, fetch failures) are simply skipped."""
    cache: dict = {}
    spent = 0.0
    out: dict[str, MaterialityResult] = {}
    for c in book:
        if spent >= max_llm_usd:
            log.warning("Deterioration Lead hit $%.2f cap; %d/%d names scored",
                        max_llm_usd, len(out), len(book))
            break
        cik = c.get("cik")
        if not cik:
            continue
        ct = changed_mdna(str(cik), as_of, cache)
        if not ct:
            continue
        try:
            res = score_materiality(client, ct)
        except Exception as e:  # noqa: BLE001 — one bad score can't kill the session
            log.warning("materiality scoring failed for %s: %s", c.get("ticker"), type(e).__name__)
            continue
        spent += res.cost_usd
        out[c["ticker"]] = res
    return out, spent
