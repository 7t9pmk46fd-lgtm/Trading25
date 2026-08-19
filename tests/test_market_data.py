"""
Tests for shared/market_data.py. No network: _get_client is monkeypatched
to return a fake client whose get_stock_bars/get_stock_latest_trade return
plain pandas objects shaped like alpaca-py's real response, so what's
under test is this module's own MultiIndex-unpacking, tz-handling, and
(for get_daily_bars_cached) the on-disk cache's coverage/merge logic --
not Alpaca's SDK.

get_daily_bars_cached is the important one: it has a documented real bug
(fixed 2026-08-11) where "covers" meant presence-only, so whichever caller
populated a symbol's cache entry FIRST each day silently capped every
later caller to that first window -- concretely, HST's stop went
unprotected because daily_review's shorter window got cached before
trail_stops' longer one asked. These tests pin the fixed behavior: a
narrower cached window must trigger a fresh, wider fetch.
"""
import sys
from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from shared import market_data


def _multi_bars(symbol_dates_closes: dict[str, list[tuple]], tz="UTC"):
    """symbol_dates_closes: {symbol: [(date_str, close), ...]}"""
    tuples = []
    rows = []
    for symbol, entries in symbol_dates_closes.items():
        for date_str, close in entries:
            ts = pd.Timestamp(date_str, tz=tz)
            tuples.append((symbol, ts))
            rows.append({"close": close, "volume": 1_000_000})
    idx = pd.MultiIndex.from_tuples(tuples, names=["symbol", "timestamp"])
    return pd.DataFrame(rows, index=idx)


class _FakeBarsResponse:
    def __init__(self, df):
        self.df = df


# ------------------------------------------------------------- get_daily_bars

def test_get_daily_bars_returns_per_symbol_frames(monkeypatch):
    df_all = _multi_bars({
        "AAPL": [("2026-08-17", 100.0), ("2026-08-18", 101.0)],
        "MSFT": [("2026-08-17", 300.0)],
    })
    fake_client = type("C", (), {"get_stock_bars": lambda self, request: _FakeBarsResponse(df_all)})()
    monkeypatch.setattr(market_data, "_get_client", lambda: fake_client)

    result = market_data.get_daily_bars(["AAPL", "MSFT"], datetime(2026, 8, 1), datetime(2026, 8, 19))

    assert set(result.keys()) == {"AAPL", "MSFT"}
    assert len(result["AAPL"]) == 2
    assert list(result["AAPL"]["close"]) == [100.0, 101.0]


def test_get_daily_bars_omits_symbols_with_no_data(monkeypatch):
    df_all = _multi_bars({"AAPL": [("2026-08-17", 100.0)]})
    fake_client = type("C", (), {"get_stock_bars": lambda self, request: _FakeBarsResponse(df_all)})()
    monkeypatch.setattr(market_data, "_get_client", lambda: fake_client)

    result = market_data.get_daily_bars(["AAPL", "ZZZZ"], datetime(2026, 8, 1), datetime(2026, 8, 19))

    assert set(result.keys()) == {"AAPL"}


def test_get_daily_bars_strips_timezone(monkeypatch):
    df_all = _multi_bars({"AAPL": [("2026-08-17", 100.0)]}, tz="UTC")
    fake_client = type("C", (), {"get_stock_bars": lambda self, request: _FakeBarsResponse(df_all)})()
    monkeypatch.setattr(market_data, "_get_client", lambda: fake_client)

    result = market_data.get_daily_bars(["AAPL"], datetime(2026, 8, 1), datetime(2026, 8, 19))

    assert result["AAPL"].index.tz is None


def test_get_daily_bars_empty_symbol_list(monkeypatch):
    df_all = _multi_bars({})
    fake_client = type("C", (), {"get_stock_bars": lambda self, request: _FakeBarsResponse(df_all)})()
    monkeypatch.setattr(market_data, "_get_client", lambda: fake_client)

    assert market_data.get_daily_bars([], datetime(2026, 8, 1), datetime(2026, 8, 19)) == {}


# ------------------------------------------------------ get_daily_bars_cached

@pytest.fixture
def isolated_cache(tmp_path, monkeypatch):
    monkeypatch.setattr(market_data, "_CACHE_DIR", tmp_path)
    return tmp_path


def test_cached_fetches_fresh_when_no_cache_file(monkeypatch, isolated_cache):
    fetch_calls = []

    def fake_get_daily_bars(symbols, start, end):
        fetch_calls.append((tuple(symbols), start))
        return {s: pd.DataFrame({"close": [100.0]}, index=[pd.Timestamp(start)]) for s in symbols}

    monkeypatch.setattr(market_data, "get_daily_bars", fake_get_daily_bars)

    start = datetime.now() - timedelta(days=10)
    result = market_data.get_daily_bars_cached(["AAPL"], start, datetime.now())

    assert fetch_calls == [(("AAPL",), start)]
    assert "AAPL" in result


def test_cached_reuses_same_day_cache_without_refetch(monkeypatch, isolated_cache):
    fetch_calls = []

    def fake_get_daily_bars(symbols, start, end):
        fetch_calls.append(tuple(symbols))
        return {s: pd.DataFrame({"close": [100.0]}, index=[pd.Timestamp(start)]) for s in symbols}

    monkeypatch.setattr(market_data, "get_daily_bars", fake_get_daily_bars)

    start = datetime.now() - timedelta(days=10)
    market_data.get_daily_bars_cached(["AAPL"], start, datetime.now())
    market_data.get_daily_bars_cached(["AAPL"], start, datetime.now())

    assert len(fetch_calls) == 1  # second call served entirely from cache


