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

_DEFAULT_WATCHLIST = "AAPL,MSFT,NVDA,AMZN,GOOGL,META,TSLA,AMD,NFLX,SPY,QQQ"
WATCHLIST = [
    t.strip().upper()
    for t in os.environ.get("TRADING_DESK_WATCHLIST", _DEFAULT_WATCHLIST).split(",")
    if t.strip()
]


KNOWN_ACCOUNTS = ("default", "sneaky_pivot")
SNEAKY_PIVOT_STRATEGIES = {"sneaky_pivot"}


def account_for_strategy(strategy: str | None) -> str:
    """Single source of truth for which Alpaca account a strategy's orders
    route through. Added 2026-07-28 when Sneaky Pivot moved to its own
    paper account (PA3OEVR40VTX) to isolate its PDT day-trade counter and
    daily circuit breaker from rd-agent's swing positions -- a bad
    intraday day trading Sneaky Pivot shouldn't be able to block a
    legitimate rd-agent entry, and vice versa."""
    return "sneaky_pivot" if strategy in SNEAKY_PIVOT_STRATEGIES else "default"


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
