"""
Combined L1×L3 funnel backtest (Phase 1+2 product test, §14.1) — SPENDS LLM $, capped.

The thesis end-to-end: among CHEAP names (L1), does NON-deteriorating 10-K language (L3
materiality reader) separate winners from value-traps? Mirrors the F-score double-sort,
but with the LLM deterioration score as the quality axis — on the survivorship-clean
2016+ window.

For each filing in the cheapest-P/TBV tercile per cohort: reconstruct the changed MD&A
spans, score deterioration (Sonnet, reusing already-scored pairs), then within each
cohort's cheap bucket report IC(-deterioration) and a low-vs-high-deterioration median
forward-return spread — alongside IC(f_score) on the SAME names for comparison.

HARD CAP: --max-llm-usd / --max-pairs, whichever first (~$0.011/call).

    uv run python scripts/l1l3_funnel_backtest.py --max-pairs 1500 --max-llm-usd 20
"""
from __future__ import annotations

import argparse
import json
import logging
import statistics
from collections import defaultdict
from itertools import zip_longest
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")

import anthropic  # noqa: E402
from deepvalue.diff.align import changed_text  # noqa: E402
from deepvalue.diff.materiality import score_materiality  # noqa: E402
from deepvalue.eval.ic import ICResult, cross_sectional_ic, ic_summary, spearman  # noqa: E402
from deepvalue.ingest.edgar import filings_by_cik  # noqa: E402
from deepvalue.ingest.edgar_filings import (  # noqa: E402
    clean_text,
    extract_sections,
    fetch_filing_document_by_cik,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logging.getLogger("httpx").setLevel(logging.WARNING)
log = logging.getLogger("l1l3")

CACHE = ROOT / "data" / "cache"
PER_CALL_EST = 0.012
RET_CLIP = (-0.95, 4.0)   # winsorize forward returns for magnitude readout (IC is rank-based)


def _clip(x):
    return None if x is None else max(RET_CLIP[0], min(RET_CLIP[1], x))


def _texts_for_cik(cik, tcache):
    if cik in tcache:
        return tcache[cik]
    out = []
    for f in filings_by_cik(cik, forms=("10-K",)):
        try:
            html = fetch_filing_document_by_cik(cik, f["accession"], f["primary_document"])
            out.append((f["filed"], extract_sections(clean_text(html), "10-K").get("mdna")))
        except Exception:
            out.append((f["filed"], None))
    tcache[cik] = out
    return out


def _changed_near(cik, as_of, tcache):
    """Changed MD&A spans for the 10-K filed at/just before the fundamentals as_of date."""
    texts = _texts_for_cik(cik, tcache)
    for (d_new, cur), (_d_old, pri) in zip(texts, texts[1:]):
        if d_new <= as_of and cur and pri:
            return changed_text(cur, pri) or None
    return None


def main() -> None:
    ap = argparse.ArgumentParser(description="Combined L1xL3 funnel backtest (capped LLM)")
    ap.add_argument("--max-pairs", type=int, default=1500)
    ap.add_argument("--max-llm-usd", type=float, default=20.0)
    ap.add_argument("--from-cohort", default="2016", help="survivorship-clean window floor")
    ap.add_argument("--value", default="p_tbv", help="cheapness axis (robust: p_tbv)")
    ap.add_argument("--min-cohort", type=int, default=8)
    args = ap.parse_args()

    l1 = json.loads((CACHE / "l1_backtest.json").read_text())
    manifest = json.loads((CACHE / "manifest.json").read_text())
    sym2cik = {}
    for m in manifest:
        if m.get("cik") and m["symbol"] not in sym2cik:
            sym2cik[m["symbol"]] = m["cik"]
    # reuse deterioration scores already computed by the L3 pilot
    reuse = {}
    p = CACHE / "l3_materiality_pilot.json"
    if p.exists():
        for r in json.loads(p.read_text()):
            reuse[(r["ticker"], r["as_of"])] = r["deterioration"]
    log.info("reusable deterioration scores from L3 pilot: %d", len(reuse))

    # cheapest-value tercile per 2016+ cohort, with a valid value metric + fwd + CIK
    by_cohort: dict[str, list[dict]] = defaultdict(list)
    for r in l1:
        if (r["cohort"] >= args.from_cohort and r.get(args.value) is not None
                and r.get("fwd252") is not None and r["ticker"] in sym2cik):
            by_cohort[r["cohort"]].append(r)
    cheap_by_cohort = {}
    for c, rows in by_cohort.items():
        rows.sort(key=lambda r: r[args.value])
        cheap_by_cohort[c] = rows[: max(args.min_cohort, len(rows) // 3)]
    # round-robin across cohorts so a cap-stopped run still spans years
    work = [r for rnd in zip_longest(*[cheap_by_cohort[c] for c in sorted(cheap_by_cohort, reverse=True)])
            for r in rnd if r]
    log.info("cheap tercile (%s, %s+): %d filing-pairs across %d cohorts | cap $%.2f / %d",
             args.value, args.from_cohort, len(work), len(cheap_by_cohort), args.max_llm_usd, args.max_pairs)

    client = anthropic.Anthropic()
    tcache: dict = {}
    scored, spent, reused_n = [], 0.0, 0
    for r in work:
        key = (r["ticker"], r["as_of"])
        if key in reuse:
            r["deterioration"] = reuse[key]; reused_n += 1
        else:
            if len(scored) - reused_n >= args.max_pairs or spent + PER_CALL_EST > args.max_llm_usd:
                continue
            ct = _changed_near(sym2cik[r["ticker"]], r["as_of"], tcache)
            if not ct:
                continue
            try:
                m = score_materiality(client, ct)
            except Exception as e:
                log.warning("score fail %s %s: %s", r["ticker"], r["as_of"], e); continue
            spent += m.cost_usd; r["deterioration"] = m.deterioration
        scored.append(r)
        if len(scored) % 100 == 0:
            log.info("scored=%d (reused %d) spent=$%.3f", len(scored), reused_n, spent)
            (CACHE / "l1l3_funnel.json").write_text(json.dumps(scored))

    (CACHE / "l1l3_funnel.json").write_text(json.dumps(scored))
    print(f"\n=== COMBINED L1xL3 FUNNEL ({args.from_cohort}+ clean) ===")
    print(f"cheap names scored={len(scored)} (reused {reused_n}, new {len(scored)-reused_n}) "
          f"| LLM spent=${spent:.3f} | cohorts={len({r['cohort'] for r in scored})}")
    print("Within the CHEAP bucket — does the quality axis separate winners from traps?\n")
    print(f"{'horizon':>8} | {'-deterioration IC (L3)':>24} | {'f_score IC (L1)':>18} | "
          f"{'lo-det med':>11} | {'hi-det med':>11} | {'spread':>8}")
    for h in (63, 126, 252):
        det_ics, f_ics, hi, lo = [], [], [], []
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
                det_ics.append(di)
            fi = spearman([r["f_score"] for r in rows if r.get("f_score") is not None],
                          [r[f"fwd{h}"] for r in rows if r.get("f_score") is not None])
            if fi is not None:
                f_ics.append(ICResult(c, fi, len(rows)))
            ds = sorted(rows, key=lambda r: r["deterioration"])
            half = len(ds) // 2
            lo.append(statistics.median([_clip(r[f"fwd{h}"]) for r in ds[:half]]))      # low det
            hi.append(statistics.median([_clip(r[f"fwd{h}"]) for r in ds[len(ds) - half:]]))  # high det
        ds_s = ic_summary(det_ics, horizon_days=h, spacing_days=252)
        fs_s = ic_summary(f_ics, horizon_days=h, spacing_days=252)
        def f(s):
            return (f"{s.mean_ic:+.4f}(t={s.t_stat:+.2f})"
                    if s.mean_ic is not None and s.t_stat is not None else "n/a")
        lo_m = statistics.mean(lo) if lo else None
        hi_m = statistics.mean(hi) if hi else None
        spread = (lo_m - hi_m) if (lo_m is not None and hi_m is not None) else None  # low-det minus high-det
        print(f"{h:>8} | {f(ds_s):>24} | {f(fs_s):>18} | "
              f"{(f'{lo_m*100:+.1f}%' if lo_m is not None else 'n/a'):>11} | "
              f"{(f'{hi_m*100:+.1f}%' if hi_m is not None else 'n/a'):>11} | "
              f"{(f'{spread*100:+.1f}%' if spread is not None else 'n/a'):>8}")
    print(f"\nrecords -> {CACHE / 'l1l3_funnel.json'}")


if __name__ == "__main__":
    main()
