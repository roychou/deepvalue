# Prototype: Technical Earnings-Call Parsing for Capex & Capability Signals

A build blueprint for turning hyperscaler/semiconductor earnings communications into a
structured, tradable signal. Written for someone who can build the pipeline themselves, so
the emphasis is on the parts that are easy to get *subtly* wrong: the schema, the
baseline, and validation.

---

## 1. Sharpen the hypothesis first

The vague version ("parse earnings calls for AI signals") is not a strategy. Pin it down:

> When a large capex spender (a hyperscaler) **revises capex guidance upward beyond its
> own prior baseline**, accompanied by credible demand/supply-constraint language, the
> **downstream supply chain** — second-order suppliers (compute, networking, power,
> cooling) and the **tertiary-order names** that feed *them* (foundries, semicap
> equipment, advanced packaging, photonics components, power semis, electrical/grid gear,
> specialty materials) — has predictable forward drift, because the market prices the
> headline name (e.g. the obvious chip vendor) within minutes but is slower and sloppier
> pricing the less-covered links. The further down the chain you go, the *less* covered
> the name — and so potentially the *larger* the under-reaction — but also the *noisier*
> the link, because that supplier's revenue is shared across many end-markets, not just AI.

Two distinct claims live here, and you should test them separately:

- **Extraction claim:** an LLM can pull *specific, structured* capex/capability signals
  from a call more consistently and comprehensively than reading by hand.
- **Inefficiency claim:** the second- and tertiary-order names under-react relative to the
  information content of those signals, over a tradable window — and the effect should be
  *more pronounced but noisier* the deeper into the chain you go.

The LLM only addresses the first. The edge — if it exists — is the second, and your
*domain knowledge* is what tells you which suppliers are load-bearing and which capability
claims are real. Keep these roles separate in your head; conflating them is how people
fool themselves.

**The cleanest quantitative anchor** is the *capex guidance surprise*, because it's
verifiable and numeric. Treat capability claims as a softer, secondary, conditioning
signal — not the primary trigger.

---

## 2. Universe & data sources

### Companies (illustrative, not recommendations)
- **Spenders (the signal source):** the major hyperscalers + large enterprise-software
  capex spenders.
- **Second-order names (the direct expression):** accelerator/GPU vendors, HBM memory,
  optical networking/switching, power & thermal/cooling, datacenter REITs.
- **Tertiary-order names (the indirect expression):** what the second-order names depend
  on — foundries and advanced packaging, semiconductor capital equipment (litho,
  deposition, etch, test), photonics/laser components for optics, power semiconductors and
  grid/transformer equipment, and specialty materials/substrates. Less-covered and
  potentially more mispriced, but their AI exposure is diluted by other end-markets, so
  attribute the signal carefully.
- Maintain this as an explicit, **multi-hop supplier graph** (see §4) rather than a flat
  list.

### Transcripts / primary text — and the licensing trap
- **Self-generated (cleanest on copyright + gives you timestamps):** capture the webcast
  audio and run ASR (e.g. Whisper) yourself. You own the output and control timestamps.
- **Licensed APIs:** several vendors sell machine-readable transcripts and call audio.
  Budget for this if you go beyond a handful of names.
- **Primary filings:** SEC EDGAR for the 8-K earnings exhibit (press release + often the
  prepared remarks) and the 10-Q/10-K for **actual** capex.
- **Caution:** scraped third-party transcript sites are copyrighted. Don't build a
  production pipeline on them.

### The consensus problem — and a cheaper workaround
Sell-side **consensus capex** estimates (the "expected" number a surprise is measured
against) live behind expensive terminals. Cheaper, and arguably better for this thesis:
build your **own baseline from the company's prior statements** — what did they guide to
last quarter? Surprise = new guidance vs. *their own* most recent guidance. This needs no
third-party consensus data and directly captures the revision, which is what moves names.

### Market data
Point-in-time prices for the full universe, including the **after-hours** print (most of
these calls are after the close — see §5).

---

## 3. The extraction schema — how the LLM fits

The LLM's job is **extraction into structured JSON**, not opinion. One record per
document. Validate every field against a strict schema (e.g. pydantic) and **log the raw
model output** so you can audit and re-run.

