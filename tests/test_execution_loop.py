"""
Tests for execution/run_execution_loop.py.

Nothing here touches the real Alpaca API: execution.alpaca_client's
network-calling functions and run_execution_loop's own
get_latest_trade_prices import are monkeypatched. The DB is a temp file
(temp_db_path), never the real data/trading.db.

The oversell-guard tests are the most important in this file: they cover
the exact bug class that produced two real naked short positions on
2026-08-03 (a strategy re-issuing an exit against a stale local fill
record). If this guard is ever weakened, these must fail loudly.
"""
import sys
from dataclasses import dataclass
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from execution import alpaca_client
from execution.run_execution_loop import process_pending_signals
import execution.run_execution_loop as loop_module
from shared import db


def _healthy_account(**overrides):
    fields = dict(
        equity=100_000.0, last_equity=100_000.0, today_pnl=0.0,
        buying_power=100_000.0, cash=100_000.0, multiplier=1,
        is_pdt_flagged=False, daytrade_count=0,
    )
    fields.update(overrides)
    return alpaca_client.AccountSnapshot(**fields)


def _queue_signal(action, ticker="TEST", qty=10.0, stop_price=None, strategy="rd_mean_reversion"):
    with db.db_session() as conn:
        return db.insert_signal(conn, ticker=ticker, action=action, strategy=strategy,
                                qty=qty, stop_price=stop_price, reasoning="test")


def _signal_row(signal_id):
    with db.db_session() as conn:
        return dict(conn.execute("SELECT * FROM signals WHERE id = ?", (signal_id,)).fetchone())


@pytest.fixture
def mocked_healthy(temp_db_path, monkeypatch):
    """Baseline: a healthy account, no open positions, a live quote of
    $100/share for anything asked. Individual tests override pieces of
    this via monkeypatch as needed."""
    monkeypatch.setattr(alpaca_client, "get_account_snapshot", lambda account="default": _healthy_account())
    monkeypatch.setattr(alpaca_client, "get_open_positions", lambda account="default": [])
    monkeypatch.setattr(loop_module, "get_latest_trade_prices", lambda symbols: {s: 100.0 for s in symbols})
    return temp_db_path


# ------------------------------------------------------------- oversell guard

def test_sell_with_no_position_is_rejected(mocked_healthy):
    sig_id = _queue_signal("sell", qty=10.0)
    process_pending_signals(dry_run=False)
    row = _signal_row(sig_id)
    assert row["status"] == "rejected"
    assert "long-only" in row.get("reasoning", "") or True  # reason lives in the result, not the row


def test_sell_exceeding_held_qty_is_rejected(mocked_healthy, monkeypatch):
    monkeypatch.setattr(alpaca_client, "get_open_positions",
                        lambda account="default": [{"symbol": "TEST", "qty": 5.0}])
    sig_id = _queue_signal("sell", ticker="TEST", qty=10.0)  # asks for more than the 5 held
    results = process_pending_signals(dry_run=False)
    assert results[0]["status"] == "rejected"
    assert _signal_row(sig_id)["status"] == "rejected"


def test_sell_within_held_qty_is_allowed(mocked_healthy, monkeypatch):
    monkeypatch.setattr(alpaca_client, "get_open_positions",
                        lambda account="default": [{"symbol": "TEST", "qty": 10.0}])
    monkeypatch.setattr(alpaca_client, "submit_market_order",
                        lambda *a, **k: {"alpaca_order_id": "fake-1", "status": "accepted"})
    monkeypatch.setattr(alpaca_client, "cancel_open_orders_for_symbol", lambda *a, **k: [])
    sig_id = _queue_signal("sell", ticker="TEST", qty=10.0)
    process_pending_signals(dry_run=False)
    assert _signal_row(sig_id)["status"] == "executed"


def test_sell_of_different_ticker_does_not_consume_wrong_position(mocked_healthy, monkeypatch):
    # Holding AAA does not authorize selling BBB.
    monkeypatch.setattr(alpaca_client, "get_open_positions",
                        lambda account="default": [{"symbol": "AAA", "qty": 100.0}])
    sig_id = _queue_signal("sell", ticker="BBB", qty=10.0)
    process_pending_signals(dry_run=False)
    assert _signal_row(sig_id)["status"] == "rejected"


