"""
Tests for signals/generate_signal.py.

_plan_one is the piece unique to this module -- it turns a raw screener
signal into a sized, actionable order, using REAL position/account state
(not the screener's own no-memory simulation). The oversell-guard tests in
test_execution_loop.py assume signals arriving downstream are already
sane; these tests cover the guards that make that true in the first
place: the already-holding guard, the not-held exit guard, the ATR/stop
refusal, and the unrecognized-signal fail-fast. generate_and_queue_batch's
ranking/rationing logic (entries sorted by conviction, capped at
MAX_CONCURRENT_POSITIONS, exits never rationed) is covered by mocking
_plan_one directly so ranking can be tested independent of screener logic.

Nothing here touches the real DB or Alpaca -- db.db_session is monkeypatched
per test that queues, and get_account/get_held_positions/get_held_qty are
passed in directly as fakes (that's the module's own seam for this).
"""
import sys
from pathlib import Path

import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from shared import db
import signals.generate_signal as gs


def _bars():
    # _plan_one only reads all_bars[ticker] to pass to compute_atr / index
    # sig["close"] -- content doesn't matter once generate_signals/
    # latest_signal/compute_atr are monkeypatched, just needs to be non-empty.
    return pd.DataFrame({"close": [100.0] * 30, "volume": [1_000_000] * 30})


def _signal(kind="enter_long", zscore=-2.0, close=100.0):
    return {"signal": kind, "zscore": zscore, "close": close}


@pytest.fixture
def stub_screener(monkeypatch):
    """Bypasses the real screener math -- generate_signals/latest_signal
    are monkeypatched to return whatever this test wants, so _plan_one's
    OWN logic (not the screener's) is what's under test."""
    def _set(signal_dict):
        monkeypatch.setattr(gs, "generate_signals", lambda df, params: df)
        monkeypatch.setattr(gs, "latest_signal", lambda result: signal_dict)
    return _set


# ------------------------------------------------------------- no data

def test_plan_one_no_data_for_missing_ticker():
    plan = gs._plan_one("AAPL", {}, lambda: None, lambda: set(), lambda: {})
    assert plan == {"ticker": "AAPL", "status": "no_data"}


def test_plan_one_no_data_for_empty_bars():
    plan = gs._plan_one("AAPL", {"AAPL": pd.DataFrame()}, lambda: None, lambda: set(), lambda: {})
    assert plan == {"ticker": "AAPL", "status": "no_data"}


# ------------------------------------------------------------- no signal

def test_plan_one_no_signal_returns_zscore(monkeypatch):
    df_with_z = _bars().assign(zscore=[-0.5] * 30)
    monkeypatch.setattr(gs, "generate_signals", lambda df, params: df_with_z)
    monkeypatch.setattr(gs, "latest_signal", lambda result: None)

    plan = gs._plan_one("AAPL", {"AAPL": _bars()}, lambda: None, lambda: set(), lambda: {})

    assert plan == {"ticker": "AAPL", "status": "no_signal", "zscore": -0.5}


def test_plan_one_no_signal_handles_nan_zscore(monkeypatch):
    import numpy as np
    df_with_z = _bars().assign(zscore=[np.nan] * 30)
    monkeypatch.setattr(gs, "generate_signals", lambda df, params: df_with_z)
    monkeypatch.setattr(gs, "latest_signal", lambda result: None)

    plan = gs._plan_one("AAPL", {"AAPL": _bars()}, lambda: None, lambda: set(), lambda: {})

    assert plan["status"] == "no_signal"
    assert plan["zscore"] is None


# --------------------------------------------------------- unknown signal

def test_plan_one_raises_on_unrecognized_signal(stub_screener):
    stub_screener(_signal(kind="something_weird"))
    with pytest.raises(ValueError):
        gs._plan_one("AAPL", {"AAPL": _bars()}, lambda: None, lambda: set(), lambda: {})


# -------------------------------------------------------------- buy path

def test_plan_one_buy_skips_when_already_holding(stub_screener):
    stub_screener(_signal(kind="enter_long"))
    plan = gs._plan_one("AAPL", {"AAPL": _bars()}, lambda: None, lambda: {"AAPL"}, lambda: {})
    assert plan == {"ticker": "AAPL", "status": "skipped_already_holding", "zscore": -2.0}


def test_plan_one_buy_refuses_when_no_atr_data(stub_screener, monkeypatch):
    stub_screener(_signal(kind="enter_long"))
    monkeypatch.setattr(gs, "compute_atr", lambda bars: float("nan"))

    plan = gs._plan_one("AAPL", {"AAPL": _bars()}, lambda: None, lambda: set(), lambda: {})

    assert plan["status"] == "no_stop_data"