```json
{
  "company": "TICKER",
  "fiscal_period": "Q2 FY2026",
  "event_timestamp_utc": "2026-04-24T20:05:00Z",
  "source_url": "https://...",
  "doc_type": "prepared_remarks | qa | press_release | 8k",

  "capex_signals": [
    {
      "normalized_claim": "Raising full-year capex; cites demand for AI infrastructure",
      "direction": "raising | lowering | maintaining | initiating",
      "magnitude_qualitative": "slight | moderate | significant",
      "explicit_numbers": { "value": null, "unit": null, "currency": null },
      "time_frame": "current_quarter | full_year | multi_year",
      "vs_prior_guidance": "higher | lower | inline | no_prior",
      "extraction_confidence": 0.0
    }
  ],

  "capability_claims": [
    {
      "paraphrase": "New inference cluster cuts latency for large models",
      "domain": "compute | model | efficiency | latency | product | other",
      "specificity": "vague | specific | quantified",
      "verifiability": "unverifiable | checkable_later | independently_verifiable",
      "novelty": "incremental | notable | breakthrough_claim"
    }
  ],

  "demand_signals":   [{ "paraphrase": "...", "strength": "soft | firm | sold_out" }],
  "supply_constraints":[{ "paraphrase": "...", "named_bottleneck": "gpu|power|hbm|..." }],

  "named_supply_chain_entities": ["supplier/sub-supplier or technology mentioned/implied"],

  "extraction_notes": "anything ambiguous the model flagged"
}
```

### Prompting rules that matter
- **Extract only what is stated.** Instruct the model explicitly: do not infer facts not
  present; if a field isn't supported by the text, return null. This is your main
  hallucination control.
- **Paraphrase, don't transcribe.** Store short normalized claims, not long verbatim text
  (cleaner on copyright, and forces the model to actually parse).
- **Calibrated confidence** on each capex extraction; you'll threshold on it later.
- **Deterministic settings** (low temperature) and a pinned model version for
  reproducibility across the backtest.
- Output **JSON only**; reject and retry on schema-validation failure.
- **Don't expect the call to name tertiary suppliers.** A hyperscaler rarely names the
  etch-tool maker three hops down. Extract only what's actually said; tertiary candidates
  come from your supplier graph (§4), not from the transcript.

---

## 4. From extraction to signal

1. **Persist a per-company guidance baseline.** Each quarter's extracted capex guidance
   becomes next quarter's baseline. This store *is* your consensus proxy.
2. **Compute the surprise score** — current-call direction/magnitude vs. that baseline,
   conditioned on demand strength and supply-constraint language. Quantify it; don't keep
   it as prose.
3. **Map to expressions via the multi-hop supplier graph.** A maintained mapping that
   chains hops: `spender → [second-order: suppliers, weights, link_type] → [tertiary-order:
   sub-suppliers, weights, link_type]`. Seed it by hand from your own knowledge, then let
   the LLM help expand it from filings (which is itself the supply-chain idea from
   earlier). Carry an **attenuation factor** along each hop: a tertiary name receives the
   spender's signal multiplied through two weights, so its effective exposure is smaller
   and shared with non-AI demand. Your judgment decides which links are load-bearing.
4. **Form the candidate trade** on the *mapped second- and tertiary-order names*, not (or
   not only) the headline name — the headline is where the edge is most likely already
   gone. Weight each candidate by its attenuated exposure, and treat thinly-covered
   tertiary names as **higher-variance** bets, not higher-conviction ones just because
   they're obscure.

---

## 5. Validation — the part that decides whether this is real

This section is the whole game. A profitable-looking notebook here is almost always a bug.

### Point-in-time discipline
- The tradable timestamp is the **call/release time**, typically after close. Your first
  realistic fill is the after-hours move or next open — **not** the prior close.
- **Never** use the 10-Q/10-K *actual* capex (filed weeks later) as if it were known at
  call time. Only the *guidance* was known then.
- Adoption/estimate data gets backfilled; snapshot everything as-of the event date.

### LLM lookahead — the killer
Because the model's training data has a cutoff, running it on an *old* call risks the
model already "knowing" how things played out, contaminating the signal.
- Frame the task as **pure extraction of stated content** — answers fully contained in the
  document leak far less than asking the model to opine or predict.
- **Sensitivity check:** redact dates and any forward-looking phrasing; does the
  extraction change? It shouldn't, for a clean extraction task.
- **True out-of-sample:** reserve calls dated *after* the model's training cutoff and
  confirm the signal survives there. This is the test that actually counts.

### Backtest as an event study
- Event = release timestamp. Measure forward returns of mapped names over windows
  `[t, t+1d]`, `[t, t+5d]`, `[t, t+20d]`, conditioned on signal sign/strength.
