"""
Fill reconciliation: polls Alpaca for the current status of any locally
recorded order that hasn't been marked filled yet, and writes the result
(status, filled_at, fill_price) back to the local `orders` table.

This closes the gap called out in the README/SKILL docs -- orders get
submitted by run_execution_loop.py but nothing previously checked back on
fill status, so the local `orders` table could show a stale
'pending_new'/etc. status indefinitely even after Alpaca had long since
filled (or rejected) the order.

Usage:
    python scripts/reconcile_orders.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from execution import alpaca_client
from shared import db
from shared.config import account_for_strategy


def reconcile() -> list[dict]:
    results = []
    with db.db_session() as conn:
        for order in db.get_unreconciled_orders(conn):
            acct = account_for_strategy(order["strategy"])
            status = alpaca_client.get_order_status(order["alpaca_order_id"], account=acct)
            db.update_order_fill(
                conn,
                alpaca_order_id=status["alpaca_order_id"],
                status=status["status"],
                filled_at=status["filled_at"],
                fill_price=status["filled_avg_price"],
            )
            results.append({
                "order_id": order["id"],
                "ticker": order["ticker"],
                "alpaca_order_id": status["alpaca_order_id"],
                "status": status["status"],
                "filled_qty": status["filled_qty"],
                "fill_price": status["filled_avg_price"],
            })
    return results


if __name__ == "__main__":
    results = reconcile()
    if not results:
        print("No unreconciled orders.")
    for r in results:
        print(r)
