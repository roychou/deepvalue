# What deepvalue set out to validate — and what it actually validated

*A plain-language record of the Phase-1/2 validation (June 2026). Written to be readable by
a non-specialist. Numbers are survivorship-free, point-in-time, mostly over ~28 annual
cohorts (1999–2026 via Sharadar). Total cost of the validation: ~$54 of LLM usage + one
month of a Sharadar data subscription.*

---

## Part 1 — The premise (what we're betting on)

Every US public company files a giant annual report with the SEC (the **10-K**). Inside is
the **MD&A** — "Management's Discussion and Analysis" — prose where management describes how
the business is doing. Most of it is copy-pasted from last year. But sometimes management
**quietly changes the wording** — deletes a sentence that used to say "our largest customer
renewed," or softens "we are comfortably in compliance with our loan covenants."

**The bet:** those quiet wording changes are meaningful, and the market **underreacts** to
them — because almost nobody reads two years of dense filings side-by-side. The edge persists
*because the work of catching it is tedious* (academic basis: "Lazy Prices," Cohen-Malloy-
Nguyen). It's labor, not a price pattern, so it doesn't get arbitraged away easily.

**The crucial design choice — LLM as READER, not JUDGE:**
- *LLM-as-judge* (the disproven predecessor "parley"): ask the AI "is this a good stock?" →
  worthless opinion, and contaminated by the AI's memory of outcomes.
- *LLM-as-reader* (deepvalue): give the AI **only the sentences that changed** and ask "does
  this wording describe the business getting worse?" That's reading comprehension, not
  prediction. The signal is the documented anomaly; the AI is just the tireless reader.

**Two non-negotiable honesty rules:**
- **Survivorship-free universe.** Test on the ~20,000 *dead/delisted* companies too, not just
  survivors. For a strategy that exists to avoid companies that die, the dead ones are the
  data. (Analogy: don't judge skydiving safety by only interviewing people who landed fine.)
- **Point-in-time discipline.** A 10-K covers fiscal-2020 but isn't *filed* until ~March 2021.
  Only use it from its filing date — never peek at data before it existed.

**The yardstick — Information Coefficient (IC).** A correlation (−1 to +1) between how a
signal ranks stocks and how they actually perform. 0 = useless. **+0.05 to +0.15 is genuinely
valuable** across many names. The **t-statistic** says how sure we are it isn't luck (t > 2 ≈
"statistically significant").

---

## Part 2 — The questions we set out to answer

0. Can we even build an honest, survivorship-free, point-in-time test-bed?
1. Does the cheap quant screen (L1) predict returns?
2. Does the language-change signal (L3) predict returns at all?
3. Is it **real reading**, or is the AI **cheating** by remembering outcomes? (contamination)
4. Does the edge live where the thesis says — in **neglected, illiquid micro-caps**?
5. Does the expensive AI signal **add anything beyond the free quant signals**?
6. Does the language change **LEAD the hard bad-news events** (early warning)? — the founding claim.

---

## Part 3 — What we found, point by point

**0. The test-bed is buildable — and doing it sloppily produces illusions.** FMP's free-ish
data only covered dead companies back to ~2016 (~10 clean years). We later bought one month of
**Sharadar** (genuinely survivorship-free to 1998) for ~28 years. Doing this right mattered —
see point 5.

**1. The cheap quant screen — quality is a strong FREE edge; cheapness alone is a trap.**
- Piotroski **F-score** (0–9 fundamental-health checklist): **IC +0.13, t≈6.7.**
- **Dilution** (printing new shares): **IC +0.14, t≈6.5** (issuers underperform).
- **Pure cheapness** (low P/E, low price-to-book, net-net): **weak** (~+0.04–0.07). Cheap is
  often cheap *for a reason*. → The cheap screen is a *funnel to narrow the field*, not the edge.

