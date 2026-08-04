"""
Build the tradeable universe from major-index membership.

The watchlist is the scanning pool -- names the strategy evaluates every
cycle looking for a signal. It is deliberately much larger than the
portfolio: `MAX_CONCURRENT_POSITIONS` caps how many can be HELD at once,
while the watchlist caps how many can be CONSIDERED. Most of it will
always be names we don't hold; that's the point.

Sources (union): S&P 500, NASDAQ-100, Dow 30. The Dow is a subset of the
S&P and the NASDAQ-100 is mostly a subset, so the union lands a little
over 500 names.

Every symbol is then validated twice before it can reach the live
scanner, because a bad ticker in the watchlist means a wasted API slot
and a per-ticker error every single cycle:
  1. It must exist in Alpaca's asset list as ACTIVE and TRADABLE.
  2. It must actually return daily bars (catches recent listings,
     halted names, and symbology mismatches like class-share dots).

Output: data/universe.json, which shared.config loads at import. Falls
back to the built-in list if the file is missing or unreadable, so a bad
refresh can never leave the desk with no universe.

Usage:
    venv/Scripts/python scripts/build_universe.py
    venv/Scripts/python scripts/build_universe.py --dry-run   # report, don't write
"""
import io
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd
import requests

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from shared.config import ALPACA_API_KEY, ALPACA_SECRET_KEY, ALPACA_PAPER, BENCHMARK_SYMBOLS
from shared.market_data import get_daily_bars

OUT_PATH = ROOT / "data" / "universe.json"
HEADERS = {"User-Agent": "trading-desk/1.0 (personal research; contact via repo)"}

SOURCES = [
    ("sp500", "https://en.wikipedia.org/wiki/List_of_S%26P_500_companies", "Symbol", (450, 520)),
    ("nasdaq100", "https://en.wikipedia.org/wiki/List_of_NASDAQ-100_companies", "Ticker", (90, 115)),
    ("dow30", "https://en.wikipedia.org/wiki/Dow_Jones_Industrial_Average", "Symbol", (25, 35)),
]


def fetch_index(name: str, url: str, column: str, row_range: tuple) -> list[str]:
    """Pull one index's constituents. Row-count bounds guard against
    grabbing an unrelated table when a page's layout shifts -- a silent
    wrong table would be worse than a loud failure."""
    html = requests.get(url, headers=HEADERS, timeout=30).text
    tables = pd.read_html(io.StringIO(html))
    lo, hi = row_range
    for table in tables:
        cols = [str(c) for c in table.columns]
        if column in cols and lo <= len(table) <= hi:
            symbols = [str(s).strip().upper() for s in table[column].tolist()]
            return [s for s in symbols if s and s != "NAN"]
    raise RuntimeError(
        f"{name}: no table with column {column!r} and {lo}-{hi} rows found at {url} "
        f"(page layout probably changed -- inspect before trusting a rebuild)"
    )


def alpaca_tradable() -> dict[str, object]:
    from alpaca.trading.client import TradingClient
    from alpaca.trading.enums import AssetClass, AssetStatus
    from alpaca.trading.requests import GetAssetsRequest

    client = TradingClient(ALPACA_API_KEY, ALPACA_SECRET_KEY, paper=ALPACA_PAPER)
    assets = client.get_all_assets(
        GetAssetsRequest(asset_class=AssetClass.US_EQUITY, status=AssetStatus.ACTIVE)
    )
    return {a.symbol: a for a in assets if a.tradable}


def has_data(symbols: list[str], chunk: int = 200) -> set[str]:
    """Symbols that actually return daily bars. Chunked so one oversized
    request can't fail the whole validation."""
    end = datetime.now()
    start = end - timedelta(days=30)
    ok: set[str] = set()
    for i in range(0, len(symbols), chunk):
        batch = symbols[i:i + chunk]
        try:
            bars = get_daily_bars(batch, start, end)
        except Exception as e:
            print(f"   chunk {i//chunk + 1} failed ({type(e).__name__}), retrying individually...")
            bars = {}
            for s in batch:
                try:
                    bars.update(get_daily_bars([s], start, end))
                except Exception:
                    pass
        ok |= {s for s, df in bars.items() if df is not None and not df.empty}
        print(f"   validated {min(i + chunk, len(symbols))}/{len(symbols)} -- {len(ok)} with data")
    return ok


def main():
    dry_run = "--dry-run" in sys.argv

    members: dict[str, list[str]] = {}
    for name, url, column, rows in SOURCES:
        syms = fetch_index(name, url, column, rows)
        members[name] = syms
        print(f"{name:10} {len(syms):>4} symbols")

    union = sorted({s for syms in members.values() for s in syms})
    print(f"\nunion of indices: {len(union)} symbols")

    # Wikipedia writes class shares as BRK.B; Alpaca agrees, but strip any
    # stray whitespace/footnote artifacts and drop the benchmarks, which
    # are watched separately and never traded.
    union = [s for s in union if s.replace(".", "").replace("-", "").isalnum()]
    union = [s for s in union if s not in BENCHMARK_SYMBOLS]

    print("checking Alpaca tradability...")
    tradable = alpaca_tradable()
    not_tradable = [s for s in union if s not in tradable]
    candidates = [s for s in union if s in tradable]
    print(f"   tradable: {len(candidates)}  |  rejected: {len(not_tradable)}"
          + (f" -> {not_tradable[:12]}" if not_tradable else ""))

    print("checking data availability...")
    with_data = has_data(candidates)
    no_data = [s for s in candidates if s not in with_data]
    final = sorted(with_data)
    if no_data:
        print(f"   dropped (no bars): {len(no_data)} -> {no_data[:12]}")

    shortable = sum(1 for s in final if getattr(tradable[s], "shortable", False))
    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "sources": {k: len(v) for k, v in members.items()},
        "counts": {
            "union": len(union),
            "tradable": len(candidates),
            "final": len(final),
            "shortable": shortable,
        },
        "rejected_not_tradable": not_tradable,
        "rejected_no_data": no_data,
        "symbols": final,
    }

    print(f"\nFINAL UNIVERSE: {len(final)} symbols "
          f"(from {len(union)} index members; {len(not_tradable)} not tradable, "
          f"{len(no_data)} without data)")
    print(f"  benchmarks watched separately: {list(BENCHMARK_SYMBOLS)}")

    if dry_run:
        print("\n--dry-run: nothing written.")
        return

    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(json.dumps(payload, indent=2))
    print(f"\nWrote {OUT_PATH}")
    print("shared.config picks this up on next import; restart the market loop to apply.")


if __name__ == "__main__":
    main()