def test_cached_refetches_when_cached_window_too_narrow(monkeypatch, isolated_cache):
    # Regression guard for the 2026-08-11 fix: a symbol cached with a SHORT
    # window (e.g. daily_review's ~12-day request) must trigger a fresh
    # fetch for a caller that asks for a WIDER window (e.g. trail_stops'
    # 40-day request), not silently return the narrower cached data.
    fetch_calls = []

    def fake_get_daily_bars(symbols, start, end):
        fetch_calls.append((tuple(symbols), start))
        return {s: pd.DataFrame({"close": [100.0]}, index=[pd.Timestamp(start)]) for s in symbols}

    monkeypatch.setattr(market_data, "get_daily_bars", fake_get_daily_bars)

    now = datetime.now()
    short_start = now - timedelta(days=8)
    long_start = now - timedelta(days=40)

    market_data.get_daily_bars_cached(["HST"], short_start, now)   # caches an 8-day-back entry
    market_data.get_daily_bars_cached(["HST"], long_start, now)    # needs 40 days back

    assert len(fetch_calls) == 2
    assert fetch_calls[1] == (("HST",), long_start)


def test_cached_widest_window_satisfies_later_narrower_callers(monkeypatch, isolated_cache):
    fetch_calls = []

    def fake_get_daily_bars(symbols, start, end):
        fetch_calls.append(tuple(symbols))
        return {s: pd.DataFrame({"close": [100.0]}, index=[pd.Timestamp(start)]) for s in symbols}

    monkeypatch.setattr(market_data, "get_daily_bars", fake_get_daily_bars)

    now = datetime.now()
    long_start = now - timedelta(days=40)
    short_start = now - timedelta(days=8)

    market_data.get_daily_bars_cached(["HST"], long_start, now)   # wide fetch first
    market_data.get_daily_bars_cached(["HST"], short_start, now)  # narrower ask, already covered

    assert len(fetch_calls) == 1  # second call served from cache, no refetch needed


def test_cached_only_fetches_missing_symbols(monkeypatch, isolated_cache):
    fetch_calls = []

    def fake_get_daily_bars(symbols, start, end):
        fetch_calls.append(tuple(sorted(symbols)))
        return {s: pd.DataFrame({"close": [100.0]}, index=[pd.Timestamp(start)]) for s in symbols}

    monkeypatch.setattr(market_data, "get_daily_bars", fake_get_daily_bars)

    now = datetime.now()
    start = now - timedelta(days=10)
    market_data.get_daily_bars_cached(["AAPL"], start, now)
    result = market_data.get_daily_bars_cached(["AAPL", "MSFT"], start, now)

    assert fetch_calls == [("AAPL",), ("MSFT",)]
    assert set(result.keys()) == {"AAPL", "MSFT"}


def test_cached_cleans_up_old_cache_files(monkeypatch, isolated_cache):
    old_file = isolated_cache / "daily_bars_2020-01-01.pkl"
    old_file.write_bytes(b"stale")

    monkeypatch.setattr(market_data, "get_daily_bars", lambda symbols, start, end: {
        s: pd.DataFrame({"close": [100.0]}, index=[pd.Timestamp(start)]) for s in symbols
    })

    market_data.get_daily_bars_cached(["AAPL"], datetime.now() - timedelta(days=10), datetime.now())

    assert not old_file.exists()


def test_cached_keeps_recent_cache_files(monkeypatch, isolated_cache):
    recent_date = (datetime.now() - timedelta(days=2)).date().isoformat()
    recent_file = isolated_cache / f"daily_bars_{recent_date}.pkl"
    recent_file.write_bytes(b"still fresh")

    monkeypatch.setattr(market_data, "get_daily_bars", lambda symbols, start, end: {
        s: pd.DataFrame({"close": [100.0]}, index=[pd.Timestamp(start)]) for s in symbols
    })

    market_data.get_daily_bars_cached(["AAPL"], datetime.now() - timedelta(days=10), datetime.now())

    assert recent_file.exists()


# ------------------------------------------------------- get_latest_trade_prices

def test_get_latest_trade_prices_parses_floats(monkeypatch):
    fake_trades = {"AAPL": type("T", (), {"price": "150.25"})(), "MSFT": type("T", (), {"price": 300})()}
    fake_client = type("C", (), {"get_stock_latest_trade": lambda self, request: fake_trades})()
    monkeypatch.setattr(market_data, "_get_client", lambda: fake_client)

    result = market_data.get_latest_trade_prices(["AAPL", "MSFT"])

    assert result == {"AAPL": 150.25, "MSFT": 300.0}


# ------------------------------------------------------------- get_latest_quote

def test_get_latest_quote_parses_fields(monkeypatch):
    fake_quote = type("Q", (), {"bid_price": 149.5, "ask_price": 150.5, "timestamp": "2026-08-19T15:00:00Z"})()
    fake_client = type("C", (), {"get_stock_latest_quote": lambda self, request: {"AAPL": fake_quote}})()
    monkeypatch.setattr(market_data, "_get_client", lambda: fake_client)

    result = market_data.get_latest_quote("AAPL")

    assert result == {"symbol": "AAPL", "bid": 149.5, "ask": 150.5, "timestamp": "2026-08-19T15:00:00Z"}
