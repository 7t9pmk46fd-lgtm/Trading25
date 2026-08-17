"""
Tests for execution/reconcile_orders.py's reconcile_missing_fills.

Confirmed live 2026-08-14/17: a resting protective stop fills directly on
the broker, outside run_execution_loop.py's submit-then-record path, so
the local `orders` table never learns about it. Traced 4 real positions
(GOOGL, CVS, KIM, REG) that were stopped out with no matching local sell
row -- which corrupts shared.risk.get_today_realized_pnl (FIFO over this
same ledger) and made daily_review.py's backfill invent phantom open
positions. These pin the backfill that closes that gap: any broker-filled
order missing from the local ledger gets a row inserted, signal_id=NULL;
anything already known is left untouched, never duplicated.
"""
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from execution import alpaca_client
from execution.reconcile_orders import reconcile_missing_fills
from shared import db


def _broker_fill(alpaca_order_id, symbol="CCI", side="sell", order_type="stop",
                  qty=18.0, filled_qty=18.0, filled_avg_price=70.23,
                  filled_at="2026-08-14 15:30:00+00:00", submitted_at="2026-08-14 13:46:20+00:00"):
    return {
        "alpaca_order_id": alpaca_order_id, "symbol": symbol, "side": side,
        "order_type": order_type, "qty": qty, "filled_qty": filled_qty,
        "filled_avg_price": filled_avg_price, "filled_at": filled_at,
        "submitted_at": submitted_at,
    }


def test_backfills_an_untracked_stop_fill(temp_db_path, monkeypatch):
    monkeypatch.setattr(alpaca_client, "get_filled_orders_since",
                         lambda since, account="default": [_broker_fill("orphan-fill-1")])

    inserted = reconcile_missing_fills(account="default")

    assert len(inserted) == 1
    assert inserted[0]["ticker"] == "CCI"
    assert inserted[0]["alpaca_order_id"] == "orphan-fill-1"

    with db.db_session() as conn:
        row = dict(conn.execute(
            "SELECT * FROM orders WHERE alpaca_order_id = 'orphan-fill-1'"
        ).fetchone())
    assert row["signal_id"] is None
    assert row["ticker"] == "CCI"
    assert row["side"] == "sell"
    assert row["status"] == "filled"
    assert row["fill_price"] == 70.23
    assert row["filled_qty"] == 18.0
    assert "backfilled" in row["notes"]


def test_does_not_duplicate_an_already_known_order(temp_db_path, monkeypatch):
    with db.db_session() as conn:
        conn.execute(
            """INSERT INTO orders (ticker, side, qty, alpaca_order_id, status,
                                    filled_at, fill_price, filled_qty)
               VALUES ('NVDA', 'buy', 52, 'known-order-1', 'filled',
                       '2026-08-01 13:30:00+00:00', 191.19, 52)"""
        )
    monkeypatch.setattr(alpaca_client, "get_filled_orders_since",
                         lambda since, account="default": [_broker_fill("known-order-1", symbol="NVDA", side="buy")])

    inserted = reconcile_missing_fills(account="default")

    assert inserted == []
    with db.db_session() as conn:
        count = conn.execute(
            "SELECT COUNT(*) AS n FROM orders WHERE alpaca_order_id = 'known-order-1'"
        ).fetchone()["n"]
    assert count == 1


def test_only_backfills_the_missing_ones_from_a_mixed_batch(temp_db_path, monkeypatch):
    with db.db_session() as conn:
        conn.execute(
            """INSERT INTO orders (ticker, side, qty, alpaca_order_id, status,
                                    filled_at, fill_price, filled_qty)
               VALUES ('MPC', 'buy', 17, 'known-order-2', 'filled',
                       '2026-08-01 13:30:00+00:00', 291.99, 17)"""
        )
    monkeypatch.setattr(alpaca_client, "get_filled_orders_since", lambda since, account="default": [
        _broker_fill("known-order-2", symbol="MPC", side="buy"),
        _broker_fill("orphan-fill-2", symbol="REG", side="sell", filled_avg_price=77.51),
    ])

    inserted = reconcile_missing_fills(account="default")

    assert [r["alpaca_order_id"] for r in inserted] == ["orphan-fill-2"]
    with db.db_session() as conn:
        tickers = {row["ticker"] for row in conn.execute("SELECT ticker FROM orders").fetchall()}
    assert tickers == {"MPC", "REG"}
