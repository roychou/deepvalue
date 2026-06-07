"""
L1 quant-gate backtest on SHARADAR ($0, no LLM) — the survivorship-free, ~28-cohort
version of scripts/l1_quant_backtest.py. Same signals + cross-sectional IC harness, but
sourced from the Sharadar DuckDB (genuinely survivorship-free 1998+, real CFO, stable
permaticker, common-stock universe) instead of the FMP cache (clean only 2016+).

This is the power unlock: ~26.9k common stocks (76% delisted) over ~28 annual cohorts —
the dates we needed to settle the signals (and, next, whether L3 adds).

    uv run python scripts/l1_sharadar_backtest.py --from-cohort 1999
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
from deepvalue.ingest import sharadar as sh  # noqa: E402
from deepvalue.quant.metrics import value_metrics  # noqa: E402
from deepvalue.quant.trap_signals import trap_signals  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("l1_sharadar")
CACHE = ROOT / "data" / "cache"

SIGNALS = {"f_score": +1, "z_score": +1, "m_score": -1, "dilution_yoy": -1,
           "price_to_ncav": -1, "ev_ebit": -1, "p_tbv": -1}


def _records_for(name: dict, horizons: list[int]) -> list[dict]:
    tk, start, end = name["ticker"], name["first"], name["last"]
    px = sh.prices(tk, start, end)              # window-bounded -> reuse-safe
    if not px:
        return []
    ps = sh.periods(tk, "ARY", start, end)
    out = []
    for p in ps:
        elig = [d for d in px if d <= p.filing_date]
        if not elig:
            continue
        price = px[max(elig)].get("close")
        if not price or price <= 0:
            continue
        vm = value_metrics(p, price)
        ts = trap_signals(p, sh.prior_year(ps, p), price)
        rec = {"ticker": tk, "cohort": p.filing_date[:4], "as_of": p.filing_date,
               "siccode": name["siccode"]}
        for s in SIGNALS:
            rec[s] = vm.get(s, ts.get(s))
        for s in ("ev_ebit", "price_to_ncav", "p_tbv"):     # null broken (negative-denominator) ratios
            if rec.get(s) is not None and rec[s] <= 0:
                rec[s] = None
        for h in horizons:
            rec[f"fwd{h}"] = forward_return(px, p.filing_date, h)
        out.append(rec)
    return out


def _ic_table(records: list[dict], horizons: list[int], min_cohort: int, from_cohort: str) -> None:
    print(f"{'signal':>14} | " + " | ".join(f"fwd{h:>3}d".rjust(20) for h in horizons))
    for sig, sign in SIGNALS.items():
        cells = []
        for h in horizons:
            byc: dict[str, dict[str, dict]] = defaultdict(dict)
            for r in records:
                if r["cohort"] >= from_cohort and r.get(sig) is not None and r.get(f"fwd{h}") is not None:
                    byc[r["cohort"]][r["ticker"]] = r
            res = []
            for c, names in byc.items():
                if len(names) < min_cohort:
                    continue
                ic = cross_sectional_ic(c, {t: sign * x[sig] for t, x in names.items()},
                                        {t: x[f"fwd{h}"] for t, x in names.items()})
                if ic.ic is not None:
                    res.append(ic)
            s = ic_summary(res, horizon_days=h, spacing_days=252)
            cells.append(f"{s.mean_ic:+.4f}(t={s.t_stat:+.1f},n={s.n_dates})"
                         if s.mean_ic is not None and s.t_stat is not None else "n/a")
        print(f"{sig:>14} | " + " | ".join(c.rjust(20) for c in cells))


def main() -> None:
    ap = argparse.ArgumentParser(description="L1 quant backtest on Sharadar ($0)")
    ap.add_argument("--from-cohort", default="1999")
    ap.add_argument("--horizons", default="63,126,252")
    ap.add_argument("--min-cohort", type=int, default=30)
    ap.add_argument("--limit", type=int, default=0, help="cap names (0=all) for a quick pass")
    args = ap.parse_args()
    horizons = [int(x) for x in args.horizons.split(",")]

    uni = sh.common_stock_universe()
    if args.limit:
        uni = uni[: args.limit]
    log.info("Sharadar common-stock universe: %d names | computing point-in-time L1 signals", len(uni))

    records, n_names = [], 0
    for i, name in enumerate(uni, 1):
        recs = _records_for(name, horizons)
        if recs:
            n_names += 1
            records.extend(recs)
        if i % 2000 == 0:
            log.info("  %d/%d names | %d filing-records", i, len(uni), len(records))

    print(f"\n=== L1 QUANT-GATE BACKTEST on SHARADAR (survivorship-free) ===")
    print(f"names={n_names} | filing-records={len(records)} | "
          f"cohorts={len({r['cohort'] for r in records if r['cohort'] >= args.from_cohort})} "
          f"(from {args.from_cohort})")
    print("POSITIVE IC = predicts HIGHER forward return.\n")
    _ic_table(records, horizons, args.min_cohort, args.from_cohort)
    (CACHE / "l1_sharadar_backtest.json").write_text(json.dumps(records))
    print(f"\nrecords -> {CACHE / 'l1_sharadar_backtest.json'}")


if __name__ == "__main__":
    main()
