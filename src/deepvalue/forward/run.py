"""
Live Tedium Premium session (forward path) — the headless weekly run that replaces parley.

Wires the three survivorship-immune live sources through the validated funnel:
  EDGAR recent filers (universe)  ->  EDGAR XBRL (fundamentals)  ->  IBKR daily bars (prices)
  ->  free funnel (Piotroski-F + low dilution + cheap-on-assets)  ->  ranked BUY/WATCH book.

The MD&A Deterioration Lead (the distinctive edge) is an OPT-IN, budget-capped step that runs
only when --max-llm-usd > 0 (CLAUDE.md spend rule: never auto-spend LLM). The session emits a
recommendation artifact + a book snapshot (entry prices) for the human and the forward monitor.
It takes NO trading action; every BUY needs human sign-off (policy). Paper execution is a
separate opt-in adapter.

    uv run python -m deepvalue.forward.run --as-of 2026-06-08            # free screen, no exec
    uv run python -m deepvalue.forward.run --days-back 7 --max-llm-usd 5 # + Deterioration Lead
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import statistics
from datetime import date, timedelta
from pathlib import Path

import yaml

from deepvalue.forward import ibkr_execution, ibkr_prices, notify
from deepvalue.forward.universe import cik_to_ticker_map, recent_filers
from deepvalue.ingest import edgar_fundamentals as ef
from deepvalue.ingest.edgar import company_sic, is_excluded_sector
from deepvalue.quant.metrics import value_metrics
from deepvalue.quant.trap_signals import trap_signals

log = logging.getLogger("tedium.forward")

ROOT = Path(__file__).resolve().parents[3]
POLICY = yaml.safe_load((ROOT / "config" / "policy.yaml").read_text())
OUT_DIR = ROOT / "data" / "forward"
RECENCY_DAYS = 550   # fundamentals must be filed within ~18 months to count as "current"
ADV_WINDOW = 60
MIN_F_CHECKS = 7     # EDGAR micro-cap XBRL is incomplete; relax the backtest's strict ==9


def _adv(prices: dict, as_of: str) -> float | None:
    ds = sorted(d for d in prices if d <= as_of)
    if len(ds) < ADV_WINDOW:
        return None
    dv = [(prices[d].get("close") or 0) * (prices[d].get("volume") or 0) for d in ds[-ADV_WINDOW:]]
    v = [x for x in dv if x > 0]
    return sum(v) / len(v) if v else None


def _z(values: list[float]) -> dict:
    if len(values) < 3:
        return {}
    return {"mu": statistics.mean(values), "sd": statistics.pstdev(values) or 1.0}


def _price_asof(prices: dict, on_or_before: str) -> float | None:
    elig = [d for d in prices if d <= on_or_before]
    return prices[max(elig)].get("close") if elig else None


def screen_one(ticker: str, cik: str, as_of: str, prices: dict,
               p_tbv_max: float, min_f: int, adv_floor: float) -> dict | None:
    """One name, point-in-time, against the funnel gates. None if not screenable/passing.
    `prices` is this ticker's {date:{close,volume}} dict (already listed-floored by IBKR)."""
    p = ef.as_of(ticker, as_of)
    if p is None or (date.fromisoformat(as_of) - date.fromisoformat(p.filing_date)).days > RECENCY_DAYS:
        return None
    price = _price_asof(prices, as_of)
    adv = _adv(prices, as_of)
    if not price or price <= 1.0 or adv is None or adv < adv_floor:
        return None
    vm = value_metrics(p, price)
    ts = trap_signals(p, ef.prior_year(ticker, p), price)
    ptbv, f, dil = vm.get("p_tbv"), ts.get("f_score"), ts.get("dilution_yoy")
    if ptbv is None or not (0.05 <= ptbv <= p_tbv_max):
        return None
    if dil is not None and not (-0.5 <= dil <= 1.0):          # share-count unit artifacts
        return None
    if (ts.get("f_checks") or 0) < MIN_F_CHECKS or f is None or f < min_f:
        return None
    if is_excluded_sector(company_sic(cik)):                  # banks/insurers/blank-check
        return None
    ptn = vm.get("price_to_ncav")
    ratios = [r for r in (ptbv, ptn) if r is not None and r > 0]
    mos = (1 - min(ratios)) if ratios else None
    return {"ticker": ticker, "cik": str(cik), "price": round(price, 2), "adv": round(adv),
            "p_tbv": ptbv, "price_to_ncav": ptn, "margin_of_safety": mos, "f_score": f,
            "f_checks": ts.get("f_checks"), "dilution": dil, "ev_ebit": vm.get("ev_ebit"),
            "z_zone": ts.get("z_zone"), "runway_months": ts.get("runway_months"),
            "market_cap": vm.get("market_cap")}


