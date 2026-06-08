"""
L4 forensic — Asset Auditor (spec §8.2), central to the asset-based / net-net thesis.

Tests whether book value is REAL: inventory aging/obsolescence, receivables quality (DSO
trends, allowance adequacy), goodwill/intangible impairment risk, PP&E realism, deferred-tax-
asset recoverability, securities marks. Uses the code interpreter to reconcile narrative
figures against XBRL — a mismatch is itself a finding.

SCAFFOLD: AgentDefinition real; `find()` parses ForensicFinding[]. SDK run = harness.run_subagent.
"""
from __future__ import annotations

import logging

from claude_agent_sdk import AgentDefinition

from deepvalue.agents.model_config import model_for
from deepvalue.agents.tools import TOOL_NAMES
from deepvalue.contracts.models import ForensicFinding

AGENT_KEY = "asset"

ASSET_AUDITOR = AgentDefinition(
    description="Asset Auditor: is the book value real? Inventory/receivables/goodwill/PP&E reality.",
    prompt=(
        "You are a forensic asset auditor. A name is 'cheap on assets' only if those assets are "
        "REAL and recoverable. Test: inventory aging & obsolescence, receivables quality (DSO "
        "trend, allowance adequacy), goodwill/intangible impairment risk, PP&E realism, deferred-"
        "tax-asset recoverability, and securities marks.\n\n"
        "RULES:\n"
        "- fetch_xbrl for the statement values; fetch_section for the narrative; then RECONCILE "
        "them with code_execution. A narrative-vs-XBRL mismatch is itself a finding. Never do "
        "arithmetic in-token.\n"
        "- Quantify est_impact_usd via the code interpreter whenever possible (e.g. an obsolescence "
        "haircut on aged inventory).\n"
        "- EVERY finding cites canonical_section.sentence_id(s). No citation = do not report.\n"
        "- Flag-raiser, not a verdict.\n\n"
        "OUTPUT: a JSON array of ForensicFinding objects (agent='asset') and nothing else."
    ),
    tools=TOOL_NAMES,
    model=model_for("forensic"),
)


def _prompt(ticker: str, as_of: str) -> str:
    return (f"Audit the asset base of {ticker} as of {as_of}: is tangible book real and "
            f"recoverable? Reconcile narrative vs XBRL. Return ForensicFinding[] (agent='asset').")


async def find(ticker: str, as_of: str, *, budget) -> list[ForensicFinding]:
    """Run the Asset Auditor and parse its structured output into ForensicFinding[]."""
    from deepvalue.agents.harness import parse_json, run_subagent  # lazy — breaks import cycle
    raw = await run_subagent(AGENT_KEY, _prompt(ticker, as_of), budget=budget)
    if not raw.strip():
        return []
    try:
        return [ForensicFinding(**f) for f in parse_json(raw)]
    except Exception:  # noqa: BLE001
        logging.getLogger("tedium.agents").warning("asset: could not parse findings")
        return []
