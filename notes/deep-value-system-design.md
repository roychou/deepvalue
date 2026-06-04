# Deep-Value Multi-Agent System — Design Specification

> Working title: a Graham-screen / Burry-lens forensic engine that reads filings at scale to find concentrated, asset-backed long positions in neglected equities.

---

## 0. How to read this document

This is a build spec written for an engineering agent (Claude Code) and for side-by-side comparison against an alternative design. Each layer section follows the same shape:

- **Purpose** — why the layer exists and what edge (if any) it carries.
- **Inputs / Outputs** — the data contract. Contracts are the comparison surface; keep them stable.
- **Deterministic vs LLM** — what runs as plain code vs what needs a model. Cost and reliability both live here.
- **Implementation** — concrete approach and tech choices.
- **Failure modes** — where this layer breaks and how it's mitigated.

The data contracts in §11 are the source of truth. If prose and a contract disagree, the contract wins.

---

## 1. Design constraints & investment posture

These three constraints are load-bearing and shape several layers. State them explicitly so the comparison design is evaluated on the same terms.

1. **Long-only. No shorting, ever.** The system's only actions are `BUY`, `WATCH`, `PASS`. This materially changes Layer 5: the "adversarial short-seller" from the source blueprint is **re-scoped to a value-trap *filter*** — a skeptic whose sole job is to decide whether to *reject* a long candidate. It emits no short thesis, no borrow analysis, no short signal. Removing shorting also removes short-squeeze risk, borrow-cost modeling, and the regulatory surface that comes with it.
2. **Burry, not Graham — concentrated conviction.** This is a deep-dive engine, not a diversified basket machine. The funnel exists to find a *small number* of names worth heavy forensic work. The default verdict is `PASS`; cash is an acceptable position; the bar to `BUY` is deliberately high. Sizing is conviction-weighted and concentrated (target ~8–15 positions). See §10 (Policy).
3. **This design will be validated against another.** Therefore: self-contained, explicit interfaces, no hidden assumptions, and an evaluation harness (§12) defining the metrics both designs are scored on. Point-in-time discipline (§12.2) is non-negotiable — most backtest disagreements between two designs trace to lookahead bias, not to the models.

### 1.1 What is and isn't claimed as edge

| Layer | Edge claim | Honest assessment |
|---|---|---|
| Quant gate | None | Table stakes. Every terminal runs these screens. Its only job is cheap universe reduction. |
| Diff engine | **Yes — primary** | Year-over-year text-change underreaction ("Lazy Prices" anomaly) is documented and persists *because* the labor is tedious. This is the durable signal. |
| Forensic specialists | Partial | Tireless reading of footnotes at scale. Real, but the hardest cases (fraud, novel maneuvers) are exactly where LLMs are weakest. |
| Adversarial filter | **Yes — secondary** | Value traps (cheap *because* dying) are the #1 killer in deep value. A real red-team that routes objections back to evidence is defensible. |
| Calibration | Enabling | Not edge itself, but without it you can't tell a sharp reader from one that cries wolf, or detect the diff edge decaying. |

The durable edge is narrow: **tireless forensic reading aimed at the illiquid corners institutions structurally cannot trade, with a calibration loop that proves it works.**

---

## 2. System overview