def fundamental_prefilter(universe: list[tuple[str, str]], as_of: str, min_f: int) -> list[tuple[str, str]]:
    """Narrow the universe on the FREE, price-independent signals (Piotroski-F, dilution,
    sector) using only EDGAR — so IBKR prices are fetched for the dozens of survivors, not the
    hundreds of filers (respects IBKR's ~60-historical-req/10min pacing). Point-in-time."""
    survivors: list[tuple[str, str]] = []
    for ticker, cik in universe:
        p = ef.as_of(ticker, as_of)
        if p is None or (date.fromisoformat(as_of) - date.fromisoformat(p.filing_date)).days > RECENCY_DAYS:
            continue
        ts = trap_signals(p, ef.prior_year(ticker, p), 1.0)  # price=1.0: F & dilution ignore it
        f, checks, dil = ts.get("f_score"), ts.get("f_checks"), ts.get("dilution_yoy")
        if (checks or 0) < MIN_F_CHECKS or f is None or f < min_f:
            continue
        if dil is not None and not (-0.5 <= dil <= 1.0):
            continue
        if is_excluded_sector(company_sic(cik)):
            continue
        survivors.append((ticker, cik))
    return survivors


def build_book(universe: list[tuple[str, str]], prices_by_ticker: dict[str, dict], as_of: str,
               max_positions: int, p_tbv_max: float = 2.0, min_f: int = 5,
               adv_floor: float = 50000) -> tuple[list[dict], list[dict]]:
    """Screen the universe, composite-rank the validated free signals (high F, low dilution,
    cheap on book), return (all_candidates, book=top max_positions with verdicts)."""
    cands = [c for ticker, cik in universe
             if (c := screen_one(ticker, cik, as_of, prices_by_ticker.get(ticker, {}),
                                 p_tbv_max, min_f, adv_floor)) is not None]
    if not cands:
        return [], []
    fz = _z([c["f_score"] for c in cands])
    dz = _z([c["dilution"] for c in cands if c["dilution"] is not None])
    pz = _z([c["p_tbv"] for c in cands])
    for c in cands:
        comp = (c["f_score"] - fz["mu"]) / fz["sd"] - (c["p_tbv"] - pz["mu"]) / pz["sd"]
        if c["dilution"] is not None and dz:
            comp += -(c["dilution"] - dz["mu"]) / dz["sd"]
        c["composite"] = round(comp, 3)
    cands.sort(key=lambda c: c["composite"], reverse=True)

    min_mos = POLICY.get("min_margin_of_safety", 0.40)
    book = cands[:max_positions]
    for i, c in enumerate(book, 1):
        c["rank"] = i
        c["flags"] = [f for f, on in (
            ("DISTRESS", c["z_zone"] == "distress"),
            ("LOW_RUNWAY", (c["runway_months"] or 999) < 12),
            ("DILUTING", (c["dilution"] or 0) > 0.05)) if on]
        buy = c["f_score"] >= 7 and (c["margin_of_safety"] or 0) >= min_mos and not c["flags"]
        c["verdict"] = "BUY" if buy else "WATCH"
    return cands, book


