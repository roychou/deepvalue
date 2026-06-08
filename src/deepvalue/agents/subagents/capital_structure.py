"""
L4 forensic — Capital-Structure / Liquidity agent (spec §8.3).

Debt maturity ladder, covenant headroom, refinancing risk, dilution trajectory. (Mergeable
into the Footnote Archaeologist for v1; kept separate for clean component comparison.)

SCAFFOLD: AgentDefinition real; `find()` parses ForensicFinding[]. SDK run = harness.run_subagent.
"""
from __future__ import annotations

import logging

from claude_agent_sdk import AgentDefinition

from deepvalue.agents.model_config import model_for
from deepvalue.agents.tools import TOOL_NAMES
from deepvalue.contracts.models import ForensicFinding

AGENT_KEY = "capital_structure"

CAPITAL_STRUCTURE = AgentDefinition(
    description="Capital-Structure / Liquidity: debt ladder, covenants, refinancing risk, dilution.",
    prompt=(
        "You assess whether a cheap balance sheet is a SOLVENCY trap. Map the debt maturity "
        "ladder, covenant headroom (and proximity to breach), refinancing/rollover risk in the "
        "next 12-24 months, and the dilution trajectory (share-count growth, ATM programs, "
        "convertibles, warrants).\n\n"
        "RULES:\n"
        "- fetch_xbrl for debt/share figures; fetch_section for covenant & maturity disclosures; "
        "compute headroom and runway with code_execution.\n"
        "- EVERY finding cites canonical_section.sentence_id(s). No citation = do not report.\n"
        "- Flag-raiser, not a verdict.\n\n"
        "OUTPUT: a JSON array of ForensicFinding objects (agent='capital_structure') and nothing else."
    ),
    tools=TOOL_NAMES,
    model=model_for("forensic"),
)


def _prompt(ticker: str, as_of: str) -> str:
    return (f"Assess {ticker}'s capital structure & liquidity as of {as_of}: debt ladder, covenant "
            f"headroom, refinancing risk, dilution. Return ForensicFinding[] (agent='capital_structure').")


async def find(ticker: str, as_of: str, *, budget) -> list[ForensicFinding]:
    """Run the Capital-Structure agent and parse its structured output into ForensicFinding[]."""
    from deepvalue.agents.harness import parse_json, run_subagent  # lazy — breaks import cycle
    raw = await run_subagent(AGENT_KEY, _prompt(ticker, as_of), budget=budget)
    if not raw.strip():
        return []
    try:
        return [ForensicFinding(**f) for f in parse_json(raw)]
    except Exception:  # noqa: BLE001
        logging.getLogger("tedium.agents").warning("capital_structure: could not parse findings")
        return []
