"""
Confound checks on the L3 backtest records (zero cost) — is the similarity->return
signal real, or a momentum artifact? Reads a records JSON written by l3_diff_backtest.py.

Per horizon, per filing-year cohort, then aggregated across cohorts (mean / t-stat):
  1. raw IC               : Spearman(similarity, fwd)
  2. momentum-PARTIAL IC  : Spearman partial correlation of (similarity, fwd) controlling
                            for prior-year return, using ALL names (full power — unlike a
                            tercile subsample). If momentum drove the signal this collapses;
                            if it's near the raw IC the edge is momentum-independent.
  3. momentum baseline    : Spearman(prior252, fwd) — how predictive momentum itself is.

    uv run python scripts/l3_confound_check.py data/cache/l3_backtest_mdna.json
"""
from __future__ import annotations

import json
import math
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
from deepvalue.eval.ic import ICResult, ic_summary, spearman  # noqa: E402


def _agg(per_cohort_ic: list[tuple[str, float, int]], h: int):
    res = [ICResult(as_of=c, ic=ic, n=n) for c, ic, n in per_cohort_ic]
    return ic_summary(res, horizon_days=h, spacing_days=252)


def _raw(records_by_cohort, signal, h, min_cohort):
    out = []
    for c, rows in records_by_cohort.items():
        pts = [(r[signal], r[f"fwd{h}"]) for r in rows
               if r.get(signal) is not None and r.get(f"fwd{h}") is not None]
        if len(pts) < min_cohort:
            continue
        ic = spearman([a for a, _ in pts], [b for _, b in pts])
        if ic is not None:
            out.append((c, ic, len(pts)))
    return _agg(out, h)


def _partial(records_by_cohort, h, min_cohort):
    """Spearman partial corr of (similarity, fwd | prior252), aggregated across cohorts."""
    out = []
    for c, rows in records_by_cohort.items():
        pts = [(r["similarity"], r[f"fwd{h}"], r["prior252"]) for r in rows
               if r.get("similarity") is not None and r.get(f"fwd{h}") is not None
               and r.get("prior252") is not None]
        if len(pts) < min_cohort:
            continue
        s = [p[0] for p in pts]; f = [p[1] for p in pts]; m = [p[2] for p in pts]
        r_sf, r_sm, r_fm = spearman(s, f), spearman(s, m), spearman(f, m)
        if None in (r_sf, r_sm, r_fm):
            continue
        denom = math.sqrt((1 - r_sm**2) * (1 - r_fm**2))
        if denom == 0:
            continue
        out.append((c, (r_sf - r_sm * r_fm) / denom, len(pts)))
    return _agg(out, h)


def main() -> None:
    path = Path(sys.argv[1] if len(sys.argv) > 1 else "data/cache/l3_backtest_mdna.json")
    records = json.loads(path.read_text())
    horizons = sorted(int(k[3:]) for k in records[0] if k.startswith("fwd"))
    by_cohort = defaultdict(list)
    for r in records:
        by_cohort[r["cohort"]].append(r)

    def fmt(s):
        return f"{s.mean_ic:+.4f}(t={s.t_stat:+.2f})" if s.mean_ic is not None and s.t_stat is not None else "n/a"

    print(f"{path.name}: {len(records)} filing-pairs, {len(by_cohort)} cohorts")
    print(f"{'horizon':>8} | {'raw IC':>18} | {'momentum-partial IC':>20} | {'momentum baseline':>18}")
    for h in horizons:
        raw = _raw(by_cohort, "similarity", h, 5)
        par = _partial(by_cohort, h, 5)
        mom = _raw(by_cohort, "prior252", h, 5)
        print(f"{h:>8} | {fmt(raw):>18} | {fmt(par):>20} | {fmt(mom):>18}")


if __name__ == "__main__":
    main()
