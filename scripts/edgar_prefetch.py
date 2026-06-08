"""
Gentle EDGAR pre-fetch (phase 1 of the two-phase L3 test) — cache the 10-K MD&A docs for
the Sharadar cheap-tercile names POLITELY, so the bulk crawl doesn't trip SEC's ~10-min
rate block (which silently returned [] for 77% of names in the one-shot attempt).

For each cheap name: fetch the submissions blob (retried) + the TWO 10-Ks around as_of,
into the filings cache. ~3 req/s, resumable (skips already-cached docs). No LLM. After this,
l1l3_sharadar_backtest.py scores from cache with zero SEC contention.

    uv run python scripts/edgar_prefetch.py --per-cohort 100
"""
from __future__ import annotations

import argparse
import json
import logging
import re
import sys
import time
from collections import defaultdict
from itertools import zip_longest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
from dotenv import load_dotenv  # noqa: E402
load_dotenv(ROOT / ".env")     # EDGAR_USER_AGENT — without this every SEC call hard-fails
import duckdb  # noqa: E402
from deepvalue.ingest.edgar import filings_by_cik  # noqa: E402
from deepvalue.ingest.edgar_filings import FILINGS_DIR, fetch_filing_document_by_cik  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logging.getLogger("httpx").setLevel(logging.WARNING)
log = logging.getLogger("edgar_prefetch")
CACHE = ROOT / "data" / "cache"
_CIK_RE = re.compile(r"CIK=(\d+)")


def _cheap_work(per_cohort: int, from_cohort: str) -> list[dict]:
    con = duckdb.connect(str(CACHE / "sharadar.duckdb"), read_only=True)
    cikmap = {}
    for tk, sf in con.execute("SELECT ticker, secfilings FROM tickers WHERE secfilings IS NOT NULL").fetchall():
        m = _CIK_RE.search(sf or "")
        if m and tk not in cikmap:
            cikmap[tk] = m.group(1)
    con.close()
    recs = json.loads((CACHE / "l1_sharadar_backtest.json").read_text())
    byc: dict[str, list[dict]] = defaultdict(list)
    for r in recs:
        if (r["cohort"] >= from_cohort and r.get("p_tbv") and r["p_tbv"] > 0
                and r.get("fwd252") is not None and r["ticker"] in cikmap):
            r["cik"] = cikmap[r["ticker"]]
            byc[r["cohort"]].append(r)
    cheap = {}
    for c, rows in byc.items():
        rows.sort(key=lambda r: r["p_tbv"])
        cheap[c] = rows[:per_cohort]
    return [r for rnd in zip_longest(*[cheap[c] for c in sorted(cheap, reverse=True)]) for r in rnd if r]


def _doc_cached(cik: str, accession: str) -> bool:
    return (FILINGS_DIR / f"CIK{str(cik).zfill(10)}_{accession}.htm").exists()


def main() -> None:
    ap = argparse.ArgumentParser(description="Gentle EDGAR pre-fetch for L3 (phase 1)")
    ap.add_argument("--per-cohort", type=int, default=100)
    ap.add_argument("--from-cohort", default="1999")
    ap.add_argument("--sleep", type=float, default=0.2, help="extra pause/name on top of edgar throttle")
    ap.add_argument("--retries", type=int, default=3)
    args = ap.parse_args()

    work = _cheap_work(args.per_cohort, args.from_cohort)
    log.info("cheap names to pre-fetch: %d", len(work))
    ok = blocked = nopair = 0
    for i, r in enumerate(work, 1):
        cik, as_of = r["cik"], r["as_of"]
        fils = []
        for a in range(args.retries):
            fils = filings_by_cik(cik, forms=("10-K",))
            if fils:
                break
            time.sleep(1.0 * (a + 1))           # ride through residual rate-block
        if not fils:
            blocked += 1
            continue
        idx = next((j for j, f in enumerate(fils) if f["filed"] <= as_of), None)
        if idx is None or idx + 1 >= len(fils):
            nopair += 1
            continue
        for f in (fils[idx], fils[idx + 1]):
            if not _doc_cached(cik, f["accession"]):
                try:
                    fetch_filing_document_by_cik(cik, f["accession"], f["primary_document"])
                except Exception:
                    pass
        ok += 1
        time.sleep(args.sleep)
        if i % 200 == 0:
            log.info("  %d/%d | cached_pairs=%d unresolved(blocked)=%d nopair=%d",
                     i, len(work), ok, blocked, nopair)
    log.info("DONE | cached_pairs=%d unresolved=%d nopair=%d. Re-run to fill 'unresolved' if SEC blocked.", ok, blocked, nopair)


if __name__ == "__main__":
    main()
