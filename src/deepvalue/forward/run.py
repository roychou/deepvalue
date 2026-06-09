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
DET_KILL = 0.5       # MD&A deterioration >= this flags DETERIORATING + blocks BUY (L3 lead)


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
        if fz and pz:  # enough names (>=3) to z-score the cross-section
            comp = (c["f_score"] - fz["mu"]) / fz["sd"] - (c["p_tbv"] - pz["mu"]) / pz["sd"]
            if c["dilution"] is not None and dz:
                comp += -(c["dilution"] - dz["mu"]) / dz["sd"]
        else:  # tiny candidate set — rank by quality then cheapness (no cross-section to scale)
            comp = c["f_score"] - c["p_tbv"]
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
             "| # | Ticker | Verdict | Price | P/TBV | MoS | F | Dil | Det | Flags |",
             "|---|--------|---------|------:|------:|----:|--:|----:|----:|-------|"]
    for c in book:
        mos = f"{c['margin_of_safety']:.0%}" if c["margin_of_safety"] is not None else "—"
        dil = f"{c['dilution']:+.1%}" if c["dilution"] is not None else "—"
        det = f"{c['deterioration']:.2f}" if c.get("deterioration") is not None else "—"
        lines.append(f"| {c['rank']} | {c['ticker']} | {c['verdict']} | {c['price']:.2f} | "
                     f"{c['p_tbv']:.2f} | {mos} | {c['f_score']} | {dil} | {det} | "
                     f"{','.join(c['flags']) or '—'} |")
    (OUT_DIR / f"recommendation_{as_of}.md").write_text("\n".join(lines) + "\n")


def _candidate_dossier(c: dict) -> str:
    """A compact L1+L3 dossier for the L4/L5 agents — why this name is a BUY candidate."""
    mos = f"{c['margin_of_safety']:.0%}" if c.get("margin_of_safety") is not None else "n/a"
    ptbv = f"{c['p_tbv']:.2f}" if c.get("p_tbv") is not None else "n/a"
    return (f"{c['ticker']} — BUY candidate. Price ${c.get('price')}, P/TBV {ptbv}, margin of "
            f"safety {mos} (discount to conservative asset value), Piotroski-F {c.get('f_score')}, "
            f"dilution {c.get('dilution')}, EV/EBIT {c.get('ev_ebit')}, MD&A deterioration "
            f"{c.get('deterioration')}, flags {c.get('flags')}. It passed L1 (cheap on assets + "
            f"healthy) and L3 (language not deteriorating). Verify the asset base is REAL and "
            f"recoverable, and find any value-trap risk before a human commits.")


def _why(c: dict) -> str:
    """One-line, plain-language reason for a name's verdict — the 'chain of thought'."""
    mos, det, flags = c.get("margin_of_safety"), c.get("deterioration"), c.get("flags", [])
    if c["verdict"] == "BUY":
        bits = [f"cheap (MoS {mos:.0%})" if mos is not None else "asset-backed", f"F{c['f_score']}",
                f"clean language (L3 {det})" if det is not None else "clean"]
        if c.get("l5_decision"):
            bits.append(f"L5 cleared (conv {c.get('l5_conviction')})")
        return "BUY — " + ", ".join(bits)
    reasons = []
    if (c.get("f_score") or 0) < 7:
        reasons.append(f"F{c.get('f_score')}<7")
    if mos is None or mos < 0.40:
        reasons.append(f"MoS {mos:.0%}<40%" if mos is not None else "no asset discount")
    if "DETERIORATING" in flags:
        reasons.append(f"MD&A DETERIORATING (L3 {det})")
    if "DISTRESS" in flags:
        reasons.append("Altman DISTRESS")
    if "LOW_RUNWAY" in flags:
        reasons.append("low cash runway")
    if "NO_L3_READ" in flags:
        reasons.append("MD&A unreadable -> not cleared")
    if "L5_KILL" in flags:
        reasons.append(f"L5 trap-filter killed: {(c.get('l5_risks') or ['trap'])[0]}")
    return "WATCH — " + ("; ".join(reasons) if reasons else "did not clear all BUY gates")


