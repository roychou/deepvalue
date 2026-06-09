"""
IBKR paper-account execution adapter for the Tedium Premium forward rig (ported from parley;
its risk-layer/Action machinery dropped for a simple policy-weighted rebalance).

The ONLY code here that can transmit orders. Safety, layered:
- _assert_paper(): every managed account id must be paper ('DU'); refuses live accounts,
  re-checked immediately before sending.
- transmit defaults to False everywhere — plans are PREVIEWED (logged), nothing is sent.
  Real placement needs transmit=True AND IB Gateway's "Read-Only API" turned OFF.
- Market orders only, whole shares. BUYs sized from policy weight x REAL account equity;
  SELLs close exactly the shares held. Long-only (no shorts).

Rebalance target = the BUY names from the session book, equal-weighted up to the policy's
per-name cap. WATCH names are not held (they await human conviction / the next filing).
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass

from ib_async import IB, MarketOrder, Stock

logger = logging.getLogger(__name__)

PAPER_ACCT_PREFIX = "DU"


class NotPaperAccountError(RuntimeError):
    """The connected IBKR account is not a paper account."""


def _assert_paper(ib: IB) -> str:
    accts = ib.managedAccounts()
    if not accts:
        raise NotPaperAccountError("no managed accounts on the IBKR connection")
    for a in accts:
        if not a.startswith(PAPER_ACCT_PREFIX):
            raise NotPaperAccountError(f"refusing to trade: account {a!r} is not paper "
                                       f"(paper ids start {PAPER_ACCT_PREFIX!r})")
    return accts[0]


@dataclass(frozen=True)
class AccountState:
    account: str
    equity: float                 # NetLiquidation
    cash: float                   # TotalCashValue
    positions: dict[str, float]   # ticker -> shares held
    avg_cost: dict[str, float]    # ticker -> avg cost basis


def account_state(ib: IB) -> AccountState:
    """Read the (paper) account: equity, cash, current share positions."""
    acct = _assert_paper(ib)

    def _f(tag: str) -> float:
        for v in ib.accountValues():
            if v.tag == tag and v.account in (acct, ""):
                try:
                    return float(v.value)
                except ValueError:
                    return float("nan")
        return float("nan")

    held = [p for p in ib.positions() if p.position]
    return AccountState(account=acct, equity=_f("NetLiquidation"), cash=_f("TotalCashValue"),
                        positions={p.contract.symbol: float(p.position) for p in held},
                        avg_cost={p.contract.symbol: float(p.avgCost) for p in held})


@dataclass(frozen=True)
class OrderPlan:
    ticker: str
    side: str       # "BUY" | "SELL"
    quantity: int
    reason: str


def plan_rebalance(state: AccountState, target_weights: dict[str, float],
                   prices: dict[str, float]) -> list[OrderPlan]:
    """Whole-share market-order plans to move the paper account toward `target_weights`
    (ticker -> fraction of equity). SELLs names held but no longer targeted (and trims
    over-weight); BUYs/tops-up targeted names. Pure — no IB calls. Long-only."""
    plans: list[OrderPlan] = []
    equity = state.equity
    # exits / trims: anything held that's not targeted, or over its target share count
    for t, shares in state.positions.items():
        tgt_shares = 0
        if t in target_weights and prices.get(t, 0) > 0:
            tgt_shares = int((equity * target_weights[t]) // prices[t])
        if shares > tgt_shares:
            plans.append(OrderPlan(t, "SELL", int(shares - tgt_shares),
                                   "exit" if t not in target_weights else "trim"))
    # entries / top-ups
    for t, w in target_weights.items():
        price = prices.get(t)
        if not price or price <= 0 or not 0.0 < w <= 1.0:
            if not price:
                logger.warning("skip BUY %s: no usable price", t)
            continue
        tgt_shares = int((equity * w) // price)
        delta = tgt_shares - int(state.positions.get(t, 0))
        if delta > 0 and delta * price <= equity:
            plans.append(OrderPlan(t, "BUY", delta, "open" if t not in state.positions else "add"))
    return plans


async def _await_done(trade, timeout: float) -> None:
    async def _loop() -> None:
        while not trade.isDone():
            await asyncio.sleep(0.3)
    await asyncio.wait_for(_loop(), timeout=timeout)


async def execute_orders(ib: IB, plans: list[OrderPlan], *, transmit: bool = False,
                         fill_timeout: float = 30.0) -> list[dict]:
    """Place (transmit=True) or PREVIEW (default) the plans against the paper account.
    Paper guard re-checked before any send. Explicit DAY TIF avoids IBKR preset warning 10349."""
    if transmit:
        _assert_paper(ib)
    results: list[dict] = []
    for p in plans:
        if not transmit:
            logger.info("[PREVIEW] %s %d %s (%s)", p.side, p.quantity, p.ticker, p.reason)
            results.append({"ticker": p.ticker, "side": p.side, "qty": p.quantity,
                            "status": "preview", "reason": p.reason})
            continue
        contract = Stock(p.ticker, "SMART", "USD")
        await ib.qualifyContractsAsync(contract)
        order = MarketOrder(p.side, p.quantity)
        order.tif = "DAY"
        trade = ib.placeOrder(contract, order)
        try:
            await _await_done(trade, fill_timeout)
        except TimeoutError:
            logger.warning("order %s %d %s: not done in %ss", p.side, p.quantity, p.ticker, fill_timeout)
        st = trade.orderStatus
        fill = st.avgFillPrice if st.filled else None
        logger.info("%s %d %s: %s filled=%s @ %s", p.side, p.quantity, p.ticker, st.status, st.filled, fill)
        results.append({"ticker": p.ticker, "side": p.side, "qty": p.quantity,
                        "status": st.status, "filled": st.filled, "avg_fill": fill})
    return results


def conviction_weights(buys: list[dict], *, kelly_fraction: float, max_weight: float) -> dict[str, float]:
    """Fractional-Kelly-STYLE, conviction-weighted, per-name-capped, no-leverage target weights
    (policy.yaml: sizing=fractional_kelly). Conviction proxy = margin of safety (the asset cushion
    — bigger discount to conservative asset value = bigger edge), so weight_i = min(max_weight,
    kelly_fraction * mos_i); gross is scaled to <=1 (long-only, no leverage) and the remainder
    stays CASH (Kelly: bet less when the edge is thin). NOTE: a true edge/variance Kelly awaits L7
    calibration; until then margin of safety is the honest, asset-backed stand-in."""
    w: dict[str, float] = {}
    for c in buys:
        mos = c.get("margin_of_safety") or 0.0
        conviction = max(0.0, min(1.0, mos))
        w[c["ticker"]] = min(max_weight, kelly_fraction * conviction)
    gross = sum(w.values())
    if gross > 1.0:  # never lever; scale the book down to fully-invested
        w = {t: x / gross for t, x in w.items()}
    return w


async def rebalance(ib: IB, buys: list[dict], prices: dict[str, float], *,
                    kelly_fraction: float, max_weight: float, transmit: bool = False) -> dict:
    """Read the paper account and move it toward the BUY book using conviction-weighted
    fractional-Kelly sizing (cash residual). `buys` are the BUY candidate dicts (need
    margin_of_safety). Preview by default. Returns account + weights + plan + results."""
    state = account_state(ib)
    target_weights = conviction_weights(buys, kelly_fraction=kelly_fraction, max_weight=max_weight)
    plans = plan_rebalance(state, target_weights, prices)
    results = await execute_orders(ib, plans, transmit=transmit)
    return {"account": state.account, "equity": state.equity, "transmit": transmit,
            "n_targets": len(buys), "weights": {t: round(x, 4) for t, x in target_weights.items()},
            "plans": [(p.side, p.quantity, p.ticker) for p in plans], "results": results}
