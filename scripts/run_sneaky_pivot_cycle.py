"""
Sneaky Pivot intraday cycle: for each watchlist ticker, evaluate an entry
(if flat) or an exit (if holding a Sneaky-Pivot-originated position),
execute through the same risk-gated execution loop everything else in
this system uses, reconcile fills, and log every run to a durable JSONL
file.

Runs against its own dedicated Alpaca paper account (account_for_strategy
routes 'sneaky_pivot' -> the SNEAKY_PIVOT_* credentials), separate from
rd-agent's, since 2026-07-28 -- isolates Sneaky Pivot's intraday PDT
day-trade count and daily circuit breaker from rd-agent's swing
positions. Because of this, rd-agent's positions are no longer visible
here at all (different account) -- the "held by other strategy" guard
below now only catches something unexpected showing up in THIS account
(e.g. a manual trade placed outside this system), not cross-strategy
collisions, which the account split makes structurally impossible.
Sneaky Pivot still only manages positions it opened itself, tracked via
the local `signals.strategy` column joined against filled orders.

Intended to run every 15 minutes during market hours (see the -run
scheduling in this session). See screeners/sneaky_pivot.py for the full
ruleset and the caveats about this being an unvalidated, same-day
translation of a discretionary YouTube strategy.

Usage:
    python scripts/run_sneaky_pivot_cycle.py             # dry run (default)
    python scripts/run_sneaky_pivot_cycle.py --live       # submits real (paper) orders
"""
import json
import sys
from datetime import datetime, time, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from shared import db
from shared.config import WATCHLIST
from shared.market_data import get_daily_bars_cached, get_intraday_bars
from execution import alpaca_client
from signals.generate_signal import generate_and_queue_sneaky_pivot_signal

ET = ZoneInfo("America/New_York")
LOG_PATH = ROOT / "data" / "sneaky_pivot_log.jsonl"


def _log(entry: dict) -> None:
    entry["logged_at"] = datetime.now(timezone.utc).isoformat()
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(LOG_PATH, "a") as f:
        f.write(json.dumps(entry, default=str) + "\n")
    print(json.dumps(entry, default=str))


def get_sneaky_pivot_held_qty(conn, ticker: str) -> float:
    """Net filled qty attributable to sneaky_pivot signals for this ticker
    -- deliberately separate from Alpaca's raw position (which may include
    exposure opened by a different strategy)."""
    rows = conn.execute(
        """
        SELECT o.side, o.qty FROM orders o
        JOIN signals s ON o.signal_id = s.id
        WHERE s.strategy = 'sneaky_pivot' AND o.ticker = ? AND o.filled_at IS NOT NULL
        """,
        (ticker,),
    ).fetchall()
    qty = 0.0
    for r in rows:
        qty += r["qty"] if r["side"] == "buy" else -r["qty"]
    return qty


