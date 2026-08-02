"""
Full autonomous cycle: scan the watchlist for signals, run them through
live risk-gated execution, reconcile fills -- and log everything to a
durable JSONL file, since nothing here waits for a human to approve.

Intended to be invoked on a schedule (see Windows Task Scheduler setup in
README.md) with no one watching in real time. Every step is wrapped so one
ticker's failure, or even a failure in execution/reconciliation, doesn't
lose the rest of the run's record -- if you're not watching this run live,
the log is the only way you'll know what happened.

Usage:
    python scripts/run_cycle.py
"""
import json
import sys
import traceback
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from shared.config import WATCHLIST, PROJECT_ROOT

LOG_PATH = Path(PROJECT_ROOT) / "data" / "cycle_log.jsonl"


def _log(entry: dict) -> None:
    entry["logged_at"] = datetime.now(timezone.utc).isoformat()
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(LOG_PATH, "a") as f:
        f.write(json.dumps(entry, default=str) + "\n")
    print(json.dumps(entry, default=str))


def run_cycle() -> dict:
    from signals.generate_signal import generate_and_queue_batch
    from execution.run_execution_loop import process_pending_signals
    from execution.reconcile_orders import reconcile

    started_at = datetime.now(timezone.utc).isoformat()
    try:
        signal_results = generate_and_queue_batch(WATCHLIST)
    except Exception as e:
        # One bad ticker no longer aborts the batch fetch (get_daily_bars
        # already skips missing symbols internally) -- this only catches a
        # genuine failure of the batch call itself (e.g. credentials, a
        # true screener bug raised as ValueError).
        signal_results = [{"status": "batch_error", "error": str(e)}]

    try:
        execution_results = process_pending_signals(dry_run=False)
        execution_error = None
    except Exception as e:
        execution_results = []
        execution_error = f"{type(e).__name__}: {e}"

    try:
        reconciliation_results = reconcile()
        reconciliation_error = None
    except Exception as e:
        reconciliation_results = []
        reconciliation_error = f"{type(e).__name__}: {e}"

    return {
        "started_at": started_at,
        "watchlist": WATCHLIST,
        "signals": signal_results,
        "execution": execution_results,
        "execution_error": execution_error,
        "reconciliation": reconciliation_results,
        "reconciliation_error": reconciliation_error,
    }


def main():
    try:
        summary = run_cycle()
        _log(summary)
    except Exception:
        # Catch-all so a genuinely unexpected failure (e.g. missing
        # credentials, DB locked) still leaves a record instead of just a
        # silent non-zero exit no one will see until much later.
        _log({"status": "fatal_error", "traceback": traceback.format_exc()})
        sys.exit(1)


if __name__ == "__main__":
    main()
