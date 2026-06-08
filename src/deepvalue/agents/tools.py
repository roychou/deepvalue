"""
L4/L5 custom tools exposed to the Agent SDK subagents (spec §3, §13.2).

These are the data tools the forensic + adversarial agents call instead of having the 10-K
dumped into context: each fetches exactly the section / XBRL the agent asks for, keeping the
filing out of the token window (spec §8: "targeted structural extraction feeds each agent only
its relevant sections"). They wrap ingest/ — no LLM, deterministic, cheap.

SCAFFOLD STATUS: fetch_xbrl is wired (edgar_fundamentals). fetch_section depends on
ingest/segmentation.py (greenfield) and returns a not-yet-built sentinel until that lands.
Code execution (reconciliation math) is a server-side Anthropic tool, enabled in the harness.
"""
from __future__ import annotations

import json

from claude_agent_sdk import create_sdk_mcp_server, tool

from deepvalue.ingest import edgar_fundamentals as ef


def _text(payload) -> dict:
    """MCP tool-result envelope."""
    body = payload if isinstance(payload, str) else json.dumps(payload, default=str)
    return {"content": [{"type": "text", "text": body}]}


@tool(
    "fetch_xbrl",
    "Point-in-time XBRL fundamentals (balance sheet, income, cash flow, share counts) for a "
    "ticker as of a date. Returns the most recent annual period FILED on/before as_of — never "
    "look-ahead. Use this to reconcile narrative claims against the statements.",
    {"ticker": str, "as_of": str},
)
async def fetch_xbrl(args: dict) -> dict:
    p = ef.as_of(args["ticker"], args["as_of"])
    if p is None:
        return _text(f"no XBRL fundamentals on file for {args['ticker']} as of {args['as_of']}")
    return _text({"ticker": p.symbol, "period_end": p.period_end, "filing_date": p.filing_date,
                  "fields": p.income})


@tool(
    "fetch_section",
    "Fetch one canonical filing section (e.g. '10-K.item_1a', 'footnote.related_party') with "
    "sentence IDs, so findings can cite exact locations (canonical_section.sentence_id).",
    {"ticker": str, "as_of": str, "canonical_id": str},
)
async def fetch_section(args: dict) -> dict:
    # TODO(L0): wire to ingest/segmentation.py (greenfield) -> FilingSection.sentences.
    # Until segmentation lands, forensic agents can only reconcile XBRL, not cite prose.
    return _text(f"SECTION_UNAVAILABLE: ingest/segmentation.py not yet built "
                 f"(requested {args.get('canonical_id')} for {args['ticker']})")


def deepvalue_tools_server():
    """The in-process MCP server bundling deepvalue's data tools for the subagents.
    Registered in the harness as mcp_servers={'deepvalue': deepvalue_tools_server()}."""
    return create_sdk_mcp_server("deepvalue", tools=[fetch_xbrl, fetch_section])


# Tool names as the agents reference them (mcp__<server>__<tool>) + the server-side code tool.
TOOL_NAMES = [
    "mcp__deepvalue__fetch_xbrl",
    "mcp__deepvalue__fetch_section",
    "code_execution",  # Anthropic server-side sandbox — the LLM never does arithmetic in-token
]
