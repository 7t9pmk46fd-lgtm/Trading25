"""
Standalone subprocess step: batch-scan the watchlist via rd-agent and
queue any actionable signals. Used by run_friday_hourly_cycle.py so this
step runs in its own process, isolated from day-trading-agent's step
(both agent dirs have a top-level `screeners` package -- importing both
into one process would collide on `import screeners`).
"""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "rd-agent" / "scripts"))
sys.path.insert(0, str(ROOT / "execution-agent"))

from generate_signal import generate_and_queue_batch
from shared.config import WATCHLIST

if __name__ == "__main__":
    results = generate_and_queue_batch(WATCHLIST)
    print(json.dumps(results, default=str))
