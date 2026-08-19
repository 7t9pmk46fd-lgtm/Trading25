"""
Tests for mcp_server.py -- the tool surface exposed to the agent.

Every tool here does its "real" imports lazily, inside the function body
(from execution import alpaca_client, from shared import db, etc.), so
monkeypatching targets the underlying modules directly rather than names
on mcp_server itself. No network, no real DB: alpaca_client/market_data
calls are stubbed, and DB-touching tools use the temp_db/temp_db_path
fixtures from conftest.

The one invariant worth calling out and testing explicitly: this server
must never place, modify, or cancel an order, and must never write to the
`signals` table -- these tests don't (and can't) prove a negative, but
every tool's own DB writes are checked to confirm they only ever touch
`research_notes`, never `signals` or `orders`.
"""
import sys
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import mcp_server
from execution import alpaca_client
from shared import db, market_data, risk
from shared import benchmark as benchmark_mod
import signals.screeners.mean_reversion as mean_reversion_mod


def _account_snapshot(**overrides):
    fields = dict(
        equity=100_000.0, last_equity=99_000.0, today_pnl=1_000.0, buying_power=200_000.0,
        cash=50_000.0, multiplier=2, is_pdt_flagged=False, daytrade_count=0,
    )
    fields.update(overrides)
    return SimpleNamespace(**fields)


def _bars(n=60, start_price=100.0):
    dates = pd.date_range("2026-06-01", periods=n, freq="D")
    closes = start_price + np.cumsum(np.random.default_rng(0).normal(0, 1, n))
    return pd.DataFrame({
        "close": closes, "high": closes + 1, "low": closes - 1, "volume": [1_000_000] * n,
    }, index=dates)


# ------------------------------------------------------------ portfolio_status

def test_portfolio_status_happy_path(monkeypatch, temp_db):
    monkeypatch.setattr(alpaca_client, "get_account_snapshot", lambda account="default": _account_snapshot())
    monkeypatch.setattr(alpaca_client, "get_open_positions", lambda account="default": [{"symbol": "AAPL"}])
    monkeypatch.setattr(alpaca_client, "get_open_stop_orders", lambda account="default": {"AAPL": {"stop_price": 90.0}})
    monkeypatch.setattr(market_data, "get_latest_trade_prices", lambda symbols: {"SPY": 450.0})
    db.set_account_baseline(temp_db, "default", "2026-08-01", 100_000.0, 440.0)
    monkeypatch.setattr(db, "db_session", lambda: _session_from_conn(temp_db))

    result = mcp_server.portfolio_status()

    assert result["equity"] == 100_000.0
    assert result["open_positions"] == [{"symbol": "AAPL"}]
    assert result["benchmark_vs_spy"]["alpha_pct"] == pytest.approx(
        (100_000.0 / 100_000.0 - 1) * 100 - (450.0 / 440.0 - 1) * 100
    )


def test_portfolio_status_benchmark_failure_does_not_crash(monkeypatch, temp_db):
    monkeypatch.setattr(alpaca_client, "get_account_snapshot", lambda account="default": _account_snapshot())
    monkeypatch.setattr(alpaca_client, "get_open_positions", lambda account="default": [])
    monkeypatch.setattr(alpaca_client, "get_open_stop_orders", lambda account="default": {})

    def boom(symbols):
        raise RuntimeError("no network")

    monkeypatch.setattr(market_data, "get_latest_trade_prices", boom)
    monkeypatch.setattr(db, "db_session", lambda: _session_from_conn(temp_db))

    result = mcp_server.portfolio_status()

    assert "error" in result["benchmark_vs_spy"]
    assert result["equity"] == 100_000.0  # rest of the tool still returns normally


def test_portfolio_status_no_baseline_returns_none_benchmark(monkeypatch, temp_db):
    monkeypatch.setattr(alpaca_client, "get_account_snapshot", lambda account="default": _account_snapshot())
    monkeypatch.setattr(alpaca_client, "get_open_positions", lambda account="default": [])
    monkeypatch.setattr(alpaca_client, "get_open_stop_orders", lambda account="default": {})
    monkeypatch.setattr(market_data, "get_latest_trade_prices", lambda symbols: {"SPY": 450.0})
    monkeypatch.setattr(db, "db_session", lambda: _session_from_conn(temp_db))

    result = mcp_server.portfolio_status()

    assert result["benchmark_vs_spy"] is None


