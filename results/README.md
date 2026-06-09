# results/ — committed, inspectable L3 validation outputs

These are **our own computed statistics** — per-cohort Information Coefficients (rank correlations
of the L3 signal with forward returns), aggregated. They are **not** Sharadar's or FMP's licensed
rows, so they are safe to commit. The point is to make deepvalue's *positive* headline numbers
**verifiable from the repo** — clone it, open the CSV, re-derive the mean and t-stat yourself —
rather than asking you to trust prose (the same standard parley meets for its committed null).

The raw inputs are gitignored and point-in-time, behind a Sharadar/FMP subscription that lapses
~end-June 2026. After that the inputs are gone; **these CSVs are the durable record.**

## Files
| file | what |
|---|---|
| `l3_leading_indicator_ic.csv` | per-cohort IC of `-deterioration` vs forward return, for `all_cheap` names and the `event_clean` subset (names with **no** trailing-12m hard-distress event), at 63/126/252 trading days |
| `l3_anonymization_ic.csv` | per-cohort IC for the same 800-name pilot scored on `original` vs name/ticker-`anonymized` filing text |
| `summary.json` | the aggregated headline (mean IC, naive cross-cohort t, cohort count, obs) per test/horizon |

## Regenerate (requires the local cache, pre-expiry)
```bash
uv run python scripts/regenerate_results.py
```
Reads `data/cache/{l1l3_sharadar.json, l3_materiality_pilot.json, l3_materiality_pilot_anon.json,
sharadar.duckdb}`. **Zero LLM cost** — the materiality scores are already cached; this only
recomputes the rank correlations.

## Re-derive the headline from the committed CSV (no cache needed)
```python
import csv, statistics, math
rows = [r for r in csv.DictReader(open("results/l3_leading_indicator_ic.csv"))
        if r["cut"] == "event_clean" and r["horizon_days"] == "126"]
ics = [float(r["ic"]) for r in rows]
m = statistics.mean(ics)
print(m, m / (statistics.stdev(ics) / math.sqrt(len(ics))))   # +0.1407, t +2.63
```

## Headline (from `summary.json`)

**Leading-indicator test** — does quiet YoY MD&A deterioration predict forward returns *among
names with no trailing hard-distress event*? If yes, the language **leads** the hard event.

| horizon | all cheap | event-clean | cohorts |
|---|---|---|---|
| 63d  | +0.029 (t 0.79) | **+0.113 (t 2.15)** | 24 |
| 126d | +0.096 (t 2.09) | **+0.141 (t 2.63)** | 24 |
| 252d | +0.094 (t 1.81) | +0.092 (t 1.61) | 24 |

**Anonymization test** — re-score the same names on name/ticker-stripped text. The IC barely
moves, so the signal lives in the **risk language**, not the model recalling the company.

| horizon | original | anonymized | retained | cohorts |
|---|---|---|---|---|
| 63d  | +0.105 (t 2.10) | +0.091 (t 1.97) | 86% | 11 |
| 126d | +0.125 (t 2.74) | +0.116 (t 2.60) | 93% | 11 |
| 252d | +0.111 (t 2.46) | +0.098 (t 2.38) | 88% | 11 |

## Honest caveats (read these)
- **Modest, not strong.** Best t-stats are ~2.1–2.7; the 252d event-clean cut (t 1.61) is **not**
  significant. The signal is clearest at the 126d horizon.
- **The t-stat is naive** — a cross-cohort t assuming independent annual cohorts. It is a point
  estimate, not a clustered/Newey-West SE. `overlap_warning` is false here (horizon ≤ annual
  spacing) but the small cohort counts (24 and 11) make these wide intervals.
- **`all_cheap` is weaker than `event_clean`.** The headline edge is specifically the
  *leading-indicator* cut; the undifferentiated cheap-bucket IC is smaller (e.g. +0.096 @126d).
- **The anonymization pilot is small** — 800 names across **11** cohorts (2015+), underpowered
  relative to the 24-cohort leading-indicator panel. Treat it as suggestive, not settled.
- **Survivorship-free, point-in-time** (Sharadar, delisted names included; signal dated by filing
  date). This is the part that makes the numbers trustworthy; it's also why they're modest.