def test_plan_one_buy_refuses_when_atr_is_zero(stub_screener, monkeypatch):
    stub_screener(_signal(kind="enter_long"))
    monkeypatch.setattr(gs, "compute_atr", lambda bars: 0.0)

    plan = gs._plan_one("AAPL", {"AAPL": _bars()}, lambda: None, lambda: set(), lambda: {})

    assert plan["status"] == "no_stop_data"


def test_plan_one_buy_refuses_when_stop_price_non_positive(stub_screener, monkeypatch):
    # entry_price - 2*atr <= 0 -- a degenerate low-price name where the
    # standard stop distance would put the stop below zero.
    stub_screener(_signal(kind="enter_long", close=1.0))
    monkeypatch.setattr(gs, "compute_atr", lambda bars: 10.0)

    plan = gs._plan_one("AAPL", {"AAPL": _bars()}, lambda: None, lambda: set(), lambda: {})

    assert plan["status"] == "no_stop_data"


def test_plan_one_buy_refuses_when_size_too_small(stub_screener, monkeypatch):
    stub_screener(_signal(kind="enter_long", close=100.0))
    monkeypatch.setattr(gs, "compute_atr", lambda bars: 5.0)

    from types import SimpleNamespace
    tiny_account = SimpleNamespace(equity=1.0)  # $1 equity -> qty rounds to 0

    plan = gs._plan_one("AAPL", {"AAPL": _bars()}, lambda: tiny_account, lambda: set(), lambda: {})

    assert plan["status"] == "size_too_small"


def test_plan_one_buy_actionable_sizes_from_2x_atr_stop(stub_screener, monkeypatch):
    stub_screener(_signal(kind="enter_long", close=100.0, zscore=-2.3))
    monkeypatch.setattr(gs, "compute_atr", lambda bars: 5.0)

    from types import SimpleNamespace
    account = SimpleNamespace(equity=100_000.0)

    plan = gs._plan_one("AAPL", {"AAPL": _bars()}, lambda: account, lambda: set(), lambda: {})

    assert plan["status"] == "actionable"
    assert plan["action"] == "buy"
    assert plan["stop_price"] == pytest.approx(90.0)  # 100 - 2*5
    assert plan["qty"] > 0
    assert plan["zscore"] == pytest.approx(-2.3)


def test_plan_one_exit_short_treated_as_buy(stub_screener, monkeypatch):
    stub_screener(_signal(kind="exit_short", close=100.0))
    monkeypatch.setattr(gs, "compute_atr", lambda bars: 5.0)

    from types import SimpleNamespace
    account = SimpleNamespace(equity=100_000.0)

    plan = gs._plan_one("AAPL", {"AAPL": _bars()}, lambda: account, lambda: set(), lambda: {})

    assert plan["action"] == "buy"


# ------------------------------------------------------------- sell path

def test_plan_one_sell_skips_when_not_held(stub_screener):
    stub_screener(_signal(kind="exit_long"))
    plan = gs._plan_one("AAPL", {"AAPL": _bars()}, lambda: None, lambda: set(), lambda: {})
    assert plan == {"ticker": "AAPL", "status": "skipped_not_held", "zscore": -2.0}


def test_plan_one_sell_skips_when_held_qty_is_zero(stub_screener):
    # get_held_qty().get(ticker) can legitimately return 0 -- `if not qty`
    # must treat that the same as "not held", not try to sell 0 shares.
    stub_screener(_signal(kind="exit_long"))
    plan = gs._plan_one("AAPL", {"AAPL": _bars()}, lambda: None, lambda: set(), lambda: {"AAPL": 0})
    assert plan["status"] == "skipped_not_held"


def test_plan_one_sell_actionable_uses_real_held_qty(stub_screener):
    # Regression guard for the 2026-07-28 NFLX bug: an exit signal must
    # carry the REAL held qty, not None -- run_execution_loop rejects
    # qty=None unconditionally.
    stub_screener(_signal(kind="exit_long", zscore=0.3))
    plan = gs._plan_one("AAPL", {"AAPL": _bars()}, lambda: None, lambda: set(), lambda: {"AAPL": 42})

    assert plan["status"] == "actionable"
    assert plan["action"] == "sell"
    assert plan["qty"] == 42
    assert plan["stop_price"] is None


# ------------------------------------------------- generate_and_queue_batch

