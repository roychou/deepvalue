"""
L1 quant-gate backtest ($0, no LLM, no network) — does the quant gate + trap signals
actually predict forward returns, point-in-time, survivorship-free?

For every annual filing in the universe (as_of = filing_date), compute the value metrics
and trap signals from that filing's fundamentals, then cross-sectional IC vs forward
returns by filing-year cohort (eval/ic.py). Survivorship-free: drives off the manifest
(active + delisted) and the FMP fundamentals/price grab — dead names included.

Sign convention: each signal is oriented so a POSITIVE IC = predictive of higher return
(value: cheaper better; quality: healthier better; traps: distress/manipulation worse).
The §14.1 question — do distress-Z / low-F / manipulator-M names underperform, and do
cheap names outperform — reads straight off the table.

    uv run python scripts/l1_quant_backtest.py --sample 4000
"""
from __future__ import annotations

import argparse
import json
import logging
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
import sys  # noqa: E402
sys.path.insert(0, str(ROOT / "src"))

from deepvalue.eval.ic import cross_sectional_ic, forward_return, ic_summary  # noqa: E402
from deepvalue.ingest.fundamentals_store import load_periods, prior_year  # noqa: E402
from deepvalue.ingest.prices import get_prices  # noqa: E402
from deepvalue.quant.metrics import value_metrics  # noqa: E402
from deepvalue.quant.trap_signals import trap_signals  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("l1_backtest")

CACHE = ROOT / "data" / "cache"

# signal -> orientation: +1 keeps the raw value, -1 flips it so POSITIVE IC = higher return
SIGNALS = {
    "f_score": +1, "z_score": +1,            # healthier / safer -> better
    "m_score": -1, "dilution_yoy": -1,        # manipulator / dilution -> worse
    "price_to_ncav": -1, "ev_ebit": -1, "p_tbv": -1,  # cheaper -> better
}


def _records_for(ticker: str, prices: dict, horizons: list[int]) -> list[dict]:
    out = []
    for p in load_periods(ticker):
        elig = [d for d in prices if d <= p.filing_date]
        if not elig:
            continue
        price = prices[max(elig)].get("close")
        if not price or price <= 0:
            continue
        vm = value_metrics(p, price)
        ts = trap_signals(p, prior_year(ticker, p), price)
        rec = {"ticker": ticker, "cohort": p.filing_date[:4], "as_of": p.filing_date}
        for s in SIGNALS:
            rec[s] = vm.get(s, ts.get(s))
        # a value RATIO is only meaningful when its denominator is positive — a negative
        # EV/EBIT (negative EBIT) or price/NCAV (negative NCAV) isn't "cheap", it's broken.
        for s in ("ev_ebit", "price_to_ncav", "p_tbv"):
            if rec.get(s) is not None and rec[s] <= 0:
                rec[s] = None
        for h in horizons:
            rec[f"fwd{h}"] = forward_return(prices, p.filing_date, h)
        out.append(rec)
    return out


def _ic_table(records: list[dict], horizons: list[int], min_cohort: int) -> None:
    print(f"{'signal':>14} | " + " | ".join(f"fwd{h:>3}d".rjust(18) for h in horizons))
    for sig, sign in SIGNALS.items():
        cells = []
        for h in horizons:
            by_cohort: dict[str, dict[str, dict]] = defaultdict(dict)
            for r in records:
                if r.get(sig) is not None and r.get(f"fwd{h}") is not None:
                    by_cohort[r["cohort"]][r["ticker"]] = r
            res = []
            for c, names in by_cohort.items():
                if len(names) < min_cohort:
                    continue
                conv = {t: sign * r[sig] for t, r in names.items()}
                rets = {t: r[f"fwd{h}"] for t, r in names.items()}
                ic = cross_sectional_ic(c, conv, rets)
                if ic.ic is not None:
                    res.append(ic)
            s = ic_summary(res, horizon_days=h, spacing_days=252)
            cells.append((f"{s.mean_ic:+.4f}(t={s.t_stat:+.2f})"
                          if s.mean_ic is not None and s.t_stat is not None else "n/a"))
        print(f"{sig:>14} | " + " | ".join(c.rjust(18) for c in cells))


def main() -> None:
    ap = argparse.ArgumentParser(description="L1 quant-gate IC backtest ($0)")
    ap.add_argument("--sample", type=int, default=0, help="random names (0 = whole universe)")
    ap.add_argument("--seed", type=int, default=1)
    ap.add_argument("--horizons", default="63,126,252")
    ap.add_argument("--min-cohort", type=int, default=20)
    args = ap.parse_args()
    horizons = [int(x) for x in args.horizons.split(",")]

    manifest = json.loads((CACHE / "manifest.json").read_text())
    pool = sorted({m["symbol"] for m in manifest if m.get("cik") and m.get("n_price", 0) > 0})
    if args.sample:
        import random
        random.seed(args.seed)
        pool = random.sample(pool, min(args.sample, len(pool)))
    log.info("universe: %d names | computing point-in-time L1 signals", len(pool))

    records, n_names = [], 0
    for i, tk in enumerate(pool, 1):
        recs = _records_for(tk, get_prices(tk), horizons)
        if recs:
            n_names += 1
            records.extend(recs)
        if i % 1000 == 0:
            log.info("  %d/%d names | %d filing records", i, len(pool), len(records))

    print(f"\n=== L1 QUANT-GATE BACKTEST ===  names={n_names} | filing-records={len(records)} "
          f"| cohorts={len({r['cohort'] for r in records})}")
    print("POSITIVE IC = signal predicts HIGHER forward return (cheaper/healthier good; "
          "distress/manipulation/dilution bad).\n")
    _ic_table(records, horizons, args.min_cohort)
    (CACHE / "l1_backtest.json").write_text(json.dumps(records))
    print(f"\nrecords -> {CACHE / 'l1_backtest.json'}")


if __name__ == "__main__":
    main()
