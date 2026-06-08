# The "Tedium Premium" — how to trade the validated edge

*The operational complement to notes/what-we-validated.md. Names + construction decided
June 2026.*

## Names
- **Indicator: "MD&A Deterioration Lead."** The forensic measurement — an LLM reads the
  year-over-year changes in a company's 10-K MD&A and scores how much the *wording* signals
  the business deteriorating. It's a *leading* indicator: it moves ~3-6 months before the
  hard distress events (bankruptcy / restatement / auditor change / default).
- **Strategy/edge: "Tedium Premium."** You get paid *because the work is tedious* — reading
  two years of dense filings side-by-side is labor almost nobody does, so the market
  underreacts and the price stays wrong long enough to act. The name states the moat: it's
  labor, not genius, and that's why it persists (for now).

## What the trade is, in one breath
A patient, long-only book of **~30-40 neglected micro-caps**, each *cheap on hard assets*
with a *real margin of safety*, *financially healthy* (high Piotroski-F, low dilution), and
— the distinctive part — with **MD&A language that is NOT quietly deteriorating**. The
indicator's job is to *kill the cheap-but-dying traps* and *tilt toward the clean*. Rebalance
quarterly off new filings; execute patiently; size by liquidity; protect the downside with
asset backing, not stops; monitor the live edge for decay.

## The ranking (what each name's score blends — all validated)
- Cheap on assets (price-to-tangible-book, NCAV) — the funnel that narrows the field.
- Quality: Piotroski F-score (strong, free: IC +0.13/t6.7).
- Low dilution (strong, free: IC +0.14/t6.5).
- Clean/improving MD&A language (the Deterioration Lead — the early-warning filter).
- No recent hard distress 8-K event (free trap filter: flagged names −5%/yr).
- Margin of safety ≥ ~40% discount to conservative asset value.
- Liquidity floor (tradeable at all).
Buy the top; AVOID/KILL anything where the language is deteriorating.

## The decisions (and why)
- **Concentration: 30-40 names (chosen), NOT 15.** The per-name edge is *modest* (IC ~+0.1).
  Grinold's Fundamental Law: realized skill ≈ IC × √(breadth). A modest edge needs breadth to
  produce a reliable return AND to survive the empirical ~1-in-5 micro-cap >50% blowup rate.
  Few enough (≤40) to still do a forensic human review on each buy; many enough that the edge
  expresses statistically. Encoded in config/policy.yaml (max_positions 35, max_weight ~6%).
- **Long-only, asset-backed.** Downside protection is STRUCTURAL, not a stop-loss (illiquid
  names gap; you can't exit). Buy below conservative asset value so assets cushion a stumble;
  the indicator removes the names whose assets are about to be eaten.
- **Patient / low turnover.** Signals refresh on filings (10-K annual, 10-Q quarterly).
  Rebalance quarterly after filing season; hold 6-18 months. The edge is "the market is slow
  to price a documented fact" — you're paid to be patient, not quick.
- **Human sign-off on every buy.** The edge is modest, so don't also eat dumb errors — a human
  reads the cited language findings and sanity-checks the asset values before buying.

## The hard constraints (these matter as much as the signal)
- **Illiquidity is where the edge lives AND why you can't scale it.** The signal is strongest
  in the most ignored, illiquid micro-caps (institutions can't trade them, analysts ignore
  them → underreaction lingers). Same illiquidity caps capacity at ~low single-digit millions
  before *you* become the price. This is a personal-capital / small-fund strategy — a feature
  of *why* the edge survives (too small for big players to arbitrage).
- **Execution:** limit orders only, accumulate over days/weeks, never market orders, never more
  than a small slice of ADV. Getting OUT is as hard as getting in — plan exits in advance.
  (The IBKR paper-trading rig exists to measure realistic fills — a first-order risk here.)

## The #1 long-term risk: the moat is eroding because of tools like this
The edge persists *because reading filings is tedious* — but LLMs (including deepvalue itself)
are making that reading cheap and instant. Assume the Tedium Premium **decays** over time.
Softeners: (1) the **illiquidity barrier is durable** even when the reading isn't — not
everyone can *trade* a $100k/day stock at size even if everyone can *read* it; (2) **forward-
monitor the live IC for decay** and harvest while it lasts. Forward paper-trading is both the
validation and the decay alarm.

## Next to operationalize
1. Deploy the v0 candidate book + fold in the Deterioration Lead → IBKR paper-trading forward.
2. Build L4/L5 (agentic forensic + adversarial layers) that act on high-deterioration names.
3. Watch the forward IC: confirm it holds out-of-sample; sound the alarm if it decays.