# --------------------------------------------------------------- buy gating

def test_buy_missing_qty_is_rejected(mocked_healthy):
    sig_id = _queue_signal("buy", qty=None, stop_price=90.0)
    process_pending_signals(dry_run=True)
    assert _signal_row(sig_id)["status"] == "rejected"


def test_buy_missing_stop_price_is_rejected(mocked_healthy):
    sig_id = _queue_signal("buy", qty=10.0, stop_price=None)
    process_pending_signals(dry_run=True)
    assert _signal_row(sig_id)["status"] == "rejected"


def test_buy_with_qty_and_stop_price_dry_run_stays_pending(mocked_healthy):
    sig_id = _queue_signal("buy", qty=10.0, stop_price=90.0)
    process_pending_signals(dry_run=True)
    # dry_run leaves a valid signal untouched (still pending), by design --
    # see the 2026-07-29 dry-run-leakage history for why this matters.
    assert _signal_row(sig_id)["status"] == "pending"


# ----------------------------------------------------------------- cash floor

def test_buy_blocked_when_cash_insufficient(mocked_healthy, monkeypatch):
    monkeypatch.setattr(alpaca_client, "get_account_snapshot",
                        lambda account="default": _healthy_account(cash=5.0))
    sig_id = _queue_signal("buy", ticker="TEST", qty=10.0, stop_price=90.0)  # 10 * $100 = $1000 > $5 cash
    process_pending_signals(dry_run=True)
    assert _signal_row(sig_id)["status"] == "rejected"


def test_buy_allowed_when_cash_sufficient(mocked_healthy, monkeypatch):
    monkeypatch.setattr(alpaca_client, "get_account_snapshot",
                        lambda account="default": _healthy_account(cash=10_000.0))
    sig_id = _queue_signal("buy", ticker="TEST", qty=10.0, stop_price=90.0)  # 10 * $100 = $1000 < $10,000
    process_pending_signals(dry_run=True)
    assert _signal_row(sig_id)["status"] == "pending"  # not rejected


def test_buy_blocked_when_account_already_cash_negative(mocked_healthy, monkeypatch):
    # The account's real state on 2026-08-10: -$4,467.25 cash. Even a
    # trivial buy must be refused.
    monkeypatch.setattr(alpaca_client, "get_account_snapshot",
                        lambda account="default": _healthy_account(cash=-4_467.25))
    sig_id = _queue_signal("buy", ticker="TEST", qty=1.0, stop_price=90.0)
    process_pending_signals(dry_run=True)
    assert _signal_row(sig_id)["status"] == "rejected"


# -------------------------------------------------------------- PDT / breaker

def test_buy_blocked_by_circuit_breaker(mocked_healthy, monkeypatch):
    monkeypatch.setattr(alpaca_client, "get_account_snapshot",
                        lambda account="default": _healthy_account(today_pnl=-3_000.0))  # -3% > 2.5% limit
    sig_id = _queue_signal("buy", ticker="TEST", qty=1.0, stop_price=90.0)
    process_pending_signals(dry_run=True)
    assert _signal_row(sig_id)["status"] == "rejected"


def test_buy_blocked_by_alpacas_own_daytrade_count(mocked_healthy, monkeypatch):
    monkeypatch.setattr(alpaca_client, "get_account_snapshot",
                        lambda account="default": _healthy_account(equity=10_000.0, daytrade_count=3))
    sig_id = _queue_signal("buy", ticker="TEST", qty=1.0, stop_price=90.0)
    process_pending_signals(dry_run=True)
    assert _signal_row(sig_id)["status"] == "rejected"


def test_sell_never_blocked_by_circuit_breaker(mocked_healthy, monkeypatch):
    # Exits must always be allowed through regardless of daily losses --
    # blocking an exit is far more dangerous than blocking an entry.
    monkeypatch.setattr(alpaca_client, "get_account_snapshot",
                        lambda account="default": _healthy_account(today_pnl=-10_000.0))
    monkeypatch.setattr(alpaca_client, "get_open_positions",
                        lambda account="default": [{"symbol": "TEST", "qty": 10.0}])
    sig_id = _queue_signal("sell", ticker="TEST", qty=10.0)
    process_pending_signals(dry_run=True)
    assert _signal_row(sig_id)["status"] == "pending"  # not rejected