def run_cycle(dry_run: bool = True) -> dict:
    now_et = datetime.now(ET)
    session_close = now_et.replace(hour=16, minute=0, second=0, microsecond=0)
    day_start_et = datetime.combine(now_et.date(), time(0, 0), tzinfo=ET)

    account = alpaca_client.get_account_snapshot(account="sneaky_pivot")
    is_pdt_account = account.equity >= 25_000

    daily_bars_all = get_daily_bars_cached(
        WATCHLIST, start=datetime.now() - timedelta(days=70), end=datetime.now()
    )
    intraday_bars_all = get_intraday_bars(
        WATCHLIST, start=day_start_et, end=now_et, minutes=15
    )

    real_positions = {p["symbol"]: p["qty"] for p in alpaca_client.get_open_positions(account="sneaky_pivot")}
    open_buy_symbols = alpaca_client.get_open_buy_order_symbols(account="sneaky_pivot")

    signal_results = []
    with db.db_session() as conn:
        for ticker in WATCHLIST:
            outcome = {"ticker": ticker}
            try:
                daily_bars = daily_bars_all.get(ticker)
                intraday_bars = intraday_bars_all.get(ticker)
                if daily_bars is None or intraday_bars is None:
                    outcome["status"] = "skipped_no_data"
                    signal_results.append(outcome)
                    continue

                today = now_et.date()
                daily_bars = daily_bars[daily_bars.index.date < today]

                real_qty = real_positions.get(ticker, 0.0)
                sneaky_qty = get_sneaky_pivot_held_qty(conn, ticker)

                if real_qty > 0 and sneaky_qty <= 0:
                    # Now that this runs against its own dedicated account,
                    # this can only mean a position exists here that our
                    # local sneaky_pivot ledger doesn't know about (e.g. a
                    # manual trade placed outside this system) -- rd-agent
                    # positions live in a different account entirely and
                    # never appear in real_positions here anymore.
                    outcome["status"] = "skipped_held_by_other_strategy"
                    signal_results.append(outcome)
                    continue

                if real_qty <= 0 and ticker in open_buy_symbols:
                    outcome["status"] = "skipped_open_buy_order_pending"
                    signal_results.append(outcome)
                    continue

                held_qty = sneaky_qty if sneaky_qty > 0 else None

                signal_id = generate_and_queue_sneaky_pivot_signal(
                    ticker=ticker,
                    daily_bars=daily_bars,
                    intraday_bars_today=intraday_bars,
                    session_close=session_close,
                    account_equity=account.equity,
                    is_pdt_account=is_pdt_account,
                    current_daily_pnl=account.today_pnl,
                    held_qty=held_qty,
                )
                outcome["status"] = "signal_queued" if signal_id else "no_signal"
                outcome["signal_id"] = signal_id

            except Exception as e:
                outcome["status"] = "error"
                outcome["error"] = f"{type(e).__name__}: {e}"

            signal_results.append(outcome)

    from execution.run_execution_loop import process_pending_signals
    from execution.reconcile_orders import reconcile

    try:
        execution_results = process_pending_signals(dry_run=dry_run)
        execution_error = None
    except Exception as e:
        execution_results = []
        execution_error = f"{type(e).__name__}: {e}"

    if dry_run and execution_results:
        # process_pending_signals(dry_run=True) deliberately leaves a
        # signal's status as 'pending' (see its own docstring) so a human
        # can later re-run with --live against the same signal without
        # regenerating it. But that also means the signal sits fully
        # exposed to ANY other process that later does a blanket live
        # pass over the shared signals table -- which is exactly what
        # happened on 2026-07-29: a Sneaky Pivot dry-run MSFT signal was
        # picked up and filled for real 8 minutes later by the separately
        # -scheduled, always-live rd-agent daily cycle (run_cycle.py),
        # which has no concept of "this signal belongs to a strategy
        # that's currently dry-run-only." Since Sneaky Pivot's standing
        # instruction is "never goes live without an explicit --live run
        # of THIS script," a dry-run evaluation must not leave anything
        # behind that a different, unrelated live pass could execute --
        # so immediately expire what was just evaluated. An explicit
        # --live invocation of this script is unaffected (dry_run=False
        # skips this block entirely; process_pending_signals already
        # marks those signals 'executed' or 'rejected' itself).
        with db.db_session() as conn:
            for r in execution_results:
                sig_id = r.get("signal_id")
                if sig_id is not None:
                    db.update_signal_status(conn, sig_id, "expired")

    try:
        reconciliation_results = [] if dry_run else reconcile()
        reconciliation_error = None
    except Exception as e:
        reconciliation_results = []
        reconciliation_error = f"{type(e).__name__}: {e}"

    return {
        "dry_run": dry_run,
        "watchlist": WATCHLIST,
        "signals": signal_results,
        "execution": execution_results,
        "execution_error": execution_error,
        "reconciliation": reconciliation_results,
        "reconciliation_error": reconciliation_error,
    }


if __name__ == "__main__":
    dry_run = "--live" not in sys.argv
    if dry_run:
        print("DRY RUN mode (default). Pass --live to actually submit orders.")
    else:
        print("LIVE mode: orders WILL be submitted to Alpaca (paper account, per ALPACA_PAPER config).")

    summary = run_cycle(dry_run=dry_run)
    _log(summary)
