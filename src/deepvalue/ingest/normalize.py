"""
L0 — text normalization + sentence IDs (spec §3, §4).

A section's text is split into sentences with STABLE ids ('s_0042') so a diff finding (L3) or a
forensic finding (L4) can cite an exact location (canonical_section.sentence_id), and so the
same sentence keeps its id across re-runs of the same filing. Deterministic, no LLM.
"""
from __future__ import annotations

import re

# Sentence boundary: ./?/! + whitespace + capital/quote/number start. A small abbreviation set
# (Inc., Corp., U.S., No., vs., titles) and decimals are protected so they don't split mid-sentence.
_ABBREV = (r"(?<!\bInc)(?<!\bCorp)(?<!\bLtd)(?<!\bCo)(?<!\bU\.S)(?<!\bNo)(?<!\bvs)"
           r"(?<!\bMr)(?<!\bMs)(?<!\bDr)")
_BOUNDARY = re.compile(_ABBREV + r"(?<!\d\.\d)[.?!]+[\"')\]]?\s+(?=[A-Z\"'(\[$0-9])")


def split_sentences(text: str) -> list[str]:
    """Split normalized section text into sentences. Robust enough for filing prose; not a full
    NLP tokenizer (filings are formal, mostly well-punctuated)."""
    text = re.sub(r"\s+", " ", text).strip()
    if not text:
        return []
    out, last = [], 0
    for m in _BOUNDARY.finditer(text):
        seg = text[last:m.end()].strip()
        if seg:
            out.append(seg)
        last = m.end()
    tail = text[last:].strip()
    if tail:
        out.append(tail)
    return out


def to_sentences(text: str) -> list[dict]:
    """Section text -> the FilingSection.sentences shape: [{'sentence_id','text'}, ...] with
    zero-padded stable ids. The unit L3/L4 cite."""
    return [{"sentence_id": f"s_{i:04d}", "text": s} for i, s in enumerate(split_sentences(text))]