**2. The language signal — it's the KIND of change, not the AMOUNT.**
- Dumb version (just % of sentences changed): **weak** (~+0.03–0.06).
- AI-reader version (score *which* changes signal deterioration): **IC +0.12, t≈3.** A computer
  counting sentences misses it; a reader judging *meaning* catches it.

**3. It's real reading, not cheating — the decisive contamination test.** Risk: the AI was
trained through early 2026; scoring a 2015 filing from a company that later went bankrupt, it
might score "bad" from *memory*, not from *reading*. That fakery is what killed the predecessor.
Test: **anonymize** the filings (strip company name/ticker/entities → "the Company") and
re-score. If the AI were recognizing companies, hiding identity would gut the signal. **Result:
IC barely moved (+0.135 → +0.127).** It *survived* → the signal is genuine reading of the risk
language, not recalled outcomes. (This is the result that separates deepvalue from the
disproven approach. Caveat: anonymization is imperfect, but removing the obvious handles barely
dented it.)

**4. The edge lives in the neglected illiquid names — exactly as claimed.** Split by liquidity:
deterioration signal **strongest in the illiquid bucket** (IC +0.13, t≈3.5), **~zero in the
liquid bucket.** (The dumb version did *not* show this — so it's specifically the AI-reader
signal that lives in the ignored corner.)

**5. Does it add over the FREE signals? — nuanced.** Inside the cheap bucket, controlling for
the free F-score, the AI signal showed *no clear improvement* on the (underpowered) 10-cohort
data — a sobering moment. Separately, we found a **free deterioration signal**: Sharadar tags
"hard" bad-news 8-K events (bankruptcy, restatement, auditor resignation, default); cheap names
with a recent such event **underperform by ~5%/yr (t≈3.8) — for free.** This reframed the real
question to point 6.

**6. Does the LANGUAGE lead the HARD event? — the founding bet, and it HOLDS.** Hard events are
*lagging* (by the time a bankruptcy is filed, the stock has already cratered). The bet is the
language softens *first*. Test: among names with **no hard event in the past year** (nothing
officially wrong yet), does the AI's language score *still* predict underperformance?
- **Yes — IC +0.113 (t≈2.2) at 3 months, +0.141 (t≈2.6) at 6 months**, among event-clean names.
- And it was **stronger** among event-clean names than across all names — the textbook
  signature of a **leading indicator** (it sees trouble before any hard signal exists).
- Economic size: clean-language cheap names beat deteriorating-language cheap names by
  **~9–13%/yr.**
- Honest nuance: significance is **real but modest** (t≈2.2–2.6); at 12 months the edge fades
  and the hard-event signal takes over; the AI's *magnitude* is comparable to the free signals.
  So the AI reader is **additive and EARLIER**, not a magic bullet.

---

## Part 4 — The honest bottom line

We took an untested thesis and established, rigorously, that its central claims are
**true-but-modest**:
- ✅ Honest survivorship-free / point-in-time test-bed (and sloppiness creates illusions —
  the "value premium" evaporated once dead companies were included).
- ✅ Quality + low-dilution = strong, durable, **free** edges (t≈6.5–6.7) — the funnel's backbone.
- ✅ The AI-reader language signal is **real** (IC +0.12) and **genuine reading, not memorized
  outcomes** (survived anonymization) — the result that distinguishes deepvalue from the
  disproven LLM-as-judge approach.
- ✅ The edge **concentrates in neglected illiquid micro-caps**, as the thesis claims.
- ✅ The founding **leading-indicator** bet holds: language **precedes** the hard event,
  significantly, at the 3–6 month horizon.

**Qualifications, stated plainly:** the AI signal is **additive and early, not dominant**;
significance is **solid but modest** (t≈2–2.6). This is concentrated deep value — you will
*never* get overwhelming statistical certainty (too few concentrated bets). The case for
deploying rests on three legs together: the significant *component* evidence, the published
economic reason the anomaly *persists*, and forward paper-trading to confirm it keeps working.

**Verdict:** worth building and deploying — with eyes open about the modest magnitude.
