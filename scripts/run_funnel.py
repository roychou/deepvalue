"""
v0 deep-value funnel — the deployable recommendation artifact, DETERMINISTIC ($0, no LLM).

Pivot decision (7 Jun 2026): the proven edge is the FREE quant funnel (Piotroski-F-led
quality + value). L3/L4/L5 LLM layers are HELD until their increment is demonstrated. So
v0 ranks the current investable universe on the validated free signals and emits a
ranked BUY/WATCH/PASS candidate book — point-in-time, illiquidity-aware (policy.yaml).

This is a recommendation artifact for a human; it takes NO trading action. Every BUY
needs human sign-off (policy). It also snapshots the book (entry prices) for the IBKR
forward paper-trading monitor.

    uv run python scripts/run_funnel.py --as-of 2026-06-04
"""
from __future__ import annotations

import argparse
import json
import statistics
import sys
from datetime import date
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
from deepvalue.ingest.edgar import company_sic, is_excluded_sector  # noqa: E402
from deepvalue.ingest.fundamentals_store import as_of as fund_as_of, prior_year  # noqa: E402
from deepvalue.ingest.prices import get_prices  # noqa: E402
from deepvalue.quant.metrics import value_metrics  # noqa: E402
from deepvalue.quant.trap_signals import trap_signals  # noqa: E402

CACHE = ROOT / "data" / "cache"
POLICY = yaml.safe_load((ROOT / "config" / "policy.yaml").read_text())
RECENCY_DAYS = 550          # fundamentals must be filed within ~18 months (still "current")
ADV_WINDOW = 60


def _days(a: str, b: str) -> int:
    return (date.fromisoformat(b) - date.fromisoformat(a)).days


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
    mu = statistics.mean(values); sd = statistics.pstdev(values) or 1.0
    return {"mu": mu, "sd": sd}


