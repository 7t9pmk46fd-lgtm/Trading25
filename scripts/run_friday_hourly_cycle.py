"""
One-time hourly cycle for Friday 2026-07-24 market hours: runs rd-agent,
then day-trading-agent, then execution-agent in sequence, with a 5-minute
gap between each step. Requested as a one-off test so results can be
reviewed after market close -- NOT wired into a recurring schedule.

learning-agent is intentionally excluded: it has no unattended entry
point (its only script takes a specific article/YouTube URL) and no
ANTHROPIC_API_KEY is configured.

day-trading-agent has no live intraday data feed yet, so its step is
expected to just log the documented placeholder message, not a real
signal.

Each agent step runs as its own subprocess -- rd-agent and
day-trading-agent both define a top-level `screeners` package, so
importing both into one long-lived process would collide.

Invoked hourly by Windows Task Scheduler (task
TradingDeskFridayHourlyCycle, one-time trigger for 2026-07-24 only).
"""
import json
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PYTHON = ROOT / "venv" / "Scripts" / "python.exe"
LOG_PATH = ROOT / "data" / "friday_hourly_cycle_log.jsonl"
GAP_SECONDS = 5 * 60

RD_AGENT_STEP = ROOT / "scripts" / "_step_rd_agent.py"
EXECUTION_STEP = ROOT / "scripts" / "_step_execution_agent.py"
DAY_TRADING_SCRIPT = ROOT / "day-trading-agent" / "scripts" / "generate_signal.py"


def _log(entry: dict) -> None:
    entry["logged_at"] = datetime.now(timezone.utc).isoformat()
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(LOG_PATH, "a") as f:
        f.write(json.dumps(entry, default=str) + "\n")
    print(json.dumps(entry, default=str))


def _run_step(name: str, script: Path) -> None:
    result = subprocess.run(
        [str(PYTHON), str(script)], capture_output=True, text=True
    )
    _log({
        "step": name,
        "returncode": result.returncode,
        "stdout": result.stdout.strip(),
        "stderr": result.stderr.strip(),
    })


def main():
    _log({"cycle": "start"})
    _run_step("rd-agent", RD_AGENT_STEP)
    time.sleep(GAP_SECONDS)
    _run_step("day-trading-agent", DAY_TRADING_SCRIPT)
    time.sleep(GAP_SECONDS)
    _run_step("execution-agent", EXECUTION_STEP)
    _log({"cycle": "end"})


if __name__ == "__main__":
    main()