```
                        [ SEC EDGAR ]
                              |
        ┌─────────────────────────────────────────┐
        │  L0  Ingestion & section segmentation     │  deterministic
        └─────────────────────────────────────────┘
                              |
        ┌─────────────────────────────────────────┐
        │  L1  Quantitative gate + trap signals     │  deterministic
        └─────────────────────────────────────────┘
                              |   (~hundreds of tickers)
        ┌─────────────────────────────────────────┐
        │  L2  Cheap triage gate + semantic cache   │  Haiku
        └─────────────────────────────────────────┘
                  |  (cache hit / no change → skip)
        ┌─────────────────────────────────────────┐
        │  L3  Structural diff engine  ◄── EDGE     │  diff + Sonnet
        └─────────────────────────────────────────┘
                              |
        ┌─────────────────────────────────────────┐
        │  L4  Forensic specialists (parallel)      │  Opus + code interp.
        │      footnote · asset · capital-structure │
        └─────────────────────────────────────────┘
                              |  ▲ rebut (routed objections)
        ┌─────────────────────────────────────────┐
        │  L5  Adversarial loop (trap filter)  ◄─EDGE│  Opus, bounded cycle
        └─────────────────────────────────────────┘
                              |
        ┌─────────────────────────────────────────┐
        │  L6  Verdict + Burry sizing policy        │  deterministic
        └─────────────────────────────────────────┘
                              |   outcomes ▼   ▲ priors
        ┌─────────────────────────────────────────┐
        │  L7  Calibration & memory (learning loop) │  deterministic
        └─────────────────────────────────────────┘
```

The two feedback paths are what distinguish this from a linear pipeline: L5 routes objections **back** to L4 specialists for rebuttal, and L7 feeds learned priors **back up** to the agents.

---

## 3. Proposed repository layout

```
deepvalue/
  pyproject.toml
  README.md
  config/
    screen_profiles.yaml      # net-net / normalized-earnings / hidden-assets profiles
    policy.yaml               # Burry sizing, thresholds, position caps
    models.yaml               # model assignment per node (Haiku/Sonnet/Opus)
  src/deepvalue/
    contracts/models.py       # ALL pydantic models — single source of truth
    ingest/
      edgar_client.py         # rate-limited EDGAR client (your code)
      xbrl.py                 # companyfacts / financial-statement extraction
      segmentation.py         # filing -> canonical sections (heuristics + Haiku fallback)
      normalize.py            # text normalization + sentence IDs
      store.py                # filing store (= cache), keyed (cik, accession, section)
    quant/
      metrics.py              # value metrics (pure python)
      trap_signals.py         # Z-score, M-score, F-score, runway, dilution
      screen.py               # profile application + ranking
    triage/
      cache.py                # sha256(section) -> verdict cache (your store)
      gate.py                 # Haiku triage, submitted via Batches API
    diff/
      align.py                # deterministic sentence alignment across periods
      materiality.py          # Sonnet materiality pass on changed spans, via Batches API
    agents/                   # Claude Agent SDK harness lives here (replaces orchestration/)
      harness.py              # supervisor: ClaudeAgentOptions, subagent registry, run loop
      tools.py                # custom tools exposed to agents (fetch_section, fetch_xbrl, ...)
      subagents/
        footnote.py           # forensic: off-balance-sheet, VIE, related-party, leases
        asset_auditor.py      # forensic: asset reality; uses code_execution for recon
        capital_structure.py  # forensic: debt ladder, covenants, dilution
        bull.py               # thesis synthesizer
        skeptic.py            # value-trap attacker (LONG-ONLY: kill/keep only)
        judge.py              # adjudication + termination of the bounded loop
      loop.py                 # bull -> skeptic -> route-to-subagent -> judge (max_rounds=3)
    calibration/
      outcomes.py             # link verdicts to subsequent events + returns
      metrics.py              # precision/recall/Brier per agent & flag type
      feedback.py             # priors -> weights (optionally persisted via Memory tool)
    eval/
      backtest.py             # point-in-time replay harness
      scorecard.py            # design-vs-design comparison metrics
  tests/
  data/                       # local cache / sqlite (gitignored)
```

No graph framework. The funnel (L0–L3, L6–L7) is plain async Python; only the agentic layers (L4–L5) use the Claude Agent SDK harness in `agents/`. Module boundaries map 1:1 to layers so the comparison design can be diffed component-by-component.

---

## 4. Layer 0 — Ingestion & section segmentation

**Purpose.** Turn raw filings into two clean planes — structured numbers (XBRL) and segmented, sentence-IDed narrative — that every downstream layer depends on. This is unglamorous and is the single biggest upstream point of failure: every agent inherits its segmentation errors.

