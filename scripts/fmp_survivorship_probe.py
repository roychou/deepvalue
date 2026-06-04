"""
One-off probe: does FMP's *paid* tier serve survivorship-bias-free (delisted)
historical prices via the endpoint our data layer already uses?

Run this AFTER upgrading FMP, with FMP_API_KEY set in .env:

    uv run python scripts/fmp_survivorship_probe.py

It checks a few known-delisted S&P 500 names against `historical-price-eod/full`
(the endpoint `src/data/fmp_client.get_historical_prices` calls). Interpretation:

- PASS: delisted names return real history that TERMINATES at/near the delisting
  date (not empty, not 402, not running to today). => the existing fmp_client is
  survivorship-free on this tier; no new integration needed, just upgrade.
- FAIL: delisted names come back EMPTY / 402. => the standard endpoint is NOT
  survivorship-free on this tier; you'd need FMP's dedicated (Legacy-tagged)
  survivorship endpoint — treat that deprecation flag as a red flag and prefer a
  purpose-built source (Sharadar).

The live control (AAPL) should run to ~today. See the data-source discussion in
notes/ for why this gate matters.
"""
import sys
from datetime import date
from pathlib import Path

from dotenv import load_dotenv

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
load_dotenv()

from deepvalue.ingest.fmp_client import FMPError, _get  # noqa: E402 (after path/dotenv setup)

# A "delisted" name PASSES only if its price history TERMINATES at/near its
# delisting date. A name whose history runs to ~today is a FAIL for our purposes
# — either it's still trading (bad test pick) or, worse, the ticker has been
# REUSED by a later company (the SBNY caveat in CLAUDE.md). We encode the
# expected last-trade year so we can tell those apart instead of eyeballing.

# Known delisted/dead US large-caps that were S&P 500 members (the original
# probe set — large-cap PASS was already confirmed on 4 Jun; kept as a control):
DELISTED_LARGE = {
    "SIVB": ("Silicon Valley Bank — failed Mar 2023", 2023),
    "SBNY": ("Signature Bank — failed Mar 2023; CLAUDE.md notes ticker-REUSE", 2023),
    "FRC": ("First Republic Bank — failed May 2023", 2023),
    "ATVI": ("Activision Blizzard — acquired by Microsoft Oct 2023", 2023),
}

# The actual question: micro-cap delisted coverage (our real universe). These are
# small/micro-cap names that went bankrupt or private with a KNOWN last-trade
# year, so we can verify history terminates there rather than running to today.
DELISTED_MICRO = {
    "TUEM": ("Tuesday Morning Corp — Ch.11 Feb 2023, liquidated/delisted ~mid 2023", 2023),
    "HMNY": ("Helios & Matheson (MoviePass) — Nasdaq delisted Feb 2020, bankrupt", 2020),
    "SHOS": ("Sears Hometown & Outlet Stores — taken private Oct 2019", 2019),
    "SSI": ("Stein Mart — Ch.11 Aug 2020, delisted ~late 2020", 2020),
    "PIR": ("Pier 1 Imports — Ch.11 Feb 2020, delisted ~early 2020", 2020),
    "FRED": ("Fred's Inc — bankrupt/delisted 2019", 2019),
}
LIVE = {"AAPL": ("control — should run to ~today", date.today().year)}


# CRITICAL: historical-price-eod/full returns only a ~5-YEAR default window unless
# an explicit `from` is passed (verified 4 Jun 2026: every name, incl. live AAPL,
# started exactly 5y back until we set `from`). Always pin a wide floor or you
# silently truncate — and pre-2021 delistings come back falsely EMPTY.
HISTORY_FLOOR = "1990-01-01"
# The endpoint also hard-caps a single response at ~5000 rows (~20y of daily bars);
# names that hit the cap need date-chunked paging in the real grab.
ROW_CAP = 5000


def _probe(symbol: str, expected_last_year: int) -> str:
    """Fetch full EOD history and classify coverage vs. the expected delisting year."""
    try:
        data = _get(
            "historical-price-eod/full",
            params={"symbol": symbol, "from": HISTORY_FLOOR, "to": str(date.today())},
        )
    except FMPError as e:
        return f"FAIL  ERR {str(e)[:70]}"
    if not isinstance(data, list) or not data:
        return "FAIL  EMPTY (genuinely absent — coverage gap for this name)"
    dates = sorted(row["date"] for row in data if "date" in row)
    if not dates:
        return f"FAIL  {len(data)} rows but no dates"

    first, last = dates[0], dates[-1]
    last_year = int(last[:4])
    capped = "  [ROW-CAP HIT — needs date paging]" if len(data) >= ROW_CAP else ""
    span = f"{len(data):>5} rows | {first} -> {last}{capped}"

    today_year = date.today().year
    is_live_control = expected_last_year >= today_year
    if is_live_control:
        verdict = "PASS" if last_year >= today_year - 1 else "WARN  stale"
    elif last_year > expected_last_year:
        # History extends well past delisting → still trading or TICKER REUSE.
        verdict = f"REUSE? last={last_year} >> delist~{expected_last_year}"
    elif last_year < expected_last_year - 1:
        verdict = f"WARN  truncated (last={last_year} < delist~{expected_last_year})"
    else:
        verdict = "PASS"
    return f"{verdict:38}  {span}"


def main() -> None:
    print("=" * 78)
    print("FMP survivorship probe — does the paid tier serve delisted MICRO-cap history?")
    print("PASS = history exists and TERMINATES at/near the delisting year.")
    print("REUSE? = history runs past delisting → ticker reused or still trading (suspect).")
    print("=" * 78)

    print("\nMICRO/SMALL-CAP DELISTED (the real question — our universe):")
    for sym, (desc, yr) in DELISTED_MICRO.items():
        print(f"  {sym:6} {_probe(sym, yr)}")
        print(f"         └ [{desc}]")

    print("\nLARGE-CAP DELISTED (control — confirmed PASS on 4 Jun):")
    for sym, (desc, yr) in DELISTED_LARGE.items():
        print(f"  {sym:6} {_probe(sym, yr)}")
        print(f"         └ [{desc}]")

    print("\nLIVE control:")
    for sym, (desc, yr) in LIVE.items():
        print(f"  {sym:6} {_probe(sym, yr)}")
        print(f"         └ [{desc}]")

    print("\nDoes FMP recognize delisted names at all? (universe-list endpoint)")
    try:
        dl = _get("delisted-companies", params={})
        n = len(dl) if isinstance(dl, list) else "n/a"
        sample = dl[0] if isinstance(dl, list) and dl else None
        print(f"  delisted-companies (page 0): {n} entries | sample keys: "
              f"{sorted(sample.keys()) if isinstance(sample, dict) else 'n/a'}")
        if isinstance(sample, dict):
            print(f"  sample row: {sample}")
    except FMPError as e:
        print(f"  delisted-companies endpoint: ERR {str(e)[:80]}")


if __name__ == "__main__":
    main()
