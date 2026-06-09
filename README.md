# deepvalue

A Graham-screen / Burry-lens forensic engine that reads SEC filings at scale to find
concentrated, asset-backed long positions in neglected equities. Long-only (`BUY`/`WATCH`/`PASS`);
emits a recommendation artifact for a human, takes no trading action.

Full design: `notes/deep-value-system-design.md` (carried over from the parley repo).

## Posture (load-bearing)
- **Long-only, no shorting.** The adversarial layer is a value-*trap filter*, not a short-seller.
- **Burry, not Graham — concentrated.** Default verdict is `PASS`; cash is a valid position; the
  bar to `BUY` is high. ~8–15 names.
- **Validation is the whole game.** Point-in-time discipline (filing_date, never period_of_report);
  survivorship-free universe (delisted names included) or the trap metric is meaningless.

## The edge claim (honest)
| Layer | Edge |
|---|---|
| L1 quant gate | none (table stakes — cheap universe reduction) |
| **L3 diff engine** | **primary** — "Lazy Prices" YoY language-change underreaction |
| L4 forensic | partial — tireless footnote reading; weakest on fraud |
| **L5 adversarial** | **secondary** — value-trap rejection |
| L7 calibration | enabling — proves it works / detects edge decay |

**Reproducible validation (don't take the numbers on trust).** The L3 headline lives in
[`results/`](results/) as committed per-cohort IC tables — *our computed statistics, not licensed
vendor data* — so you can clone, open the CSV, and re-derive the mean and t yourself (the same
standard parley meets for its committed null). Leading indicator: **IC +0.113 (t 2.15) @63d,
+0.141 (t 2.63) @126d** among event-clean names; survives name/ticker anonymization
(+0.125 → +0.116 @126d). Modest, survivorship-free, point-in-time — `results/README.md` states the
caveats. Regenerate: `uv run python scripts/regenerate_results.py`.

**L4/L5 status — engineering, not (yet) a validated analytical capability.** The Agent-SDK
forensic (L4) and adversarial trap-filter (L5) layers are *built and run end-to-end* — per-name
budget caps, wall-clock timeouts, tolerant contract coercion, and human-escalation when the judge
can't render (it holds at `WATCH` + alerts, never a silent `PASS`). But *running to completion is
not producing good verdicts*: there is **no committed, validated verdict**, which is why the table
rates them partial/secondary. Claim them as engineering. The validated edge is **L3**.

## Layers (1:1 with `src/deepvalue/`)
L0 ingest+segmentation · L1 quant gate · L2 triage+cache · L3 diff (EDGE) · L4 forensic
subagents · L5 adversarial loop · L6 sizing policy · L7 calibration. Orchestration is
Anthropic-native (Claude Agent SDK for L4/L5; plain async Python funnel) — no graph framework.

## Build order — validate the edge before building the machine (spec §15)
1. **Phase 1 — L0 + L3.** Backtest the YoY language-change anomaly on a survivorship-free
   universe *before writing any agent*. Statistically powered (thousands of filing-pairs). If it
   doesn't predict, stop. **← start here.**
2. L1 quant gate + trap signals → 3. L2 triage+cache → 4. L4 specialists → 5. L5 trap filter →
   6. L6 sizing + L7 calibration.

## Provenance
Seeded from the **parley** repo. Harvested: EDGAR layer, sentence-diff helpers, the point-in-time
validation harness (IC / temporal contamination guard / runlog / costs), Batches client, model
registry + cutoff ladder, signal cache, FMP grab tooling. Greenfield: full segmentation, trap
signals, the Agent-SDK forensic/adversarial layers, calibration. Each module's docstring marks
PORT-from-parley vs GREENFIELD.

> Note (4 Jun 2026): parley's cross-sectional LLM ranking approach showed **no** measurable IC
> over a clean window. This project bets differently — LLM as *reader/parser* on a *documented*
> anomaly, not LLM-as-judge. The L3 test (Phase 1) is the go/no-go.

## Setup
```
uv sync
cp .env.example .env   # ANTHROPIC_API_KEY, SEC_USER_AGENT, FMP_API_KEY
```