def _reasoning_digest(as_of: str, n_universe: int, cands: list[dict], book: list[dict],
                      acct: str) -> str:
    """A richer, explanatory alert body — what the screen did and WHY each name landed where it
    did (chain-of-thought), so the ping explains the decision, not just the count."""
    n_buy = sum(1 for c in book if c["verdict"] == "BUY")
    lines = [f"Tedium Premium — {as_of} (acct {acct})",
             f"{n_universe} recent EDGAR filers -> {len(cands)} screenable -> book {len(book)}, "
             f"{n_buy} BUY."]
    if not book:
        lines.append("No name cleared the screen (cheap-on-assets + healthy + liquid).")
        return "\n".join(lines)
    lines.append("BUY bar: F>=7, margin-of-safety>=40%, no risk flags, L3 language not deteriorating"
                 + (", L5 trap-filter cleared." if any(c.get("l5_decision") for c in book) else "."))
    for c in sorted(book, key=lambda x: x.get("composite", 0), reverse=True):
        lines.append(f"- {c['ticker']} @ ${c.get('price')}: {_why(c)}")
    return "\n".join(lines)


async def _session(as_of: str, days_back: int, max_positions: int, p_tbv_max: float,
                   min_f: int, max_llm_usd: float, execute: str, transmit: bool,
                   max_universe: int = 0, deepdive_usd: float = 0.0) -> str:
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

        # 5) MD&A Deterioration Lead — OPT-IN, budget-capped (the spend rule). The validated
        #    edge: softening YoY 10-K language LEADS distress, so a deteriorating name is a
        #    trap-in-waiting even if the screen looks clean -> flag it and block it from BUY.
        l3_note = ""
        if max_llm_usd > 0 and book:
            from anthropic import Anthropic

            from deepvalue.forward.deterioration import score_deterioration
            from deepvalue.ingest.segmentation import SegLLM
            from deepvalue.models import LEAF
            client = Anthropic()
            # Haiku locate-fallback for the ~35% of filings the heuristic segmenter misses;
            # shares the same hard cap as the Sonnet materiality read.
            seg_llm = SegLLM(client=client, model=LEAF.id, spec=LEAF, cap_usd=max_llm_usd)
            det, spent = score_deterioration(book, as_of, client, max_llm_usd=max_llm_usd,
                                             seg_llm=seg_llm)
            for c in book:
                r = det.get(c["ticker"])
                if r is not None:
                    c["deterioration"] = round(r.deterioration, 3)
                    c["det_categories"] = r.categories
                    if r.deterioration >= DET_KILL:  # leading-indicator kill: softening language
                        if "DETERIORATING" not in c["flags"]:
                            c["flags"].append("DETERIORATING")
                        if c["verdict"] == "BUY":
                            c["verdict"] = "WATCH"
                elif c["verdict"] == "BUY":
                    # SAFETY GATE: no clean MD&A read -> can't clear a BUY of trap risk. Require
                    # L3 clearance for BUY, so an extraction miss is a MISSED buy (conservative,
                    # Burry-tilt default), never a blind buy of a name we couldn't read.
                    c["verdict"] = "WATCH"
                    if "NO_L3_READ" not in c["flags"]:
                        c["flags"].append("NO_L3_READ")
            n_blocked = sum(1 for c in book if "NO_L3_READ" in c["flags"])
            log.info("Deterioration Lead: scored %d/%d, spent $%.2f (%d BUY blocked: no read)",
                     len(det), len(book), spent, n_blocked)
            l3_note = f" | L3 {len(det)}/{len(book)} (${spent:.2f})"

        # 6) L4 forensic + L5 adversarial trap filter on the BUY shortlist (opt-in, capped).
        #    Spec gate: a clean screen alone never triggers BUY — L5 must clear. Runs only on BUYs
        #    (rare), fast in the deployed container (the SDK only stalls when nested in a CLI).
        if deepdive_usd > 0:
            buys0 = [c for c in book if c["verdict"] == "BUY"]
            if buys0:
                from deepvalue.agents import forensic_then_adversarial
                from deepvalue.agents.subagents.judge import needs_review
                # split the budget across BUYs, but cap any single name (a lively debate can
                # otherwise consume the whole pot — AMS hit ~$8 on its own).
                per, killed, review = min(deepdive_usd / len(buys0), 8.0), 0, []
                for c in buys0:
                    try:
                        v = await forensic_then_adversarial(c["ticker"], as_of,
                                                            _candidate_dossier(c), max_llm_usd=per)
                    except Exception as e:  # noqa: BLE001 — an agent failure escalates, not silently passes
                        log.warning("L4/L5 errored for %s: %s -> human review", c["ticker"], type(e).__name__)
                        c["verdict"] = "WATCH"
                        if "NEEDS_REVIEW" not in c["flags"]:
                            c["flags"].append("NEEDS_REVIEW")
                        review.append(c["ticker"])
                        continue
                    c["l5_decision"] = v.decision
                    c["l5_conviction"] = round(v.conviction, 2)
                    c["l5_risks"] = v.surviving_risks[:4]
                    if needs_review(v):  # judge couldn't render -> hold for a human, do NOT pass
                        c["verdict"] = "WATCH"
                        if "NEEDS_REVIEW" not in c["flags"]:
                            c["flags"].append("NEEDS_REVIEW")
                        review.append(c["ticker"])
                    elif v.decision != "BUY":  # the trap filter held/killed it
                        c["verdict"] = "WATCH"
                        if "L5_KILL" not in c["flags"]:
                            c["flags"].append("L5_KILL")
                        killed += 1
                log.info("L4/L5: vetted %d BUYs, %d killed, %d need review", len(buys0), killed, len(review))
                if review:  # raise for human intervention — a failed adjudication must be LOUD
                    notify.notify(
                        f"⚠️ HUMAN REVIEW REQUIRED — Tedium Premium {as_of}",
                        f"The L5 trap filter could NOT render a verdict for BUY candidate(s): "
                        f"{', '.join(review)}. They are HELD at WATCH (not bought, not auto-passed) "
                        f"pending manual review — re-run the deepdive or vet by hand before any buy.")

        _write_artifacts(as_of, len(universe), cands, book)
        digest = _reasoning_digest(as_of, len(universe), cands, book, acct) + l3_note

        # 6) paper rebalance toward the BUY book — preview unless --transmit (opt-in)
        if execute == "ibkr":
            buys = [c for c in book if c["verdict"] == "BUY"]  # candidate dicts (carry margin_of_safety)
            last_close = {t: px[max(px)]["close"] for t, px in prices.items() if px}
            rb = await ibkr_execution.rebalance(
                ib, buys, last_close, kelly_fraction=POLICY.get("kelly_fraction", 0.25),
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
    ap.add_argument("--deepdive-usd", type=float, default=0.0,
                    help="enable the L4/L5 forensic+adversarial trap filter on BUY candidates with "
                         "this hard cap split across them (0 = off). Only runs when there are BUYs.")
    args = ap.parse_args()

    if args.healthcheck:
        raise SystemExit(_healthcheck())

    try:
        digest = asyncio.run(_session(args.as_of, args.days_back, args.max_positions,
                                      args.p_tbv_max, args.min_f, args.max_llm_usd,
                                      args.execute, args.transmit, args.max_universe,
                                      args.deepdive_usd))
        notify.write_heartbeat("ok", args.as_of, digest)
        notify.notify(f"✅ Tedium Premium forward OK ({args.as_of})", digest)
    except Exception as e:  # noqa: BLE001 — record the failure loudly, then re-raise
        log.exception("forward session FAILED")
        notify.write_heartbeat("error", args.as_of, f"{type(e).__name__}: {e}")
        notify.notify(f"❌ Tedium Premium forward FAILED ({args.as_of})", f"{type(e).__name__}: {e}")
        raise


if __name__ == "__main__":
    main()
