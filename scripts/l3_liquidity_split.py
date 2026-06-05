"""
Thesis-critical free test: does the diff signal CONCENTRATE in illiquid / neglected
names (where deepvalue claims the edge lives), or is it uniform? Splits the L3 backtest
records into per-cohort liquidity terciles by point-in-time ADV and reports the
momentum-partial similarity IC in each.

ADV = trailing 60-trading-day mean dollar volume (close*volume) ending at the filing
date — computed from the survivorship price cache (get_prices includes volume), so it's
point-in-time with no lookahead.

    uv run python scripts/l3_liquidity_split.py data/cache/l3_backtest_mdna_masked.json
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
from deepvalue.ingest.prices import get_prices  # noqa: E402

_ADV_WINDOW = 60


def _adv(prices: dict[str, dict], as_of: str) -> float | None:
    dates = sorted(d for d in prices if d <= as_of)
    if len(dates) < _ADV_WINDOW:
        return None
    dollar = [
        (prices[d].get("close") or 0) * (prices[d].get("volume") or 0)
        for d in dates[-_ADV_WINDOW:]
    ]
    vals = [x for x in dollar if x > 0]
    return sum(vals) / len(vals) if vals else None


def _partial_ic(rows: list[dict], h: int, min_cohort: int):
    """Momentum-partial similarity IC over the given rows, aggregated by cohort."""
    by_cohort: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        by_cohort[r["cohort"]].append(r)
    res = []
    for c, rs in by_cohort.items():
        pts = [(r["similarity"], r[f"fwd{h}"], r["prior252"]) for r in rs
               if r.get("similarity") is not None and r.get(f"fwd{h}") is not None
               and r.get("prior252") is not None]
        if len(pts) < min_cohort:
            continue
        s, f, m = [p[0] for p in pts], [p[1] for p in pts], [p[2] for p in pts]
        r_sf, r_sm, r_fm = spearman(s, f), spearman(s, m), spearman(f, m)
        if None in (r_sf, r_sm, r_fm):
            continue
        denom = math.sqrt((1 - r_sm**2) * (1 - r_fm**2))
        if denom:
            res.append(ICResult(as_of=c, ic=(r_sf - r_sm * r_fm) / denom, n=len(pts)))
    return ic_summary(res, horizon_days=h, spacing_days=252)


def main() -> None:
    path = Path(sys.argv[1] if len(sys.argv) > 1 else "data/cache/l3_backtest_mdna_masked.json")
    records = json.loads(path.read_text())
    horizons = sorted(int(k[3:]) for k in records[0] if k.startswith("fwd"))

    # attach point-in-time ADV (cache get_prices per ticker)
    px_cache: dict[str, dict] = {}
    kept = []
    for r in records:
        t = r["ticker"]
        if t not in px_cache:
            px_cache[t] = get_prices(t)
        adv = _adv(px_cache[t], r["as_of"])
        if adv is not None:
            r["adv"] = adv
            kept.append(r)
    print(f"{path.name}: {len(kept)}/{len(records)} pairs with ADV")

    # per-cohort liquidity terciles (liquidity drifts over time -> rank within cohort)
    by_cohort: dict[str, list[dict]] = defaultdict(list)
    for r in kept:
        by_cohort[r["cohort"]].append(r)
    for rs in by_cohort.values():
        rs.sort(key=lambda r: r["adv"])
        n = len(rs)
        for i, r in enumerate(rs):
            r["liq_tercile"] = 0 if i < n / 3 else (1 if i < 2 * n / 3 else 2)

    labels = {0: "ILLIQUID (bottom 1/3)", 1: "mid", 2: "LIQUID (top 1/3)"}
    print("momentum-partial similarity IC by point-in-time liquidity tercile:")
    print(f"{'horizon':>8} | " + " | ".join(f"{labels[t]:>22}" for t in (0, 1, 2)))
    for h in horizons:
        cells = []
        for t in (0, 1, 2):
            s = _partial_ic([r for r in kept if r["liq_tercile"] == t], h, 4)
            cells.append(f"{s.mean_ic:+.4f}(t={s.t_stat:+.2f})"
                         if s.mean_ic is not None and s.t_stat is not None else "n/a")
        print(f"{h:>8} | " + " | ".join(f"{c:>22}" for c in cells))


if __name__ == "__main__":
    main()
