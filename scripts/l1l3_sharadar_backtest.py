"""
L1×L3 on SHARADAR (28 cohorts) — the definitive test of whether the LLM materiality
signal adds incremental value over the free Piotroski-F quality signal, WITH POWER.

The earlier L1×L3 was 10 underpowered FMP cohorts ("no clear add"). This runs the same
test on the survivorship-free Sharadar universe (~28 cohorts): within the cheapest-P/TBV
tercile per cohort, score L3 deterioration on the 10-K MD&A and compare IC(-deterioration)
vs IC(f_score) on the same names, plus a low-vs-high-deterioration return spread.

Sharadar gives the cheap-name set + fundamentals; EDGAR (by CIK, from TICKERS.secfilings)
gives the 10-K text. SPENDS LLM $ — hard --max-llm-usd / --max-pairs gate.

    uv run python scripts/l1l3_sharadar_backtest.py --count-only        # free: size the cost
    uv run python scripts/l1l3_sharadar_backtest.py --per-cohort 100 --max-llm-usd 40
"""
from __future__ import annotations

import argparse
import json
import logging
import re
import statistics
from collections import defaultdict
from itertools import zip_longest
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")

import duckdb  # noqa: E402
from deepvalue.diff.align import changed_text  # noqa: E402
from deepvalue.eval.ic import ICResult, cross_sectional_ic, ic_summary, spearman  # noqa: E402
from deepvalue.ingest.edgar import filings_by_cik  # noqa: E402
from deepvalue.ingest.edgar_filings import clean_text, extract_sections, fetch_filing_document_by_cik  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logging.getLogger("httpx").setLevel(logging.WARNING)
log = logging.getLogger("l1l3_sharadar")
CACHE = ROOT / "data" / "cache"
PER_CALL_EST = 0.012
RET_CLIP = (-0.95, 4.0)
_CIK_RE = re.compile(r"CIK=(\d+)")


def _clip(x):
    return None if x is None else max(RET_CLIP[0], min(RET_CLIP[1], x))


def _cik_map() -> dict[str, str]:
    con = duckdb.connect(str(CACHE / "sharadar.duckdb"), read_only=True)
    out = {}
    for tk, sf in con.execute("SELECT ticker, secfilings FROM tickers WHERE secfilings IS NOT NULL").fetchall():
        m = _CIK_RE.search(sf or "")
        if m and tk not in out:
            out[tk] = m.group(1)
    con.close()
    return out


def _cheap_work(per_cohort: int, from_cohort: str, cikmap: dict) -> list[dict]:
    recs = json.loads((CACHE / "l1_sharadar_backtest.json").read_text())
    byc: dict[str, list[dict]] = defaultdict(list)
    for r in recs:
        if (r["cohort"] >= from_cohort and r.get("p_tbv") is not None
                and r.get("fwd252") is not None and r["ticker"] in cikmap):
            r["cik"] = cikmap[r["ticker"]]
            byc[r["cohort"]].append(r)
    cheap = {}
    for c, rows in byc.items():
        rows.sort(key=lambda r: r["p_tbv"])
        cheap[c] = rows[:per_cohort]
    return [r for rnd in zip_longest(*[cheap[c] for c in sorted(cheap, reverse=True)]) for r in rnd if r]


def _changed_near(cik, as_of, tcache):
    if cik not in tcache:
        out = []
        for f in filings_by_cik(cik, forms=("10-K",)):
            try:
                html = fetch_filing_document_by_cik(cik, f["accession"], f["primary_document"])
                out.append((f["filed"], extract_sections(clean_text(html), "10-K").get("mdna")))
            except Exception:
                out.append((f["filed"], None))
        tcache[cik] = out
    for (d_new, cur), (_d, pri) in zip(tcache[cik], tcache[cik][1:]):
        if d_new <= as_of and cur and pri:
            return changed_text(cur, pri) or None
    return None


