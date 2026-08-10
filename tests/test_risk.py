"""
Tests for shared/risk.py.

Every one of these covers a rule that has a real consequence if it's
wrong: PDT is a FINRA requirement, the circuit breaker and cash floor
guard real money (paper or not), and the realized-P&L FIFO logic is the
same class of "which fill matches which" reasoning that produced two
naked shorts on 2026-08-03 when it was done informally instead of
mechanically.
"""
import sys
from datetime import datetime, timedelta
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from shared import risk


# --------------------------------------------------------------- sizing

def test_position_size_risk_based_binds_when_stop_is_wide():
    # Counterintuitive but correct: a WIDER stop means MORE risk per share,
    # which means the $1000 risk budget buys FEWER shares -- so a wide
    # stop is what makes the risk-based number the smaller (binding) one.
    limits = risk.RiskLimits(account_equity=100_000, target_risk_per_trade_pct=0.01, max_position_pct=0.10)
    # risk_per_share=$10 -> risk-based qty = 1000/10 = 100.
    # cap: 10% of $100k = $10,000 / $50 entry = 200 shares.
    # 100 < 200, so risk-based binds.
    result = risk.compute_position_size(limits, entry_price=50.0, stop_loss_price=40.0)
    assert result["binding_constraint"] == "risk_per_trade"
    assert result["qty"] == 100
    assert result["max_loss_if_stopped"] == pytest.approx(1000.0, abs=1.0)


def test_position_size_cap_binds_when_stop_is_tight():
    # A tight stop means LESS risk per share, so the risk budget alone
    # would buy a large number of shares -- large enough that the 10%
    # equity cap becomes the binding (smaller) constraint instead.
    limits = risk.RiskLimits(account_equity=100_000, target_risk_per_trade_pct=0.01, max_position_pct=0.10)
    # risk_per_share=$2 -> risk-based qty = 1000/2 = 500.
    # cap: 10% of $100k = $10,000 / $50 entry = 200 shares.
    # 200 < 500, so the cap binds.
    result = risk.compute_position_size(limits, entry_price=50.0, stop_loss_price=48.0)
    assert result["binding_constraint"] == "max_position_pct"
    assert result["qty"] == 200


def test_position_size_rejects_equal_entry_and_stop():
    limits = risk.RiskLimits(account_equity=100_000)
    with pytest.raises(ValueError):
        risk.compute_position_size(limits, entry_price=50.0, stop_loss_price=50.0)


def test_position_size_rejects_non_positive_prices():
    limits = risk.RiskLimits(account_equity=100_000)
    with pytest.raises(ValueError):
        risk.compute_position_size(limits, entry_price=-10.0, stop_loss_price=5.0)


# ------------------------------------------------------------------ PDT

def _insert_filled_order(conn, ticker, side, qty, filled_at, strategy="rd_mean_reversion"):
    sig_id = conn.execute(
        "INSERT INTO signals (ticker, action, strategy, qty) VALUES (?, ?, ?, ?)",
        (ticker, side, strategy, qty),
    ).lastrowid
    conn.execute(
        "INSERT INTO orders (signal_id, ticker, side, qty, filled_at, fill_price) VALUES (?, ?, ?, ?, ?, ?)",
        (sig_id, ticker, side, qty, filled_at, 100.0),
    )


def test_pdt_blocks_at_three_day_trades(temp_db):
    conn = temp_db
    today = datetime.now().strftime("%Y-%m-%d 12:00:00")
    for ticker in ("AAA", "BBB", "CCC"):
        _insert_filled_order(conn, ticker, "buy", 10, today)
        _insert_filled_order(conn, ticker, "sell", 10, today)
    limits = risk.RiskLimits(account_equity=10_000, is_pdt_account=False)
    with pytest.raises(risk.RiskViolation):
        risk.check_pdt_allows_trade(conn, limits)


def test_pdt_allows_below_three_day_trades(temp_db):
    conn = temp_db
    today = datetime.now().strftime("%Y-%m-%d 12:00:00")
    for ticker in ("AAA", "BBB"):
        _insert_filled_order(conn, ticker, "buy", 10, today)
        _insert_filled_order(conn, ticker, "sell", 10, today)
    limits = risk.RiskLimits(account_equity=10_000, is_pdt_account=False)
    risk.check_pdt_allows_trade(conn, limits)  # must not raise


