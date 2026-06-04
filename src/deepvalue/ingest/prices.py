"""
Price access for deepvalue — sourced from FMP (survivorship-free, incl. delisted).

PORT-ADAPT: parley's fundamentals.py called src.data.fetch_prices.get_prices (an IBKR/EDGAR
price cache). deepvalue's prices come from FMP instead (fmp_client.py + the perishable grab).
Wire get_prices() to the FMP grab cache during Phase 1. Not on the Phase-1 L3 critical path
(the anomaly test needs filing text + forward returns, not P/E), so left as a clear stub.
"""
from __future__ import annotations


def get_prices(ticker: str, period: str = "max") -> dict:
    raise NotImplementedError(
        "deepvalue price access not wired yet — source from FMP (ingest/fmp_client.py) "
        "via the survivorship grab. See PORT-ADAPT note in ingest/fundamentals.py."
    )