- Compare three buckets: signal-positive vs. signal-negative vs. universe baseline.

### Baselines you must beat (or the LLM adds nothing)
- The **headline number alone** — does parsing the language beat just reacting to the
  reported capex figure?
- A **regex/keyword** approach — if "capex" + "raising" + "demand" matches the LLM, you
  don't need the LLM.
- The **naive mapping** — "buy supplier after customer raises capex," no NLP at all.
- If a dumb baseline ties the LLM, the LLM is cost without edge. Kill or simplify.

### Costs, fills, and overfitting
- Model after-hours/open slippage realistically — the obvious move happens *before* you'd
  trade, so backtest fills at prices you could actually get.
- You will test many mappings, windows, and thresholds. Use **walk-forward**, hold out a
  final period you never inspect, and discount results by the number of hypotheses tried.

### Order-of-supplier attenuation
The signal weakens with each hop. A tertiary name's price responds to its *own* mixed
demand (AI + everything else), so a hyperscaler's capex revision explains a smaller,
noisier share of its moves than for a direct supplier. Practical implications:
- Test each order **separately** — does the second-order bucket show drift before you
  bother with tertiary? If second-order is flat, tertiary is almost certainly noise.
- Expect lower signal-to-noise and **worse liquidity** the deeper you go; size for it.
- **Attribution gets hard:** when a tertiary name moves, was it your hyperscaler signal, a
  different customer, or its non-AI business? Often you can't tell — which weakens any
  causal claim and makes the result easier to overfit.

### The honest sample-size problem
~4 calls/company/year × a handful of relevant companies ≈ **~100–200 events over several
years**. That is *small*. Statistical confidence will be limited, and a few big moves can
dominate. Two consequences:
- This is better as a **discretionary-augmentation tool** (it surfaces and structures the
  signal fast; you decide) than as a high-N statistical system.
- If you want real statistics, **broaden the universe** (more spenders, more quarters,
  adjacent sectors) to grow the event count — at the cost of a fuzzier thesis. Note that
  adding tertiary names does *not* add events (they hang off the same calls); it adds more
  expressions per event, which **inflates multiple-testing risk** rather than relieving the
  small-sample problem.

---

## 6. Build sequence (don't skip v0)

- **v0 — extraction quality only, no trading.** ~10 companies, 1–2 quarters, transcripts
  in by hand. Run the extractor; compare its JSON against your own reading of the same
  calls. You're validating that it parses correctly before anything downstream. If
  extraction is noisy here, nothing built on top can work.
- **v0.5 — baseline store + surprise score.** Backfill prior-quarter guidance; compute
  surprises.
- **v1 — event-study backtest** across 3–4 years on **second-order names first**, with the
  §5 baselines and out-of-sample/after-cutoff checks. Only if second-order shows real drift
  do you extend the graph one hop and test **tertiary names** as a separate bucket — never
  assume the signal survives the extra hop; prove it.
- **v2 — paper trade live** for a couple of real earnings cycles before committing
  capital. Live earnings season behaves differently from history.

---

## 7. Failure modes / when to kill it

- **Small sample → fragile stats.** Your biggest structural weakness; respect it.
- **The most-watched links aren't slow.** The edge depends on second-order names
  under-reacting; the famous suppliers don't. Any edge is in the *less-covered* links —
  which also have worse liquidity.
- **The signal can die before it reaches tertiary names.** Obscurity is not edge. A
  thinly-covered sub-supplier may be mispriced — or may simply be unmoved by your signal
  because its AI exposure is small and diluted across other end-markets. Don't confuse "no
  coverage" with "free money."
- **Reflexivity.** This is an obvious idea; others run versions of it, so decay is likely.
- **Data cost.** Good transcripts/audio and clean point-in-time prices aren't free.
- **Capability claims are cheap talk.** Treat them as conditioning, not triggers, until
  you've shown they carry incremental information over the capex surprise alone.

---

### The one-line summary
The LLM turns calls into a consistent, structured capex-surprise feed faster than you
could by hand; your domain knowledge maps it through a multi-hop graph to the right second-
and tertiary-order names (weighting for attenuation as the signal travels down the chain);
and the validation section — point-in-time data, after-cutoff out-of-sample, baselines to
beat, and honesty about the tiny sample — is what tells you whether any of it is an edge or
just a good-looking backtest.
