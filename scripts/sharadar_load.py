"""
Load the Sharadar bulk CSVs into an indexed DuckDB (data/cache/sharadar.duckdb), once.

The CSVs are large (SEP 3GB, SF1 2.2GB); per-ticker scans over raw CSV are far too slow.
DuckDB gives fast indexed point lookups + low memory. This is a one-time build; the .duckdb
is the queryable asset the adapter (ingest/sharadar.py) reads.

    uv run python scripts/sharadar_load.py
"""
from __future__ import annotations

import logging
from pathlib import Path

import duckdb

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "data" / "cache" / "sharadar"
DB = ROOT / "data" / "cache" / "sharadar.duckdb"

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("sharadar_load")


def main() -> None:
    con = duckdb.connect(str(DB))
    con.execute("PRAGMA memory_limit='4GB'")

    def load(table: str, sql: str, index_cols: str) -> None:
        log.info("loading %s ...", table)
        con.execute(f"CREATE OR REPLACE TABLE {table} AS {sql}")
        con.execute(f"CREATE INDEX IF NOT EXISTS {table}_idx ON {table}({index_cols})")
        n = con.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
        log.info("  %s: %d rows", table, n)

    csv = lambda t: f"read_csv_auto('{SRC / f'{t}.csv'}', sample_size=-1)"  # noqa: E731

    load("tickers", f"SELECT * FROM {csv('TICKERS')}", "ticker")
    # annual (ARY) is the point-in-time backtest grain; keep ARQ too for the faster-cadence test
    load("sf1", f"SELECT * FROM {csv('SF1')} WHERE dimension IN ('ARY','ARQ')", "ticker")
    load("sep", f"SELECT ticker, date, closeadj, close, volume FROM {csv('SEP')}", "ticker")
    load("events", f"SELECT * FROM {csv('EVENTS')}", "ticker")
    load("actions", f"SELECT * FROM {csv('ACTIONS')}", "ticker")

    con.close()
    log.info("DONE -> %s (%.0f MB)", DB, DB.stat().st_size / 1e6)


if __name__ == "__main__":
    main()