def test_pdt_exempt_account_skips_check_regardless_of_count(temp_db):
    conn = temp_db
    today = datetime.now().strftime("%Y-%m-%d 12:00:00")
    for ticker in ("AAA", "BBB", "CCC", "DDD"):
        _insert_filled_order(conn, ticker, "buy", 10, today)
        _insert_filled_order(conn, ticker, "sell", 10, today)
    limits = risk.RiskLimits(account_equity=30_000, is_pdt_account=True)
    risk.check_pdt_allows_trade(conn, limits)  # must not raise -- equity >= $25k


def test_pdt_only_counts_within_lookback_window(temp_db):
    conn = temp_db
    stale = (datetime.now() - timedelta(days=10)).strftime("%Y-%m-%d 12:00:00")
    for ticker in ("AAA", "BBB", "CCC"):
        _insert_filled_order(conn, ticker, "buy", 10, stale)
        _insert_filled_order(conn, ticker, "sell", 10, stale)
    limits = risk.RiskLimits(account_equity=10_000, is_pdt_account=False)
    risk.check_pdt_allows_trade(conn, limits)  # 3 day trades, but all 10 days old


# ------------------------------------------------------------- circuit breaker

def test_circuit_breaker_blocks_at_loss_limit():
    limits = risk.RiskLimits(account_equity=100_000, daily_loss_limit_pct=0.025)
    with pytest.raises(risk.RiskViolation):
        risk.check_circuit_breaker(None, limits, current_daily_pnl=-2_500.0)


def test_circuit_breaker_allows_loss_under_limit():
    limits = risk.RiskLimits(account_equity=100_000, daily_loss_limit_pct=0.025)
    risk.check_circuit_breaker(None, limits, current_daily_pnl=-2_000.0)  # must not raise


def test_circuit_breaker_allows_gains():
    limits = risk.RiskLimits(account_equity=100_000, daily_loss_limit_pct=0.025)
    risk.check_circuit_breaker(None, limits, current_daily_pnl=5_000.0)  # must not raise


# ---------------------------------------------------------------- cash floor

def test_cash_floor_blocks_when_order_exceeds_cash():
    with pytest.raises(risk.RiskViolation):
        risk.check_cash_floor(cash=100.0, order_cost=200.0)


def test_cash_floor_blocks_when_already_negative():
    # This is the account's REAL current state (confirmed -$4,467.25 on
    # 2026-08-10) -- even a trivial order must be refused, not just large ones.
    with pytest.raises(risk.RiskViolation):
        risk.check_cash_floor(cash=-4_467.25, order_cost=1.0)


def test_cash_floor_allows_when_cash_covers_order():
    risk.check_cash_floor(cash=1_000.0, order_cost=500.0)  # must not raise


def test_cash_floor_boundary_exactly_zero_is_allowed():
    # Leaves exactly $0.00, which is the floor itself, not below it.
    risk.check_cash_floor(cash=500.0, order_cost=500.0)  # must not raise


# ------------------------------------------------------------- realized P&L

def test_realized_pnl_single_round_trip(temp_db):
    conn = temp_db
    today = datetime.now().strftime("%Y-%m-%d 10:00:00")
    conn.execute("INSERT INTO orders (ticker, side, qty, fill_price, filled_at) VALUES ('TEST','buy',10,100.0,?)", (today,))
    conn.execute("INSERT INTO orders (ticker, side, qty, fill_price, filled_at) VALUES ('TEST','sell',10,110.0,?)", (today,))
    assert risk.get_today_realized_pnl(conn) == pytest.approx(100.0)


def test_realized_pnl_ignores_sells_from_prior_days(temp_db):
    conn = temp_db
    yesterday = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d 10:00:00")
    conn.execute("INSERT INTO orders (ticker, side, qty, fill_price, filled_at) VALUES ('TEST','buy',10,100.0,?)", (yesterday,))
    conn.execute("INSERT INTO orders (ticker, side, qty, fill_price, filled_at) VALUES ('TEST','sell',10,110.0,?)", (yesterday,))
    assert risk.get_today_realized_pnl(conn) == pytest.approx(0.0)


