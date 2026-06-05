"""
L3 materiality pass (spec §7) — Sonnet 4.6 reads the YoY *changed* spans only and
types them. This is the thesis's core move: LLM as READER of which changes are
negative, not judge of returns. One call per filing-pair; input is diff/align's
changed_text (~3k tokens), output is a structured deterioration score.

Cost-controlled: every call returns its USD cost (from response.usage + pinned
models.py pricing), so the runner can enforce a hard --max-llm-usd cap. The constant
instructions carry a cache_control breakpoint; caching only engages if that prefix
exceeds Sonnet 4.6's ~2048-token minimum (the unique changed_text is never cacheable),
so the cap, not caching, is the real cost lever here.
"""
from __future__ import annotations

import json
from dataclasses import dataclass

from deepvalue.models import ROOT

# Typed change categories (spec §7). `removed_reassurance` is the high-value signal —
# the quiet deletion of a sentence that previously said a customer/covenant was secure.
CATEGORIES = [
    "removed_reassurance", "risk_escalation", "customer_concentration",
    "covenant", "liquidity", "litigation", "accounting_policy_change", "none",
]

_SYSTEM = (
    "You are a forensic 10-K reader for a long-only deep-value fund. You are shown ONLY "
    "the sentences that were ADDED or REWRITTEN in a company's MD&A versus the prior year "
    "(the deterministic year-over-year diff). Judge, from the language ALONE, whether these "
    "changes signal DETERIORATION in the business's prospects or risk profile. You are a "
    "reader of the text, not a predictor of the stock — do not use any outside knowledge of "
    "what the company did next; score only what the changed wording itself conveys.\n\n"
    "Return:\n"
    "- deterioration: 0.0 (changes are neutral/boilerplate/positive — routine updates, "
    "growth, resolved issues) to 1.0 (changes clearly signal worsening risk: new or "
    "escalated risk language, tightened liquidity, covenant pressure, new litigation, "
    "customer concentration, or — most important — REMOVED REASSURANCE, where prior-year "
    "language asserting a customer/covenant/segment was secure has quietly disappeared).\n"
    "- removed_reassurance: true iff the changes drop or soften a prior affirmative "
    "assurance (e.g. 'our largest customer has renewed' is gone, 'we are in compliance with "
    "all covenants' weakened).\n"
    "- categories: the change types present (use [\"none\"] if the changes are immaterial).\n"
    "- rationale: one sentence citing the specific wording that drove the score.\n\n"
    "Most YoY MD&A changes are routine (numbers, dates, ordinary growth) and score LOW. "
    "Reserve high scores for genuine, specific deterioration in the prose."
)

_SCHEMA = {
    "type": "object",
    "properties": {
        # numeric range can't be schema-enforced (structured-output limitation) -> clamp in code
        "deterioration": {"type": "number"},
        "removed_reassurance": {"type": "boolean"},
        "categories": {"type": "array", "items": {"type": "string", "enum": CATEGORIES}},
        "rationale": {"type": "string"},
    },
    "required": ["deterioration", "removed_reassurance", "categories", "rationale"],
    "additionalProperties": False,
}

_USER = "Changed MD&A spans (this year vs prior):\n\n{spans}"


@dataclass(frozen=True)
class MaterialityResult:
    deterioration: float
    removed_reassurance: bool
    categories: list[str]
    rationale: str
    cost_usd: float
    cache_read_tokens: int


def call_cost_usd(usage, spec=ROOT) -> float:
    """USD for one response from token usage + pinned list price. Cache writes bill at
    ~1.25x, reads at ~0.1x; uncached input and output at list."""
    inp = getattr(usage, "input_tokens", 0) or 0
    cw = getattr(usage, "cache_creation_input_tokens", 0) or 0
    cr = getattr(usage, "cache_read_input_tokens", 0) or 0
    out = getattr(usage, "output_tokens", 0) or 0
    billed_in = inp + 1.25 * cw + 0.1 * cr
    return billed_in / 1e6 * spec.input_per_mtok + out / 1e6 * spec.output_per_mtok


def score_materiality(client, changed_text: str, model: str = ROOT.id) -> MaterialityResult:
    """One Sonnet call scoring a filing-pair's changed MD&A spans. Returns the typed
    result plus this call's USD cost (so the caller can enforce a budget)."""
    resp = client.messages.create(
        model=model,
        max_tokens=500,
        output_config={
            "format": {"type": "json_schema", "schema": _SCHEMA},
            "effort": "low",   # cheap classification — no deep thinking needed
        },
        system=[{"type": "text", "text": _SYSTEM, "cache_control": {"type": "ephemeral"}}],
        messages=[{"role": "user", "content": _USER.format(spans=changed_text)}],
    )
    text = next(b.text for b in resp.content if b.type == "text")
    data = json.loads(text)
    return MaterialityResult(
        deterioration=max(0.0, min(1.0, float(data["deterioration"]))),
        removed_reassurance=bool(data["removed_reassurance"]),
        categories=list(data.get("categories", [])),
        rationale=str(data.get("rationale", "")),
        cost_usd=call_cost_usd(resp.usage),
        cache_read_tokens=getattr(resp.usage, "cache_read_input_tokens", 0) or 0,
    )
