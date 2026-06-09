"""
On-demand L4 -> L5 forensic deep-dive on a single BUY candidate (operator-run).

This is the human's forensic magnifying glass: run it on a name the weekly funnel surfaced as a
BUY, BEFORE signing off (CLAUDE.md: mandatory human sign-off on every BUY). It runs the forensic
specialists (L4) then the adversarial trap filter (L5) and prints the ThesisVerdict. Spends LLM
under a HARD cap — kept off the weekly cron so it only runs when a real candidate warrants it.

    uv run python -m deepvalue.agents.deepdive --ticker KSS --as-of 2026-06-08 --max-llm-usd 3
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
from datetime import date
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parents[3] / ".env")  # EDGAR_USER_AGENT for the in-process tools

from deepvalue.agents import forensic_then_adversarial  # noqa: E402
from deepvalue.ingest import edgar_fundamentals as ef  # noqa: E402

log = logging.getLogger("tedium.deepdive")

_DOSSIER_FIELDS = ["totalAssets", "totalCurrentAssets", "totalCurrentLiabilities",
                   "totalLiabilities", "totalStockholdersEquity", "netIncome",
                   "operatingCashFlow", "retainedEarnings", "revenue", "totalDebt",
                   "inventory", "goodwillAndIntangibleAssets", "weightedAverageShsOutDil"]


def _dossier(ticker: str, as_of: str) -> str:
    """A compact L1 dossier for the bull/forensic agents — the point-in-time fundamentals plus
    why it's a candidate. The agents fetch detail themselves via fetch_xbrl/fetch_section."""
    p = ef.as_of(ticker, as_of)
    if p is None:
        return f"{ticker} as of {as_of}: no fundamentals on file."
    figs = {k: p.income.get(k) for k in _DOSSIER_FIELDS if p.income.get(k) is not None}
    return (f"{ticker} — deep-value candidate as of {as_of} (latest 10-K filed {p.filing_date}, "
            f"fiscal period {p.period_end}). It passed the L1 quant screen: cheap on assets, "
            f"healthy Piotroski-F, low dilution. Reported figures (USD): {json.dumps(figs)}. "
            f"Task: verify the asset base behind the cheap valuation is REAL and recoverable, and "
            f"surface any value-trap risk before a human commits.")


async def _run(ticker: str, as_of: str, cap: float) -> int:
    log.info("L4->L5 deep-dive: %s as of %s (cap $%.2f)", ticker, as_of, cap)
    verdict = await forensic_then_adversarial(ticker, as_of, _dossier(ticker, as_of), max_llm_usd=cap)
    print("\n===== ThesisVerdict =====")
    print(verdict.model_dump_json(indent=2))
    from deepvalue.agents.subagents.judge import needs_review
    if needs_review(verdict):  # raise for human intervention — judge could not adjudicate
        print("\n⚠️  HUMAN REVIEW REQUIRED: the judge could not render a clean verdict. Held at "
              "WATCH (not PASS) — review the surviving objections above and decide by hand.")
        return 2
    return 0


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    ap = argparse.ArgumentParser(description="L4->L5 forensic deep-dive (operator-run)")
    ap.add_argument("--ticker", required=True)
    ap.add_argument("--as-of", default=date.today().isoformat())
    ap.add_argument("--max-llm-usd", type=float, required=True, help="hard spend cap")
    a = ap.parse_args()
    raise SystemExit(asyncio.run(_run(a.ticker.upper(), a.as_of, a.max_llm_usd)))


if __name__ == "__main__":
    main()
