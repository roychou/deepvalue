import json
from pathlib import Path

import pytest

from deepvalue.ingest.prices import get_prices


def _write(cache: Path, key: str, rows: list[dict]) -> None:
    (cache / f"{key}.json").write_text(json.dumps({"symbol": key.split("__")[0], "rows": rows}))


def test_get_prices_reads_and_coerces(tmp_path):
    cache = tmp_path / "prices"
    cache.mkdir()
    _write(cache, "ABC__active", [
        {"date": "2020-01-02", "close": "10.5", "open": "10", "high": "11", "low": "9", "volume": "100", "vwap": "10.2"},
    ])
    px = get_prices("ABC", cache_dir=cache)
    assert set(px) == {"2020-01-02"}
    assert px["2020-01-02"]["close"] == 10.5          # str -> float
    assert isinstance(px["2020-01-02"]["volume"], float)


def test_get_prices_unions_active_and_delisted_keys(tmp_path):
    # A company present under both an active dup and a delisted key -> one merged series.
    cache = tmp_path / "prices"
    cache.mkdir()
    _write(cache, "XYZ__delisted_20231114", [{"date": "2023-11-14", "close": 5.0}])
    _write(cache, "XYZ__active", [{"date": "2022-06-01", "close": 7.0}])
    px = get_prices("XYZ", cache_dir=cache)
    assert sorted(px) == ["2022-06-01", "2023-11-14"]


def test_get_prices_missing_ticker_returns_empty(tmp_path):
    cache = tmp_path / "prices"
    cache.mkdir()
    assert get_prices("NOPE", cache_dir=cache) == {}


def test_get_prices_missing_cache_raises(tmp_path):
    with pytest.raises(FileNotFoundError):
        get_prices("ABC", cache_dir=tmp_path / "does_not_exist")
