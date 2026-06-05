"""
L3 materiality PILOT (Phase 1b — SPENDS LLM $, hard-capped). Tests the thesis's core
claim: does TYPING the YoY changes (Sonnet reads which are negative) beat the raw
deterministic similarity signal (IC ~+0.03)?

For each filing-pair already in the deterministic backtest records, reconstruct the
changed MD&A spans from the cached filings, score deterioration with Sonnet 4.6
(diff/materiality.py), and compare — on the SAME pairs — the cross-sectional IC of the
typed signal vs the raw similarity baseline. Recent cohorts first (more names/cohort).

HARD CAP: stops before --max-llm-usd or --max-pairs, whichever first. ~$0.010/call.

    uv run python scripts/l3_materiality_pilot.py --max-pairs 800 --max-llm-usd 20
"""
from __future__ import annotations

import argparse
import json
import logging
from collections import defaultdict
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
log = logging.getLogger("l3_materiality")

CACHE = ROOT / "data" / "cache"
PER_CALL_EST = 0.012   # conservative per-call estimate for the pre-call budget gate


def _texts_for_cik(cik: str, cache: dict[str, list]) -> list[tuple[str, str | None]]:
    if cik in cache:
        return cache[cik]
    out = []
    for f in filings_by_cik(cik, forms=("10-K",)):
        try:
            html = fetch_filing_document_by_cik(cik, f["accession"], f["primary_document"])
            out.append((f["filed"], extract_sections(clean_text(html), "10-K").get("mdna")))
        except Exception:
            out.append((f["filed"], None))
    cache[cik] = out
    return out


def _changed_for_record(cik: str, as_of: str, tcache: dict) -> str | None:
    """Rebuild the changed MD&A spans for the pair whose NEWER 10-K filed on `as_of`."""
    texts = _texts_for_cik(cik, tcache)
    for (d_new, cur), (_d_old, pri) in zip(texts, texts[1:]):
        if d_new == as_of and cur and pri:
            return changed_text(cur, pri) or None
    return None


def _ic(scored: list[dict], signal_fn, h: int, min_cohort: int):
    """Cross-sectional IC by cohort of signal_fn(record) vs fwd-h return."""
    by_cohort: dict[str, dict[str, dict]] = defaultdict(dict)
    for r in scored:
        if r.get(f"fwd{h}") is not None:
            by_cohort[r["cohort"]][r["ticker"]] = r
    res = []
    for c, names in by_cohort.items():
        if len(names) < min_cohort:
            continue
        conv = {t: signal_fn(r) for t, r in names.items()}
        rets = {t: r[f"fwd{h}"] for t, r in names.items()}
        ic = cross_sectional_ic(c, conv, rets)
        if ic.ic is not None:
            res.append(ic)
    return ic_summary(res, horizon_days=h, spacing_days=252)


def main() -> None:
    ap = argparse.ArgumentParser(description="L3 materiality pilot (LLM, hard-capped)")
    ap.add_argument("--records", default=str(CACHE / "l3_backtest_mdna_masked.json"))
    ap.add_argument("--max-pairs", type=int, default=800)
    ap.add_argument("--max-llm-usd", type=float, default=20.0)
    ap.add_argument("--recent-from", default="2015", help="cohort year floor (more names/cohort)")
    ap.add_argument("--min-cohort", type=int, default=5)
    args = ap.parse_args()

    records = json.loads(Path(args.records).read_text())
    manifest = json.loads((CACHE / "manifest.json").read_text())
    sym2cik = {}
    for m in manifest:                       # first cik seen per symbol
        if m.get("cik") and m["symbol"] not in sym2cik:
            sym2cik[m["symbol"]] = m["cik"]

    # Require fwd252 present (so every scored pair contributes to ALL horizon ICs — a
    # 252d window implies the 63/126d ones exist). Then INTERLEAVE across cohorts
    # round-robin: a single recent year has enough names to exhaust the budget on its
    # own, which would leave one cohort and no cross-date t-stat. Round-robin spends the
    # budget evenly across years, so even a partial (cap-stopped) run has breadth.
    from itertools import zip_longest
    pool = [r for r in records if r["cohort"] >= args.recent_from
            and r.get("fwd252") is not None and r["ticker"] in sym2cik]
    by_c: dict[str, list] = defaultdict(list)
    for r in pool:
        by_c[r["cohort"]].append(r)
    for c in by_c:
        by_c[c].sort(key=lambda r: r["ticker"])
    cohorts_desc = sorted(by_c, reverse=True)
    work = [r for rnd in zip_longest(*[by_c[c] for c in cohorts_desc]) for r in rnd if r]
    log.info("candidate pairs: %d across %d cohorts (round-robin) | cap=$%.2f / %d pairs",
             len(work), len(cohorts_desc), args.max_llm_usd, args.max_pairs)

    client = anthropic.Anthropic()
    tcache: dict[str, list] = {}
    scored, spent = [], 0.0
    out_path = CACHE / "l3_materiality_pilot.json"

    for rec in work:
        if len(scored) >= args.max_pairs or spent + PER_CALL_EST > args.max_llm_usd:
            log.info("STOP: pairs=%d spent=$%.3f (cap $%.2f / %d)",
                     len(scored), spent, args.max_llm_usd, args.max_pairs)
            break
        ct = _changed_for_record(sym2cik[rec["ticker"]], rec["as_of"], tcache)
        if not ct:
            continue
        try:
            m = score_materiality(client, ct)
        except Exception as e:
            log.warning("score failed %s %s: %s", rec["ticker"], rec["as_of"], e)
            continue
        spent += m.cost_usd
        scored.append({
            "ticker": rec["ticker"], "as_of": rec["as_of"], "cohort": rec["cohort"],
            "similarity": rec["similarity"], "deterioration": m.deterioration,
            "removed_reassurance": m.removed_reassurance, "categories": m.categories,
            "fwd63": rec.get("fwd63"), "fwd126": rec.get("fwd126"), "fwd252": rec.get("fwd252"),
        })
        if len(scored) % 50 == 0:
            log.info("scored=%d spent=$%.3f", len(scored), spent)
            out_path.write_text(json.dumps(scored, indent=2))

    out_path.write_text(json.dumps(scored, indent=2))
    n_rr = sum(1 for r in scored if r["removed_reassurance"])
    print(f"\n=== L3 MATERIALITY PILOT ===  scored={len(scored)} pairs | spent=${spent:.3f} "
          f"| removed_reassurance flagged on {n_rr} ({n_rr/max(1,len(scored))*100:.0f}%)")
    print(f"cohorts={len({r['cohort'] for r in scored})} | records -> {out_path}")
    print("\nCross-sectional IC on the SAME scored pairs — typed signal vs raw-similarity baseline:")
    print(f"{'horizon':>8} | {'similarity (det.)':>20} | {'-deterioration (LLM)':>22} | "
          f"{'-removed_reassur (LLM)':>24}")
    for h in (63, 126, 252):
        base = _ic(scored, lambda r: r["similarity"], h, args.min_cohort)
        det = _ic(scored, lambda r: -r["deterioration"], h, args.min_cohort)
        rr = _ic(scored, lambda r: -1.0 if r["removed_reassurance"] else 0.0, h, args.min_cohort)
        def f(s):
            return (f"{s.mean_ic:+.4f}(t={s.t_stat:+.2f})"
                    if s.mean_ic is not None and s.t_stat is not None else "n/a")
        print(f"{h:>8} | {f(base):>20} | {f(det):>22} | {f(rr):>24}")


if __name__ == "__main__":
    main()
