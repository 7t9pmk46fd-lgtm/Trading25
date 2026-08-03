"""
Shared configuration for the trading desk.
All secrets/config come from environment variables — never hardcode keys.

Required env vars (set these in your shell, a .env file, or your OS's
secret manager — never commit them):

  ALPACA_API_KEY        - Alpaca API key ID
  ALPACA_SECRET_KEY      - Alpaca API secret key
  ALPACA_PAPER           - "true" (default) or "false". Controls whether the
                            execution agent hits paper-trading or live endpoints.

Required for the learning agent:
  ANTHROPIC_API_KEY      - used to extract structured claims from articles/
                            transcripts. Get one at console.anthropic.com.

Optional:
  TRADING_DESK_DB_PATH   - path to the shared SQLite db (default: ./data/trading.db)
  TRADING_DESK_WATCHLIST - comma-separated tickers for automated signal
                            generation (default: a small liquid-name list)
"""
import os
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def _load_dotenv(path: Path) -> None:
    """Minimal .env loader -- only fills vars not already set in the real
    environment, so an interactive shell export always takes priority over
    the file (used by scheduled/unattended runs, which have no shell to
    export into). Not committed -- see .env.example."""
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        if key and key not in os.environ:
            os.environ[key] = value.strip()


_load_dotenv(PROJECT_ROOT / ".env")

ALPACA_API_KEY = os.environ.get("ALPACA_API_KEY", "")
ALPACA_SECRET_KEY = os.environ.get("ALPACA_SECRET_KEY", "")
ALPACA_PAPER = os.environ.get("ALPACA_PAPER", "true").lower() != "false"

ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")

DB_PATH = os.environ.get(
    "TRADING_DESK_DB_PATH", str(PROJECT_ROOT / "data" / "trading.db")
)

# Expanded 2026-08-03 from 16 names (heavily mega-cap tech) to a
# sector-diversified set of liquid large caps, so the strategy has more
# independent opportunities and less single-sector concentration. All are
# high-volume names, which also matters for short-side borrow availability.
_DEFAULT_WATCHLIST = ",".join([
    # Tech & semis
    "AAPL", "MSFT", "NVDA", "AMD", "INTC", "AVGO", "QCOM", "TXN", "ORCL", "CRM", "ADBE", "CSCO",
    # Internet & media
    "AMZN", "GOOGL", "META", "NFLX", "DIS", "CMCSA",
    # Consumer discretionary
    "TSLA", "HD", "MCD", "NKE", "SBUX", "LOW",
    # Consumer staples
    "PG", "KO", "PEP", "COST", "WMT",
    # Financials
    "JPM", "BAC", "GS", "MS", "WFC", "V", "MA",
    # Healthcare
    "JNJ", "UNH", "PFE", "ABBV", "MRK", "LLY", "TMO",
    # Energy
    "XOM", "CVX", "COP",
    # Industrials
    "CAT", "BA", "GE", "HON", "UPS",
    # Telecom
    "T", "VZ",
    # Higher-beta / speculative (carried over from the original list)
    "NOK", "AAL", "NU", "SOFI",
    # Benchmarks / broad ETFs -- MONITORED, NEVER TRADED (see BENCHMARK_SYMBOLS)
    "SPY", "QQQ",
])
WATCHLIST = [
    t.strip().upper()
    for t in os.environ.get("TRADING_DESK_WATCHLIST", _DEFAULT_WATCHLIST).split(",")
    if t.strip()
]

# Broad-market ETFs are watched (dashboard, daily review, benchmarking) but
# never traded by a single-name mean-reversion strategy. Every backtest has
# always excluded them; the LIVE cycle was scanning the raw WATCHLIST and so
# could trade them -- a live/backtest mismatch, fixed 2026-08-03 by routing
# signal generation through TRADE_UNIVERSE instead.
BENCHMARK_SYMBOLS = ("SPY", "QQQ")
TRADE_UNIVERSE = [t for t in WATCHLIST if t not in BENCHMARK_SYMBOLS]

# Hard ceiling on concurrent open positions. Necessary as of the 2026-08-03
# universe expansion: with 16 tickers and a 10% position cap the portfolio
# was implicitly bounded at ~10 names, but 57 tickers at a 5% cap means a
# single broad selloff could push the z-score of dozens of names below the
# entry threshold on the SAME day -- the daily cycle would then try to open
# them all at once and commit the whole account in one cycle. Nothing else
# in the pipeline limits total exposure, so this is the limit.
# 20 x 5% = 100% of equity, i.e. fully invested at the ceiling.
MAX_CONCURRENT_POSITIONS = int(os.environ.get("TRADING_DESK_MAX_POSITIONS", "20"))


# Accounts this system polls. The 'sneaky_pivot' paper account was CLOSED
# by the user on 2026-08-03 -- Alpaca now returns ACCOUNT_CLOSED for it,
# which alpaca-py can't even deserialize. Anything that iterates this tuple
# (trail_stops, the dashboard, the daily review) would raise every cycle if
# it were still listed. Back to a single live account.
KNOWN_ACCOUNTS = ("default",)
SNEAKY_PIVOT_STRATEGIES = {"sneaky_pivot"}

# Sneaky Pivot is DISABLED (2026-08-03, at the user's direction). In its
# first and only live session it produced two naked short positions from a
# stale-fill oversell bug (MSFT -25, NOK -1119) -- the bug itself is fixed
# in three layers, but the strategy was never validated (backtest: -0.44%,
# a coded approximation of an explicitly discretionary YouTube method), so
# it was costing more attention than it was earning.
#
# Effect: no new sneaky_pivot entries OR exits are generated, by the cycle
# or by a manual run. Two things deliberately still run against that
# account: trail_stops (account-level protection of anything still held)
# and the loop's end-of-day flatten (so positions the strategy already
# opened get closed rather than orphaned).
#
# To re-enable: set SNEAKY_PIVOT_ENABLED=true in .env. Do NOT re-enable
# without first backtesting it and confirming the oversell guards hold --
# see signals/SKILL.md.
SNEAKY_PIVOT_ENABLED = os.environ.get("SNEAKY_PIVOT_ENABLED", "false").lower() == "true"


def account_for_strategy(strategy: str | None) -> str:
    """Single source of truth for which Alpaca account a strategy's orders
    route through.

    Everything routes to 'default' again as of 2026-08-03: Sneaky Pivot
    got its own paper account on 2026-07-28 to isolate its intraday PDT
    count and circuit breaker from the swing strategy's, but that account
    has since been closed and the strategy disabled. Routing anything to
    the dead account would produce an unreconcilable order. The mapping
    stays as a function (rather than being inlined) so a future strategy
    can be given its own account without touching every call site."""
    return "default"


def require_alpaca_credentials() -> None:
    """Call this before any live Alpaca API call. Fails loudly instead of
    silently proceeding with empty credentials."""
    if not ALPACA_API_KEY or not ALPACA_SECRET_KEY:
        raise RuntimeError(
            "Missing Alpaca credentials. Set ALPACA_API_KEY and "
            "ALPACA_SECRET_KEY as environment variables before running "
            "any script that talks to Alpaca."
        )


def require_anthropic_credentials() -> None:
    """Call this before any Anthropic API call (learning agent extraction)."""
    if not ANTHROPIC_API_KEY:
        raise RuntimeError(
            "Missing Anthropic API key. Set ANTHROPIC_API_KEY as an "
            "environment variable before running the learning agent's "
            "extraction step."
        )