**Sources (SEC EDGAR).**
- Submissions index: `https://data.sec.gov/submissions/CIK{10-digit}.json` — filing history, accession numbers, form types, dates.
- XBRL company facts: `https://data.sec.gov/api/xbrl/companyfacts/CIK{10-digit}.json` — all reported us-gaap concepts.
- Bulk Financial Statement Data Sets — for full-universe screening without per-ticker calls.
- Raw filing documents from the EDGAR archives (primary `.htm` document + iXBRL).

**Compliance.** Declare a descriptive `User-Agent` (SEC requires it), respect the ~10 requests/sec fair-access limit, back off on 429s. Cache aggressively (L0 store doubles as the L2 cache).

**Section segmentation (the hard part).** Map each filing into stable canonical sections:
- 10-K Items: `1`, `1A` (Risk Factors), `1B`, `2`, `3`, `7` (MD&A), `7A`, `8` (Financial Statements + notes), etc.
- 10-Q: Part I/II items.
- Footnotes within Item 8, classified by content into canonical types: `revenue_recognition`, `leases`, `vie`, `related_party`, `pension_opeb`, `debt`, `commitments_contingencies`, `segment`, `goodwill_intangibles`, `income_taxes`.

Approach: header/anchor heuristics first (regex on `Item 1A.`, TOC anchors, bold/numbered note headers) to propose boundaries → a **small model only resolves ambiguous boundaries and classifies footnotes by content**. Do not send whole filings to a model for segmentation; it's expensive and unstable.

**Normalization for diffing.** Strip formatting, normalize whitespace and number formats, sentence-tokenize, and assign **stable sentence IDs within each canonical section** so L3 diffs can cite exact spans (`10-K.item_1a.s_042`).

**Storage.** Filing store keyed by `(cik, accession_no, canonical_section_id)`. This is also the semantic cache (§6).

**Failure modes.**
- Formatting drift across issuers and years → heuristics miss boundaries. Mitigation: model fallback + per-section confidence; low-confidence sections flagged, not silently dropped.
- Amended filings (`10-K/A`) and restatements → must supersede correctly, keyed by period_of_report not filing order.
- iXBRL extraction quirks; exhibits vs primary document.
- Foreign private issuers file `20-F`/`40-F` (different structure) — either support explicitly or exclude (and record the exclusion, since neglected foreign issuers are also higher fraud-risk; see §9 risk).

---

## 5. Layer 1 — Quantitative gate (deterministic, no LLM)

**Purpose.** Cheap reduction of the full universe (thousands) to a few hundred candidates, each tagged with *why it's cheap* and *how likely it's dying*. No LLM touches this layer — LLMs are bad at precise arithmetic and there's no edge here to justify the cost.

**Value metrics (per `screen_profiles.yaml`).** Burry deep value is broader than pure net-nets, so support multiple profiles:
- `net_net` — NCAV = current assets − total liabilities; net-net working capital; classic price ≤ ⅔ NCAV. Plus price-to-tangible-book.
- `normalized_earnings` — cheap on through-cycle EV/EBIT or EV/FCF with a temporary, identifiable problem.
- `hidden_assets` — sum-of-parts / non-operating assets (real estate, securities, NOLs) worth more than market cap.

**Trap-risk signals (computed in the same pass — this is the pre-warning that makes the expensive layers efficient).**
- Altman Z-score — bankruptcy proximity.
- Beneish M-score — earnings-manipulation likelihood.
- Piotroski F-score (0–9) — fundamental strength.
- Cash runway = cash / quarterly burn; debt maturity wall within 24 months.
- Share-count growth (dilution), auditor change, going-concern language presence (boolean from L0 sections).

**Output.** Ranked `QuantScreenResult` records; only `passed=True` rows proceed.

