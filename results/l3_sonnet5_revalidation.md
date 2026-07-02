# L3 re-validation: sonnet-4-6 → claude-sonnet-5 (2 Jul 2026)

Per `models.py`'s contract (a ROOT change is a deploy → re-run validation), the L3
materiality scorer was re-validated after migrating ROOT from `claude-sonnet-4-6` to
`claude-sonnet-5`. Method: **paired A/B** — the same filing-pairs from the committed
leading-indicator panel (`data/cache/l1l3_sharadar.json`, 1,512 rows) were re-scored with
Sonnet 5 (`scripts/l3_sonnet5_revalidation.py`), and per-cohort IC was recomputed for both
scorers on the identical names/dates/forward-returns. Any delta is the model, not the sample.

Scored 1,192 of 1,512 pairs before the $20 metered hard cap stopped the run (metered at the
$3/$15 sticker; Sonnet 5 intro pricing billed ~2/3 of that). Cohort gate MIN_COHORT=8,
spacing 252d — same as `regenerate_results.py`.

## Result: the edge survives, and is stronger, under Sonnet 5

Paired n=1,192. IC = per-cohort rank correlation of −deterioration vs forward return
(mean across k=24 cohorts, t on the cohort means).

### ALL cheap names

| horizon | sonnet-4-6 (validated) | sonnet-5 (new) |
|--------:|-----------------------:|---------------:|
| 63d  | +0.022 (t +0.5) | +0.035 (t +1.0) |
| 126d | +0.081 (t +1.6) | +0.104 (t +1.9) |
| 252d | +0.065 (t +1.2) | +0.093 (t +1.6) |

### EVENT-CLEAN (no trailing-12m hard distress — the committed headline cut)

n=694 paired.

| horizon | sonnet-4-6 (validated) | sonnet-5 (new) |
|--------:|-----------------------:|---------------:|
| 63d  | +0.090 (t +1.4) | **+0.121 (t +2.0)** |
| 126d | +0.089 (t +1.3) | **+0.131 (t +1.8)** |
| 252d | +0.031 (t +0.5) | +0.087 (t +1.4) |

The sonnet-4-6 columns differ from the committed full-panel numbers
(`l3_leading_indicator_ic.csv`: +0.113/+0.141) because the cap cut ~320 rows; the paired
columns are the apples-to-apples read. Sonnet 5's IC is higher at every horizon on both cuts.

## Calibration shift — DET_KILL re-tuned 0.5 → 0.65

Sonnet 5 preserves the RANKING (spearman +0.86 vs the 4.6 scores) but scores deterioration
systematically higher (panel mean 0.517 → 0.652). At the old live threshold 0.5, the
kill-rate on the cheap-name panel would have jumped 57.9% → 81.3% — a silent, large
tightening of the BUY gate. `DET_KILL` in `forward/run.py` was re-tuned to **0.65**, the
Sonnet 5 score that reproduces the validated 57.9% kill-rate on the same panel.

| threshold | kill-rate (sonnet-4-6) | kill-rate (sonnet-5) |
|----------:|-----------------------:|---------------------:|
| 0.5 | 57.9% | 81.3% |
| 0.6 | 47.8% | 66.6% |
| 0.7 | 34.1% | 53.9% |

## Caveats

- Same maturity caveats as the original validation: modest t-stats, 252d weakest, and the
  in-sample window is contamination-bound (both models' training cutoffs are Jan 2026, so
  the contamination boundary did not move with this migration).
- The re-score ran with the production call shape (`thinking: disabled`, `effort: low`,
  structured output, max_tokens 700).
- Re-derivable while the cached panel + EDGAR document cache exist:
  `uv run python scripts/l3_sonnet5_revalidation.py --report-only` (free).
