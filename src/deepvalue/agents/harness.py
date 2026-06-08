"""
L4/L5 supervisor — the Claude Agent SDK harness (spec §13.1).

Owns the subagent registry + tool wiring (build_options), the single SDK-execution primitive
(run_subagent — the one real TODO), and the L4 forensic fan-out (run_forensic: 3 specialists in
parallel, deduped). The L5 bounded cycle lives in loop.py. No graph engine — plain async Python.
"""
from __future__ import annotations

import asyncio
import logging

from claude_agent_sdk import ClaudeAgentOptions

from deepvalue.agents.model_config import model_for
from deepvalue.agents.subagents import (
    asset_auditor,
    bull,
    capital_structure,
    footnote,
    judge,
    skeptic,
)
from deepvalue.agents.tools import TOOL_NAMES, deepvalue_tools_server
from deepvalue.contracts.models import ForensicFinding

log = logging.getLogger("tedium.agents")

# name -> AgentDefinition. Names match the keys callers pass to run_subagent / client.query(agent=).
SUBAGENTS = {
    "footnote": footnote.FOOTNOTE,
    "asset": asset_auditor.ASSET_AUDITOR,
    "capital_structure": capital_structure.CAPITAL_STRUCTURE,
    "bull": bull.BULL,
    "skeptic": skeptic.SKEPTIC,
    "judge": judge.JUDGE,
}


def build_options() -> ClaudeAgentOptions:
    """Supervisor options: the subagent registry, deepvalue's data tools (in-process MCP) + the
    server-side code interpreter, and a default model. setting_sources=[] keeps it a pure library
    run (ignore filesystem CLAUDE.md/settings)."""
    return ClaudeAgentOptions(
        agents=SUBAGENTS,
        mcp_servers={"deepvalue": deepvalue_tools_server()},
        allowed_tools=TOOL_NAMES,
        model=model_for("forensic"),
        setting_sources=[],
        max_turns=16,
    )


async def run_subagent(agent_key: str, user_prompt: str, *, max_llm_usd: float) -> str:
    """THE core execution primitive (the single scaffold TODO). Dispatch one registered subagent
    via the Agent SDK and return its final assistant text; callers parse it into §12 contracts.

    Intended implementation (Agent SDK):
        async with ClaudeSDKClient(options=build_options()) as client:
            await client.query(user_prompt, agent=agent_key)     # route to the named subagent
            out, spent = [], 0.0
            async for msg in client.receive_response():
                if isinstance(msg, AssistantMessage):
                    out += [b.text for b in msg.content if isinstance(b, TextBlock)]
                if isinstance(msg, ResultMessage):
                    spent += _usd(msg.usage)                      # enforce the hard cap
                    if spent > max_llm_usd: raise BudgetExceeded(agent_key, spent, max_llm_usd)
            return "".join(out)

    Spending is gated by CLAUDE.md (ASK FIRST) + the max_llm_usd cap. Until wired, this raises so
    no agentic run can silently spend.
    """
    raise NotImplementedError(
        f"L4/L5 SDK execution not wired (scaffold): subagent={agent_key!r}. See docstring + spec "
        "§13.3. Wiring this spends LLM — requires operator go-ahead and a hard --max-llm-usd cap."
    )


def _dedupe(findings: list[ForensicFinding]) -> list[ForensicFinding]:
    """Drop overlapping findings (same agent + type + citations) from the parallel specialists."""
    seen, out = set(), []
    for f in findings:
        key = (f.agent, f.finding_type, tuple(sorted(f.citation)))
        if key not in seen:
            seen.add(key)
            out.append(f)
    return out


async def run_forensic(ticker: str, as_of: str, *, max_llm_usd: float) -> list[ForensicFinding]:
    """L4 (spec §8): dispatch the 3 forensic specialists IN PARALLEL, flatten + dedupe their
    findings. Budget split evenly; a failed specialist is logged, not fatal."""
    per = max_llm_usd / 3.0
    results = await asyncio.gather(
        footnote.find(ticker, as_of, max_llm_usd=per),
        asset_auditor.find(ticker, as_of, max_llm_usd=per),
        capital_structure.find(ticker, as_of, max_llm_usd=per),
        return_exceptions=True,
    )
    findings: list[ForensicFinding] = []
    for r in results:
        if isinstance(r, Exception):
            log.warning("forensic specialist failed: %s: %s", type(r).__name__, r)
            continue
        findings.extend(r)
    return _dedupe(findings)