def _write_artifacts(as_of: str, n_universe: int, cands: list[dict], book: list[dict]) -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    (OUT_DIR / f"book_{as_of}.json").write_text(json.dumps(
        {"as_of": as_of, "universe": n_universe, "candidates": len(cands), "book": book}, indent=2))
    n_buy = sum(1 for c in book if c["verdict"] == "BUY")
    lines = [f"# Tedium Premium — candidate book @ {as_of}", "",
             f"Live universe (recent EDGAR filers, listed, screenable): {len(cands)} · "
             f"book: {len(book)} · BUY: {n_buy}", "",
             "Recommendation artifact — NO trading action taken. Every BUY needs human sign-off.", "",
             "| # | Ticker | Verdict | Price | P/TBV | MoS | F | Dil | Flags |",
             "|---|--------|---------|------:|------:|----:|--:|----:|-------|"]
    for c in book:
        mos = f"{c['margin_of_safety']:.0%}" if c["margin_of_safety"] is not None else "—"
        dil = f"{c['dilution']:+.1%}" if c["dilution"] is not None else "—"
        lines.append(f"| {c['rank']} | {c['ticker']} | {c['verdict']} | {c['price']:.2f} | "
                     f"{c['p_tbv']:.2f} | {mos} | {c['f_score']} | {dil} | {','.join(c['flags']) or '—'} |")
    (OUT_DIR / f"recommendation_{as_of}.md").write_text("\n".join(lines) + "\n")


async def _session(as_of: str, days_back: int, max_positions: int, p_tbv_max: float,
                   min_f: int, max_llm_usd: float, execute: str, transmit: bool,
                   max_universe: int = 0) -> str:
    """One forward session. Returns a one-line digest. Raises on hard failure (the caller
    records an error heartbeat + alerts)."""
    adv_floor = POLICY.get("liquidity_floor_adv_usd", 50000)
    # 1) live universe: who filed a 10-K/10-Q recently, resolved to a current ticker
    c2t = cik_to_ticker_map()
    since = (date.fromisoformat(as_of) - timedelta(days=days_back)).isoformat()
    filers = recent_filers(since, as_of)
    universe = sorted({(c2t[f.cik], f.cik) for f in filers if f.cik in c2t})
    log.info("universe: %d recent filings -> %d priceable tickers", len(filers), len(universe))
    if not universe:
        log.warning("empty universe; nothing to do")
        return f"{as_of}: empty universe (no recent filers resolved)"

    # 2) FUNDAMENTALS-FIRST: narrow on the free EDGAR signals before spending IBKR price
    #    requests (pacing). Optional cap applies to the survivor set actually priced.
    survivors = fundamental_prefilter(universe, as_of, min_f)
    if max_universe and len(survivors) > max_universe:
        survivors = survivors[:max_universe]
    log.info("fundamental pre-filter: %d universe -> %d survivors to price", len(universe), len(survivors))
    if not survivors:
        _write_artifacts(as_of, len(universe), [], [])
        return f"{as_of}: {len(universe)} universe, 0 passed fundamentals"

    # 3) current prices from IBKR (listed-floored), paper-guarded — connection stays open
    #    through any execution, then closes.
    ib = await ibkr_prices.connect()
    try:
        acct = ibkr_prices.assert_paper_ready(ib)
        prices = await ibkr_prices.fetch_prices_for(ib, [t for t, _ in survivors], lookback_days=400)

        # 4) funnel -> ranked book (price-dependent gates + composite)
        cands, book = build_book(survivors, prices, as_of, max_positions, p_tbv_max, min_f, adv_floor)
        log.info("screened: %d candidates -> book of %d", len(cands), len(book))

        # 5) MD&A Deterioration Lead — OPT-IN, budget-capped (the spend rule)
        if max_llm_usd > 0 and book:
            log.warning("Deterioration Lead requested ($%.0f cap) — not yet wired; screen-only "
                        "book emitted. (next: current+prior 10-K MD&A -> diff -> materiality)",
                        max_llm_usd)

        _write_artifacts(as_of, len(universe), cands, book)
        n_buy = sum(1 for c in book if c["verdict"] == "BUY")
        digest = (f"{as_of}: {len(cands)} candidates, book {len(book)}, {n_buy} BUY "
                  f"(acct {acct})")

        # 6) paper rebalance toward the BUY book — preview unless --transmit (opt-in)
        if execute == "ibkr":
            buys = [c["ticker"] for c in book if c["verdict"] == "BUY"]
            last_close = {t: px[max(px)]["close"] for t, px in prices.items() if px}
            rb = await ibkr_execution.rebalance(
                ib, buys, last_close, max_positions=max_positions,
                max_weight=POLICY.get("max_position_weight", 0.06), transmit=transmit)
            mode = "TRANSMITTED" if transmit else "PREVIEW"
            log.info("rebalance (%s): %d orders planned", mode, len(rb["plans"]))
            digest += f" | rebalance {mode}: {len(rb['plans'])} orders"
    finally:
        ib.disconnect()

    log.info("wrote artifacts to %s (book_%s.json, recommendation_%s.md)", OUT_DIR, as_of, as_of)
    return digest