def test_realized_pnl_fifo_matches_oldest_lot_first(temp_db):
    conn = temp_db
    yesterday = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d 10:00:00")
    today = datetime.now().strftime("%Y-%m-%d 10:00:00")
    # Two buy lots on different days at different prices; a same-day sell
    # should consume the OLDER (cheaper) lot first.
    conn.execute("INSERT INTO orders (ticker, side, qty, fill_price, filled_at) VALUES ('TEST','buy',10,100.0,?)", (yesterday,))
    conn.execute("INSERT INTO orders (ticker, side, qty, fill_price, filled_at) VALUES ('TEST','buy',10,150.0,?)", (today,))
    conn.execute("INSERT INTO orders (ticker, side, qty, fill_price, filled_at) VALUES ('TEST','sell',10,160.0,?)", (today,))
    # FIFO: matches the $100 lot, not the $150 one -> (160-100)*10 = 600, not (160-150)*10 = 100.
    assert risk.get_today_realized_pnl(conn) == pytest.approx(600.0)


def test_realized_pnl_partial_lot_consumption(temp_db):
    conn = temp_db
    today = datetime.now().strftime("%Y-%m-%d 10:00:00")
    conn.execute("INSERT INTO orders (ticker, side, qty, fill_price, filled_at) VALUES ('TEST','buy',10,100.0,?)", (today,))
    conn.execute("INSERT INTO orders (ticker, side, qty, fill_price, filled_at) VALUES ('TEST','sell',4,110.0,?)", (today,))
    # Only 4 of the 10 shares sold: (110-100)*4 = 40.
    assert risk.get_today_realized_pnl(conn) == pytest.approx(40.0)


def test_realized_pnl_prefers_filled_qty_over_ordered_qty(temp_db):
    conn = temp_db
    today = datetime.now().strftime("%Y-%m-%d 10:00:00")
    # Ordered 13, only 11 actually filled -- exactly the real AMD case
    # from 2026-08-04 that motivated adding this column.
    conn.execute(
        "INSERT INTO orders (ticker, side, qty, filled_qty, fill_price, filled_at) VALUES ('TEST','buy',13,13,100.0,?)",
        (today,),
    )
    conn.execute(
        "INSERT INTO orders (ticker, side, qty, filled_qty, fill_price, filled_at) VALUES ('TEST','sell',13,11,110.0,?)",
        (today,),
    )
    # Must use filled_qty=11, not the ordered qty=13: (110-100)*11 = 110.
    assert risk.get_today_realized_pnl(conn) == pytest.approx(110.0)


def test_realized_pnl_sums_across_multiple_tickers(temp_db):
    conn = temp_db
    today = datetime.now().strftime("%Y-%m-%d 10:00:00")
    conn.execute("INSERT INTO orders (ticker, side, qty, fill_price, filled_at) VALUES ('AAA','buy',10,100.0,?)", (today,))
    conn.execute("INSERT INTO orders (ticker, side, qty, fill_price, filled_at) VALUES ('AAA','sell',10,110.0,?)", (today,))
    conn.execute("INSERT INTO orders (ticker, side, qty, fill_price, filled_at) VALUES ('BBB','buy',5,50.0,?)", (today,))
    conn.execute("INSERT INTO orders (ticker, side, qty, fill_price, filled_at) VALUES ('BBB','sell',5,40.0,?)", (today,))
    # AAA: +100, BBB: -50 -> net +50.
    assert risk.get_today_realized_pnl(conn) == pytest.approx(50.0)


# ------------------------------------------------------------------- ATR

def test_compute_atr_insufficient_history_is_nan():
    import pandas as pd
    df = pd.DataFrame({
        "high": [10, 11, 10],
        "low": [9, 9, 9],
        "close": [9.5, 10.5, 9.5],
    })
    atr = risk.compute_atr(df, period=14)
    assert pd.isna(atr)


def test_compute_atr_positive_with_sufficient_history():
    import pandas as pd
    n = 20
    df = pd.DataFrame({
        "high": [100 + i * 0.1 + 1 for i in range(n)],
        "low": [100 + i * 0.1 - 1 for i in range(n)],
        "close": [100 + i * 0.1 for i in range(n)],
    })
    atr = risk.compute_atr(df, period=14)
    assert atr > 0
