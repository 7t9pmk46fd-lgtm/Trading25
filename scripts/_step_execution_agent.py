"""
Standalone subprocess step: process any pending signals live and
reconcile fills. Used by run_friday_hourly_cycle.py.
"""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "execution-agent" / "scripts"))
sys.path.insert(0, str(ROOT / "execution-agent"))

from run_execution_loop import process_pending_signals
from reconcile_orders import reconcile

if __name__ == "__main__":
    execution_results = process_pending_signals(dry_run=False)
    reconciliation_results = reconcile()
    print(json.dumps(
        {"execution": execution_results, "reconciliation": reconciliation_results},
        default=str,
    ))