def main() -> None:
    ap = argparse.ArgumentParser(description="L1xL3 on Sharadar (28 cohorts, capped LLM)")
    ap.add_argument("--per-cohort", type=int, default=100, help="cheap names scored per cohort")
    ap.add_argument("--from-cohort", default="1999")
    ap.add_argument("--max-pairs", type=int, default=3500)
    ap.add_argument("--max-llm-usd", type=float, default=40.0)
    ap.add_argument("--min-cohort", type=int, default=8)
    ap.add_argument("--count-only", action="store_true", help="size the cost, spend nothing")
    args = ap.parse_args()

    cikmap = _cik_map()
    work = _cheap_work(args.per_cohort, args.from_cohort, cikmap)
    cohorts = len({r["cohort"] for r in work})
    log.info("cheap tercile (p_tbv, %s+, cik known): %d names across %d cohorts",
             len(work), cohorts, args.from_cohort)
    if args.count_only:
        n = min(len(work), args.max_pairs)
        print(f"\nWould score {n} names across {cohorts} cohorts "
              f"(~{args.per_cohort}/cohort) -> est ${n * 0.011:.0f} (cap ${args.max_llm_usd:.0f})")
        return

    import anthropic
    from deepvalue.diff.materiality import score_materiality
    client = anthropic.Anthropic()
    tcache: dict = {}
    scored, spent = [], 0.0
    for r in work:
        if len(scored) >= args.max_pairs or spent + PER_CALL_EST > args.max_llm_usd:
            log.info("STOP: scored=%d spent=$%.3f", len(scored), spent); break
        ct = _changed_near(r["cik"], r["as_of"], tcache)
        if not ct:
            continue
        try:
            m = score_materiality(client, ct)
        except Exception as e:
            log.warning("score fail %s %s: %s", r["ticker"], r["as_of"], e); continue
        spent += m.cost_usd
        r["deterioration"] = m.deterioration
        scored.append(r)
        if len(scored) % 100 == 0:
            log.info("scored=%d spent=$%.3f", len(scored), spent)
            (CACHE / "l1l3_sharadar.json").write_text(json.dumps(scored))

    (CACHE / "l1l3_sharadar.json").write_text(json.dumps(scored))
    print(f"\n=== L1xL3 on SHARADAR ({args.from_cohort}+, survivorship-free) ===")
    print(f"cheap names scored={len(scored)} | LLM spent=${spent:.3f} | "
          f"cohorts={len({r['cohort'] for r in scored})}")
    print("Within the CHEAP bucket — does L3 deterioration add over free f_score?\n")
    print(f"{'horizon':>8} | {'-deterioration IC':>22} | {'f_score IC':>18} | "
          f"{'lo-det med':>11} | {'hi-det med':>11} | {'spread':>8}")
    for h in (63, 126, 252):
        det, fic, hi, lo = [], [], [], []
        byc = defaultdict(list)
        for r in scored:
            if r.get(f"fwd{h}") is not None and r.get("deterioration") is not None:
                byc[r["cohort"]].append(r)
        for c, rows in byc.items():
            if len(rows) < args.min_cohort:
                continue
            rets = {r["ticker"]: r[f"fwd{h}"] for r in rows}
            di = cross_sectional_ic(c, {r["ticker"]: -r["deterioration"] for r in rows}, rets)
            if di.ic is not None:
                det.append(di)
            fi = spearman([r["f_score"] for r in rows], [r[f"fwd{h}"] for r in rows])
            if fi is not None:
                fic.append(ICResult(c, fi, len(rows)))
            ds = sorted(rows, key=lambda r: r["deterioration"]); half = len(ds) // 2
            lo.append(statistics.median([_clip(r[f"fwd{h}"]) for r in ds[:half]]))
            hi.append(statistics.median([_clip(r[f"fwd{h}"]) for r in ds[len(ds) - half:]]))
        ds_s, fs_s = ic_summary(det, horizon_days=h, spacing_days=252), ic_summary(fic, horizon_days=h, spacing_days=252)
        f = lambda s: (f"{s.mean_ic:+.4f}(t={s.t_stat:+.1f})" if s.mean_ic is not None and s.t_stat is not None else "n/a")  # noqa: E731
        lo_m, hi_m = (statistics.mean(lo) if lo else None), (statistics.mean(hi) if hi else None)
        sp = (lo_m - hi_m) if (lo_m is not None and hi_m is not None) else None
        print(f"{h:>8} | {f(ds_s):>22} | {f(fs_s):>18} | "
              f"{(f'{lo_m*100:+.1f}%' if lo_m is not None else 'n/a'):>11} | "
              f"{(f'{hi_m*100:+.1f}%' if hi_m is not None else 'n/a'):>11} | "
              f"{(f'{sp*100:+.1f}%' if sp is not None else 'n/a'):>8}")
    print(f"\nrecords -> {CACHE / 'l1l3_sharadar.json'}")


if __name__ == "__main__":
    main()
