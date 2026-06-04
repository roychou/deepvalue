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
from pathlib import Path

from dotenv import load_dotenv

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
load_dotenv()

from deepvalue.ingest.fmp_client import FMPError, _get  # noqa: E402 (after path/dotenv setup)

# Known delisted/dead US large-caps that were S&P 500 members (2022–2023):
DELISTED = {
    "SIVB": "Silicon Valley Bank — failed Mar 2023",
    "SBNY": "Signature Bank — failed Mar 2023",
    "FRC": "First Republic Bank — failed May 2023",
    "ATVI": "Activision Blizzard — acquired by Microsoft Oct 2023",
}
LIVE = {"AAPL": "control — should run to ~today"}


def _probe(symbol: str) -> str:
    try:
        data = _get("historical-price-eod/full", params={"symbol": symbol})
    except FMPError as e:
        return f"ERR {str(e)[:80]}"
    if not isinstance(data, list) or not data:
        return "EMPTY (no history returned)"
    dates = [row["date"] for row in data if "date" in row]
    if not dates:
        return f"{len(data)} rows but no dates"
    return f"{len(data)} rows | {min(dates)} -> {max(dates)}"


def main() -> None:
    print("DELISTED (PASS = history exists and ENDS at the delisting date):")
    for sym, desc in DELISTED.items():
        print(f"  {sym:6} {_probe(sym):44}  [{desc}]")

    print("\nLIVE control:")
    for sym, desc in LIVE.items():
        print(f"  {sym:6} {_probe(sym):44}  [{desc}]")

    print("\nDoes FMP recognize delisted names at all?")
    try:
        dl = _get("delisted-companies", params={})
        n = len(dl) if isinstance(dl, list) else "n/a"
        print(f"  delisted-companies endpoint: {n} entries")
    except FMPError as e:
        print(f"  delisted-companies endpoint: ERR {str(e)[:80]}")


if __name__ == "__main__":
    main()