**Failure modes.** Stale/missing XBRL tags; negative-working-capital businesses that aren't distressed (banks, insurers — exclude by SIC); GAAP tag inconsistency across filers. Mitigation: tag normalization map; sector exclusions; require minimum data completeness before a ticker is screenable.

---

## 6. Layer 2 — Cheap triage gate + semantic cache (Haiku)

**Purpose.** Prevent the expensive layers from re-running on the whole screen every cycle. Most of the steady-state cost savings live here.

**Cache.** Keyed on `sha256(normalized_section_text)`. If a candidate's relevant sections are unchanged and a prior verdict exists → return the cached verdict, do nothing else.

**Triage decision (Haiku-class).** For names with new/changed filings, a cheap model makes a `proceed` decision and kills obvious disqualifiers that aren't worth waking Opus for (already-disclosed going concern with no asset coverage, blank-check/shell structures, classic reverse-merger micro-cap red flags). It also returns *which* canonical sections changed, so L3 only diffs those.

**Output.** `TriageDecision{proceed, reason, cache_hit, sections_changed[]}`.

**Failure modes.** Over-eager triage killing a real opportunity (false negative is worse than a wasted Opus call here, given concentration). Mitigation: triage may only `PASS` on hard disqualifiers; anything ambiguous proceeds. Log every triage kill so calibration (§8) can measure its false-negative rate.

---

## 7. Layer 3 — Structural diff engine (PRIMARY EDGE)

**Purpose.** Capture the documented underreaction to *quiet* year-over-year language changes in Risk Factors and MD&A. This is the layer worth proving before building anything else (§13 Phase 1).

