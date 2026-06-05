"""
Rebuild the authoritative coverage manifest from what's actually on disk — no API
calls. Use this after a grab (or several backfill passes) to get one accurate
picture, instead of trusting whichever run wrote manifest.json last.

Key idea: coverage is a property of the *company (symbol)*, not the roster row. A
name that delisted is captured under its `SYMBOL__delisted_<date>` key; the
screener often ALSO lists it as a stale "active" duplicate whose own price file is
legitimately empty (a dead company has no post-delisting trading). So a symbol is
"covered" if ANY of its keys has price data — counting the empty active duplicate
as a gap would badly understate true coverage.

    uv run python scripts/fmp_build_manifest.py
"""
from __future__ import annotations

import json
import re
from collections import Counter, defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CACHE = ROOT / "data" / "cache"
DIR_PRICES = CACHE / "prices"
DIR_FUND = CACHE / "fundamentals"


def cache_key(symbol: str, status: str, delisted_date: str | None) -> str:
    safe = re.sub(r"[^A-Za-z0-9._-]", "_", symbol)
    if status == "delisted":
        return f"{safe}__delisted_{(delisted_date or 'unknown').replace('-', '')}"
    return f"{safe}__active"


def main() -> None:
    roster = json.loads((CACHE / "universe" / "roster.json").read_text())
    sym_counts = Counter(e["symbol"] for e in roster)

    rows, sym_has_data = [], defaultdict(bool)
    for e in roster:
        key = cache_key(e["symbol"], e["status"], e["delistedDate"])
        pf = DIR_PRICES / f"{key}.json"
        n_price, first, last, cik = 0, None, None, None
        if pf.exists():
            data = json.loads(pf.read_text())
            cik = data.get("cik")        # the EDGAR join key (price grab stored it per file)
            prs = data.get("rows", [])
            if prs:
                ds = sorted(r["date"] for r in prs if "date" in r)
                n_price, first, last = len(prs), ds[0], ds[-1]
        sym_has_data[e["symbol"]] |= n_price > 0
        rows.append({
            "key": key, "symbol": e["symbol"], "status": e["status"], "cik": cik,
            "exchange": e["exchange"], "delistedDate": e["delistedDate"],
            "reused": sym_counts[e["symbol"]] > 1,
            "n_price": n_price, "price_first": first, "price_last": last,
            "has_fundamentals": (DIR_FUND / f"{key}.json").exists(),
        })

    for r in rows:                       # backfill the company-level verdict
        r["symbol_covered"] = sym_has_data[r["symbol"]]

    (CACHE / "manifest.json").write_text(json.dumps(rows, indent=2))

    # ---- summary (per distinct symbol = per company) ----
    syms = set(sym_has_data)
    covered = {s for s in syms if sym_has_data[s]}
    uncovered = syms - covered
    unc_status = defaultdict(set)
    for e in roster:
        if e["symbol"] in uncovered:
            unc_status[e["symbol"]].add(e["status"])
    deli_gap = sorted(s for s in uncovered if "delisted" in unc_status[s])
    act_gap = sorted(s for s in uncovered if unc_status[s] == {"active"})

    print(f"roster rows: {len(rows)} | distinct symbols: {len(syms)}")
    print(f"COVERED (>=1 key has prices): {len(covered)} ({len(covered)/len(syms)*100:.1f}%)")
    print(f"UNCOVERED: {len(uncovered)} ({len(uncovered)/len(syms)*100:.1f}%)"
          f"  -> {len(deli_gap)} with a delisted entry (perishable), {len(act_gap)} active-only")
    print(f"  delisted gaps: {deli_gap}")
    print(f"price files present: {sum(1 for r in rows if r['n_price'] > 0)} | "
          f"fundamentals files: {sum(1 for r in rows if r['has_fundamentals'])}")
    print(f"manifest -> {CACHE / 'manifest.json'}")


if __name__ == "__main__":
    main()
