"""
Measures what fraction of the FULL watchlist is actually tradeable at a
small starting balance, and writes a cache the dashboard's readiness
meter reads from.

Why this needs its own script rather than reusing whatever's in
data/cache/daily_bars_*.pkl: that cache is populated incrementally by
whichever caller ran most recently that day (trail_stops fetches only
currently-held symbols, ~20; daily_review's gather fetches a 12-day
window for the full watchlist but only during its own run and the file
gets pruned after 3 days). Confirmed 2026-08-24: the live cache file at
that moment held exactly the 20 currently-held positions -- a biased
sample (already-selected mean-reversion winners at $100k+ sizing), not a
cross-section of the universe a small-capital investor would actually be
choosing from. This does one deliberate fetch of the whole ~522-name
watchlist's latest prices instead.

Not run live on every dashboard load, same reasoning as
measure_coverage.py: this doesn't change intraday in any way that
matters for the question it's answering, and a 500+ symbol batch fetch
on every ~20s page poll would be slow and needlessly hits Alpaca.

Usage:
    venv/Scripts/python scripts/measure_sizing_viability.py
"""
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from shared.config import WATCHLIST
from shared.market_data import get_latest_trade_prices

OUT_PATH = ROOT / "data" / "sizing_viability_report.json"

# Matches shared.risk.RiskLimits.max_position_pct's live default -- if
# that constant ever changes, this should be re-run, not assumed stale
# forever; the dashboard check applies its own staleness decay on top.
MAX_POSITION_PCT = 0.05
REFERENCE_CAPITALS = [1000.0, 5000.0, 10000.0]


def measure() -> dict:
    prices = get_latest_trade_prices(WATCHLIST)
    by_capital = {}
    for capital in REFERENCE_CAPITALS:
        cap_dollars = capital * MAX_POSITION_PCT
        tradeable = sum(1 for p in prices.values() if p > 0 and p <= cap_dollars)
        by_capital[str(int(capital))] = {
            "tradeable": tradeable,
            "total": len(prices),
            "pct": round(tradeable / len(prices), 3) if prices else 0.0,
        }
    report = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "watchlist_size": len(WATCHLIST),
        "prices_resolved": len(prices),
        "max_position_pct": MAX_POSITION_PCT,
        "by_capital": by_capital,
    }
    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(OUT_PATH, "w") as f:
        json.dump(report, f, indent=2)
    return report


if __name__ == "__main__":
    r = measure()
    print(json.dumps(r, indent=2))
