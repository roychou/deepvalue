"""
L3 re-validation on Sonnet 5 — paired A/B against the validated sonnet-4-6 panel.

models.py's contract: changing ROOT is a deploy, and a deploy means re-running validation.
ROOT moved claude-sonnet-4-6 -> claude-sonnet-5 (2 Jul 2026), so this script re-scores the
SAME 1512 filing-pairs from the committed leading-indicator panel with the new model and
recomputes the per-cohort IC on the paired subset — old score vs new score on identical
names/dates/returns, so any delta is the MODEL, not the sample.

Never touches data/cache/l1l3_sharadar.json (the validated panel). New scores go to
data/cache/l1l3_sharadar_sonnet5.json, incrementally (resumable).

SPENDS LLM $ — hard --max-llm-usd gate, metered at the models.py sticker price ($3/$15;
Sonnet 5 intro pricing bills ~2/3 of the meter through 2026-08-31).

    uv run python scripts/l3_sonnet5_revalidation.py --limit 3            # smoke test
    uv run python scripts/l3_sonnet5_revalidation.py --max-llm-usd 20     # the run
    uv run python scripts/l3_sonnet5_revalidation.py --report-only        # free: recompute tables
"""
from __future__ import annotations

import argparse
import json
import logging
import statistics
import threading
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from dotenv import load_dotenv

ROOT_DIR = Path(__file__).resolve().parent.parent
load_dotenv(ROOT_DIR / ".env")

import duckdb  # noqa: E402
from deepvalue.diff.align import changed_text  # noqa: E402
from deepvalue.eval.ic import ICResult, cross_sectional_ic, ic_summary, spearman  # noqa: E402
from deepvalue.ingest.edgar import filings_by_cik  # noqa: E402
from deepvalue.ingest.edgar_filings import clean_text, extract_sections, fetch_filing_document_by_cik  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logging.getLogger("httpx").setLevel(logging.WARNING)
log = logging.getLogger("l3_s5_reval")

CACHE = ROOT_DIR / "data" / "cache"
OLD_PANEL = CACHE / "l1l3_sharadar.json"
NEW_PANEL = CACHE / "l1l3_sharadar_sonnet5.json"
HORIZONS = (63, 126, 252)
MIN_COHORT = 8                      # same gate as the backtest + regenerate_results.py
_DISTRESS = ["13", "42", "41", "24", "31", "26"]   # Sharadar EVENTS hard-distress codes


def _mdna(cik: str, f: dict, tcache: dict):
    key = f["accession"]
    if key not in tcache:
        try:
            html = fetch_filing_document_by_cik(cik, f["accession"], f["primary_document"])
            tcache[key] = extract_sections(clean_text(html), "10-K").get("mdna")
        except Exception:  # noqa: BLE001 — a fetch/extract miss just skips the row
            tcache[key] = None
    return tcache[key]


def _changed_near(cik: str, as_of: str, tcache: dict):
    """Same pairing rule as the original backtest: the 10-K at/just before as_of vs its prior."""
    fils = filings_by_cik(cik, forms=("10-K",))
    idx = next((i for i, f in enumerate(fils) if f["filed"] <= as_of), None)
    if idx is None or idx + 1 >= len(fils):
        return None
    cur, pri = _mdna(cik, fils[idx], tcache), _mdna(cik, fils[idx + 1], tcache)
    return changed_text(cur, pri) or None if (cur and pri) else None


def _attach_distress(rows: list[dict]) -> None:
    con = duckdb.connect(str(CACHE / "sharadar.duckdb"), read_only=True)
    like = " OR ".join(f"'|'||eventcodes||'|' LIKE '%|{c}|%'" for c in _DISTRESS)
    for r in rows:
        n = con.execute(
            f"SELECT count(*) FROM events WHERE ticker = ? AND date >= (?::DATE - INTERVAL 365 DAY) "
            f"AND date <= ?::DATE AND ({like})", [r["ticker"], r["as_of"], r["as_of"]]).fetchone()[0]
        r["distress"] = 1 if n else 0
    con.close()