def test_batch_queues_exits_unconditionally_even_over_capacity(monkeypatch, temp_db_path):
    # Exits are never rationed by MAX_CONCURRENT_POSITIONS -- only entries are.
    monkeypatch.setattr(gs, "MAX_CONCURRENT_POSITIONS", 0)

    def fake_plan_one(ticker, all_bars, get_account, get_held_positions, get_held_qty):
        return {"ticker": ticker, "status": "actionable", "action": "sell",
                "qty": 5, "stop_price": None, "reasoning": "exit", "zscore": 0.1}

    monkeypatch.setattr(gs, "_plan_one", fake_plan_one)
    monkeypatch.setattr(gs, "get_daily_bars", lambda tickers, start, end: {t: _bars() for t in tickers})
    monkeypatch.setattr("execution.alpaca_client.get_open_positions", lambda: [])
    monkeypatch.setattr("execution.alpaca_client.get_open_buy_order_symbols", lambda: set())

    results = gs.generate_and_queue_batch(["AAPL"])

    assert results[0]["status"] == "queued"
    assert results[0]["action"] == "sell"


def test_batch_ranks_entries_by_most_negative_zscore_and_caps_at_capacity(monkeypatch, temp_db_path):
    monkeypatch.setattr(gs, "MAX_CONCURRENT_POSITIONS", 1)

    plans_by_ticker = {
        "AAPL": {"ticker": "AAPL", "status": "actionable", "action": "buy",
                 "qty": 1, "stop_price": 90.0, "reasoning": "r", "zscore": -1.6},
        "MSFT": {"ticker": "MSFT", "status": "actionable", "action": "buy",
                 "qty": 1, "stop_price": 90.0, "reasoning": "r", "zscore": -3.0},
    }

    def fake_plan_one(ticker, all_bars, get_account, get_held_positions, get_held_qty):
        return plans_by_ticker[ticker]

    monkeypatch.setattr(gs, "_plan_one", fake_plan_one)
    monkeypatch.setattr(gs, "get_daily_bars", lambda tickers, start, end: {t: _bars() for t in tickers})
    monkeypatch.setattr("execution.alpaca_client.get_open_positions", lambda: [])
    monkeypatch.setattr("execution.alpaca_client.get_open_buy_order_symbols", lambda: set())

    results = gs.generate_and_queue_batch(["AAPL", "MSFT"])

    by_ticker = {r["ticker"]: r for r in results}
    # MSFT (-3.0, more stretched) must win the single free slot over AAPL (-1.6).
    assert by_ticker["MSFT"]["status"] == "queued"
    assert by_ticker["MSFT"]["rank"] == 1
    assert by_ticker["AAPL"]["status"] == "skipped_position_limit"
    assert by_ticker["AAPL"]["rank"] == 2


def test_batch_capacity_accounts_for_already_held_positions(monkeypatch, temp_db_path):
    # capacity = MAX_CONCURRENT_POSITIONS - len(held positions) -- a nearly
    # full book must ration new entries even with a high raw ceiling.
    monkeypatch.setattr(gs, "MAX_CONCURRENT_POSITIONS", 5)

    plan = {"ticker": "AAPL", "status": "actionable", "action": "buy",
            "qty": 1, "stop_price": 90.0, "reasoning": "r", "zscore": -2.0}

    monkeypatch.setattr(gs, "_plan_one", lambda *a, **k: plan)
    monkeypatch.setattr(gs, "get_daily_bars", lambda tickers, start, end: {t: _bars() for t in tickers})
    # Force get_held_positions() (inside the batch's own _cache closures) to
    # report 5 held positions already -- capacity becomes 0.
    monkeypatch.setattr(
        "execution.alpaca_client.get_open_positions",
        lambda: [{"symbol": f"T{i}"} for i in range(5)],
    )
    monkeypatch.setattr("execution.alpaca_client.get_open_buy_order_symbols", lambda: set())

    results = gs.generate_and_queue_batch(["AAPL"])

    assert results[0]["status"] == "skipped_position_limit"


def test_batch_isolates_per_ticker_errors(monkeypatch, temp_db_path):
    # A bug/exception evaluating one ticker must not cost the rest of the
    # batch its signals.
    def fake_plan_one(ticker, all_bars, get_account, get_held_positions, get_held_qty):
        if ticker == "BAD":
            raise ValueError("boom")
        return {"ticker": ticker, "status": "no_signal", "zscore": 0.0}

    monkeypatch.setattr(gs, "_plan_one", fake_plan_one)
    monkeypatch.setattr(gs, "get_daily_bars", lambda tickers, start, end: {t: _bars() for t in tickers})
    monkeypatch.setattr("execution.alpaca_client.get_open_positions", lambda: [])
    monkeypatch.setattr("execution.alpaca_client.get_open_buy_order_symbols", lambda: set())

    results = gs.generate_and_queue_batch(["BAD", "AAPL"])

    by_ticker = {r["ticker"]: r for r in results}
    assert by_ticker["BAD"]["status"] == "error"
    assert "boom" in by_ticker["BAD"]["error"]
    assert by_ticker["AAPL"]["status"] == "no_signal"
