"""Regenerate the committed L3 results artifacts under results/ from the local data cache.

The OUTPUTS are our own computed statistics — per-cohort Information Coefficients (rank
correlations of the L3 signal with forward returns), aggregated. They are NOT Sharadar's or FMP's
licensed rows, so they are safe to commit. This is the deepvalue analogue of parley's committed
ic_validation.md: it makes the *positive* headline numbers inspectable and re-derivable from a
committed file, instead of asking a reader to trust prose.

    uv run python scripts/regenerate_results.py

Inputs (gitignored, license-bound, point-in-time — Sharadar sub expires ~end-June 2026):
  data/cache/l1l3_sharadar.json          the leading-indicator panel (cheap names, LLM-scored)
  data/cache/l3_materiality_pilot.json   the anonymization pilot — ORIGINAL filing text
  data/cache/l3_materiality_pilot_anon.json  same names, NAME/TICKER-ANONYMIZED text, re-scored
  data/cache/sharadar.duckdb             EVENTS table, for the trailing-12m hard-distress flag

Once the subscription lapses the inputs are gone, but the committed results/ CSVs remain the
durable, inspectable record. Run this BEFORE then.
"""
from __future__ import annotations

import csv
import json
from collections import defaultdict
from pathlib import Path

import duckdb

from deepvalue.eval.ic import ICResult, ic_summary, spearman

CACHE = Path("data/cache")
OUT = Path("results")
OUT.mkdir(exist_ok=True)
HORIZONS = (63, 126, 252)
MIN_COHORT = 8                       # same gate as the backtest (a cohort needs >=8 names)
_DISTRESS = ["13", "42", "41", "24", "31", "26"]   # Sharadar EVENTS hard-distress codes


def _attach_distress(rows: list[dict]) -> None:
    """Flag each record with a trailing-12m hard distress event (free, local EVENTS table)."""
    con = duckdb.connect(str(CACHE / "sharadar.duckdb"), read_only=True)
    like = " OR ".join(f"'|'||eventcodes||'|' LIKE '%|{c}|%'" for c in _DISTRESS)
    for r in rows:
        n = con.execute(
            f"SELECT count(*) FROM events WHERE ticker = ? AND date >= (?::DATE - INTERVAL 365 DAY) "
            f"AND date <= ?::DATE AND ({like})", [r["ticker"], r["as_of"], r["as_of"]]).fetchone()[0]
        r["distress"] = 1 if n else 0
    con.close()


def _by_cohort(rows: list[dict], signal_fn, h: int):
    """Per-cohort rank-IC of signal vs fwd{h}; returns (ICSummary, [(cohort, ic, n), ...])."""
    byc: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        if signal_fn(r) is not None and r.get(f"fwd{h}") is not None:
            byc[r["cohort"]].append(r)
    summary_in, per = [], []
    for c in sorted(byc):
        rs = byc[c]
        if len(rs) < MIN_COHORT:
            continue
        ic = spearman([signal_fn(r) for r in rs], [r[f"fwd{h}"] for r in rs])
        if ic is not None:
            summary_in.append(ICResult(c, ic, len(rs)))
            per.append((c, round(ic, 4), len(rs)))
    return ic_summary(summary_in, horizon_days=h, spacing_days=252), per


def _row(test: str, horizon: int, s) -> dict:
    return {"test": test, "horizon_days": horizon,
            "mean_ic": None if s.mean_ic is None else round(s.mean_ic, 4),
            "t_stat_naive": None if s.t_stat is None else round(s.t_stat, 2),
            "n_cohorts": s.n_dates, "total_obs": s.total_obs,
            "overlap_warning": s.overlap_warning}


def main() -> None:
    summary_rows: list[dict] = []

    # 1) LEADING-INDICATOR: does L3 language deterioration predict among names with NO trailing
    #    hard-distress event? (event-clean) vs all cheap names.
    scored = json.loads((CACHE / "l1l3_sharadar.json").read_text())
    _attach_distress(scored)
    clean = [r for r in scored if not r.get("distress")]
    per_li = []
    for h in HORIZONS:
        s_all, p_all = _by_cohort(scored, lambda r: -r["deterioration"], h)
        s_cln, p_cln = _by_cohort(clean, lambda r: -r["deterioration"], h)
        summary_rows.append(_row("l3_all_cheap", h, s_all))
        summary_rows.append(_row("l3_event_clean", h, s_cln))
        for c, ic, n in p_all:
            per_li.append({"horizon_days": h, "cohort": c, "cut": "all_cheap", "ic": ic, "n": n})
        for c, ic, n in p_cln:
            per_li.append({"horizon_days": h, "cohort": c, "cut": "event_clean", "ic": ic, "n": n})
    _write_csv(OUT / "l3_leading_indicator_ic.csv",
               ["horizon_days", "cohort", "cut", "ic", "n"], per_li)

    # 2) ANONYMIZATION: re-score the SAME names on name/ticker-stripped text. If the IC survives,
    #    the signal lives in the risk LANGUAGE, not the model recalling the company.
    pilot = json.loads((CACHE / "l3_materiality_pilot.json").read_text())
    anon = json.loads((CACHE / "l3_materiality_pilot_anon.json").read_text())
    per_anon = []
    for h in HORIZONS:
        s_o, p_o = _by_cohort(pilot, lambda r: -r["deterioration"], h)
        s_a, p_a = _by_cohort(anon, lambda r: -r["deterioration"], h)
        summary_rows.append(_row("anonymization_original", h, s_o))
        summary_rows.append(_row("anonymization_anonymized", h, s_a))
        for c, ic, n in p_o:
            per_anon.append({"horizon_days": h, "cohort": c, "variant": "original", "ic": ic, "n": n})
        for c, ic, n in p_a:
            per_anon.append({"horizon_days": h, "cohort": c, "variant": "anonymized", "ic": ic, "n": n})
    _write_csv(OUT / "l3_anonymization_ic.csv",
               ["horizon_days", "cohort", "variant", "ic", "n"], per_anon)

    (OUT / "summary.json").write_text(json.dumps(summary_rows, indent=2))

    print(f"{'test':>26} | {'h':>4} | {'mean_IC':>9} | {'t(naive)':>8} | {'cohorts':>7} | {'obs':>6} | overlap")
    for r in summary_rows:
        mi = "n/a" if r["mean_ic"] is None else f"{r['mean_ic']:+.4f}"
        ts = "n/a" if r["t_stat_naive"] is None else f"{r['t_stat_naive']:+.2f}"
        print(f"{r['test']:>26} | {r['horizon_days']:>4} | {mi:>9} | {ts:>8} | "
              f"{r['n_cohorts']:>7} | {r['total_obs']:>6} | {r['overlap_warning']}")
    print(f"\nwrote: {OUT}/l3_leading_indicator_ic.csv, {OUT}/l3_anonymization_ic.csv, {OUT}/summary.json")


def _write_csv(path: Path, fields: list[str], rows: list[dict]) -> None:
    with path.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        w.writerows(rows)


if __name__ == "__main__":
    main()
