"""
Measures test coverage and writes a compact cache the dashboard's
readiness meter reads from -- NOT run live on every page load.

Why cached rather than live: dashboard.py's _readiness_section already
had a documented profiling problem once (2026-08-11, accounts fetch
duplicated ~1.85s of a 5.38s page build) from doing real work on every
poll. Actually running pytest --cov here takes a couple of seconds;
doing that on every ~20s dashboard refresh would make the page
consistently slow for no benefit, since coverage does not change between
test runs. Same pattern as rd_cadence: write a timestamped file when
something real happens, let the dashboard just read and age it.

Coverage on the whole codebase is a misleading single number -- report
generation, R&D backtesting, and the Alpaca-API-touching modules are
legitimately close to 0% (see this script's own RISK_CRITICAL list
below and analyst/dashboard.py's readiness check for why that's treated
differently from risk-critical code). What actually matters for a
go-live decision is coverage on the modules that gate real money:
sizing, the oversell/cash-floor/PDT guards, the trailing-stop logic that
protects every position, the signal logic itself, and the ledger
reconciliation that realized P&L depends on.

Usage:
    venv/Scripts/python scripts/measure_coverage.py
"""
import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT_PATH = ROOT / "data" / "coverage_report.json"

# The modules a real-money decision actually depends on. Deliberately
# excludes execution/alpaca_client.py and shared/market_data.py's live-
# fetch functions -- those talk to the real API and their correctness
# comes from smoke-testing, not unit tests; a low score there would just
# be noise, not signal. Confirmed via a real coverage run 2026-08-24.
RISK_CRITICAL_MODULES = [
    "shared/risk.py",
    "execution/run_execution_loop.py",
    "execution/trail_stops.py",
    "execution/reconcile_orders.py",
    "signals/screeners/mean_reversion.py",
    "signals/generate_signal.py",
]


def measure() -> dict:
    raw_json_path = ROOT / ".coverage_raw.json"
    result = subprocess.run(
        [
            sys.executable, "-m", "pytest", "tests/", "-q",
            "--cov=shared", "--cov=execution", "--cov=signals", "--cov=analyst",
            f"--cov-report=json:{raw_json_path}",
        ],
        cwd=ROOT, capture_output=True, text=True,
    )
    tests_passed = result.returncode == 0

    with open(raw_json_path) as f:
        raw = json.load(f)
    raw_json_path.unlink()

    files = raw.get("files", {})
    overall_pct = raw.get("totals", {}).get("percent_covered", 0.0)

    def _pct_for(rel_path: str) -> float | None:
        # coverage.json keys files by the path pytest was invoked with,
        # normalize both sides to forward slashes for a reliable match.
        rel_path = rel_path.replace("\\", "/")
        for key, data in files.items():
            if key.replace("\\", "/").endswith(rel_path):
                return data["summary"]["percent_covered"]
        return None

    risk_critical = {m: _pct_for(m) for m in RISK_CRITICAL_MODULES}
    measured = [p for p in risk_critical.values() if p is not None]
    risk_critical_pct = sum(measured) / len(measured) if measured else 0.0

    report = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "tests_passed": tests_passed,
        "overall_percent": round(overall_pct, 1),
        "risk_critical_percent": round(risk_critical_pct, 1),
        "risk_critical_by_module": {k: (round(v, 1) if v is not None else None)
                                     for k, v in risk_critical.items()},
    }
    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(OUT_PATH, "w") as f:
        json.dump(report, f, indent=2)
    return report


if __name__ == "__main__":
    r = measure()
    print(json.dumps(r, indent=2))
    if not r["tests_passed"]:
        print("\nWARNING: test suite did not pass during this coverage run.", file=sys.stderr)
        sys.exit(1)
