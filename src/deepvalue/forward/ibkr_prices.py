"""
IBKR daily-bar price adapter for the live Tedium Premium session (ported from parley's
src/forward/ibkr.py, which proved this against a real Gateway).

The patient strategy needs daily closes, not real-time quotes — so prices come from
`reqHistoricalData` daily bars (works after-hours, no live market-data subscription needed;
the coverage spike confirmed this for every listed micro-cap). Returns the SAME shape as
ingest/prices.get_prices: {date: {open,high,low,close,volume}}, so the funnel's price
consumers work unchanged.

Two hardenings the spike taught us:
  - LISTED-only floor: OTC/PINK names resolve a contract but have no clean trade history and
    HANG reqHistoricalData -> skip anything whose primaryExchange isn't a real exchange.
  - per-ticker timeout: never let one illiquid name stall the whole weekly run.

Thin IO over ib_async — must be validated live against a running Gateway (can't run in CI).
"""
from __future__ import annotations

import asyncio
import logging
import math
import os

from ib_async import IB, Stock

logger = logging.getLogger(__name__)

# IB Gateway paper = 4002 (TWS paper = 7497); the deploy compose points these at the
# gateway container (gateway:4004). Env-overridable so laptop/VPS is a config change.
DEFAULT_HOST = os.getenv("IBKR_HOST", "127.0.0.1")
DEFAULT_PORT = int(os.getenv("IBKR_PORT", "4002"))
DEFAULT_CLIENT_ID = int(os.getenv("IBKR_CLIENT_ID", "17"))

PAPER_ACCT_PREFIX = "DU"  # paper accounts; live individual accounts start 'U'
# Real exchanges IBKR prices cleanly. PINK/OTC resolve a contract but hang historical data.
LISTED = {"NASDAQ", "NYSE", "AMEX", "ARCA", "BATS", "ISLAND", "NYSENAT"}


class GatewayNotReadyError(RuntimeError):
    """Reachable Gateway that isn't in a usable paper-trading state."""


async def connect(host: str = DEFAULT_HOST, port: int = DEFAULT_PORT,
                  client_id: int = DEFAULT_CLIENT_ID) -> IB:
    """Connect to a running IB Gateway/TWS. Caller disconnects."""
    ib = IB()
    await ib.connectAsync(host, port, clientId=client_id)
    logger.info("connected to IBKR at %s:%s (clientId=%s)", host, port, client_id)
    return ib


def assert_paper_ready(ib: IB) -> str:
    """Preflight: confirm a PAPER account is connected, so we fail fast (before any LLM
    spend) if the Gateway is logged out, half-initialized, or — critically — live."""
    accts = [a for a in ib.managedAccounts() if a]
    if not accts:
        raise GatewayNotReadyError("Gateway connected but reports no managed account "
                                   "(still logging in, or IBC session not ready)")
    for a in accts:
        if not a.startswith(PAPER_ACCT_PREFIX):
            raise GatewayNotReadyError(f"refusing to run: account {a!r} is not paper "
                                       f"(paper ids start {PAPER_ACCT_PREFIX!r})")
    logger.info("preflight OK: paper account %s ready", accts[0])
    return accts[0]


def _bars_to_price_dict(bars) -> dict[str, dict]:
    """ib_async BarData list -> {date: {open,high,low,close,volume}} (get_prices shape)."""
    out: dict[str, dict] = {}
    for b in bars:
        d = b.date.isoformat() if hasattr(b.date, "isoformat") else str(b.date)[:10]
        out[d] = {
            "open": round(float(b.open), 2), "high": round(float(b.high), 2),
            "low": round(float(b.low), 2), "close": round(float(b.close), 2),
            "volume": int(b.volume) if b.volume and b.volume > 0 else 0,
        }
    return out


def _duration_str(lookback_days: int) -> str:
    """IBKR rejects day-durations over 365 ('must be made in years')."""
    return f"{math.ceil(lookback_days / 365)} Y" if lookback_days > 365 else f"{lookback_days} D"


async def fetch_daily_bars(ib: IB, ticker: str, lookback_days: int = 400,
                           timeout: float = 15.0) -> dict[str, dict]:
    """Daily TRADES bars for the trailing window, as our price-dict. Returns {} (skips)
    for an unresolvable contract, a non-listed (OTC/PINK) primaryExchange, or a timeout
    — so one stuck illiquid name never stalls the weekly run."""
    try:
        contract = Stock(ticker, "SMART", "USD")
        details = await asyncio.wait_for(ib.reqContractDetailsAsync(contract), timeout=timeout)
        if not details:
            return {}
        primex = details[0].contract.primaryExchange or ""
        if primex not in LISTED:
            logger.info("skip %s: primaryExchange %r not a listed exchange", ticker, primex)
            return {}
        bars = await asyncio.wait_for(
            ib.reqHistoricalDataAsync(details[0].contract, endDateTime="",
                                      durationStr=_duration_str(lookback_days),
                                      barSizeSetting="1 day", whatToShow="TRADES",
                                      useRTH=True),
            timeout=timeout)
        return _bars_to_price_dict(bars or [])
    except (asyncio.TimeoutError, Exception) as e:  # noqa: BLE001 — one bad name can't kill the run
        logger.warning("price fetch failed for %s: %s", ticker, type(e).__name__)
        return {}


async def fetch_prices_for(ib: IB, tickers: list[str], lookback_days: int = 400
                           ) -> dict[str, dict[str, dict]]:
    """Daily bars for a universe of tickers, sequentially (one Gateway connection).
    Returns {ticker: price_dict} for the names that priced; non-listed / failed names are
    dropped (which is correct — an un-priceable name isn't tradeable)."""
    out: dict[str, dict[str, dict]] = {}
    for t in tickers:
        px = await fetch_daily_bars(ib, t, lookback_days)
        if px:
            out[t] = px
    logger.info("priced %d/%d names from IBKR", len(out), len(tickers))
    return out