def _score_all(rows: list[dict], cap_usd: float, workers: int, limit: int = 0) -> None:
    """Score rows lacking deterioration_s5 in place, thread-pooled, hard-capped. Saves NEW_PANEL
    incrementally so an interrupt (or the cap) loses nothing. `rows` is ALWAYS the full panel —
    `limit` caps the work queue, never the saved file."""
    import anthropic

    from deepvalue.diff.materiality import score_materiality

    client = anthropic.Anthropic()
    lock = threading.Lock()
    spent = sum(r.get("cost_s5", 0.0) for r in rows)   # resume-aware
    todo = [r for r in rows if r.get("deterioration_s5") is None and not r.get("s5_failed")]
    if limit:
        todo = todo[:limit]
    log.info("to score: %d rows (%.2f USD already metered)", len(todo), spent)
    if not todo:
        return
    # Warm the prompt cache with one sequential call so the fan-out reads, not re-writes, it.
    first, rest = todo[0], todo[1:]
    _score_one(first, client, score_materiality)
    spent += first.get("cost_s5", 0.0)
    done = 1

    def run(r):
        _score_one(r, client, score_materiality)
        return r

    stop = False
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futs = {}
        it = iter(rest)
        # keep ~2x workers in flight; stop submitting once the cap is hit
        def submit_some():
            nonlocal stop
            while not stop and len(futs) < workers * 2:
                r = next(it, None)
                if r is None:
                    return
                futs[pool.submit(run, r)] = r
        submit_some()
        while futs:
            for f in as_completed(list(futs), timeout=600):
                futs.pop(f)
                r = f.result()
                with lock:
                    spent += r.get("cost_s5", 0.0)
                    done += 1
                    if done % 50 == 0:
                        NEW_PANEL.write_text(json.dumps(rows))
                        log.info("scored=%d/%d metered=$%.2f", done, len(todo), spent)
                    if spent >= cap_usd:
                        stop = True
                break
            if stop:
                for f in futs:
                    f.cancel()
                log.info("HARD CAP hit: metered=$%.2f >= $%.2f — stopping", spent, cap_usd)
                break
            submit_some()
    NEW_PANEL.write_text(json.dumps(rows))
    log.info("scoring done: %d newly scored, metered total=$%.2f", done, spent)


def _score_one(r: dict, client, score_materiality) -> None:
    ct = _changed_near(r["cik"], r["as_of"], {})
    if not ct:
        r["s5_failed"] = "no_changed_text"
        return
    try:
        m = score_materiality(client, ct)   # model defaults to ROOT.id == claude-sonnet-5
    except Exception as e:  # noqa: BLE001 — record and move on; the pair just drops out
        log.warning("score fail %s %s: %s", r["ticker"], r["as_of"], type(e).__name__)
        r["s5_failed"] = type(e).__name__
        return
    r["deterioration_s5"] = m.deterioration
    r["cost_s5"] = m.cost_usd


def _table(rows: list[dict]) -> None:
    """Paired IC comparison — identical names/dates/returns, only the scorer differs."""
    paired = [r for r in rows if r.get("deterioration") is not None
              and r.get("deterioration_s5") is not None]
    if not paired:
        print("no paired rows yet"); return
    o = [r["deterioration"] for r in paired]
    n = [r["deterioration_s5"] for r in paired]
    print(f"\n=== L3 re-validation: sonnet-4-6 (validated) vs sonnet-5 — paired n={len(paired)} ===")
    print(f"score agreement: spearman={spearman(o, n):+.3f} | "
          f"mean old={statistics.mean(o):.3f} new={statistics.mean(n):.3f}")
    for cut, name in ((paired, "ALL cheap names"),
                      ([r for r in paired if not r.get("distress")],
                       "EVENT-CLEAN (no trailing-12m hard distress — the committed headline cut)")):
        print(f"\n--- {name} (n={len(cut)}) ---")
        print(f"{'horizon':>8} | {'IC -det (sonnet-4-6)':>22} | {'IC -det (sonnet-5)':>22}")
        for h in HORIZONS:
            byc = defaultdict(list)
            for r in cut:
                if r.get(f"fwd{h}") is not None:
                    byc[r["cohort"]].append(r)
            def _ic(key):
                res = []
                for c, rs in byc.items():
                    if len(rs) < MIN_COHORT:
                        continue
                    d = cross_sectional_ic(c, {r["ticker"]: -r[key] for r in rs},
                                           {r["ticker"]: r[f"fwd{h}"] for r in rs})
                    if d.ic is not None:
                        res.append(d)
                s = ic_summary(res, horizon_days=h, spacing_days=252)
                return (f"{s.mean_ic:+.4f}(t={s.t_stat:+.1f},k={len(res)})"
                        if s.mean_ic is not None and s.t_stat is not None else "n/a")
            print(f"{h:>8} | {_ic('deterioration'):>22} | {_ic('deterioration_s5'):>22}")


def main() -> None:
    ap = argparse.ArgumentParser(description="Re-score the L3 panel on Sonnet 5 (paired A/B)")
    ap.add_argument("--max-llm-usd", type=float, default=20.0)
    ap.add_argument("--limit", type=int, default=0, help="score only N rows (smoke test)")
    ap.add_argument("--workers", type=int, default=5)
    ap.add_argument("--report-only", action="store_true", help="no scoring, just the tables")
    args = ap.parse_args()

    if NEW_PANEL.exists():                       # resume from prior partial run
        rows = json.loads(NEW_PANEL.read_text())
        log.info("resuming: %s (%d rows)", NEW_PANEL.name, len(rows))
    else:
        rows = json.loads(OLD_PANEL.read_text())
        log.info("fresh start from %s (%d rows)", OLD_PANEL.name, len(rows))

    if not args.report_only:
        _score_all(rows, args.max_llm_usd, args.workers, limit=args.limit)

    _attach_distress(rows)
    _table(rows)
    n_ok = sum(1 for r in rows if r.get("deterioration_s5") is not None)
    metered = sum(r.get("cost_s5", 0.0) for r in rows)
    print(f"\nscored {n_ok}/{len(rows)} | metered ${metered:.2f} at sticker "
          f"(intro pricing bills ~2/3 of this through 2026-08-31)")


if __name__ == "__main__":
    main()
