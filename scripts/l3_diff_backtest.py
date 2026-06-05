"""
L3 PRIMARY-EDGE backtest (Phase 1, spec §7/§15) — DETERMINISTIC, $0, no LLM.

Tests the "Lazy Prices" anomaly on our universe: does the YoY change in 10-K language
predict forward returns? Signal = deterministic section similarity (diff/align.py);
returns = the FMP survivorship grab; verdict = cross-sectional IC (eval/ic.py).

No model is involved, so there is NO training-cutoff contamination (temporal.py) — the
test runs over the FULL multi-decade history, which is what makes it statistically
powered. Survivorship-free by construction: it drives off manifest.json (active +
delisted) and fetches filings BY CIK, so dead names are included.

Disqualify-fast gate: if similarity doesn't rank positively with forward return here,
the quiet-language-change edge isn't real on this universe and the agents won't save it.

    uv run python scripts/l3_diff_backtest.py --sample 200            # pilot
    uv run python scripts/l3_diff_backtest.py --sample 2000 --section mdna
"""
from __future__ import annotations

import argparse
import json
import logging
import random
import time
from collections import defaultdict
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")

from deepvalue.diff.align import section_change  # noqa: E402
from deepvalue.eval.ic import (  # noqa: E402
    ICResult,
    cross_sectional_ic,
    forward_return,
    ic_summary,
)
from deepvalue.ingest.edgar import filings_by_cik  # noqa: E402
from deepvalue.ingest.edgar_filings import (  # noqa: E402
    clean_text,
    extract_sections,
    fetch_filing_document_by_cik,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logging.getLogger("httpx").setLevel(logging.WARNING)
log = logging.getLogger("l3_backtest")

CACHE = ROOT / "data" / "cache"
MANIFEST = CACHE / "manifest.json"
DIR_PRICES = CACHE / "prices"

_SEC_MIN_INTERVAL = 0.11   # stay under SEC's ~10 req/s fair-access cap
_last_call = 0.0


def _throttle() -> None:
    global _last_call
    wait = _last_call + _SEC_MIN_INTERVAL - time.monotonic()
    if wait > 0:
        time.sleep(wait)
    _last_call = time.monotonic()


def _price_series(key: str) -> dict[str, dict]:
    """{date: {'close': float}} for one cache key, or {} if absent."""
    f = DIR_PRICES / f"{key}.json"
    if not f.exists():
        return {}
    rows = json.loads(f.read_text()).get("rows", [])
    return {r["date"]: {"close": r.get("close")} for r in rows if r.get("date")}


def _name_records(entry: dict, section: str, horizons: list[int]) -> list[dict]:
    """All consecutive-10-K change records for one company (survivorship-correct, by CIK)."""
    cik = entry.get("cik")
    if not cik:
        return []
    _throttle()
    filings = filings_by_cik(cik, forms=("10-K",))
    if len(filings) < 2:
        return []
    prices = _price_series(entry["key"])
    if not prices:
        return []

    # newest-first; extract each filing's section text once
    texts: list[tuple[str, str | None]] = []
    for fil in filings:
        _throttle()
        try:
            html = fetch_filing_document_by_cik(cik, fil["accession"], fil["primary_document"])
        except Exception as e:  # one bad doc shouldn't kill the name
            log.debug("fetch fail %s %s: %s", cik, fil["accession"], e)
            texts.append((fil["filed"], None))
            continue
        texts.append((fil["filed"], extract_sections(clean_text(html), "10-K").get(section)))

    out = []
    for (d_new, cur), (d_old, pri) in zip(texts, texts[1:]):  # consecutive (new, older)
        if not cur or not pri:
            continue
        chg = section_change(cur, pri)
        if chg is None:
            continue
        rec = {"ticker": entry["symbol"], "as_of": d_new, "cohort": d_new[:4],
               "similarity": chg["similarity"], "changed_frac": chg["changed_frac"]}
        for h in horizons:
            rec[f"fwd{h}"] = forward_return(prices, d_new, h)
        out.append(rec)
    return out


def _ic_by_cohort(records: list[dict], signal: str, h: int, min_cohort: int) -> None:
    """Cross-sectional IC of `signal` vs fwd-h return, grouped by filing-year cohort."""
    by_cohort: dict[str, dict[str, dict]] = defaultdict(dict)
    for r in records:
        by_cohort[r["cohort"]][r["ticker"]] = r   # one 10-K per ticker-year (last wins)

    results = []
    for cohort, names in sorted(by_cohort.items()):
        if len(names) < min_cohort:
            continue
        conv = {t: r[signal] for t, r in names.items()}
        rets = {t: r[f"fwd{h}"] for t, r in names.items()}
        res = cross_sectional_ic(cohort, conv, rets)
        if res.ic is not None:
            results.append(res)
    summ = ic_summary(results, horizon_days=h, spacing_days=252)  # cohorts ~1yr apart
    mean = f"{summ.mean_ic:+.4f}" if summ.mean_ic is not None else "n/a"
    t = f"{summ.t_stat:+.2f}" if summ.t_stat is not None else "n/a"
    print(f"  {signal:13} vs fwd{h:>3}d | cohorts={summ.n_dates:>2} obs={summ.total_obs:>5} "
          f"| mean_IC={mean}  t={t}")


def main() -> None:
    ap = argparse.ArgumentParser(description="L3 deterministic diff-anomaly backtest ($0)")
    ap.add_argument("--sample", type=int, default=200, help="random names from the universe")
    ap.add_argument("--seed", type=int, default=1)
    ap.add_argument("--section", choices=["mdna", "risk_factors"], default="mdna")
    ap.add_argument("--horizons", default="63,126,252", help="forward trading-day windows")
    ap.add_argument("--min-cohort", type=int, default=5, help="min names/cohort for an IC date")
    args = ap.parse_args()
    horizons = [int(x) for x in args.horizons.split(",")]

    manifest = json.loads(MANIFEST.read_text())
    pool = [m for m in manifest if m.get("cik") and m.get("n_price", 0) > 0]
    random.seed(args.seed)
    sample = random.sample(pool, min(args.sample, len(pool)))
    log.info("universe pool=%d | sampling %d | section=%s", len(pool), len(sample), args.section)

    records, n_with_data = [], 0
    for i, entry in enumerate(sample, 1):
        recs = _name_records(entry, args.section, horizons)
        if recs:
            n_with_data += 1
            records.extend(recs)
        if i % 25 == 0:
            log.info("  processed %d/%d names | %d filing-pairs so far", i, len(sample), len(records))

    print(f"\n=== L3 deterministic diff anomaly | section={args.section} ===")
    print(f"names sampled={len(sample)} | with usable pairs={n_with_data} | "
          f"filing-pairs={len(records)} | cohorts={len({r['cohort'] for r in records})}")
    print("Lazy Prices prediction: similarity ranks POSITIVE with forward return "
          "(more YoY rewriting -> lower returns).")
    for h in horizons:
        _ic_by_cohort(records, "similarity", h, args.min_cohort)
    # changed_frac is the inverse signal; one line as a sanity mirror
    _ic_by_cohort(records, "changed_frac", horizons[-1], args.min_cohort)

    out = CACHE / f"l3_backtest_{args.section}.json"
    out.write_text(json.dumps(records, indent=2))
    print(f"records -> {out}")


if __name__ == "__main__":
    main()
