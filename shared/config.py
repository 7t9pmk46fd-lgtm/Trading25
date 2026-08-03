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


# Every Alpaca account this system polls. Single account today; the
# per-account plumbing (alpaca_client's `account` parameter,
# account_for_strategy below) is retained so a future strategy can be
# given its own isolated account without touching every call site.
KNOWN_ACCOUNTS = ("default",)


def account_for_strategy(strategy: str | None) -> str:
    """Single source of truth for which Alpaca account a strategy's orders
    route through. One account today, so everything maps to 'default'.

    Kept as a function rather than inlined because the mapping is the only
    thing that would need to change to give a future strategy its own
    isolated account (separate PDT day-trade count and circuit breaker).
    Historical rows in the DB may carry strategy names that no longer
    exist; they resolve here to 'default' rather than erroring."""
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
