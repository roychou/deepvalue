"""
EVENTS deterioration baseline ($0, no SEC, no LLM) — the free analog of the L3 test.

Within the cheapest-P/TBV tercile per cohort (Sharadar, ~28 survivorship-free cohorts),
flag names that had a HARD distress event in the trailing 12 months (point-in-time, from
Sharadar EVENTS 8-K codes: bankruptcy/restatement/auditor-change/default/delisting-notice/
impairment), and ask: do flagged names underperform, and does the flag add over Piotroski-F?

If even a hard-event flag adds nothing here, the subtle-language L3 version is a long shot;
if it adds, L3 (earlier warning via MD&A wording) is worth the ~$30 EDGAR+LLM test.

    uv run python scripts/l1_events_baseline.py
"""
from __future__ import annotations

import statistics
import sys
from collections import defaultdict
from pathlib import Path

import duckdb

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
from deepvalue.eval.ic import ICResult, ic_summary, spearman  # noqa: E402

CACHE = ROOT / "data" / "cache"
RECORDS = CACHE / "l1_sharadar_backtest.json"
DB = CACHE / "sharadar.duckdb"
DISTRESS = ["13", "42", "41", "24", "31", "26"]   # bankruptcy/restatement/auditor/default/delist/impair
RET_CLIP = (-0.95, 4.0)


def _clip(x):
    return max(RET_CLIP[0], min(RET_CLIP[1], x))


def main() -> None:
    con = duckdb.connect(str(DB), read_only=True)
    like = " OR ".join(f"'|'||e.eventcodes||'|' LIKE '%|{c}|%'" for c in DISTRESS)
    # cheapest p/tbv tercile per cohort; LEFT JOIN any trailing-12m distress event (point-in-time)
    q = f"""
    WITH recs AS (
        SELECT ticker, cohort, as_of, p_tbv, f_score, fwd63, fwd126, fwd252
        FROM read_json_auto('{RECORDS}')
        WHERE p_tbv IS NOT NULL AND p_tbv > 0 AND fwd252 IS NOT NULL
    ),
    cheap AS (
        SELECT * FROM recs
        QUALIFY ntile(3) OVER (PARTITION BY cohort ORDER BY p_tbv) = 1
    )
    SELECT c.ticker, c.cohort, c.f_score, c.fwd63, c.fwd126, c.fwd252,
           CASE WHEN EXISTS (
               SELECT 1 FROM events e
               WHERE e.ticker = c.ticker
                 AND e.date >= (c.as_of::DATE - INTERVAL 365 DAY) AND e.date <= c.as_of::DATE
                 AND ({like})
           ) THEN 1 ELSE 0 END AS distress
    FROM cheap c
    """
    rows = con.execute(q).fetchall()
    cols = ["ticker", "cohort", "f_score", "fwd63", "fwd126", "fwd252", "distress"]
    recs = [dict(zip(cols, r)) for r in rows]
    con.close()

    n_flag = sum(r["distress"] for r in recs)
    print(f"=== EVENTS DETERIORATION BASELINE (cheap tercile, Sharadar 28-cohort) ===")
    print(f"cheap filing-records: {len(recs)} | with trailing-12m distress event: "
          f"{n_flag} ({n_flag/len(recs)*100:.1f}%)\n")

    print(f"{'horizon':>8} | {'-distress IC':>20} | {'f_score IC':>18} | "
          f"{'flagged med':>12} | {'clean med':>11} | {'spread':>8}")
    for h in (63, 126, 252):
        dist_ic, f_ic, flg, cln = [], [], [], []
        byc = defaultdict(list)
        for r in recs:
            if r.get(f"fwd{h}") is not None:
                byc[r["cohort"]].append(r)
        for c, rs in byc.items():
            if len(rs) < 20:
                continue
            ic = spearman([-r["distress"] for r in rs], [r[f"fwd{h}"] for r in rs])
            if ic is not None:
                dist_ic.append(ICResult(c, ic, len(rs)))
            fi = spearman([r["f_score"] for r in rs if r["f_score"] is not None],
                          [r[f"fwd{h}"] for r in rs if r["f_score"] is not None])
            if fi is not None:
                f_ic.append(ICResult(c, fi, len(rs)))
            f_ret = [_clip(r[f"fwd{h}"]) for r in rs if r["distress"]]
            c_ret = [_clip(r[f"fwd{h}"]) for r in rs if not r["distress"]]
            if f_ret and c_ret:
                flg.append(statistics.median(f_ret)); cln.append(statistics.median(c_ret))
        di, fis = ic_summary(dist_ic, horizon_days=h, spacing_days=252), ic_summary(f_ic, horizon_days=h, spacing_days=252)
        fmt = lambda s: (f"{s.mean_ic:+.4f}(t={s.t_stat:+.1f})" if s.mean_ic is not None and s.t_stat is not None else "n/a")  # noqa: E731
        fm = statistics.mean(flg) if flg else None
        cm = statistics.mean(cln) if cln else None
        sp = (fm - cm) if (fm is not None and cm is not None) else None
        print(f"{h:>8} | {fmt(di):>20} | {fmt(fis):>18} | "
              f"{(f'{fm*100:+.1f}%' if fm is not None else 'n/a'):>12} | "
              f"{(f'{cm*100:+.1f}%' if cm is not None else 'n/a'):>11} | "
              f"{(f'{sp*100:+.1f}%' if sp is not None else 'n/a'):>8}")
    print("\n(-distress IC > 0 / negative spread = flagged cheap names underperform -> the flag adds.)")


if __name__ == "__main__":
    main()
