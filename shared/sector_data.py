"""
Sector/industry reference data -- read-only, no network, no MCP.

Backing data comes from the FMP MCP server (company.profile-symbol),
which is only callable from a Claude session -- there is no FMP API key
in .env, so nothing in the live desk (run_trading_day.py,
generate_signal.py, scripts run unattended by Task Scheduler) can reach
it directly. The pattern here is the same one already used for
data/coverage_report.json and data/sizing_viability_report.json: a
Claude session fetches the data and writes a JSON cache
(data/sector_tags.json), and any code -- live, research, or dashboard --
reads that cache as a plain file. That means this module has zero
runtime dependency on FMP or the network; it only knows how to read a
file someone already produced.

Regenerating the cache is manual today: it has to be a Claude session
issuing FMP MCP company.profile-symbol calls and rewriting
data/sector_tags.json (see that file's own "source" field). No Python
script can do this end to end without a real FMP API key -- the
`calendar` tool's earnings-date endpoints hit an ACCESS DENIED for the
current FMP plan tier when tried 2026-08-25, so that data was not
pulled at all; sector/industry via `company` worked fine at this tier.
The natural place to keep this refreshed is the weekly-rd task (which
already runs periodically and already calls an external MCP for
research), but wiring it in is a deliberate choice for a human to make,
not something this module does on its own.

Coverage as of the 2026-08-25 cache: the 20 symbols held at that time,
NOT the full ~518-symbol universe -- a full-universe pull is 500+
individual MCP calls and wasn't attempted. `load_sector_tags()` returns
whatever the cache actually has; callers must handle a symbol being
absent (see `sector_concentration`'s `"unknown"` bucket) rather than
assume full coverage.
"""
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_PATH = ROOT / "data" / "sector_tags.json"


def load_sector_tags(path: Path = DEFAULT_PATH) -> dict:
    """
    Returns {symbol: {"sector": str, "industry": str}}. Empty dict if the
    cache doesn't exist yet -- callers should treat that as "no data",
    not raise, since this is reference data, not something risk-critical
    code should ever hard-fail on.
    """
    if not path.exists():
        return {}
    data = json.loads(path.read_text())
    return data.get("tags", {})


def sector_concentration(positions: list[dict], tags: dict | None = None) -> dict:
    """
    positions: list of dicts with at least 'symbol' and 'unrealized_pl'
    (the shape mcp__trading-desk__portfolio_status / alpaca_client.get_
    open_positions already return). Groups position count and P&L by
    sector. A symbol missing from `tags` (cache not covering it) lands
    in an explicit "unknown" bucket rather than being silently dropped
    or mis-bucketed.

    Pure function -- no I/O of its own; pass the result of
    load_sector_tags() (or a stand-in for testing) as `tags`.
    """
    if tags is None:
        tags = load_sector_tags()

    by_sector: dict[str, dict] = {}
    for p in positions:
        sector = tags.get(p["symbol"], {}).get("sector", "unknown")
        bucket = by_sector.setdefault(sector, {"count": 0, "unrealized_pl": 0.0, "symbols": []})
        bucket["count"] += 1
        bucket["unrealized_pl"] += p.get("unrealized_pl", 0.0)
        bucket["symbols"].append(p["symbol"])

    total_positions = len(positions)
    total_pl = sum(p.get("unrealized_pl", 0.0) for p in positions)
    for sector, bucket in by_sector.items():
        bucket["pct_of_positions"] = bucket["count"] / total_positions if total_positions else 0.0
        bucket["pct_of_unrealized_pl"] = (
            bucket["unrealized_pl"] / total_pl if total_pl else 0.0
        )

    return {
        "total_positions": total_positions,
        "total_unrealized_pl": total_pl,
        "by_sector": by_sector,
    }
