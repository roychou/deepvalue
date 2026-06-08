"""
L0 — filing segmentation: carve a 10-K/10-Q into canonical sections (MD&A first) with high
coverage, superseding the narrow edgar_filings.extract_sections heuristic.

Cascade (cheapest first):
  1. DETERMINISTIC (free): TOC-aware start search + multi-pattern start/end PANELS + an
     over-capture guard (smallest validated span). Fixes the two failure modes that capped the
     old extractor at ~57%: (a) narrow patterns missing 'Item 7.' / all-caps / no-Item-7A
     filers, and (b) a TOC 'Item 7' -> body 'Item 7A' largest-span over-capture.
  2. LLM FALLBACK (opt-in, capped): only when (1) fails. The locate-a-boundary task is easy, so
     a cheap model (config/models.yaml 'segmentation', default Haiku) suffices; escalate only if
     accuracy disappoints. Not auto-run — pass a client + cap to enable (spend rule).

Returns the section text, a confidence, which method found it, and sentence IDs (normalize.py).
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from deepvalue.ingest.edgar_filings import clean_text
from deepvalue.ingest.normalize import to_sentences

_APOS = "['’‘]?"  # straight / curly apostrophe, optional

# START panel for MD&A: Item 7 (10-K) / Item 2 (10-Q), period optional; + all-caps standalone.
_MDNA_STARTS = [
    r"Item\s*7\.?\s*A?\b.{0,60}?Management" + _APOS + r"s\s+Discussion\s+and\s+Analysis",
    r"Item\s*2\.?\b.{0,60}?Management" + _APOS + r"s\s+Discussion\s+and\s+Analysis",   # 10-Q Part I
    r"MANAGEMENT" + _APOS + r"S\s+DISCUSSION\s+AND\s+ANALYSIS",                         # all-caps header
]
# END panel: the section after MD&A. Item 7A (Quant) else Item 8 (Financial Statements) for a
# 10-K; Item 3 (Quant) / Item 4 (Controls) for a 10-Q.
_MDNA_ENDS = [
    r"Item\s*7A\.?\b.{0,60}?Quantitative\s+and\s+Qualitative",
    r"Item\s*8\.?\b.{0,60}?Financial\s+Statements",
    r"Item\s*3\.?\b.{0,60}?Quantitative\s+and\s+Qualitative",
    r"Item\s*4\.?\b.{0,40}?Controls\s+and\s+Procedures",
]
_ANCHOR = "results of operations"   # a real MD&A body contains this
_MIN_CHARS, _MAX_CHARS = 1500, 250_000


@dataclass(frozen=True)
class Section:
    canonical_id: str          # e.g. "10-K.mdna"
    text: str | None
    method: str                # "heuristic" | "llm" | "none"
    confidence: float

    @property
    def sentences(self) -> list[dict]:
        return to_sentences(self.text) if self.text else []


def _toc_end(text: str) -> int:
    """Heuristic end-of-table-of-contents offset. The TOC is a dense cluster of 'Item N' refs
    near the top; return the offset after the last one in the first ~15% of the doc so body
    search starts below it. 0 when no dense cluster is detected."""
    head_len = max(3000, len(text) // 7)
    items = [m.end() for m in re.finditer(r"Item\s*\d+A?\b", text[:head_len], re.IGNORECASE)]
    return items[-1] if len(items) >= 4 else 0


def _find(text: str, pats: list[str], *, after: int = 0) -> list[int]:
    return sorted({m.start() for pat in pats for m in re.finditer(pat, text, re.IGNORECASE)
                   if m.start() >= after})


def _best_span(text: str, starts: list[int], ends: list[int]) -> str | None:
    """SMALLEST validated (start, first-end-after) span containing the anchor and within the
    size band — smallest beats largest precisely because a TOC-rooted start yields a huge span
    while the true body start yields the tight one."""
    best: tuple[int, int] | None = None
    for s in starts:
        e = next((x for x in ends if x > s), None)
        if e is None:
            continue
        seg = text[s:e]
        if not (_MIN_CHARS <= len(seg) <= _MAX_CHARS) or _ANCHOR not in seg.lower():
            continue
        if best is None or (e - s) < (best[1] - best[0]):
            best = (s, e)
    return text[best[0]:best[1]].strip() if best else None


def _heuristic_mdna(clean: str) -> str | None:
    """Deterministic MD&A extraction. Try below the TOC first (kills over-capture); if that
    finds nothing (over-aggressive TOC cut, or no TOC), retry over the whole document."""
    ends = _find(clean, _MDNA_ENDS)
    if not ends:
        return None
    toc = _toc_end(clean)
    for after in (toc, 0) if toc else (0,):
        seg = _best_span(clean, _find(clean, _MDNA_STARTS, after=after), ends)
        if seg:
            return seg
    return None


def segment_mdna(html: str, form: str, *, llm_client=None, max_llm_usd: float = 0.0) -> Section:
    """Carve MD&A from a filing's HTML. Deterministic by default; pass llm_client + a cap to
    enable the locate-a-boundary fallback on the names heuristics miss (opt-in; spends LLM)."""
    base = form.upper().split("/")[0]
    clean = clean_text(html)
    seg = _heuristic_mdna(clean)
    if seg:
        return Section(f"{base}.mdna", seg, "heuristic", 0.9)
    if llm_client is not None and max_llm_usd > 0:
        # TODO(fallback): ask the cheap 'segmentation' model for MD&A start/end offsets given the
        # candidate headers (NOT the whole filing). Only worth wiring if the heuristic residual
        # is large — measure first. Spends LLM => operator go + cap.
        pass
    return Section(f"{base}.mdna", None, "none", 0.0)


def extract_mdna(html: str, form: str) -> str | None:
    """Convenience: just the MD&A text (or None). The drop-in L3 uses in place of the old
    edgar_filings.extract_sections(...).get('mdna')."""
    return segment_mdna(html, form).text
