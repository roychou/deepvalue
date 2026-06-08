"""
One-time Sharadar bulk grab (perishable — subscribe SFA for one month, grab, CANCEL).

Pulls the full survivorship-free history of the tables deepvalue needs into
data/cache/sharadar/, via Nasdaq Data Link's async bulk-export endpoint (the pattern from
sharadar.com/meta/bulk_fetch.py): trigger export -> poll until 'fresh' -> download the zip
-> extract the CSV. Raw httpx, no extra dependency (same approach as the FMP grab).

Why these tables:
  TICKERS  - permaticker (stable id, fixes ticker reuse) + SIC/sector + category (common-
             stock filter) + delisting dates -> fixes our reuse/bank/universe gaps natively.
  SF1      - Core US Fundamentals (income+balance+cashflow), survivorship-free 1990+ -> L1.
  SEP      - Equity Prices (daily OHLCV incl. delisted), 1998+ -> forward returns / mktcap.
  ACTIONS  - splits/dividends/delistings/ticker-changes (adjustment + delisting events).
  EVENTS   - parsed 8-K flags: going-concern, auditor change, restatement, bankruptcy ->
             fills the trap signals we left empty + outcome labels for §14.1 calibration.
  SF3      - institutional ownership -> a direct "neglect" measure for the illiquid-edge bet.

Set NASDAQ_DATA_LINK_API_KEY in .env first.

    uv run python scripts/sharadar_grab.py
    uv run python scripts/sharadar_grab.py --tables TICKERS,SF1,SEP   # subset
"""
from __future__ import annotations

import argparse
import io
import logging
import os
import time
import zipfile
from pathlib import Path

import httpx
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")

OUT = ROOT / "data" / "cache" / "sharadar"
EXPORT_URL = "https://data.nasdaq.com/api/v3/datatables/SHARADAR/{table}.json?qopts.export=true&api_key={key}"
DEFAULT_TABLES = ["TICKERS", "SF1", "SEP", "ACTIONS", "EVENTS", "SF3"]
POLL_SECONDS = 30
POLL_TIMEOUT = 3600   # exports of SEP/SF1 can take many minutes to generate

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("sharadar_grab")


def _key() -> str:
    k = os.environ.get("NASDAQ_DATA_LINK_API_KEY") or os.environ.get("QUANDL_API_KEY")
    if not k:
        raise SystemExit("Set NASDAQ_DATA_LINK_API_KEY in .env (Nasdaq Data Link account -> API key)")
    return k


def grab_table(client: httpx.Client, table: str, key: str) -> None:
    """Trigger the async bulk export, poll until fresh, download + extract the CSV."""
    dest = OUT / f"{table}.csv"
    if dest.exists() and dest.stat().st_size > 0:
        log.info("%s: already present (%.0f MB) — skipping", table, dest.stat().st_size / 1e6)
        return
    url = EXPORT_URL.format(table=table, key=key)
    deadline = time.monotonic() + POLL_TIMEOUT
    link, status = None, ""
    while status != "fresh":
        r = client.get(url, timeout=60)
        r.raise_for_status()
        f = r.json()["datatable_bulk_download"]["file"]
        status, link = f["status"], f.get("link")
        if status == "fresh":
            break
        if time.monotonic() > deadline:
            raise TimeoutError(f"{table}: export not ready after {POLL_TIMEOUT}s (status={status})")
        log.info("%s: status=%s — waiting %ds", table, status, POLL_SECONDS)
        time.sleep(POLL_SECONDS)

    log.info("%s: downloading bulk zip", table)
    with client.stream("GET", link, timeout=None, follow_redirects=True) as resp:
        resp.raise_for_status()
        buf = io.BytesIO(resp.read())
    with zipfile.ZipFile(buf) as z:
        name = z.namelist()[0]
        OUT.mkdir(parents=True, exist_ok=True)
        with z.open(name) as src, open(dest, "wb") as out:
            out.write(src.read())
    log.info("%s -> %s (%.0f MB)", table, dest, dest.stat().st_size / 1e6)


def main() -> None:
    ap = argparse.ArgumentParser(description="Sharadar bulk grab (one-time, perishable)")
    ap.add_argument("--tables", default=",".join(DEFAULT_TABLES))
    args = ap.parse_args()
    key = _key()
    tables = [t.strip().upper() for t in args.tables.split(",") if t.strip()]
    OUT.mkdir(parents=True, exist_ok=True)
    log.info("grabbing Sharadar tables: %s -> %s", tables, OUT)
    with httpx.Client() as client:
        for t in tables:
            try:
                grab_table(client, t, key)
            except Exception as e:
                log.error("%s FAILED: %s", t, e)
    log.info("DONE. Remember: bulk-grab complete -> you can CANCEL the SFA subscription.")


if __name__ == "__main__":
    main()
