"""
L5 — Bull synthesizer (spec §9, step 1).

Assembles the long thesis from the L1-L4 dossier: WHY it's cheap, WHERE the margin of safety
is (asset coverage), and WHAT could re-rate it. Deliberately steelmans the long case so the
skeptic has a real thesis to attack — the debate only filters traps if the bull is honest.

SCAFFOLD: AgentDefinition real; `synthesize()` returns the bull_summary text.
"""
from __future__ import annotations

from claude_agent_sdk import AgentDefinition

from deepvalue.agents.model_config import model_for
from deepvalue.agents.tools import TOOL_NAMES

AGENT_KEY = "bull"

BULL = AgentDefinition(
    description="Bull synthesizer: steelman the long thesis from the L1-L4 dossier.",
    prompt=(
        "You build the BEST honest long thesis for a deep-value candidate from the dossier "
        "(quant screen, MD&A-deterioration read, and forensic findings). State plainly: (1) why "
        "it is cheap, (2) where the margin of safety sits — the asset coverage that protects the "
        "downside, with figures reconciled via fetch_xbrl/code_execution, (3) the realistic "
        "re-rating catalyst. Cite filing locations for load-bearing claims. Do NOT hide the "
        "forensic flags — a thesis that ignores them is worthless to the skeptic. Output a concise "
        "thesis suitable for adversarial attack."
    ),
    tools=TOOL_NAMES,
    model=model_for("adversarial"),
)


def _prompt(ticker: str, as_of: str, dossier: str) -> str:
    return (f"Build the long thesis for {ticker} as of {as_of} from this dossier:\n{dossier}\n\n"
            f"Steelman the long case; surface the margin of safety; do not bury the forensic flags.")


async def synthesize(ticker: str, as_of: str, dossier: str, *, max_llm_usd: float) -> str:
    """Run the bull and return the thesis text (the ThesisVerdict.bull_summary seed)."""
    from deepvalue.agents.harness import run_subagent  # lazy — breaks import cycle
    return await run_subagent(AGENT_KEY, _prompt(ticker, as_of, dossier), max_llm_usd=max_llm_usd)