def _healthcheck(max_age_hours: float = 192.0) -> int:
    """Watchdog (daily cron): alert + nonzero exit if the weekly clock has gone quiet."""
    hb = notify.read_heartbeat()
    if notify.heartbeat_stale(hb, max_age_hours):
        notify.notify("⚠️ Tedium Premium forward: STALE",
                      f"last heartbeat: {hb}" if hb else "no heartbeat on record")
        return 1
    log.info("healthcheck OK: %s", hb)
    return 0


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    ap = argparse.ArgumentParser(description="Live Tedium Premium session (forward path)")
    ap.add_argument("--as-of", default=date.today().isoformat())
    ap.add_argument("--days-back", type=int, default=7, help="EDGAR filer window to scan")
    ap.add_argument("--max-positions", type=int, default=POLICY.get("max_positions", 35))
    ap.add_argument("--p-tbv-max", type=float, default=2.0)
    ap.add_argument("--min-f", type=int, default=5)
    ap.add_argument("--max-llm-usd", type=float, default=0.0,
                    help="enable the Deterioration Lead with this hard cap (0 = free screen only)")
    ap.add_argument("--execute", choices=["none", "ibkr"], default="none",
                    help="'ibkr' rebalances the paper account toward the BUY book")
    ap.add_argument("--transmit", action="store_true",
                    help="actually send orders (default previews); requires --execute ibkr")
    ap.add_argument("--healthcheck", action="store_true", help="watchdog mode: alert if stale, exit 1")
    ap.add_argument("--max-universe", type=int, default=0,
                    help="cap the universe (0 = no cap); respects IBKR historical-data pacing")
    args = ap.parse_args()

    if args.healthcheck:
        raise SystemExit(_healthcheck())

    try:
        digest = asyncio.run(_session(args.as_of, args.days_back, args.max_positions,
                                      args.p_tbv_max, args.min_f, args.max_llm_usd,
                                      args.execute, args.transmit, args.max_universe))
        notify.write_heartbeat("ok", args.as_of, digest)
        notify.notify(f"✅ Tedium Premium forward OK ({args.as_of})", digest)
    except Exception as e:  # noqa: BLE001 — record the failure loudly, then re-raise
        log.exception("forward session FAILED")
        notify.write_heartbeat("error", args.as_of, f"{type(e).__name__}: {e}")
        notify.notify(f"❌ Tedium Premium forward FAILED ({args.as_of})", f"{type(e).__name__}: {e}")
        raise


if __name__ == "__main__":
    main()