from contextlib import contextmanager


@contextmanager
def _session_from_conn(conn):
    # temp_db yields a single open connection; portfolio_status calls
    # db.db_session() (a context manager) rather than taking a connection
    # directly, so tests reuse the same underlying connection through it.
    yield conn


# ------------------------------------------------------------- recent_activity

def test_recent_activity_filters_by_cutoff_and_joins_strategy(temp_db_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", temp_db_path)
    with db.db_session() as conn:
        signal_id = db.insert_signal(conn, "AAPL", "buy", "rd_mean_reversion")
        conn.execute(
            """INSERT INTO orders (signal_id, ticker, side, qty, status, submitted_at)
               VALUES (?, 'AAPL', 'buy', 10, 'submitted', datetime('now'))""",
            (signal_id,),
        )
        old_signal_id = db.insert_signal(conn, "MSFT", "buy", "rd_mean_reversion")
        conn.execute(
            "UPDATE signals SET created_at = '2020-01-01 00:00:00' WHERE id = ?", (old_signal_id,)
        )

    result = mcp_server.recent_activity(days=3)

    tickers = {s["ticker"] for s in result["signals"]}
    assert "AAPL" in tickers
    assert "MSFT" not in tickers
    assert result["orders"][0]["strategy"] == "rd_mean_reversion"


# --------------------------------------------------------- historical_analysis

def test_historical_analysis_no_data(monkeypatch):
    monkeypatch.setattr(market_data, "get_daily_bars", lambda symbols, start, end: {})
    result = mcp_server.historical_analysis("ZZZZ")
    assert result == {"symbol": "ZZZZ", "error": "no data returned"}


def test_historical_analysis_computes_metrics(monkeypatch):
    bars = _bars(60)
    monkeypatch.setattr(market_data, "get_daily_bars", lambda symbols, start, end: {"AAPL": bars})

    result = mcp_server.historical_analysis("aapl", days=90)

    assert result["symbol"] == "AAPL"  # uppercased
    assert result["bars"] == 60
    assert result["last_close"] == pytest.approx(float(bars["close"].iloc[-1]))
    assert result["period_high"] == pytest.approx(float(bars["high"].max()))
    assert result["period_low"] == pytest.approx(float(bars["low"].min()))
    assert isinstance(result["annualized_volatility_pct"], float)


def test_historical_analysis_handles_nan_atr_and_zscore(monkeypatch):
    # Too little history for a 14-day ATR / 20-day z-score -> both must
    # come back as None, not NaN (which isn't valid JSON).
    bars = _bars(5)
    monkeypatch.setattr(market_data, "get_daily_bars", lambda symbols, start, end: {"AAPL": bars})

    result = mcp_server.historical_analysis("AAPL")

    assert result["atr_14"] is None
    assert result["zscore_20d"] is None


# -------------------------------------------------------------- run_screener

def test_run_screener_unknown_strategy():
    result = mcp_server.run_screener(strategy="momentum")
    assert "error" in result


def test_run_screener_mean_reversion_reports_per_ticker_status(monkeypatch):
    import shared.config as config_mod

    monkeypatch.setattr(config_mod, "TRADE_UNIVERSE", ["AAPL", "ZZZZ"])
    bars = _bars(60)
    monkeypatch.setattr(market_data, "get_daily_bars", lambda symbols, start, end: {"AAPL": bars})

    result = mcp_server.run_screener(symbols=["AAPL", "ZZZZ"])

    by_ticker = {r["ticker"]: r for r in result["results"]}
    assert by_ticker["ZZZZ"]["status"] == "no_data"
    assert by_ticker["AAPL"]["status"] in ("signal", "no_signal")
    assert "close" in by_ticker["AAPL"]


def test_run_screener_never_queues_signals(monkeypatch, temp_db_path):
    # Hard boundary check: analysis-only, must never write to `signals`.
    monkeypatch.setattr(db, "DB_PATH", temp_db_path)
    bars = _bars(60)
    monkeypatch.setattr(market_data, "get_daily_bars", lambda symbols, start, end: {"AAPL": bars})

    mcp_server.run_screener(symbols=["AAPL"])

    with db.db_session() as conn:
        count = conn.execute("SELECT COUNT(*) AS n FROM signals").fetchone()["n"]
    assert count == 0


# ----------------------------------------------------------------- risk_check

def test_risk_check_uses_caller_supplied_stop_price(monkeypatch, temp_db_path):
    monkeypatch.setattr(db, "DB_PATH", temp_db_path)
    monkeypatch.setattr(alpaca_client, "get_account_snapshot", lambda account="default": _account_snapshot())

    result = mcp_server.risk_check("AAPL", entry_price=100.0, stop_price=90.0)

    assert result["stop_source"] == "caller"
    assert result["stop_price"] == 90.0
    assert result["sizing"]["qty"] > 0
    assert result["risk_gates"]["pdt"] == "clear"
    assert result["risk_gates"]["circuit_breaker"] == "clear"


def test_risk_check_derives_stop_from_atr_when_omitted(monkeypatch, temp_db_path):
    monkeypatch.setattr(db, "DB_PATH", temp_db_path)
    monkeypatch.setattr(alpaca_client, "get_account_snapshot", lambda account="default": _account_snapshot())
    bars = _bars(60, start_price=100.0)
    monkeypatch.setattr(market_data, "get_daily_bars", lambda symbols, start, end: {"AAPL": bars})

    result = mcp_server.risk_check("AAPL", entry_price=100.0)

    assert result["stop_source"] == "2xATR(14) default"
    assert result["stop_price"] < 100.0


def test_risk_check_no_bars_and_no_stop_price_errors(monkeypatch, temp_db_path):
    monkeypatch.setattr(db, "DB_PATH", temp_db_path)
    monkeypatch.setattr(alpaca_client, "get_account_snapshot", lambda account="default": _account_snapshot())
    monkeypatch.setattr(market_data, "get_daily_bars", lambda symbols, start, end: {})

    result = mcp_server.risk_check("ZZZZ", entry_price=100.0)

    assert "error" in result


def test_risk_check_pdt_blocked_under_25k_after_3_day_trades(monkeypatch, temp_db_path):
    monkeypatch.setattr(db, "DB_PATH", temp_db_path)
    monkeypatch.setattr(alpaca_client, "get_account_snapshot", lambda account="default": _account_snapshot(equity=10_000.0))

    # 3 same-day round trips within the last 5 days -> PDT limit reached.
    with db.db_session() as conn:
        for i in range(3):
            sig = db.insert_signal(conn, f"T{i}", "buy", "rd_mean_reversion")
            conn.execute(
                """INSERT INTO orders (signal_id, ticker, side, qty, status, filled_at)
                   VALUES (?, ?, 'buy', 10, 'filled', datetime('now'))""",
                (sig, f"T{i}"),
            )
            conn.execute(
                """INSERT INTO orders (signal_id, ticker, side, qty, status, filled_at)
                   VALUES (?, ?, 'sell', 10, 'filled', datetime('now'))""",
                (sig, f"T{i}"),
            )

    result = mcp_server.risk_check("AAPL", entry_price=100.0, stop_price=90.0)

    assert "BLOCKED" in result["risk_gates"]["pdt"]


def test_risk_check_circuit_breaker_blocked_on_large_daily_loss(monkeypatch, temp_db_path):
    monkeypatch.setattr(db, "DB_PATH", temp_db_path)
    monkeypatch.setattr(
        alpaca_client, "get_account_snapshot",
        lambda account="default": _account_snapshot(equity=100_000.0, today_pnl=-5_000.0),
    )

    result = mcp_server.risk_check("AAPL", entry_price=100.0, stop_price=90.0)

    assert "BLOCKED" in result["risk_gates"]["circuit_breaker"]


def test_risk_check_uppercases_symbol(monkeypatch, temp_db_path):
    monkeypatch.setattr(db, "DB_PATH", temp_db_path)
    monkeypatch.setattr(alpaca_client, "get_account_snapshot", lambda account="default": _account_snapshot())

    result = mcp_server.risk_check("aapl", entry_price=100.0, stop_price=90.0)

    assert result["symbol"] == "AAPL"


def test_risk_check_never_writes_orders_or_signals(monkeypatch, temp_db_path):
    # Hard boundary check: pure simulation.
    monkeypatch.setattr(db, "DB_PATH", temp_db_path)
    monkeypatch.setattr(alpaca_client, "get_account_snapshot", lambda account="default": _account_snapshot())

    mcp_server.risk_check("AAPL", entry_price=100.0, stop_price=90.0)

    with db.db_session() as conn:
        assert conn.execute("SELECT COUNT(*) AS n FROM signals").fetchone()["n"] == 0
        assert conn.execute("SELECT COUNT(*) AS n FROM orders").fetchone()["n"] == 0


# ------------------------------------------------------------------- backtest

def test_backtest_unknown_strategy():
    result = mcp_server.backtest(strategy="momentum")
    assert "error" in result


def test_backtest_no_usable_data_for_any_symbol(monkeypatch):
    import shared.config as config_mod

    monkeypatch.setattr(config_mod, "TRADE_UNIVERSE", ["AAPL"])
    monkeypatch.setattr(market_data, "get_daily_bars", lambda symbols, start, end: {})

    result = mcp_server.backtest(symbols=["AAPL"])

    assert result == {"error": "no usable data for any requested symbol"}


def test_backtest_happy_path_reports_metrics_and_spy_comparison(monkeypatch):
    bars = _bars(400, start_price=100.0)
    spy_bars = _bars(400, start_price=450.0)
    monkeypatch.setattr(
        market_data, "get_daily_bars",
        lambda symbols, start, end: {"AAPL": bars, "SPY": spy_bars},
    )

    result = mcp_server.backtest(symbols=["AAPL"], lookback_days=365)

    assert result["strategy"] == "mean_reversion"
    assert "AAPL" in result["symbols"]
    assert "total_return" in result["metrics"]
    assert result["spy_buy_and_hold_return"] is not None
    assert result["beats_spy"] in (True, False)


def test_backtest_never_writes_to_backtest_runs_table(monkeypatch, temp_db_path):
    # Docstring is explicit: results are NOT logged to backtest_runs from
    # this tool -- only signals/backtest/*.py scripts do that.
    monkeypatch.setattr(db, "DB_PATH", temp_db_path)
    bars = _bars(400, start_price=100.0)
    monkeypatch.setattr(market_data, "get_daily_bars", lambda symbols, start, end: {"AAPL": bars, "SPY": bars})

    mcp_server.backtest(symbols=["AAPL"], lookback_days=365)

    with db.db_session() as conn:
        assert conn.execute("SELECT COUNT(*) AS n FROM backtest_runs").fetchone()["n"] == 0


# -------------------------------------------------------------- market_research

def test_market_research_delegates_to_ingest_source(monkeypatch):
    import analyst.ingest_source as ingest_source_mod

    monkeypatch.setattr(ingest_source_mod, "ingest_source", lambda url: 42)

    result = mcp_server.market_research("https://example.com/article")

    assert result["stored_research_note_id"] == 42


# ---------------------------------------------------------- list_research_notes

def test_list_research_notes_filters_by_status(temp_db_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", temp_db_path)
    with db.db_session() as conn:
        db.insert_research_note(conn, "news_article", "https://a", source_title="A")
        promising_id = db.insert_research_note(conn, "news_article", "https://b", source_title="B")
        conn.execute("UPDATE research_notes SET status = 'reviewed_promising' WHERE id = ?", (promising_id,))

    unreviewed = mcp_server.list_research_notes(status="unreviewed")
    assert len(unreviewed) == 1
    assert unreviewed[0]["source_title"] == "A"

    promising = mcp_server.list_research_notes(status="reviewed_promising")
    assert len(promising) == 1
    assert promising[0]["source_title"] == "B"

    everything = mcp_server.list_research_notes(status="all")
    assert len(everything) == 2


def test_list_research_notes_respects_limit(temp_db_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", temp_db_path)
    with db.db_session() as conn:
        for i in range(5):
            db.insert_research_note(conn, "news_article", f"https://{i}")

    result = mcp_server.list_research_notes(status="all", limit=2)

    assert len(result) == 2
