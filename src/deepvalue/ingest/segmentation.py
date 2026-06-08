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

import json
import logging
import re
from dataclasses import dataclass

from deepvalue.ingest.edgar_filings import clean_text
from deepvalue.ingest.normalize import to_sentences

log = logging.getLogger("tedium.segmentation")

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
    cost_usd: float = 0.0      # LLM-fallback spend (0 for the deterministic path)

    @property
    def sentences(self) -> list[dict]:
        return to_sentences(self.text) if self.text else []


@dataclass
class SegLLM:
    """Config for the locate-a-boundary fallback. The task is easy -> a cheap model (Haiku)."""
    client: object             # anthropic.Anthropic
    model: str
    spec: object               # models.ModelSpec (for cost)
    cap_usd: float


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


_LOCATE_PROMPT = (
    "Below is plain text from a SEC 10-K, starting near Management's Discussion and Analysis "
    "(Item 7). Return STRICT JSON: {\"start_phrase\": <first ~10 words of the MD&A section body>, "
    "\"end_phrase\": <first ~10 words of the section that immediately FOLLOWS MD&A, e.g. 'Item 7A "
    "Quantitative and Qualitative...' or 'Item 8 Financial Statements...'>}. Copy the phrases "
    "VERBATIM from the text so they can be located. If there is no MD&A body here, return "
    "{\"start_phrase\": null, \"end_phrase\": null}.\n\nTEXT:\n"
)


def _llm_locate(clean: str, seg: SegLLM) -> tuple[str | None, float]:
    """Ask the cheap model for the MD&A start/end PHRASES, then slice the deterministic text
    between them (lean output, no hallucinated content). Returns (mdna_or_none, cost_usd)."""
    from deepvalue.diff.materiality import call_cost_usd

    starts = _find(clean, _MDNA_STARTS)
    region_start = starts[0] if starts else 0
    region = clean[region_start: region_start + 140_000]  # from the first MD&A cue; bound tokens
    try:
        resp = seg.client.messages.create(
            model=seg.model, max_tokens=300,
            messages=[{"role": "user", "content": _LOCATE_PROMPT + region}])
    except Exception as e:  # noqa: BLE001 — fallback failure just drops the name
        log.warning("LLM locate failed: %s", type(e).__name__)
        return None, 0.0
    cost = call_cost_usd(resp.usage, spec=seg.spec)
    try:
        txt = resp.content[0].text
        obj = json.loads(re.search(r"\{.*\}", txt, re.S).group(0))
        sp, ep = obj.get("start_phrase"), obj.get("end_phrase")
    except Exception:  # noqa: BLE001
        return None, cost
    if not sp:
        return None, cost
    si = clean.lower().find(sp.strip().lower()[:48])
    if si < 0:
        return None, cost
    ei = clean.lower().find(ep.strip().lower()[:48], si + _MIN_CHARS) if ep else -1
    body = clean[si: ei if ei > si else si + _MAX_CHARS].strip()
    if not (_MIN_CHARS <= len(body) <= _MAX_CHARS) or _ANCHOR not in body.lower():
        return None, cost
    return body, cost


def segment_mdna(html: str, form: str, *, seg_llm: SegLLM | None = None) -> Section:
    """Carve MD&A from a filing's HTML. Deterministic by default; pass seg_llm to enable the
    locate-a-boundary fallback on the ~35% the heuristics miss (opt-in; spends a little LLM)."""
    base = form.upper().split("/")[0]
    clean = clean_text(html)
    seg = _heuristic_mdna(clean)
    if seg:
        return Section(f"{base}.mdna", seg, "heuristic", 0.9)
    if seg_llm is not None and seg_llm.cap_usd > 0:
        body, cost = _llm_locate(clean, seg_llm)
        if body:
            return Section(f"{base}.mdna", body, "llm", 0.7, cost)
        return Section(f"{base}.mdna", None, "none", 0.0, cost)
    return Section(f"{base}.mdna", None, "none", 0.0)


def extract_mdna(html: str, form: str, *, seg_llm: SegLLM | None = None) -> str | None:
    """Convenience: just the MD&A text (or None). The drop-in L3 uses in place of the old
    edgar_filings.extract_sections(...).get('mdna')."""
    return segment_mdna(html, form, seg_llm=seg_llm).text
