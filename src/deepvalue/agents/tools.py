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
    "Fetch one canonical filing section from the latest 10-K filed on/before as_of, WITH sentence "
    "IDs so findings cite exact locations. canonical_id is '10-K.mdna' or 'footnote.<topic>' where "
    "topic is one of: related_party, commitments_contingencies, leases, debt, income_taxes, "
    "pension, vie, goodwill.",
    {"ticker": str, "as_of": str, "canonical_id": str},
)
async def fetch_section(args: dict) -> dict:
    from deepvalue.ingest.edgar import filings_by_cik, ticker_to_cik
    from deepvalue.ingest.edgar_filings import fetch_filing_document_by_cik
    from deepvalue.ingest.normalize import to_sentences
    from deepvalue.ingest.segmentation import FOOTNOTE_TOPICS, segment_footnote, segment_mdna

    cid, ticker, as_of = args["canonical_id"], args["ticker"], args["as_of"]
    try:
        cik = ticker_to_cik(ticker)
        fils = filings_by_cik(cik, forms=("10-K",))
        f = next((x for x in fils if x["filed"] <= as_of), None)
        if f is None:
            return _text(f"no 10-K on/before {as_of} for {ticker}")
        html = fetch_filing_document_by_cik(cik, f["accession"], f["primary_document"])
    except Exception as e:  # noqa: BLE001
        return _text(f"fetch failed for {ticker}: {type(e).__name__}")

    if "mdna" in cid:
        sec = segment_mdna(html, "10-K")
    elif cid.startswith("footnote."):
        sec = segment_footnote(html, cid.split(".", 1)[1])
    else:
        return _text(f"unknown canonical_id {cid!r}; use '10-K.mdna' or 'footnote.<topic>' "
                     f"(topics: {', '.join(FOOTNOTE_TOPICS)})")
    if not sec.text:
        return _text(f"SECTION_UNAVAILABLE: {cid} not extractable for {ticker} "
                     f"(irregular formatting or not present in this filing)")
    sents = to_sentences(sec.text)
    body = "\n".join(f"[{s['sentence_id']}] {s['text']}" for s in sents)
    return _text(f"{cid} from 10-K filed {f['filed']} ({len(sents)} sentences):\n{body}")


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
