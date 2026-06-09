"""
Agentic layers L4 (forensic specialists) + L5 (adversarial trap filter) — Claude Agent SDK.

SCAFFOLD STATUS (greenfield): the structure is REAL and importable — §12 contracts, the six
AgentDefinitions (role/tools/model), the parallel L4 fan-out (harness.run_forensic), the bounded
L5 cycle with objection routing (loop.run_adversarial), data tools (tools.py), and model
assignment (model_config). The ONE unwired primitive is harness.run_subagent — the actual SDK
query + structured-output parse — because running it spends LLM (CLAUDE.md: ASK FIRST). It
raises NotImplementedError until wired with operator go + a hard --max-llm-usd cap.

INTEGRATION (spec §2 funnel): forward/run.py calls forensic_then_adversarial() on the BUY
shortlist only — between the free screen and the final verdict, never on the whole universe
(cost control: ~30-40 names max). A clean L4 pass alone never triggers BUY; L5 must clear.

    universe -> L1 screen -> book -> L3 Deterioration Lead -> [L4 forensic] -> [L5 trap filter]
             -> ThesisVerdict (BUY/WATCH/PASS) -> human sign-off
"""
from __future__ import annotations

from deepvalue.agents.harness import build_options, run_forensic, run_subagent
from deepvalue.agents.loop import run_adversarial
from deepvalue.contracts.models import ThesisVerdict

__all__ = ["build_options", "run_forensic", "run_subagent", "run_adversarial",
           "forensic_then_adversarial"]


async def forensic_then_adversarial(ticker: str, as_of: str, dossier: str, *,
                                    max_llm_usd: float) -> ThesisVerdict:
    """L4 -> L5 for one BUY-shortlist candidate: run the forensic specialists, fold their findings
    into the dossier, then run the adversarial trap filter. Returns the ThesisVerdict the human
    signs off on. A SHARED BudgetMeter spans both layers (hard total cap, graceful per-agent)."""
    from deepvalue.agents.harness import BudgetMeter

    budget = BudgetMeter(max_llm_usd)
    findings = await run_forensic(ticker, as_of, budget=budget)
    summary = "\n".join(f.model_dump_json() for f in findings) or "(no forensic findings)"
    full_dossier = f"{dossier}\n\nFORENSIC FINDINGS:\n{summary}"
    return await run_adversarial(ticker, as_of, full_dossier, budget=budget)
