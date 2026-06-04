"""
One-time, perishable FMP survivorship grab (sub expires ~end Jun 2026).

Locks the survivorship-free US universe (active + delisted, listed + OTC) plus
max-depth daily prices and pre-2009 fundamentals into data/cache/, so the L3
anomaly backtest (spec §7, §14.2) has point-in-time prices for dead names.

Honors the caveats proven by scripts/fmp_survivorship_probe.py (4 Jun 2026):
- historical-price-eod/full only returns ~5y unless `from` is pinned  -> FROM_FLOOR.
- the endpoint hard-caps a response at ~5000 rows                      -> page backward.
- ticker reuse is silent (HMNY, SBNY trade to today post-delist)       -> clip at
  delistedDate + key every series by CIK, not bare symbol.
- coverage is good but not complete (SSI absent)                       -> manifest
  records present/absent/clipped per name; missing != never-traded.

Resumable: a name whose output already exists on disk is skipped, so an
interrupted run just re-runs cheaply. Throttled under the 750-calls/min ceiling.

Usage (smoke test first, then full):
    uv run python scripts/fmp_grab.py --limit 25          # ~25-name end-to-end check
    uv run python scripts/fmp_grab.py                     # full grab
    uv run python scripts/fmp_grab.py --phase universe    # just (re)build the roster
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import re
import time
from collections import Counter
from datetime import date, timedelta
from pathlib import Path

import httpx
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")

BASE_URL = "https://financialmodelingprep.com/stable"
FROM_FLOOR = "1990-01-01"      # pin or the 5y default window truncates dead names
ROW_CAP = 5000                 # single-response hard cap -> page backward past it
US_EXCHANGES = {"NASDAQ", "NYSE", "AMEX", "OTC"}   # listed + OTC, per operator
TODAY = date.today().isoformat()

CACHE = ROOT / "data" / "cache"
DIR_UNIVERSE = CACHE / "universe"
DIR_PRICES = CACHE / "prices"
DIR_FUND = CACHE / "fundamentals"
MANIFEST = CACHE / "manifest.json"

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logging.getLogger("httpx").setLevel(logging.WARNING)   # else 1 log line per call
logging.getLogger("httpcore").setLevel(logging.WARNING)
log = logging.getLogger("fmp_grab")


class Budget:
    """Global call counter + hard cap. Raises StopAsyncIteration-free sentinel."""

    def __init__(self, max_calls: int | None):
        self.max_calls = max_calls
        self.n = 0

    def exhausted(self) -> bool:
        return self.max_calls is not None and self.n >= self.max_calls


class RateGate:
    """Cap request *starts* to stay under calls/min, allowing bounded concurrency."""

    def __init__(self, per_min: int):
        self._min_interval = 60.0 / per_min
        self._lock = asyncio.Lock()
        self._last = 0.0

    async def wait(self) -> None:
        async with self._lock:
            now = time.monotonic()
            delay = self._last + self._min_interval - now
            if delay > 0:
                await asyncio.sleep(delay)
            self._last = time.monotonic()


def _key() -> str:
    k = os.environ.get("FMP_API_KEY")
    if not k:
        raise SystemExit("FMP_API_KEY not set — add it to .env")
    return k


async def fetch(
    client: httpx.AsyncClient,
    gate: RateGate,
    budget: Budget,
    sem: asyncio.Semaphore,
    path: str,
    params: dict,
    *,
    retries: int = 4,
):
    """One throttled GET with retry/backoff. Returns parsed JSON or None on hard fail."""
    params = {**params, "apikey": _key()}
    url = f"{BASE_URL}/{path}"
    for attempt in range(retries):
        if budget.exhausted():
            return None
        await gate.wait()
        async with sem:
            budget.n += 1
            try:
                r = await client.get(url, params=params, timeout=30.0)
            except httpx.RequestError as e:
                log.warning("net error %s (%s) try %d", path, e, attempt)
                await asyncio.sleep(2 ** attempt)
                continue
        if r.status_code == 429 or r.status_code >= 500:
            await asyncio.sleep(2 ** attempt)
            continue
        if r.status_code != 200:
            log.warning("%s -> %d %s", path, r.status_code, r.text[:120])
            return None
        data = r.json()
        if isinstance(data, dict) and "Error Message" in data:
            log.warning("%s error: %s", path, data["Error Message"])
            return None
        return data
    return None


# ---------------------------------------------------------------- universe ----
async def build_universe(client, gate, budget, sem) -> list[dict]:
    """Active screener + paginated delisted list -> one US roster, deduped by symbol
    (a reused symbol keeps BOTH rows: the delisted old co and the active new co)."""
    roster: list[dict] = []

    # Query the screener ONE EXCHANGE AT A TIME: it hard-caps any response at 10k
    # rows, and the combined US union (~11.7k) exceeds that and silently truncates.
    # Per-exchange each is well under 10k (NASDAQ 4.4k / NYSE 2.4k / AMEX 0.3k / OTC 4.7k).
    seen: set[str] = set()
    for ex in sorted(US_EXCHANGES):
        active = await fetch(client, gate, budget, sem, "company-screener",
                             {"exchange": ex, "isEtf": "false", "isFund": "false",
                              "country": "US", "limit": 20000})
        n = len(active) if isinstance(active, list) else 0
        if n >= 10000:
            log.warning("exchange %s returned %d (>=10k cap) — may be truncated", ex, n)
        for r in active or []:
            exr = r.get("exchangeShortName") or r.get("exchange")
            if exr in US_EXCHANGES and r["symbol"] not in seen:
                seen.add(r["symbol"])
                roster.append({"symbol": r["symbol"], "exchange": exr,
                               "companyName": r.get("companyName"), "status": "active",
                               "delistedDate": None, "ipoDate": None})
    log.info("active US equities: %d", len(roster))

    page, n_delisted = 0, 0
    while True:
        d = await fetch(client, gate, budget, sem, "delisted-companies", {"page": page})
        if not d:
            break
        for r in d:
            if r.get("exchange") in US_EXCHANGES:
                roster.append({"symbol": r["symbol"], "exchange": r.get("exchange"),
                               "companyName": r.get("companyName"), "status": "delisted",
                               "delistedDate": r.get("delistedDate"),
                               "ipoDate": r.get("ipoDate")})
                n_delisted += 1
        page += 1
        if budget.exhausted():
            break
    log.info("delisted US rows: %d across %d pages", n_delisted, page)

    DIR_UNIVERSE.mkdir(parents=True, exist_ok=True)
    (DIR_UNIVERSE / "roster.json").write_text(json.dumps(roster, indent=2))
    return roster


def cache_key(entry: dict) -> str:
    """Collision-free disk key. NOT CIK-based: CIK collides on dual-class tickers
    (GOOG/GOOGL share a CIK) and is the *wrong* identity for a reused delisted
    ticker (profile returns the current holder). symbol+status(+delistedDate)
    keeps every incarnation distinct; CIK rides along as join metadata."""
    safe = re.sub(r"[^A-Za-z0-9._-]", "_", entry["symbol"])
    if entry["status"] == "delisted":
        return f"{safe}__delisted_{(entry['delistedDate'] or 'unknown').replace('-', '')}"
    return f"{safe}__active"


# ------------------------------------------------------------------ prices ----
async def fetch_prices(client, gate, budget, sem, symbol: str,
                       from_floor: str, to_date: str) -> list[dict]:
    """Full daily history [from_floor, to_date], paging backward past the 5000-row cap."""
    rows: list[dict] = []
    cursor_to = to_date
    while True:
        chunk = await fetch(client, gate, budget, sem, "historical-price-eod/full",
                            {"symbol": symbol, "from": from_floor, "to": cursor_to})
        if not isinstance(chunk, list) or not chunk:
            break
        rows.extend(chunk)
        if len(chunk) < ROW_CAP:
            break
        oldest = min(r["date"] for r in chunk if "date" in r)
        if oldest <= from_floor:
            break
        cursor_to = (date.fromisoformat(oldest) - timedelta(days=1)).isoformat()
    # dedupe by date (page boundaries can overlap), newest-first
    by_date = {r["date"]: r for r in rows if "date" in r}
    return sorted(by_date.values(), key=lambda r: r["date"], reverse=True)


# -------------------------------------------------------------- per-name ------
async def grab_name(client, gate, budget, sem, entry: dict, want_fund: bool,
                    from_floor: str, reused: bool) -> dict:
    sym, status = entry["symbol"], entry["status"]
    key = cache_key(entry)

    # CIK/CUSIP via profile -> EDGAR join metadata (NOT the disk key; for a reused
    # delisted ticker this is the *current* holder's CIK, hence cik_uncertain).
    prof = await fetch(client, gate, budget, sem, "profile", {"symbol": sym})
    p0 = prof[0] if isinstance(prof, list) and prof else {}
    cik = p0.get("cik")

    rec = {"key": key, "symbol": sym, "status": status, "cik": cik,
           "cusip": p0.get("cusip"), "exchange": entry["exchange"],
           "delistedDate": entry["delistedDate"], "reused": reused,
           "cik_uncertain": reused and status == "delisted",
           "from_floor": from_floor, "coverage": "absent", "clipped": False,
           "n_price": 0, "price_first": None, "price_last": None,
           "n_income": 0, "n_balance": 0}

    price_path = DIR_PRICES / f"{key}.json"
    if price_path.exists():                       # resumable: trust existing file
        rec["coverage"] = "cached"
        return rec

    # Two-sided clip isolates one company's life from a reused ticker's shared series:
    #  - back clip: delisted names end at delistedDate.
    #  - front clip (from_floor): an *active* name on a reused ticker starts the day
    #    after the prior incarnation's delisting, so the dead co's bars don't bleed in.
    to_date = entry["delistedDate"] or TODAY
    prices = await fetch_prices(client, gate, budget, sem, sym, from_floor, to_date)
    if entry["delistedDate"]:
        cut = entry["delistedDate"]
        kept = [r for r in prices if r.get("date", "") <= cut]
        rec["clipped"] = len(kept) != len(prices)
        prices = kept

    if prices:
        rec.update(coverage="present", n_price=len(prices),
                   price_first=prices[-1]["date"], price_last=prices[0]["date"])
        DIR_PRICES.mkdir(parents=True, exist_ok=True)
        price_path.write_text(json.dumps({"symbol": sym, "cik": cik,
                                          "delistedDate": entry["delistedDate"],
                                          "reused": reused, "from_floor": from_floor,
                                          "rows": prices}))

    if want_fund:
        inc = await fetch(client, gate, budget, sem, "income-statement",
                          {"symbol": sym, "limit": 200})
        bal = await fetch(client, gate, budget, sem, "balance-sheet-statement",
                          {"symbol": sym, "limit": 200})
        inc = inc if isinstance(inc, list) else []
        bal = bal if isinstance(bal, list) else []
        rec["n_income"], rec["n_balance"] = len(inc), len(bal)
        if inc or bal:
            DIR_FUND.mkdir(parents=True, exist_ok=True)
            (DIR_FUND / f"{key}.json").write_text(
                json.dumps({"symbol": sym, "cik": cik, "income": inc, "balance": bal}))
    return rec


# --------------------------------------------------------------------- run ----
async def run(args) -> None:
    CACHE.mkdir(parents=True, exist_ok=True)
    gate = RateGate(args.rate)
    budget = Budget(args.max_calls)
    sem = asyncio.Semaphore(args.concurrency)

    async with httpx.AsyncClient() as client:
        roster_path = DIR_UNIVERSE / "roster.json"
        if args.phase in ("universe", "all") or not roster_path.exists():
            roster = await build_universe(client, gate, budget, sem)
        else:
            roster = json.loads(roster_path.read_text())
            log.info("loaded roster: %d names", len(roster))
        if args.phase == "universe":
            return

        if args.limit:
            # smoke test: mix active + delisted + a known reused ticker if present
            delisted = [r for r in roster if r["status"] == "delisted"]
            active = [r for r in roster if r["status"] == "active"]
            reused = [r for r in roster if r["symbol"] in ("HMNY", "SBNY")]
            half = max(1, args.limit // 2)
            roster = (reused + delisted[:half] + active[:args.limit - half])[: args.limit]
            log.info("SMOKE TEST: %d names (%d reused-ticker probes included)",
                     len(roster), len(reused))

        # Ticker-reuse map: a symbol appearing in >1 roster row is reused. For an
        # active name on a reused ticker, front-clip its series to the day after the
        # prior incarnation's latest delisting so the dead co's bars don't bleed in.
        sym_counts = Counter(e["symbol"] for e in roster)
        prior_delist: dict[str, str] = {}
        for e in roster:
            if e["status"] == "delisted" and e["delistedDate"]:
                cur = prior_delist.get(e["symbol"])
                if cur is None or e["delistedDate"] > cur:
                    prior_delist[e["symbol"]] = e["delistedDate"]

        want_fund = not args.skip_fundamentals
        manifest, done = [], 0
        for entry in roster:
            if budget.exhausted():
                log.warning("call budget %d exhausted — stopping early", args.max_calls)
                break
            reused = sym_counts[entry["symbol"]] > 1
            floor = FROM_FLOOR
            if entry["status"] == "active" and entry["symbol"] in prior_delist:
                floor = (date.fromisoformat(prior_delist[entry["symbol"]])
                         + timedelta(days=1)).isoformat()
            manifest.append(
                await grab_name(client, gate, budget, sem, entry, want_fund, floor, reused))
            done += 1
            if done % 200 == 0:
                log.info("progress %d/%d  calls=%d", done, len(roster), budget.n)
                MANIFEST.write_text(json.dumps(manifest, indent=2))

    MANIFEST.write_text(json.dumps(manifest, indent=2))
    present = sum(1 for m in manifest if m["coverage"] == "present")
    cached = sum(1 for m in manifest if m["coverage"] == "cached")
    absent = sum(1 for m in manifest if m["coverage"] == "absent")
    clipped = sum(1 for m in manifest if m["clipped"])
    log.info("DONE %d names | present=%d cached=%d absent=%d clipped=%d | calls=%d",
             len(manifest), present, cached, absent, clipped, budget.n)
    log.info("manifest -> %s", MANIFEST)


def main() -> None:
    ap = argparse.ArgumentParser(description="One-time FMP survivorship grab")
    ap.add_argument("--limit", type=int, default=0, help="smoke-test on N names (0 = full)")
    ap.add_argument("--max-calls", type=int, default=None, help="hard cap on API calls")
    ap.add_argument("--rate", type=int, default=700, help="calls/min ceiling (<750)")
    ap.add_argument("--concurrency", type=int, default=16)
    ap.add_argument("--skip-fundamentals", action="store_true")
    ap.add_argument("--phase", choices=["all", "universe"], default="all")
    asyncio.run(run(ap.parse_args()))


if __name__ == "__main__":
    main()
