"""
L5 — Judge (spec §9, step 3).

Adjudicates each contested point against the CITED evidence (the specialist rebuttals), then
emits the verdict. Burry tilt: the default decision is PASS; only a strongly-defended thesis
with real asset-backed margin of safety reaches WATCH/BUY. The judge also decides termination —
the loop stops when no material, unrebutted objection remains.

SCAFFOLD: AgentDefinition real; `adjudicate()` parses a ThesisVerdict.
"""
from __future__ import annotations

import json

from claude_agent_sdk import AgentDefinition

from deepvalue.agents.model_config import model_for
from deepvalue.agents.tools import TOOL_NAMES
from deepvalue.contracts.models import Objection, ThesisVerdict

AGENT_KEY = "judge"

JUDGE = AgentDefinition(
    description="Judge: adjudicate objections vs cited evidence; emit ThesisVerdict. Default PASS.",
    prompt=(
        "You adjudicate a long-candidate debate. For each objection, weigh the skeptic's claim "
        "against the specialist's rebuttal AND the cited filing text — an objection is sustained "
        "unless the rebuttal actually defeats it with evidence (plausible-sounding is not enough). "
        "Mark each objection 'sustained' or 'rebutted'.\n\n"
        "Then decide:\n"
        "- BURRY TILT: default to PASS. Reach WATCH/BUY only if the thesis survives with a REAL "
        "asset-backed margin of safety and no sustained, book-impairing objection.\n"
        "- Any sustained objection that impairs book value or threatens solvency => PASS.\n"
        "- conviction in [0,1]; margin_of_safety = discount to conservative asset value; list "
        "surviving_risks, dependencies, and the unresolved (sustained) objections.\n\n"
        "OUTPUT: a single JSON object matching ThesisVerdict (decision in BUY/WATCH/PASS) and nothing else."
    ),
    tools=TOOL_NAMES,
    model=model_for("adversarial"),
)


def _prompt(ticker: str, as_of: str, bull_summary: str, objections: list[Objection]) -> str:
    objs = json.dumps([o.model_dump() for o in objections], default=str)
    return (f"Adjudicate the thesis for {ticker} as of {as_of}.\n\nTHESIS:\n{bull_summary}\n\n"
            f"OBJECTIONS (with rebuttal evidence):\n{objs}\n\n"
            f"Mark each sustained/rebutted, then emit one ThesisVerdict. Default PASS.")


async def adjudicate(ticker: str, as_of: str, bull_summary: str, objections: list[Objection], *,
                     max_llm_usd: float) -> ThesisVerdict:
    """Run the judge and parse the ThesisVerdict."""
    from deepvalue.agents.harness import run_subagent  # lazy — breaks import cycle
    raw = await run_subagent(AGENT_KEY, _prompt(ticker, as_of, bull_summary, objections),
                             max_llm_usd=max_llm_usd)
    return ThesisVerdict(**json.loads(raw))