def main() -> None:
    ap = argparse.ArgumentParser(description="v0 deep-value funnel (deterministic, $0)")
    ap.add_argument("--as-of", default="2026-06-04")
    ap.add_argument("--max-positions", type=int, default=POLICY.get("max_positions", 15))
    ap.add_argument("--p-tbv-max", type=float, default=2.0, help="cheapness ceiling")
    ap.add_argument("--min-f", type=int, default=5, help="min Piotroski F (quality floor)")
    args = ap.parse_args()
    adv_floor = POLICY.get("liquidity_floor_adv_usd", 50000)

    manifest = json.loads((CACHE / "manifest.json").read_text())
    active = [(m["symbol"], m["cik"]) for m in manifest
              if m.get("status") == "active" and m.get("cik") and m.get("n_price", 0) > 0]

    # 1) build the INVESTABLE, screenable set (point-in-time, current fundamentals, liquid enough)
    cands = []
    for ticker, cik in active:
        p = fund_as_of(ticker, args.as_of)
        if p is None or _days(p.filing_date, args.as_of) > RECENCY_DAYS:
            continue
        prices = get_prices(ticker)
        elig = [d for d in prices if d <= args.as_of]
        if not elig:
            continue
        price = prices[max(elig)].get("close")
        adv = _adv(prices, args.as_of)
        if not price or price <= 1.0 or adv is None or adv < adv_floor:
            continue
        vm = value_metrics(p, price)
        ts = trap_signals(p, prior_year(ticker, p), price)
        ptbv, f, dil = vm.get("p_tbv"), ts.get("f_score"), ts.get("dilution_yoy")
        # data-glitch guards: p/tbv≈0 and |dilution|>50% are share-count/reporting artifacts
        # (weightedAverageShsOutDil reported in inconsistent units across years).
        if ptbv is None or not (0.05 <= ptbv <= args.p_tbv_max):
            continue
        if dil is not None and not (-0.5 <= dil <= 1.0):
            continue
        if ts.get("f_checks") != 9 or f < args.min_f:
            continue
        if is_excluded_sector(company_sic(cik)):     # banks / insurers / blank-check (policy)
            continue
        ptn = vm.get("price_to_ncav")
        # margin of safety = discount to the most conservative asset value (NCAV or tangible
        # book): mos = 1 - price/value = 1 - min(cheapness ratios). >0 means below asset value.
        ratios = [r for r in (ptbv, ptn) if r is not None and r > 0]
        mos = (1 - min(ratios)) if ratios else None
        cands.append({"ticker": ticker, "cik": str(cik), "price": price, "adv": adv,
                      "p_tbv": ptbv, "price_to_ncav": ptn, "margin_of_safety": mos,
                      "f_score": f, "dilution": ts.get("dilution_yoy"),
                      "ev_ebit": vm.get("ev_ebit"), "z_zone": ts.get("z_zone"),
                      "runway_months": ts.get("runway_months"), "market_cap": vm.get("market_cap")})

    # 2) composite of the VALIDATED free signals: high F, low dilution, cheap on book
    fz = _z([c["f_score"] for c in cands])
    dz = _z([c["dilution"] for c in cands if c["dilution"] is not None])
    pz = _z([c["p_tbv"] for c in cands])
    for c in cands:
        comp = (c["f_score"] - fz["mu"]) / fz["sd"]                       # higher F better
        comp += -(c["p_tbv"] - pz["mu"]) / pz["sd"]                       # cheaper better
        if c["dilution"] is not None and dz:
            comp += -(c["dilution"] - dz["mu"]) / dz["sd"]               # less dilution better
        c["composite"] = round(comp, 3)
    cands.sort(key=lambda c: c["composite"], reverse=True)

    # 3) verdict (v0, deterministic — ranked TIERS, NOT calibrated conviction; human signs off)
    min_mos = POLICY.get("min_margin_of_safety", 0.40)
    book = cands[: args.max_positions]
    for i, c in enumerate(book, 1):
        c["rank"] = i
        c["flags"] = [f for f, on in (
            ("DISTRESS", c["z_zone"] == "distress"),
            ("LOW_RUNWAY", (c["runway_months"] or 999) < 12),
            ("DILUTING", (c["dilution"] or 0) > 0.05)) if on]
        # BUY needs: strong quality + a real asset-value discount (policy) + not a flagged trap
        buy = (c["f_score"] >= 7 and (c["margin_of_safety"] or 0) >= min_mos and not c["flags"])
        c["verdict"] = "BUY" if buy else "WATCH"

    n_buy = sum(1 for c in book if c["verdict"] == "BUY")
    print(f"=== v0 DEEP-VALUE FUNNEL @ {args.as_of} (deterministic, $0) ===")
    print(f"investable universe: {len(cands)} | book: {len(book)} (max {args.max_positions}) "
          f"| BUY: {n_buy} | human sign-off required on every BUY\n")
    hdr = (f"{'#':>2} {'tkr':<6}{'verdict':<8}{'comp':>6}{'F':>3}{'p/tbv':>7}{'mos%':>6}"
           f"{'dil%':>7}{'Z':>9}{'ADV$':>9}  flags")
    print(hdr)
    for c in book:
        print(f"{c['rank']:>2} {c['ticker']:<6}{c['verdict']:<8}{c['composite']:>6.2f}"
              f"{c['f_score']:>3}{c['p_tbv']:>7.2f}"
              f"{(c['margin_of_safety']*100 if c['margin_of_safety'] is not None else 0):>6.0f}"
              f"{(c['dilution']*100 if c['dilution'] is not None else 0):>7.1f}"
              f"{str(c['z_zone']):>9}{c['adv']/1e3:>8.0f}k  {','.join(c['flags'])}")

    # 4) artifacts: a human-readable recommendation + a snapshot (entry prices) for the
    #    IBKR forward monitor. The system emits a recommendation only; it takes no action.
    snap = {"as_of": args.as_of, "policy": "v0_free_quant", "n_investable": len(cands),
            "book": [{k: c[k] for k in ("rank", "ticker", "cik", "verdict", "price", "composite",
                      "f_score", "p_tbv", "margin_of_safety", "adv", "flags")} for c in book]}
    (CACHE / f"book_{args.as_of}.json").write_text(json.dumps(snap, indent=2))

    md = [f"# Deep-value candidate book — {args.as_of} (v0, deterministic)", "",
          f"Investable universe: {len(cands)} · candidates: {len(book)} · BUY: {n_buy}", "",
          "**Recommendation artifact for human review — no trading action taken. Every BUY "
          "requires sign-off. Ranking is a deterministic composite of validated free signals "
          "(Piotroski-F + low dilution + cheap-on-book), NOT a calibrated probability.**", "",
          "| # | ticker | verdict | composite | F | p/tbv | margin-of-safety | div.yld dil% | Z | flags |",
          "|--:|---|---|--:|--:|--:|--:|--:|---|---|"]
    for c in book:
        md.append(f"| {c['rank']} | {c['ticker']} | {c['verdict']} | {c['composite']:.2f} | "
                  f"{c['f_score']} | {c['p_tbv']:.2f} | "
                  f"{(c['margin_of_safety']*100 if c['margin_of_safety'] is not None else 0):.0f}% | "
                  f"{(c['dilution']*100 if c['dilution'] is not None else 0):.1f}% | "
                  f"{c['z_zone']} | {', '.join(c['flags']) or '—'} |")
    out_md = CACHE / f"recommendation_{args.as_of}.md"
    out_md.write_text("\n".join(md) + "\n")
    print(f"\nartifacts -> {out_md}  +  book_{args.as_of}.json (entry prices for forward tracking)")


if __name__ == "__main__":
    main()
