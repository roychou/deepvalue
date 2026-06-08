"""
L5 — Skeptic (spec §9, step 2). LONG-ONLY value-trap attacker.

Re-scoped from the source blueprint's "short-seller": this is a bankruptcy attorney / skeptical
analyst whose ONLY job is to decide whether to REJECT a long candidate. No short thesis, no
borrow analysis, no short signal. It attacks the bull thesis with TYPED objections; each
objection's `type` routes it back to the specialist that can rebut it from cited filing text
(see ROUTING in agents/loop.py).

SCAFFOLD: AgentDefinition real; `attack()` parses Objection[].
"""
from __future__ import annotations

import logging

from claude_agent_sdk import AgentDefinition

from deepvalue.agents.model_config import model_for
from deepvalue.agents.tools import TOOL_NAMES
from deepvalue.contracts.models import Objection

AGENT_KEY = "skeptic"

# Objection types the skeptic may raise -> the specialist each routes to (mirrored in loop.ROUTING).
OBJECTION_TYPES = {
    "inventory_obsolescence": "asset",
    "receivables_quality": "asset",
    "impairment": "asset",
    "hidden_liability": "footnote",
    "disclosure": "footnote",
    "related_party": "footnote",
    "refinancing": "capital_structure",
    "covenant_breach": "capital_structure",
    "dilution": "capital_structure",
    "valuation_arithmetic": "asset",  # reconciled via code_execution
}

SKEPTIC = AgentDefinition(
    description="Skeptic: long-only value-trap attacker. Kill/keep only — no short thesis.",
    prompt=(
        "You are a skeptical bankruptcy analyst. Your ONLY question: is this long candidate a "
        "value TRAP — cheap because it is dying? You do not short and you propose no short thesis; "
        "you decide whether to reject a long.\n\n"
        "Attack the bull thesis with specific, TYPED objections. Each objection has a `type` from "
        f"this set (which routes it to the specialist who can rebut it): {sorted(OBJECTION_TYPES)}.\n"
        "Ground every objection in something checkable against the filing — not mere plausibility. "
        "Set routed_to = the specialist for the type, status='open', and list the citations the "
        "rebuttal must address.\n\n"
        "OUTPUT: a JSON array of Objection objects and nothing else."
    ),
    tools=TOOL_NAMES,
    model=model_for("adversarial"),
)


def _prompt(ticker: str, as_of: str, bull_summary: str, dossier: str) -> str:
    return (f"Attack this long thesis for {ticker} (as of {as_of}) as a potential value trap.\n\n"
            f"THESIS:\n{bull_summary}\n\nDOSSIER:\n{dossier}\n\n"
            f"Return typed, citation-grounded Objection[].")


async def attack(ticker: str, as_of: str, bull_summary: str, dossier: str, *,
                 budget) -> list[Objection]:
    """Run the skeptic and parse its typed objections into Objection[]."""
    from deepvalue.agents.harness import parse_json, run_subagent  # lazy — breaks import cycle
    raw = await run_subagent(AGENT_KEY, _prompt(ticker, as_of, bull_summary, dossier), budget=budget)
    if not raw.strip():
        return []
    try:
        return [Objection(**o) for o in parse_json(raw)]
    except Exception:  # noqa: BLE001 — no parseable objections -> none survive (judge still runs)
        logging.getLogger("tedium.agents").warning("skeptic: could not parse objections")
        return []
