"""
Market-hours trading loop: one process that covers a full trading day
unattended, replacing the old one-shot TradingDeskDailyCycle task
(removed 2026-08-02 at the user's request in favor of this).

What it does, driven by Alpaca's own market clock (holidays and early
closes handled by the broker, not local weekday math):

  - Before the open: sleeps until the market opens (or exits immediately
    if the next open is more than ~12h away, e.g. weekend starts).
  - Once per day, at/after 09:45 ET on Mon-Wed only: the swing
    mean-reversion cycle (signal scan -> live risk-gated execution ->
    reconciliation). Mon-Wed preserves the schedule the old daily task
    deliberately used.
  - Every 15 minutes while the market is open: the sneaky_pivot intraday
    cycle (LIVE -- this loop is the explicit go-live path that
    scripts/run_sneaky_pivot_cycle.py's dry-run default requires),
    followed by trail_stops across every account (stop raising +
    DAY->GTC conversion).
  - After the close: one final reconciliation pass, then exits. The
    scheduled task starts a fresh process next trading morning.

Safety rails, enforced in code:
  - REFUSES to start unless ALPACA_PAPER=true -- this loop is the
    "no approvals needed" path and that autonomy is scoped to paper
    trading only. Going live-money must be a deliberate, separate act.
  - Single-instance lock (data/trading_day.lock, OS-level file lock) so
    a manual run + the scheduled task can't double-trade the same day.
  - Every step is exception-isolated and logged to
    data/trading_day_log.jsonl -- one bad cycle never kills the day.

Usage:
    python scripts/run_trading_day.py           # the real loop
    python scripts/run_trading_day.py --once    # one status/cycle pass, then exit (for testing)
"""
import json
import sys
import time as time_mod
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

from shared.config import ALPACA_PAPER
from execution import alpaca_client
from execution.reconcile_orders import reconcile
from execution.trail_stops import check_and_trail
from shared.config import KNOWN_ACCOUNTS

ET = ZoneInfo("America/New_York")
CYCLE_SECONDS = 15 * 60
SWING_SCAN_WEEKDAYS = (0, 1, 2)   # Mon-Wed, matching the old TradingDeskDailyCycle schedule
SWING_SCAN_AFTER_ET = (9, 45)     # don't scan on the opening print
MAX_WAIT_FOR_OPEN_HOURS = 12      # started on a weekend/holiday evening? just exit
LOG_PATH = ROOT / "data" / "trading_day_log.jsonl"
LOCK_PATH = ROOT / "data" / "trading_day.lock"


def _log(event: str, **fields) -> None:
    entry = {"event": event, "logged_at": datetime.now(timezone.utc).isoformat(), **fields}
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(LOG_PATH, "a") as f:
        f.write(json.dumps(entry, default=str) + "\n")
    print(json.dumps(entry, default=str), flush=True)


def _acquire_single_instance_lock():
    """Windows file lock held for the process lifetime -- auto-released by
    the OS if the process dies, so no stale-lockfile handling needed."""
    import msvcrt

    LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
    handle = open(LOCK_PATH, "w")
    try:
        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
    except OSError:
        return None
    handle.write(str(datetime.now()))
    handle.flush()
    return handle  # keep a reference alive; closing it releases the lock


def _step(name: str, fn):
    """Run one loop step, exception-isolated, logged either way."""
    try:
        result = fn()
        _log(name, status="ok", result=result)
        return result
    except Exception as e:
        _log(name, status="error", error=f"{type(e).__name__}: {e}")
        return None


def _swing_cycle():
    from run_cycle import run_cycle as swing_run_cycle
    return swing_run_cycle()  # generate + live execute + reconcile, returns summary


def _sneaky_pivot_cycle():
    from run_sneaky_pivot_cycle import run_cycle as sneaky_run_cycle
    return sneaky_run_cycle(dry_run=False)


def _trail_all_stops():
    return {account: check_and_trail(dry_run=False, account=account) for account in KNOWN_ACCOUNTS}


def run_trading_day(once: bool = False) -> None:
    if not ALPACA_PAPER:
        _log("refused_to_start", reason="ALPACA_PAPER is not true. This loop trades with no "
             "human approval and is restricted to paper accounts by design.")
        sys.exit(1)

    lock = _acquire_single_instance_lock()
    if lock is None:
        _log("refused_to_start", reason="another run_trading_day.py instance holds the lock")
        sys.exit(1)

    _log("session_start", paper=ALPACA_PAPER, once=once)
    swing_scan_done_for = None  # date the once-a-day swing scan last ran

    while True:
        clock = _step("clock", alpaca_client.get_market_clock)
        if clock is None:
            # Can't even reach Alpaca -- wait a cycle and retry rather than die.
            if once:
                return
            time_mod.sleep(CYCLE_SECONDS)
            continue

        if not clock["is_open"]:
            seconds_to_open = (clock["next_open"] - clock["timestamp"]).total_seconds()
            if seconds_to_open > MAX_WAIT_FOR_OPEN_HOURS * 3600:
                _step("final_reconcile", reconcile)
                _log("session_end", reason="market closed, next open too far away",
                     next_open=clock["next_open"])
                return
            _log("waiting_for_open", next_open=clock["next_open"],
                 minutes=round(seconds_to_open / 60, 1))
            if once:
                return
            time_mod.sleep(min(CYCLE_SECONDS, max(30, seconds_to_open)))
            continue

        now_et = datetime.now(ET)
        if (
            swing_scan_done_for != now_et.date()
            and now_et.weekday() in SWING_SCAN_WEEKDAYS
            and (now_et.hour, now_et.minute) >= SWING_SCAN_AFTER_ET
        ):
            _step("swing_cycle", _swing_cycle)
            swing_scan_done_for = now_et.date()

        _step("sneaky_pivot_cycle", _sneaky_pivot_cycle)
        _step("trail_stops", _trail_all_stops)

        if once:
            _log("session_end", reason="--once pass complete")
            return

        # Sleep the remainder of the 15-min slot, but wake for the close.
        seconds_to_close = (clock["next_close"] - clock["timestamp"]).total_seconds()
        time_mod.sleep(max(30, min(CYCLE_SECONDS, seconds_to_close + 60)))


if __name__ == "__main__":
    run_trading_day(once="--once" in sys.argv)
