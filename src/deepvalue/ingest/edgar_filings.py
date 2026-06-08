"""
EDGAR filing-document layer: fetch the primary 10-Q/10-K HTML and extract the
narrative sections (MD&A, Risk Factors) that feed the sentiment specialist.

Extraction is **best-effort** (title-anchored section headers + a validation gate).
Item anchors appear in the TOC, the body header, and inline cross-references, and
10-Qs repeat item numbers across Part I/II — so both the start and end boundaries
are anchored on the *item number + section title together*, and among candidates we
take the pair with the largest span (the real section body is long). If extraction
fails its validation gate, the section is returned as None and the caller (the
map-reduce scaffold) falls back to processing the whole cleaned filing. See
notes/sentiment-specialist-design.md.
"""
from __future__ import annotations

import html as _html
import logging
import re

from deepvalue.ingest.edgar import (
    CACHE_DIR,
    SEC_WWW_BASE,
    _get_text,
    filing_doc_url_by_cik,
    ticker_to_cik,
)

logger = logging.getLogger(__name__)

FILINGS_DIR = CACHE_DIR / "filings"

_APOS = "['’‘]?"  # straight / curly apostrophe, optional

# Title-anchored section header patterns (item number + title within a small window).
_START_MDNA = r"Item\s+\d+A?\b.{0,40}?Management" + _APOS + r"s\s+Discussion\s+and\s+Analysis"
_END_QUANT = r"Item\s+\d+A?\b.{0,40}?Quantitative\s+and\s+Qualitative"
_START_RISK = r"Item\s+1A\b.{0,40}?Risk\s*Factors"
# Section that follows Risk Factors: Item 1B in a 10-K, Part II Item 2 in a 10-Q.
_END_UNRESOLVED = r"Item\s+1B\b.{0,40}?Unresolved\s+Staff\s+Comments"
_END_UNREGISTERED = r"Item\s+\d+\b.{0,40}?Unregistered\s+Sales"

# form -> section -> (start_pattern, end_pattern, validation_anchor)
_SPECS: dict[str, dict[str, tuple[str, str, str]]] = {
    "10-Q": {
        "mdna": (_START_MDNA, _END_QUANT, "Results of Operations"),
        "risk_factors": (_START_RISK, _END_UNREGISTERED, "Risk Factors"),
    },
    "10-K": {
        "mdna": (_START_MDNA, _END_QUANT, "Results of Operations"),
        "risk_factors": (_START_RISK, _END_UNRESOLVED, "Risk Factors"),
    },
}

_MIN_SECTION_CHARS = 1500
_MAX_SECTION_CHARS = 400_000


# ==========================================
# FETCH
# ==========================================


def filing_url(ticker: str, accession: str, primary_document: str) -> str:
    cik = int(ticker_to_cik(ticker))  # archives path uses the un-padded CIK
    return f"{SEC_WWW_BASE}/Archives/edgar/data/{cik}/{accession}/{primary_document}"


def fetch_filing_document(ticker: str, accession: str, primary_document: str) -> str:
    """Fetch (and cache) the primary filing HTML. Keyed by accession (immutable)."""
    FILINGS_DIR.mkdir(parents=True, exist_ok=True)
    path = FILINGS_DIR / f"{ticker.upper()}_{accession}.htm"
    if path.exists():
        return path.read_text(encoding="utf-8", errors="ignore")
    html = _get_text(filing_url(ticker, accession, primary_document))
    path.write_text(html, encoding="utf-8")
    return html


def fetch_filing_document_by_cik(cik: str, accession: str, primary_document: str) -> str:
    """Like `fetch_filing_document` but keyed by CIK — the survivorship-correct path for
    delisted names whose ticker no longer resolves. Cached by accession (immutable)."""
    FILINGS_DIR.mkdir(parents=True, exist_ok=True)
    path = FILINGS_DIR / f"CIK{str(cik).zfill(10)}_{accession}.htm"
    if path.exists():
        return path.read_text(encoding="utf-8", errors="ignore")
    html = _get_text(filing_doc_url_by_cik(cik, accession, primary_document))
    path.write_text(html, encoding="utf-8")
    return html


# ==========================================
# EXTRACT
# ==========================================


def clean_text(html: str) -> str:
    """Strip scripts/styles/tags, decode HTML entities, collapse whitespace."""
    t = re.sub(r"(?is)<script.*?</script>", " ", html)
    t = re.sub(r"(?is)<style.*?</style>", " ", t)
    t = re.sub(r"<[^>]+>", " ", t)
    t = _html.unescape(t)
    return re.sub(r"\s+", " ", t).strip()


def _extract_one(text: str, start_pat: str, end_pat: str, anchor: str) -> str | None:
    """Largest-span section between a title-anchored start and end header, validated."""
    starts = sorted(m.start() for m in re.finditer(start_pat, text, re.IGNORECASE))
    ends = sorted(m.start() for m in re.finditer(end_pat, text, re.IGNORECASE))
    if not starts or not ends:
        return None
    # Largest span between a title-anchored start and a *real* end-header, then
    # validate (size band + content anchor). Strict by design: if no confident
    # span is found we return None and the caller falls back to the whole-filing
    # path. Better to fall back than to feed a mis-bounded section to the LLM
    # (a cap-to-fixed-length end produced false positives off cross-reference
    # starts — worse than falling back). Real-filer precision is a tuning follow-up
    # against the backfilled corpus; see notes/sentiment-specialist-design.md.
    best: tuple[int, int] | None = None
    for s in starts:
        e = next((x for x in ends if x > s), None)
        if e is not None and (best is None or e - s > best[1] - best[0]):
            best = (s, e)
    if best is None:
        return None
    seg = text[best[0]:best[1]].strip()
    if not (_MIN_SECTION_CHARS <= len(seg) <= _MAX_SECTION_CHARS):
        return None
    if anchor.lower() not in seg.lower():
        return None
    return seg


def extract_sections(text: str, form: str) -> dict[str, str | None]:
    """Best-effort {mdna, risk_factors} from cleaned filing text.

    `text` is the output of clean_text(). A value is None when extraction fails its
    validation gate — the caller should then fall back to the whole-filing path.
    Amended forms (10-K/A, 10-Q/A) map to their base form.
    """
    base = form.upper().split("/")[0]
    spec = _SPECS.get(base)
    if spec is None:
        return {"mdna": None, "risk_factors": None}
    return {name: _extract_one(text, *patterns) for name, patterns in spec.items()}
