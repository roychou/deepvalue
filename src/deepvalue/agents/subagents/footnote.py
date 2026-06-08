"""
L4 forensic — Footnote Archaeologist (spec §8.1).

Targets disguised debt and hidden liabilities that impair tangible book: off-balance-sheet
arrangements, VIEs, related-party transactions, pension/OPEB shortfalls, lease obligations,
commitments & contingencies, debt covenants. Every finding MUST cite an exact location
(canonical_section.sentence_id) — an uncited finding is noise and can't be routed back by L5.

SCAFFOLD: the AgentDefinition (role/tools/model) is real; `find()` runs it and parses
ForensicFinding[]. The SDK execution lives in harness.run_subagent (the single TODO).
"""
from __future__ import annotations

import json

from claude_agent_sdk import AgentDefinition

from deepvalue.agents.model_config import model_for
from deepvalue.agents.tools import TOOL_NAMES
from deepvalue.contracts.models import ForensicFinding

AGENT_KEY = "footnote"

FOOTNOTE = AgentDefinition(
    description="Footnote Archaeologist: disguised debt & hidden liabilities impairing tangible book.",
    prompt=(
        "You are a forensic accountant reading a SEC filing's footnotes to find STRUCTURAL RISK "
        "management has disclosed but buried: off-balance-sheet arrangements, VIEs ('debt they "
        "don't own'), related-party transactions, pension/OPEB shortfalls, operating & finance "
        "lease obligations, commitments & contingencies, and debt covenants.\n\n"
        "RULES:\n"
        "- Use fetch_section to read the exact footnote/section; use fetch_xbrl + code_execution "
        "to reconcile narrative figures against the statements. Never do arithmetic in your head.\n"
        "- EVERY finding MUST cite the canonical_section.sentence_id(s) it rests on. No citation = "
        "do not report it.\n"
        "- Set impairs_book_value and requires_rebuttal honestly; severity in [0,1].\n"
        "- You are a flag-raiser for the adversarial filter, NOT a verdict. Report what you find, "
        "cited; do not conclude BUY/PASS.\n\n"
        "OUTPUT: a JSON array of ForensicFinding objects (agent='footnote') and nothing else."
    ),
    tools=TOOL_NAMES,
    model=model_for("forensic"),
)


def _prompt(ticker: str, as_of: str) -> str:
    return (f"Analyze {ticker} as of {as_of}. Find hidden-liability / disguised-debt issues that "
            f"impair tangible book value. Return ForensicFinding[] (agent='footnote'), each cited.")


async def find(ticker: str, as_of: str, *, max_llm_usd: float) -> list[ForensicFinding]:
    """Run the Footnote Archaeologist and parse its structured output into ForensicFinding[]."""
    from deepvalue.agents.harness import run_subagent  # lazy import — breaks the harness<->subagent cycle
    raw = await run_subagent(AGENT_KEY, _prompt(ticker, as_of), max_llm_usd=max_llm_usd)
    return [ForensicFinding(**f) for f in json.loads(raw)]
