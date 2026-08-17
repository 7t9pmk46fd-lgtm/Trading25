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
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from execution import alpaca_client
from shared import db
from shared.config import ALPACA_PAPER, KNOWN_ACCOUNTS, account_for_strategy

# How far back reconcile_missing_fills looks for broker-side fills the
# local ledger never learned about. Matches the lookback daily_review.py's
# backfill already uses for Alpaca's portfolio history (45 days) -- no
# reason for this to see further back than that already reaches.
MISSING_FILL_LOOKBACK_DAYS = 45


def reconcile() -> list[dict]:
    """
    One order's lookup failing must never abort the rest of the batch --
    a single bad alpaca_order_id (e.g. from a since-closed account) used
    to be able to raise out of the loop and silently skip reconciling
    every order queued after it, including live ones. Each order is now
    isolated in its own try/except, same pattern as run_execution_loop's
    per-signal isolation.
    """
    results = []
    with db.db_session() as conn:
        for order in db.get_unreconciled_orders(conn):
            acct = account_for_strategy(order["strategy"])
            try:
                status = alpaca_client.get_order_status(order["alpaca_order_id"], account=acct)
            except Exception as e:
                results.append({
                    "order_id": order["id"], "ticker": order["ticker"],
                    "alpaca_order_id": order["alpaca_order_id"],
                    "status": "error", "error": f"{type(e).__name__}: {e}",
                })
                continue
            db.update_order_fill(
                conn,
                alpaca_order_id=status["alpaca_order_id"],
                status=status["status"],
                filled_at=status["filled_at"],
                fill_price=status["filled_avg_price"],
                filled_qty=status["filled_qty"],
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


def reconcile_missing_fills(account: str = "default") -> list[dict]:
    """
    Finds broker-side fills the local `orders` table never learned about
    at all -- not a status update on an order we already know (that's
    reconcile() above), but an order we never recorded a row for in the
    first place.

    Confirmed live 2026-08-14/17: a resting protective stop fills directly
    on Alpaca when price crosses it. run_execution_loop.py only inserts an
    `orders` row for orders IT submits (buys, and strategy-driven exit
    sells) -- a stop placed once via place_protective_stop and left
    resting is never revisited by that code path, so when it later
    triggers, nothing local ever records the fill. Traced 4 positions
    (GOOGL, CVS, KIM, REG) that show every sign of exactly this: each
    one's last trail_stops log entry has price sitting right at its stop,
    then the position is gone from every later cycle, with no matching
    sell in `orders`. Consequence: shared.risk.get_today_realized_pnl
    (FIFO over this same ledger) silently under/mis-counts realized P&L
    for anything ever stopped out rather than exited by a strategy signal,
    and daily_review.py's backfill path invents phantom still-open
    positions for the same reason (confirmed: it produced exactly that for
    2026-08-14 before this fix).

    Backfilled rows get signal_id=NULL -- accurate, since a triggered stop
    was never a strategy-driven decision recorded anywhere as a signal.
    Every reader that joins orders to signals already treats a NULL/absent
    strategy as "not sneaky_pivot" (COALESCE(s.strategy, '') != 'sneaky_pivot'
    in daily_review._reconstruct_positions), so this doesn't need any
    reader-side change to be picked up correctly.
    """
    since = datetime.now(timezone.utc) - timedelta(days=MISSING_FILL_LOOKBACK_DAYS)
    broker_fills = alpaca_client.get_filled_orders_since(since, account=account)

    with db.db_session() as conn:
        known_ids = {
            row["alpaca_order_id"]
            for row in conn.execute(
                "SELECT alpaca_order_id FROM orders WHERE alpaca_order_id IS NOT NULL"
            ).fetchall()
        }

        inserted = []
        for fill in broker_fills:
            if fill["alpaca_order_id"] in known_ids:
                continue
            conn.execute(
                """INSERT INTO orders
                   (signal_id, ticker, side, qty, order_type, alpaca_order_id,
                    status, is_paper, submitted_at, filled_at, fill_price,
                    filled_qty, notes)
                   VALUES (NULL, ?, ?, ?, ?, ?, 'filled', ?, ?, ?, ?, ?, ?)""",
                (
                    fill["symbol"], fill["side"], fill["qty"], fill["order_type"],
                    fill["alpaca_order_id"], int(ALPACA_PAPER), fill["submitted_at"],
                    fill["filled_at"], fill["filled_avg_price"], fill["filled_qty"],
                    "backfilled by reconcile_missing_fills: broker-side fill "
                    "(e.g. a triggered protective stop) with no local order row",
                ),
            )
            inserted.append({
                "ticker": fill["symbol"], "side": fill["side"],
                "order_type": fill["order_type"], "alpaca_order_id": fill["alpaca_order_id"],
                "filled_qty": fill["filled_qty"], "fill_price": fill["filled_avg_price"],
                "filled_at": fill["filled_at"],
            })
    return inserted


if __name__ == "__main__":
    results = reconcile()
    if not results:
        print("No unreconciled orders.")
    for r in results:
        print(r)

    for acct in KNOWN_ACCOUNTS:
        backfilled = reconcile_missing_fills(account=acct)
        if not backfilled:
            print(f"[{acct}] No missing fills found.")
        for r in backfilled:
            print(f"[{acct}] backfilled:", r)
