# Deploying the Tedium Premium — reuse parley's VPS stack

*Decided June 2026. Replaces parley on the Ionos VPS. Local-first build, VPS last.*

## The key insight: no paid data vendor for the LIVE system
Survivorship bias only bites *backtests* (you can only trade what exists now). So the live
forward system needs only what parley already runs on, both ~free:
- **Current prices** ← IBKR (the same paper connection; ~$10/mo market data).
- **Fundamentals + 10-K text** ← free SEC EDGAR (XBRL company-facts + filing prose).
=> Cancel Sharadar; let FMP lapse. They were validation-only costs.

## Reuse from parley (`../parley`)
- `deploy/` Docker Compose stack: `gateway` (IB Gateway+IBC headless via Xvfb) + `app`
  (Python 3.12/uv + supercronic cron). Restart policies, healthcheck, named volumes.
- `src/forward/ibkr.py` — IBKR connect + price/news pull, **paper-account guard** (refuses
  non-`DU` accounts). Highest-value reuse.
- `src/forward/ibkr_execution.py` — account_state + broker_rebalance (market orders, whole
  shares, transmit=False preview default, re-checks paper before every send).
- `src/forward/notify.py` — email + Telegram + heartbeat watchdog. Reuse directly.
- `src/data/edgar.py` — XBRL company-facts fundamentals (free). Adapt to our fundamentals_store.
- supercronic crontab format; Doppler secrets (runtime-injected, only a token on disk).
- `--max-llm-usd` hard-cap budget meter pattern (matches our spend rule).

## The decisions (locked)
- **Fresh IBKR paper account** for the Tedium Premium track record (clean out-of-sample
  evidence; parley's old book archived separately, not commingled).
- **Cadence: weekly scan + quarterly rebalance.** Weekly: screen the week's new EDGAR
  10-K/10-Q filers, update the candidate book + MD&A Deterioration Lead, alert on changes.
  Rebalance the actual book quarterly. Matches the filing-driven, patient edge.
- **Local-first, VPS-last.** Build + test on the laptop against IBKR paper; deploy (retiring
  parley) only once it works.
- Rename to tediumpremium: HELD for now (keep deepvalue naming).

## The gap to BUILD (deepvalue-specific)
parley's universe = Nasdaq-100 (tiny, liquid, known). Ours = thousands of neglected micro-caps,
historically from Sharadar/FMP (retiring). For LIVE, the universe is **EDGAR itself**: each
week, the companies that just filed a 10-K/10-Q -> screen those. This is the *right* trigger
for a filing-driven signal, and free.
- NEW: `forward/universe.py` — recent-EDGAR-filers loader (full-index/form.idx firehose or
  full-text search), filtered to domestic common-stock 10-K/10-Q filers, sector-excluded.
- NEW: `forward/run.py` — parley-style weekly session: universe -> current prices (IBKR) ->
  funnel (Piotroski/dilution/cheap-on-book from EDGAR XBRL) + Deterioration Lead (budget-capped
  LLM on the changed MD&A) -> candidate book -> optional IBKR paper exec (preview default) ->
  heartbeat + notify.
- PORT: parley `ibkr.py` / `ibkr_execution.py` / `notify.py` into `forward/`.
- WIRE: existing `scripts/run_funnel.py` + `diff/materiality.py` into the session.

## The RISK to test first (IBKR micro-cap coverage spike)
parley traded liquid large-caps IBKR covers perfectly. We trade illiquid micro-caps/OTC —
IBKR may not provide market data or paper execution for the most illiquid names. Before
building much: take a sample of the micro-caps the funnel actually surfaces and confirm IBKR
can (a) return prices and (b) accept paper orders. If many are uncoverable, that sets the
universe's liquidity floor. NEEDS: IBKR paper creds + a running IB Gateway.

## Build sequence
1. **IBKR coverage spike** (needs IBKR paper creds/gateway) — cheap, decisive.
2. `forward/universe.py` — recent-filers loader (creds-free; build + test now).
3. `forward/run.py` session + port ibkr/notify; wire funnel + Deterioration Lead.
4. Run locally vs IBKR paper a cycle or two; verify book + fills.
5. Containerize (copy parley `deploy/`), new Doppler config, retire parley (stop + archive
   its data/forward, don't delete), bring deepvalue up, verify heartbeat/notify.

## Needed from operator
- IBKR: a fresh paper account + creds (TWS_USERID/PASSWORD), gateway reachable, for the spike
  and forward execution.
- Later (VPS): Doppler project/config for deepvalue; VPS access to retire parley + deploy.
