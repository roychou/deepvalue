# CLAUDE.md — deepvalue

Project instructions, auto-loaded each session. Read `notes/deep-value-system-design.md` (the
spec, §12 contracts are the source of truth) before working on any layer.

## What this is
A long-only Graham-screen / Burry-lens forensic engine: reads SEC filings at scale to find
concentrated, asset-backed long positions in neglected (micro-cap) equities. Emits a
recommendation artifact for a human — **takes no trading action**. Actions: `BUY` / `WATCH` / `PASS`.

## Why it exists (the bet — read this before "improving" anything)
Seeded from the **parley** repo (sibling: `../parley`). On 4 Jun 2026
parley's cross-sectional **LLM-as-judge** stock-ranking approach was shown to have **no**
measurable Information Coefficient over a contamination-free window (mean IC −0.005, t −0.21,
39 dates). deepvalue bets *differently*:
- **LLM as reader/parser, NOT judge.** The edge is a *documented* anomaly, not model opinion.
- **Primary edge = L3 diff engine** — the "Lazy Prices" YoY filing-language-change underreaction.
  It persists *because* the reading labor is tedious. This is the one durable signal.
- **Secondary edge = L5 adversarial trap filter** — kill value traps (cheap *because* dying).
Do not reintroduce "LLM forms a conviction that ranks names" — that's the disproven approach.

## Hard rules (load-bearing)
- **Point-in-time discipline is non-negotiable.** Use `Filing.filing_date`, NEVER
  `period_of_report`. The backtest must fail loudly if any feature references data dated after
  `as_of`. Most design-vs-design disagreements are lookahead bias, not model quality.
- **Survivorship-free universe.** Include delisted/dead tickers or the trap-detection metric is
  meaningless. Prices come from FMP (see Data, below).
- **Every forensic/diff finding cites an exact location** (`canonical_section.sentence_id`). An
  uncited finding is noise.
- **Long-only.** L5 is a trap *filter* (keep/kill), never a short-seller.
- **Mandatory human sign-off on every BUY** (`config/policy.yaml`).

## Spending money (LLM) — ASK FIRST
Never launch an LLM-spending run (materiality passes, agent runs, backtests) without the
operator's explicit go-ahead. State the run + estimated cost + a hard cap, then WAIT for "go".
Prefer cheap synchronous iteration over big set-and-forget runs. The Batches API is for large
set-and-forget sweeps, NOT iterative probes (its latency stacks badly). Carry a `--max-llm-usd`
cap on anything that spends.

## Architecture (Anthropic-native — NO graph framework)
Layers map 1:1 to `src/deepvalue/`. The funnel (L0–L3, L6–L7) is plain async Python; only the
agentic layers (L4 forensic, L5 adversarial) use the **Claude Agent SDK** (`agents/`). Bounded
L5 loop = ordinary `while` (max_rounds=3), not a graph engine. See spec §13 for the primitive
mapping (Batches, code-execution tool, prompt caching, structured outputs, Files/Memory).

## Provenance — PORTED from parley vs GREENFIELD
Each module docstring marks its status. Already ported and working (IC tests pass here):
- `eval/ic.py`, `eval/temporal.py`, `models.py` — the contamination-aware validation harness +
  model registry / cutoff ladder. **The crown jewel — reuse it for the L3 backtest.**
- `ingest/edgar.py`, `ingest/edgar_filings.py`, `ingest/fundamentals.py` — EDGAR data + filings
  layer (cwd-relative `data/cache/` paths; run from repo root).
- `diff/align.py` — deterministic sentence-diff core (needs the §7 extensions: sentence IDs,
  3-way alignment, embedding moved-text detection).
- `ingest/fmp_client.py` + `scripts/fmp_survivorship_probe.py` — FMP client + survivorship probe.
- `ingest/prices.py` — **PORTED (done)**: `get_prices(ticker)` reads the FMP grab cache
  (`data/cache/prices/`), unioning a ticker's active + delisted keys into `{date: {close,...}}`.
Greenfield (not started): full segmentation (`ingest/segmentation.py`), trap signals
(`quant/trap_signals.py`), all `agents/subagents/*`, calibration (`calibration/*`).

## Data
- **EDGAR** (free): filing text + post-2009 XBRL fundamentals. Set `SEC_USER_AGENT`; ~10 req/s cap.
- **FMP** (paid sub — **expires ~end June 2026**): survivorship-free universe + delisted prices +
  pre-2009 fundamentals. **GRAB DONE (5 Jun 2026)** — `scripts/fmp_grab.py` pulled ~17.5k US names
  (active+delisted, listed+OTC) to `data/cache/` (gitignored: `prices/`, `fundamentals/`,
  `manifest.json`). **99.0% per-symbol coverage**, 0 point-in-time violations; the ~1% gap is
  non-common-equity (units/preferreds/warrants) the screen excludes. Keyed by `symbol+status+
  delistedDate` (NOT CIK — collides on dual-class), series clipped at `delistedDate`. Rebuild the
  manifest from disk anytime: `uv run python scripts/fmp_build_manifest.py`. **NEXT: port
  `ingest/prices.py` to read this cache** (still a stub). Keys in `.env` (gitignored).

## Build order — validate the edge before building the machine (spec §15)
1. **Phase 1 — L0 (ingest+segmentation) + L3 (diff anomaly backtest). START HERE.** Statistically
   powered (thousands of filing-pairs × forward returns — unlike the concentrated BUY verdicts).
   If quiet YoY language changes don't predict underperformance, **stop** — the agents won't save it.
2. L1 quant gate + trap signals → 3. L2 triage+cache → 4. L4 specialists → 5. L5 trap filter →
   6. L6 sizing + L7 calibration.

The immediate gate before Phase 1 spend: the FMP survivorship grab is **done** (universe locked in
`data/cache/`); the remaining gate is to **port `ingest/prices.py`** to read that cache.

## Environment
Python ≥3.11, `uv`. `uv sync`; run commands with `uv run` (e.g. `uv run pytest`). Cross-repo:
to read parley source for further porting, launch with `--add-dir ../parley`.
