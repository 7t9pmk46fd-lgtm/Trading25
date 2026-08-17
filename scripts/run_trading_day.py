"""
Market-hours trading loop: one process that covers a full trading day
unattended, replacing the old one-shot TradingDeskDailyCycle task
(removed 2026-08-02 at the user's request in favor of this).

What it does, driven by Alpaca's own market clock (holidays and early
closes handled by the broker, not local weekday math):

  - Before the open: sleeps until the market opens (or exits immediately
    if the next open is more than ~12h away, e.g. weekend starts).
  - Once per day, at/after 09:45 ET on any weekday: the swing
    mean-reversion cycle (signal scan -> live risk-gated execution ->
    reconciliation).
  - Every hour while the market is open: trail_stops across every
    account (stop raising + DAY->GTC conversion).
  - After the close: one final reconciliation pass, then exits. The
    scheduled task starts a fresh process next trading morning.

There is no end-of-day flatten step: the only strategy running is a
swing strategy that holds positions across sessions by design. An
intraday strategy added later would need one -- and note the trap that
made it necessary before, since it is a property of 15-minute bars and
not of any particular strategy: a screener's own force_flatten_eod
cannot fire if its cutoff falls after the last bar of the session (the
final 15-minute bar is stamped 15:45 ET, so a >= 15:55 cutoff is never
reached) and the loop stops cycling at the close.

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

from shared.config import ALPACA_PAPER, KNOWN_ACCOUNTS
from execution import alpaca_client
from execution.reconcile_orders import reconcile
from execution.trail_stops import check_and_trail

ET = ZoneInfo("America/New_York")
CYCLE_SECONDS = 60 * 60  # hourly, changed from 15 min at the user's request 2026-08-17
# Mon-Fri. Was Mon-Wed, inherited from the retired TradingDeskDailyCycle
# task; widened 2026-08-06 at the user's request. There was never a
# strategy reason for skipping Thu/Fri -- a daily mean-reversion signal is
# equally valid on any session -- and with a 522-name universe and ranked
# entries, two skipped days a week is two days of the best candidates
# going untaken. Weekends are not listed because the market is closed;
# the loop's Alpaca-clock check would exit before reaching this anyway.
SWING_SCAN_WEEKDAYS = (0, 1, 2, 3, 4)
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


def _swing_scan_already_ran_today():
    """Returns today's ET date if a successful swing cycle is already in
    today's log, else None. Log stamps are UTC; during market hours the UTC
    and ET dates agree, which is the only window this is consulted in."""
    if not LOG_PATH.exists():
        return None
    today_et = datetime.now(ET).date()
    for line in LOG_PATH.read_text().splitlines():
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        if entry.get("event") != "swing_cycle" or entry.get("status") != "ok":
            continue
        stamp = str(entry.get("logged_at", ""))[:10]
        if stamp == today_et.isoformat():
            return today_et
    return None


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
    # Survive a mid-session restart (crash, or a redeploy like the 2026-08-03
    # oversell fix) without re-running the once-a-day swing scan: the log is
    # the source of truth for whether it already ran today, not process state.
    swing_scan_done_for = _swing_scan_already_ran_today()
    if swing_scan_done_for:
        _log("swing_cycle_skipped", reason="already ran today per log", date=str(swing_scan_done_for))

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
        seconds_to_close = (clock["next_close"] - clock["timestamp"]).total_seconds()

        if (
            swing_scan_done_for != now_et.date()
            and now_et.weekday() in SWING_SCAN_WEEKDAYS
            and (now_et.hour, now_et.minute) >= SWING_SCAN_AFTER_ET
        ):
            _step("swing_cycle", _swing_cycle)
            swing_scan_done_for = now_et.date()

        _step("trail_stops", _trail_all_stops)

        if once:
            _log("session_end", reason="--once pass complete")
            return

        # Sleep the remainder of the 15-min slot, but never sleep past the
        # close -- the next wake should land on the closed-market branch,
        # which does the final reconcile and ends the session.
        time_mod.sleep(max(30, min(CYCLE_SECONDS, seconds_to_close + 60)))


if __name__ == "__main__":
    run_trading_day(once="--once" in sys.argv)
