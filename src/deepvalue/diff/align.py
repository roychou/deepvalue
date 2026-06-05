"""
L3 (PRIMARY EDGE) — deterministic sentence alignment + change isolation across filings.

PORTED from parley `src/backtest/filing_sentiment.py` (`_split_sentences`, `_changed_text`),
which proved the cheap diff path works. GREENFIELD extensions still owed for the full spec §7:
  - stable sentence IDs per canonical section (so DiffFinding.citation can name 10-K.item_1a.s_042)
  - 3-way alignment: this-Q vs prior-Q vs same-Q-prior-year (not just current vs one prior)
  - embedding-similarity pass to detect MOVED/REWORDED text (so relocation != delete+add)
  - boilerplate suppressor (down-weight changes common across many unrelated filers)

For now this gives the deterministic added/changed-sentence isolation the materiality pass
(diff/materiality.py) consumes. Pure stdlib — no LLM, no deps beyond difflib/re.
"""
from __future__ import annotations

import difflib
import re

_MAX_DIFF_CHARS = 12000  # bound payload to the materiality model


def split_sentences(text: str) -> list[str]:
    """Cheap sentence split. Normalized filing text collapses whitespace, so sentences are the
    finest deterministic unit to diff on. Drops sub-20-char fragments (headers, page numbers)."""
    return [s.strip() for s in re.split(r"(?<=[.!?])\s+", text) if len(s.strip()) > 20]


def changed_sentences(current_text: str, prior_text: str | None) -> list[str]:
    """Sentences present in `current` but not `prior` (added or substantially rewritten), via a
    deterministic SequenceMatcher diff. With no prior filing, returns the current head (bootstrap
    baseline). This is the high-signal 'what changed' input to the L3 materiality pass."""
    if not prior_text:
        return split_sentences(current_text[:_MAX_DIFF_CHARS])
    cur = split_sentences(current_text)
    pri = split_sentences(prior_text)
    matcher = difflib.SequenceMatcher(a=pri, b=cur, autojunk=False)
    added: list[str] = []
    for tag, _i1, _i2, j1, j2 in matcher.get_opcodes():
        if tag in ("insert", "replace"):
            added.extend(cur[j1:j2])
    return added


def changed_text(current_text: str, prior_text: str | None) -> str:
    """`changed_sentences` joined and length-bounded — the span payload for the materiality LLM."""
    return " ".join(changed_sentences(current_text, prior_text))[:_MAX_DIFF_CHARS]


def section_change(current_text: str, prior_text: str) -> dict | None:
    """Deterministic YoY change magnitude for one section — the L3 'Lazy Prices' signal,
    no LLM. Returns None if either side is empty (no comparable pair).

    - similarity: difflib sentence-sequence ratio in [0,1] (1.0 = identical). The Cohen-
      Malloy-Nguyen anomaly: LOW similarity (lots of quiet YoY rewriting) predicts LOWER
      forward returns, so `similarity` should rank POSITIVELY with forward return.
    - changed_frac: share of current sentences that are inserted/replaced vs prior.
    """
    cur = split_sentences(current_text)
    pri = split_sentences(prior_text)
    if not cur or not pri:
        return None
    matcher = difflib.SequenceMatcher(a=pri, b=cur, autojunk=False)
    changed = sum(j2 - j1 for tag, _i1, _i2, j1, j2 in matcher.get_opcodes()
                  if tag in ("insert", "replace"))
    return {
        "similarity": matcher.ratio(),
        "changed_frac": changed / len(cur),
        "n_cur": len(cur),
        "n_pri": len(pri),
    }
