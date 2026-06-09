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
from deepvalue.contracts.models import ForensicFinding, Objection, ThesisVerdict

log = logging.getLogger("tedium.agents")


# --- tolerant coercion: the SDK agents emit valid JSON but with their OWN field names + rich
# nested content; these map whatever they produce into the §12 contracts, defaulting unknowns and
# preserving the agent's substance in the free-text field rather than dropping it. ---
def _f01(x, default=0.5) -> float:
    try:
        return max(0.0, min(1.0, float(x)))
    except (TypeError, ValueError):
        return default


def _num(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def _strs(x) -> list[str]:
    if isinstance(x, list):
        return [str(v) for v in x]
    return [str(x)] if x else []


def coerce_finding(d, ticker: str, agent: str) -> ForensicFinding:
    import json
    if not isinstance(d, dict):
        d = {"rationale": str(d)}
    return ForensicFinding(
        ticker=str(d.get("ticker") or ticker), agent=agent,
        finding_type=str(d.get("finding_type") or d.get("type") or d.get("category") or "finding"),
        severity=_f01(d.get("severity", d.get("risk", 0.5))),
        impairs_book_value=bool(d.get("impairs_book_value", d.get("impairs_book", False))),
        est_impact_usd=_num(d.get("est_impact_usd") or d.get("est_impact") or d.get("impact_usd")),
        citation=_strs(d.get("citation") or d.get("citations") or d.get("evidence") or []),
        rationale=str(d.get("rationale") or d.get("finding") or d.get("summary") or json.dumps(d)[:1200]),
        requires_rebuttal=bool(d.get("requires_rebuttal", True)))


def coerce_objection(d, i: int) -> Objection:
    import json
    if not isinstance(d, dict):
        d = {"claim": str(d)}
    status = d.get("status")
    return Objection(
        id=str(d.get("id") or f"O{i}"),
        type=str(d.get("type") or d.get("objection_type") or "general"),
        claim=str(d.get("claim") or d.get("objection") or d.get("text") or json.dumps(d)[:800]),
        routed_to=str(d.get("routed_to") or d.get("specialist") or "asset"),
        status=status if status in ("open", "rebutted", "sustained") else "open",
        evidence=_strs(d.get("evidence") or d.get("citation") or []))

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


class BudgetMeter:
    """A SHARED running budget across all subagents in one deep-dive. We don't pre-divide the cap
    into fixed slices (a Sonnet tool-using agent costs ~$0.5-0.6, more than any fair slice) — each
    subagent runs to completion and adds its real cost; the meter gates whether the NEXT one runs.
    Soft per-agent, hard total."""

    def __init__(self, cap_usd: float):
        self.cap = cap_usd
        self.spent = 0.0

    def exhausted(self) -> bool:
        return self.spent >= self.cap

    def add(self, usd: float) -> None:
        self.spent += usd


def parse_json(text: str):
    """Extract a JSON value from a model response that wraps it in prose/markdown (the SDK agents
    narrate their tool use, then emit the answer). Tries fenced ```json blocks, then bracket-matches
    the first BALANCED [...] / {...} (greedy regex broke on multi-array / prose-between output)."""
    import json
    import re

    for m in re.finditer(r"```(?:json)?\s*(.*?)```", text, re.S):  # fenced blocks first
        try:
            return json.loads(m.group(1).strip())
        except Exception:  # noqa: BLE001
            continue
    for open_c, close_c in (("[", "]"), ("{", "}")):  # then balanced bracket scan
        i = text.find(open_c)
        while i != -1:
            depth = 0
            for j in range(i, len(text)):
                if text[j] == open_c:
                    depth += 1
                elif text[j] == close_c:
                    depth -= 1
                    if depth == 0:
                        try:
                            return json.loads(text[i:j + 1])
                        except Exception:  # noqa: BLE001
                            break
            i = text.find(open_c, i + 1)
    return json.loads(text)  # last resort — raises if there is truly no JSON


_JSON_DIRECTIVE = (
    "\n\nCRITICAL OUTPUT FORMAT: do all analysis and tool calls FIRST, then make your FINAL "
    "message contain ONLY the requested JSON — no markdown fences, no commentary, no preamble or "
    "postscript. The final message must start with '[' or '{' and be valid, parseable JSON.")


async def run_subagent(agent_key: str, user_prompt: str, *, budget: BudgetMeter,
                       json_only: bool = True) -> str:
    """Run one registered subagent via the Agent SDK; return its final assistant text (callers
    parse it into §12 contracts). Drains the SDK stream fully then accounts the cost to the shared
    meter — no mid-stream raise (that broke the SDK generator). Skips (returns '') if the shared
    budget is already exhausted. json_only appends a hard final-format directive (off for the bull,
    which returns free-form thesis prose)."""
    if budget.exhausted():
        log.warning("budget exhausted ($%.2f/$%.2f); skipping %s", budget.spent, budget.cap, agent_key)
        return ""
    from claude_agent_sdk import (
        AssistantMessage,
        ClaudeAgentOptions,
        ResultMessage,
        TextBlock,
        query,
    )
    from deepvalue.agents.tools import deepvalue_tools_server

    agent = SUBAGENTS[agent_key]
    opts = ClaudeAgentOptions(
        system_prompt=agent.prompt,
        model=agent.model,
        allowed_tools=TOOL_NAMES,
        mcp_servers={"deepvalue": deepvalue_tools_server()},
        setting_sources=[],   # pure library run — ignore filesystem CLAUDE.md/settings
        max_turns=16,
    )
    chunks: list[str] = []
    spent = 0.0
    async for msg in query(prompt=user_prompt + (_JSON_DIRECTIVE if json_only else ""), options=opts):
        if isinstance(msg, AssistantMessage):
            chunks += [b.text for b in msg.content if isinstance(b, TextBlock)]
        elif isinstance(msg, ResultMessage):
            spent = getattr(msg, "total_cost_usd", 0.0) or 0.0
    out = "".join(chunks)
    budget.add(spent)
    log.info("%s: $%.3f (budget $%.2f/$%.2f); final tail: %s",
             agent_key, spent, budget.spent, budget.cap, repr(out[-120:]))
    return out


def _dedupe(findings: list[ForensicFinding]) -> list[ForensicFinding]:
    """Drop overlapping findings (same agent + type + citations) from the parallel specialists."""
    seen, out = set(), []
    for f in findings:
        key = (f.agent, f.finding_type, tuple(sorted(f.citation)))
        if key not in seen:
            seen.add(key)
            out.append(f)
    return out


async def run_forensic(ticker: str, as_of: str, *, budget: BudgetMeter) -> list[ForensicFinding]:
    """L4 (spec §8): dispatch the 3 forensic specialists IN PARALLEL on the shared budget, flatten
    + dedupe. A specialist that fails (parse/SDK error) is logged, not fatal."""
    results = await asyncio.gather(
        footnote.find(ticker, as_of, budget=budget),
        asset_auditor.find(ticker, as_of, budget=budget),
        capital_structure.find(ticker, as_of, budget=budget),
        return_exceptions=True,
    )
    findings: list[ForensicFinding] = []
    for r in results:
        if isinstance(r, Exception):
            log.warning("forensic specialist failed: %s: %s", type(r).__name__, r)
            continue
        findings.extend(r)
    return _dedupe(findings)