**Deterministic diff first.** For each candidate, align the same canonical section across three references: this period vs prior quarter vs same quarter prior year. Sentence-level alignment via `difflib.SequenceMatcher` for exact/near matches, with an embedding-similarity pass to detect *moved* or *reworded* text (so a relocated paragraph isn't flagged as delete+add). Classify each change as `added` / `deleted` / `modified`.

**LLM materiality pass — changed spans only.** Never send a whole section to a model; send only the diffed spans. A Sonnet-class model scores each change for materiality (0–1) and category: `risk_escalation`, `customer_concentration`, `covenant`, `liquidity`, `litigation`, `accounting_policy_change`, `removed_reassurance`. The subtle, high-value signal is **`removed_reassurance`** — the quiet deletion of a sentence that previously said a key customer or covenant was secure.

**Output.** List of `DiffFinding`, each citing exact `sentence_ids`. These are the primary input to the L7 backtest.

**Failure modes.** Segmentation errors misalign sections → spurious diffs. Boilerplate churn (legal language updates) flagged as material. Mitigation: a boilerplate suppressor (changes appearing across many unrelated filers in the same window are likely template updates, down-weighted); alignment confidence gating.

---

## 8. Layer 4 — Forensic specialists (parallel, Opus + code interpreter)

**Purpose.** Read the exact sections where management hides structural risk, and verify the asset base behind a cheap valuation is real. Targeted structural extraction feeds each agent only its relevant sections (cheaper, higher precision than dumping the whole 10-K).

**Hard rule: every finding cites an exact location (`canonical_section.sentence_id`).** An uncited finding is noise, and the citation is what lets L5 route an objection back to the specific claim.

### 8.1 Footnote Archaeologist
Targets disguised debt and hidden liabilities that impair tangible book: off-balance-sheet arrangements, VIEs ("debt they don't own"), related-party transactions (CEO buying from a relative's company at inflated prices), pension/OPEB shortfalls, operating and finance lease obligations, commitments & contingencies, and debt covenants.

### 8.2 Asset Auditor (central to an asset-based / net-net thesis)
Tests whether book value is real: inventory aging and obsolescence, receivables quality (DSO trends, allowance adequacy), goodwill/intangible impairment risk, PP&E realism, deferred-tax-asset recoverability, and securities marks. **Uses the code interpreter to reconcile narrative figures against XBRL statement values** — a mismatch is itself a finding.

### 8.3 Capital-Structure / Liquidity Agent
Debt maturity ladder, covenant headroom, refinancing risk, and dilution trajectory. (Can be merged into 8.1 for v1; kept separate here for clean comparison.)

**Code interpreter.** A sandboxed Python tool. The LLM *writes and runs* the math; it never does arithmetic in-token. All reconciliations and impairment estimates flow through it.

**Supervisor.** Dispatches the three agents in parallel, dedupes overlapping findings, and assembles a structured dossier.

**Output.** `ForensicFinding[]` + `CodeAudit[]`.

**Failure modes.** This is the weakest layer: the screen output is mostly traps and fraud, and fraud detection is exactly where LLMs fail (the source blueprint admits novel maneuvers defeat it). Mitigation: treat forensic findings as *flags for the adversarial filter*, not as conclusions; force citation; never let a clean forensic pass alone trigger a BUY (L5 must still clear).

---

## 9. Layer 5 — Adversarial reflexive loop (SECONDARY EDGE, long-only trap filter)

**Purpose.** Kill value traps — names cheap *because* they're dying. This is the biggest single source of permanent loss in deep value.

**Long-only re-scope (important for the comparison).** The source blueprint frames this as a "short-seller." Under the no-shorting constraint, the agent is a **bankruptcy attorney / skeptical analyst whose only output is keep-or-kill on a long candidate.** It produces no short thesis, no borrow analysis, no short signal. The system's action space is `BUY` / `WATCH` / `PASS`.

**Flow.**
1. **Bull synthesizer** assembles a thesis from L1–L4: why it's cheap, where the margin of safety is (asset coverage), and what could re-rate it.
2. **Skeptic** attacks the thesis. Each objection is *typed* and routed back to the relevant specialist for rebuttal: inventory-obsolescence → Asset Auditor; disclosure/hidden-liability → Footnote Archaeologist; valuation arithmetic → code interpreter; refinancing → Capital-Structure agent.
3. **Judge** adjudicates each contested point against the cited evidence. The loop terminates when no new *material, unrebutted* objection remains, or at `max_rounds = 3` (so it can't spin).

**Honest caveat.** Debate amplifies whatever reasoning dominates; two confident agents can converge on a wrong answer. The defense is the routing requirement — every objection must survive contact with cited filing text, not just sound plausible. This is why citations in L4 are mandatory.

**Burry tilt.** Default verdict is `PASS`. Only strongly defended theses with real asset-backed margin of safety reach `WATCH`/`BUY`.

**Output.** `ThesisVerdict{decision, conviction, margin_of_safety, surviving_risks[], dependencies[], unresolved_objections[]}`.

---

## 10. Layer 6 — Verdict + Burry sizing policy (deterministic)

**Purpose.** Convert verdicts into a concentrated, conviction-weighted target book. This is where the Burry-over-Graham choice is encoded; the *same* pipeline would serve Graham mode with different config.

`config/policy.yaml` (illustrative):
```yaml
posture: burry_conviction
max_positions: 15          # concentrated, not a basket
min_conviction_to_buy: 0.72
min_margin_of_safety: 0.40 # require ~40% discount to conservative asset value
sizing: fractional_kelly
kelly_fraction: 0.25       # capped; never full Kelly
max_position_weight: 0.12  # hard cap per name
cash_ok: true              # holding cash when nothing qualifies is a valid state
illiquidity_aware: true    # scale size down by ADV; this is where the edge lives
liquidity_floor_adv_usd: 50000
exclude_sectors: [banks, insurers, blank_check]
```

Notes: concentration means each error hurts more, so the L7 calibration loop is *more* important here, not less. Illiquidity-awareness is deliberate — the durable edge is in names institutions can't trade, so position sizing must respect average daily volume rather than pretend the names are liquid.

**Output.** `TargetBook` of `Position{ticker, target_weight, conviction, thesis_ref}`. This is a recommendation artifact for a human; the system takes no trading action.

---

## 11. Layer 7 — Calibration & memory (the learning loop)

**Purpose.** Make the system know whether it's any good — and detect the diff-engine edge decaying as the anomaly gets arbitraged. Skipping this is the most common reason a design like this silently rots.

**Outcome tracking.** Link every verdict and forensic flag to subsequent reality:
- Events: restatements (8-K Item 4.02), going-concern issuance, bankruptcy, delisting, large impairments.
- Forward returns over 6 / 12 / 24-month windows from `as_of` (point-in-time, no lookahead).

**Metrics.**
- Per-agent and per-flag-type precision/recall (did flagged risks actually materialize?).
- Brier score on `conviction` (is a 0.8 conviction right ~80% of the time?).
- BUY-verdict hit rate; triage false-negative rate; diff-finding predictive power over time (edge-decay monitor).

**Feedback.** Priors that down-weight noisy agents/flag types feed back to L4/L5; conviction scoring is recalibrated against realized Brier. Storage: sqlite for dev, postgres for scale.

**Output.** `OutcomeRecord[]`, `AgentCalibration[]`.

---

## 12. Data contracts (source of truth)

Pydantic-style; the comparison design should expose equivalents.

```python
from pydantic import BaseModel
from datetime import date
from typing import Optional, Literal

class Filing(BaseModel):
    cik: str
    ticker: str
    accession_no: str
    form_type: str
    filing_date: date           # USE THIS for point-in-time, never period_of_report
    period_of_report: date
    primary_doc_url: str

class FilingSection(BaseModel):
    filing_id: str              # accession_no
    canonical_id: str           # e.g. "10-K.item_1a", "footnote.related_party"
    title: str
    normalized_text: str
    sentences: list[dict]       # [{ "sentence_id": "s_042", "text": "..." }]
    seg_confidence: float

class QuantScreenResult(BaseModel):
    ticker: str
    cik: str
    as_of: date
    screen_profile: Literal["net_net", "normalized_earnings", "hidden_assets"]
    value_metrics: dict         # ncav, ev_ebit, p_tbv, ...
    trap_signals: dict          # z_score, m_score, f_score, runway_months, going_concern
    passed: bool
    rank: int

class TriageDecision(BaseModel):
    ticker: str
    proceed: bool
    reason: str
    cache_hit: bool
    sections_changed: list[str]

class DiffFinding(BaseModel):
    ticker: str
    section: str
    change_type: Literal["added", "deleted", "modified"]
    category: str               # removed_reassurance, covenant, liquidity, ...
    materiality: float          # 0-1
    old_span: Optional[str]
    new_span: Optional[str]
    citation: list[str]         # sentence_ids
    rationale: str

class ForensicFinding(BaseModel):
    ticker: str
    agent: Literal["footnote", "asset", "capital_structure"]
    finding_type: str
    severity: float             # 0-1
    impairs_book_value: bool
    est_impact_usd: Optional[float]   # computed via code interpreter
    citation: list[str]
    rationale: str
    requires_rebuttal: bool

class Objection(BaseModel):
    id: str
    type: str                   # routes to a specialist
    claim: str
    routed_to: str
    status: Literal["open", "rebutted", "sustained"]
    evidence: list[str]         # citations

class ThesisVerdict(BaseModel):
    ticker: str
    as_of: date
    decision: Literal["BUY", "WATCH", "PASS"]
    conviction: float           # 0-1, calibrated against realized Brier
    margin_of_safety: float     # discount to conservative asset value
    surviving_risks: list[str]
    dependencies: list[str]
    unresolved_objections: list[Objection]
    bull_summary: str

class OutcomeRecord(BaseModel):
    verdict_id: str
    ticker: str
    decision: str
    conviction: float
    as_of: date
    outcome_events: list[str]
    forward_returns: dict       # {"6m": .., "12m": .., "24m": ..}
    realized: bool

class AgentCalibration(BaseModel):
    target: str                 # agent or flag_type
    precision: float
    recall: float
    brier: Optional[float]
    sample_n: int
    weight: float               # fed back into L4/L5
```

---

## 13. Orchestration (Anthropic-native — no graph framework)

This pipeline does **not** need LangGraph or any graph engine. Graph frameworks earn their keep on dynamic topologies with checkpointing; this is a linear funnel with one bounded cycle (the L5 rebuttal loop) and two conditional skips (cache hit, triage PASS). Those are `if` / `while` / `asyncio.gather` in plain Python. Everything the orchestration needs maps to a first-party Anthropic primitive.

### 13.1 What runs where

- **Funnel (L0–L3, L6–L7): plain async Python.** No agent loop, no framework. The conditional skips and the `max_rounds=3` cycle are ordinary control flow that the codebase owns directly.
- **Agentic layers (L4–L5): Claude Agent SDK.** The SDK is the agent harness that powers Claude Code, exposed as a library — it provides the agent loop, built-in tools, context management, and **subagents**, so we don't hand-roll a tool-execution loop. The supervisor lives in `agents/harness.py`; each specialist and the bull/skeptic/judge are **subagents**.

### 13.2 Primitive mapping

| Concern | Anthropic primitive | Notes |
|---|---|---|
| Agent loop + tool execution (L4/L5) | **Claude Agent SDK** (`claude_agent_sdk`) | Replaces the LangGraph supervisor-worker. Subagents = the parallel specialists. |
| Code interpreter (L4 reconciliation, L5 valuation math) | **Code execution tool** (`code_execution_20250825`) | Sandboxed Python/Bash on Anthropic infra. ~$0.05/container-hour beyond a free monthly allowance; free when combined with web search/fetch. The LLM never does arithmetic in-token. |
| Specialist fan-out / tool orchestration | **Programmatic Tool Calling** | Claude writes one Python script that calls our tools (fetch_section, fetch_xbrl) and runs reconciliation in-sandbox, returning only the final finding — keeps the 10-K out of the context window. Enables parallel tool execution. |
| Bulk LLM passes (L2 triage, L3 materiality across hundreds of names) | **Message Batches API** (`client.messages.batches`) | Up to 10k requests/batch, <24h, ~50% cheaper. Non-time-sensitive classification is the canonical use case. Higher rate limits, isolated from interactive limits. |
| Shared filing context across the 3 specialists | **Prompt caching** | One filing is read by footnote + asset + capital-structure agents; cache the shared context so it's not re-billed per agent. (Distinct from the L0 persistent filing store.) |
| Contract-shaped outputs (§12 models) | **Structured outputs** | Get pydantic-shaped `DiffFinding` / `ForensicFinding` / `ThesisVerdict` reliably. |
| Filing/PDF ingestion | **Files API** | Upload-once / reuse-many across Messages, Batches, and code execution. |
| Persisted priors for agents (L7) | **Memory tool** (`memory_20250818`) | Optional; file-based cross-session memory. Structured outcome stats still live in our own DB. |

### 13.3 The L5 bounded cycle, concretely

```python
# agents/loop.py  — no graph engine, just control flow
async def run_adversarial(thesis, subagents, max_rounds=3):
    verdict = await subagents.bull(thesis)
    for _ in range(max_rounds):
        objections = await subagents.skeptic(verdict)          # typed objections
        open_objs = [o for o in objections if o.status == "open"]
        if not open_objs:
            break
        for o in open_objs:                                    # route back to evidence
            specialist = ROUTING[o.type]                       # asset / footnote / capital
            o.evidence = await subagents[specialist].rebut(o)  # re-invoke the specialist
        verdict = await subagents.judge(verdict, objections)   # adjudicate, may sustain/kill
        if verdict.decision == "PASS":
            break
    return verdict
```

The "routing back to a specialist" that the architecture diagram shows as the `rebut` arrow is just re-invoking a subagent with the objection — no special graph machinery.

### 13.4 Model assignment

`config/models.yaml` assigns a model per node — Haiku (triage), Sonnet (diff materiality), Opus-class (forensic + adversarial) — parameterized so the comparison design can swap tiers without code changes.

### 13.5 Caveats to honor

- Code-execution environments **do not share state**; pass outputs forward explicitly between calls rather than assuming a shared filesystem.
- Batch results and code-execution containers have multi-week retention and are **not ZDR-eligible**. Irrelevant here (SEC filings are public), but note it if the comparison design makes a data-handling claim.
- The only non-Anthropic dependencies are the data source (SEC EDGAR) and ordinary Python libraries (pandas, difflib, an EDGAR client). No agent/orchestration framework is required.

---

## 14. Evaluation harness (for design-vs-design comparison)

### 14.1 Metrics both designs are scored on
- **Trap-detection precision/recall** — of names later hit by bankruptcy/restatement/going-concern, how many did the system `PASS`? (The core long-only safety metric.)
- **BUY hit rate & forward return** — 12/24-month returns of `BUY` verdicts vs a matched cheap-universe benchmark.
- **Conviction calibration (Brier)** — are the confidence scores honest?
- **Cost per analyzed name** and **end-to-end latency**.
- **Coverage** — fraction of the eligible universe successfully processed (segmentation success rate).

### 14.2 Point-in-time discipline (non-negotiable)
The backtest must replay using **`filing_date`**, never `period_of_report`, and must use as-of fundamentals and prices with no forward leakage. Most disagreements between two designs are artifacts of lookahead bias, not model quality. The harness should fail loudly if any feature references data dated after `as_of`.

### 14.3 Scorecard
`eval/scorecard.py` runs both designs over the same point-in-time universe and emits a single comparison table across the §14.1 metrics, plus a per-layer cost breakdown.

---

## 15. Build order (validate the edge before building the machine)

1. **Phase 1 — L0 (ingest + segmentation) + L3 (diff engine).** Backtest the year-over-year language-change anomaly on the target universe *before writing any agent*. If quiet language changes don't predict underperformance in these names, stop — the rest won't save it.
2. **Phase 2 — L1 quant gate + trap signals.** Establishes the candidate funnel.
3. **Phase 3 — L2 triage + cache.** Makes steady-state runs affordable.
4. **Phase 4 — L4 specialists** with the citation rule and code-interpreter reconciliation.
5. **Phase 5 — L5 adversarial trap filter** with typed routing and the bounded cycle.
6. **Phase 6 — L6 Burry sizing policy + L7 calibration.** Close the loop; start measuring.

---

## 16. Risks & open questions

- **Fraud detection is the weakest link** and the screen output is fraud-heavy. The system's value rests on its hardest layer (L4/L5). Treat all "clean" verdicts with suspicion; require human sign-off on every BUY.
- **Segmentation fragility (L0)** propagates everywhere. Invest here first; gate on seg confidence.
- **Anomaly decay (L3).** The diff edge erodes as more funds run the same idea. L7 must monitor predictive power over time and alert on decay.
- **Small-sample calibration.** Concentrated Burry mode produces few verdicts, so calibration statistics are slow to converge. Consider pooling flag-type stats across names.
- **LLM confabulation** in forensic/adversarial layers. Mitigation: mandatory citations + code-interpreter arithmetic.
- **Foreign-issuer handling.** Neglected foreign micro-caps are both fertile (cheap, ignored) and higher fraud-risk (different filing regime). Decide explicitly: support `20-F` or exclude and record the exclusion.
- **Survivorship bias** in the backtest universe — include delisted/dead tickers or the trap-detection metric is meaningless.
- **This system does not replace human judgment.** It reads 10,000 pages of footnotes without losing focus and surfaces the three pages where the hidden value or hidden crisis lives. A human makes the call.