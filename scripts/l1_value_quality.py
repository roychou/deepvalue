"""
L1 value×quality double-sort ($0, no LLM) — the deterministic half of the deepvalue
thesis: among CHEAP names, do quality / non-trap signals separate the winners from the
value-traps (cheap *because* dying)?

Reads the records written by l1_quant_backtest.py. For each filing-year cohort: take the
cheapest tercile on a value metric, then within it (a) rank-correlate a quality signal
against forward return (IC), and (b) compare the mean forward return of the high-quality
vs low-quality half. Aggregated across cohorts. A POSITIVE quality IC and a positive
high-minus-low spread *within the cheap bucket* means the trap filter earns its place.

    uv run python scripts/l1_value_quality.py --value p_tbv --quality f_score
"""
from __future__ import annotations

import argparse
import json
import statistics
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
from deepvalue.eval.ic import ICResult, ic_summary, spearman  # noqa: E402

CACHE = ROOT / "data" / "cache"
QUALITY_SIGN = {"f_score": +1, "z_score": +1, "m_score": -1, "dilution_yoy": -1}  # higher=better


def _cohort_cheap_quality(rows, value, quality, qsign, h, min_bucket):
    """Within a cohort: (oriented_quality, fwd) pairs for the cheapest-tercile names."""
    cheap = [r for r in rows if r.get(f"fwd{h}") is not None and r.get(value) is not None]
    if len(cheap) < min_bucket * 3:
        return None
    cheap.sort(key=lambda r: r[value])                       # ascending = cheapest first
    tercile = cheap[: max(min_bucket, len(cheap) // 3)]
    q = [(qsign * r[quality], r[f"fwd{h}"]) for r in tercile if r.get(quality) is not None]
    return q if len(q) >= min_bucket else None


def main() -> None:
    ap = argparse.ArgumentParser(description="L1 value x quality double-sort ($0)")
    ap.add_argument("--records", default=str(CACHE / "l1_backtest.json"))
    ap.add_argument("--value", default="p_tbv", choices=["p_tbv", "ev_ebit", "price_to_ncav"])
    ap.add_argument("--quality", default="f_score", choices=list(QUALITY_SIGN))
    ap.add_argument("--horizons", default="63,126,252")
    ap.add_argument("--min-bucket", type=int, default=8)
    args = ap.parse_args()
    horizons = [int(x) for x in args.horizons.split(",")]
    qsign = QUALITY_SIGN[args.quality]

    records = json.loads(Path(args.records).read_text())
    by_cohort: dict[str, list[dict]] = defaultdict(list)
    for r in records:
        by_cohort[r["cohort"]].append(r)

    n_val = sum(1 for r in records if r.get(args.value) is not None)
    print(f"value={args.value} (cheapest tercile)  x  quality={args.quality}  "
          f"| records with {args.value}: {n_val}")
    print(f"{'horizon':>8} | {'quality IC in cheap':>22} | {'cheap+HiQ':>10} | "
          f"{'cheap+LoQ':>10} | {'spread':>9} | {'cohorts':>7}")
    for h in horizons:
        ics, hi_ret, lo_ret = [], [], []
        for c, rows in by_cohort.items():
            q = _cohort_cheap_quality(rows, args.value, args.quality, qsign, h, args.min_bucket)
            if q is None:
                continue
            ic = spearman([a for a, _ in q], [b for _, b in q])
            if ic is not None:
                ics.append(ICResult(c, ic, len(q)))
            qs = sorted(q, key=lambda t: t[0])
            half = len(qs) // 2
            lo_ret.append(statistics.mean([b for _, b in qs[:half]]))
            hi_ret.append(statistics.mean([b for _, b in qs[len(qs) - half:]]))
        s = ic_summary(ics, horizon_days=h, spacing_days=252)
        ic_str = (f"{s.mean_ic:+.4f}(t={s.t_stat:+.2f})"
                  if s.mean_ic is not None and s.t_stat is not None else "n/a")
        hi = statistics.mean(hi_ret) if hi_ret else None
        lo = statistics.mean(lo_ret) if lo_ret else None
        spread = (hi - lo) if (hi is not None and lo is not None) else None
        print(f"{h:>8} | {ic_str:>22} | "
              f"{(f'{hi*100:+.2f}%' if hi is not None else 'n/a'):>10} | "
              f"{(f'{lo*100:+.2f}%' if lo is not None else 'n/a'):>10} | "
              f"{(f'{spread*100:+.2f}%' if spread is not None else 'n/a'):>9} | {len(ics):>7}")


if __name__ == "__main__":
    main()
