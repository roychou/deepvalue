"""
L5 — the adversarial bounded cycle (spec §9 + §13.3), long-only trap filter.

bull -> [ skeptic -> route each open objection back to its specialist for rebuttal -> judge ]
repeated up to max_rounds=3, terminating early when no open objection remains or the judge says
PASS. Plain control flow — no graph engine. The "rebut" arrow is just re-invoking a specialist
with the objection.
"""
from __future__ import annotations

import json
import logging

from deepvalue.agents.subagents import (
    asset_auditor,
    bull,
    capital_structure,
    footnote,
    judge,
    skeptic,
)
from deepvalue.contracts.models import Objection, ThesisVerdict

log = logging.getLogger("tedium.agents.loop")

# objection.type -> the specialist module that rebuts it (mirrors skeptic.OBJECTION_TYPES).
ROUTING = {
    "inventory_obsolescence": asset_auditor,
    "receivables_quality": asset_auditor,
    "impairment": asset_auditor,
    "valuation_arithmetic": asset_auditor,
    "hidden_liability": footnote,
    "disclosure": footnote,
    "related_party": footnote,
    "refinancing": capital_structure,
    "covenant_breach": capital_structure,
    "dilution": capital_structure,
}


async def _rebut(spec_module, ticker: str, as_of: str, objection: Objection, *,
                 max_llm_usd: float) -> list[str]:
    """Re-invoke the routed specialist to rebut (or concede) one objection from cited filing
    text; return the sentence_id citations that bear on it."""
    from deepvalue.agents.harness import run_subagent  # lazy — avoid cycle
    prompt = (f"Objection on {ticker} (as of {as_of}), type={objection.type}: {objection.claim}\n"
              f"Rebut it from cited filing text, or concede it. Return ONLY a JSON array of the "
              f"canonical_section.sentence_id strings that bear on it.")
    raw = await run_subagent(spec_module.AGENT_KEY, prompt, max_llm_usd=max_llm_usd)
    return json.loads(raw)


async def run_adversarial(ticker: str, as_of: str, dossier: str, *,
                          max_llm_usd: float, max_rounds: int = 3) -> ThesisVerdict:
    """The bounded L5 cycle. Budget split across the bull + up to max_rounds judge passes.
    Long-only keep/kill; the judge defaults to PASS (Burry tilt)."""
    per = max_llm_usd / (max_rounds + 1)
    bull_summary = await bull.synthesize(ticker, as_of, dossier, max_llm_usd=per)

    verdict: ThesisVerdict | None = None
    for round_i in range(max_rounds):
        objections = await skeptic.attack(ticker, as_of, bull_summary, dossier, max_llm_usd=per)
        open_objs = [o for o in objections if o.status == "open"]
        if not open_objs:
            break
        for o in open_objs:  # route each objection back to evidence
            spec = ROUTING.get(o.type)
            if spec is None:
                log.warning("no route for objection type %r; left unrebutted", o.type)
                continue
            o.evidence = await _rebut(spec, ticker, as_of, o, max_llm_usd=per)
        verdict = await judge.adjudicate(ticker, as_of, bull_summary, objections, max_llm_usd=per)
        log.info("L5 round %d: %s (%d objections)", round_i + 1, verdict.decision, len(objections))
        if verdict.decision == "PASS":
            break

    if verdict is None:  # skeptic raised nothing — the thesis still must clear the judge once
        verdict = await judge.adjudicate(ticker, as_of, bull_summary, [], max_llm_usd=per)
    return verdict
