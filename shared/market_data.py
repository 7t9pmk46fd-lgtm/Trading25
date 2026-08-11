"""
Alpaca market data client.

Wraps alpaca-py's historical data client to pull bars in the shape the
screeners expect: a DataFrame indexed by timestamp with 'close' and
'volume' columns, per ticker.

This is written against alpaca-py's documented interface but has NOT been
run against the live API (no network access in the build environment).
Test this against your real account before trusting it -- see
scripts/smoke_test.py for a minimal first check.
"""
import pickle
from datetime import datetime
from pathlib import Path

import pandas as pd

from shared.config import ALPACA_API_KEY, ALPACA_SECRET_KEY, PROJECT_ROOT, require_alpaca_credentials

_CACHE_DIR = Path(PROJECT_ROOT) / "data" / "cache"


def _get_client():
    require_alpaca_credentials()
    from alpaca.data.historical import StockHistoricalDataClient

    return StockHistoricalDataClient(ALPACA_API_KEY, ALPACA_SECRET_KEY)


def get_daily_bars(
    symbols: list[str],
    start: datetime,
    end: datetime,
) -> dict[str, pd.DataFrame]:
    """
    Returns {symbol: DataFrame} with columns ['close', 'volume'] (plus
    Alpaca's other OHLC columns, kept for reference), indexed by date.
    Symbols with no data (e.g. bad ticker, no history in range) are
    silently omitted from the result -- check len(result) vs len(symbols)
    if you need to know which ones failed.
    """
    from alpaca.data.requests import StockBarsRequest
    from alpaca.data.timeframe import TimeFrame

    client = _get_client()
    request = StockBarsRequest(
        symbol_or_symbols=symbols,
        timeframe=TimeFrame.Day,
        start=start,
        end=end,
    )
    bars = client.get_stock_bars(request)
    df_all = bars.df  # MultiIndex (symbol, timestamp)

    result = {}
    for symbol in symbols:
        if symbol not in df_all.index.get_level_values(0):
            continue
        df = df_all.loc[symbol].copy()
        df.index = df.index.tz_localize(None) if df.index.tz is not None else df.index
        result[symbol] = df

    return result


def get_daily_bars_cached(
    symbols: list[str],
    start: datetime,
    end: datetime,
) -> dict[str, pd.DataFrame]:
    """
    Same as get_daily_bars, but reuses a same-day on-disk cache instead of
    hitting the API every call.

    Added 2026-07-27: daily bars only change once a trading day completes,
    but a 15-minute cycle re-fetches 40-70 days of daily history every
    pass purely to compute ATR -- up to 4x/hour of identical, redundant
    network calls.
    Each cron fire is a fresh Python process, so a module-level/in-memory
    cache wouldn't survive between calls -- this has to be a real file.

    Cache key is just today's date. If a same-day cache file exists and
    already covers every requested symbol, it's used as-is with no API
    call; otherwise this fetches (only) what's missing and merges it in.
    Old cache files (>3 days) are cleaned up opportunistically.

    "Covers" means present AND spans back to `start` (added 2026-08-11,
    fixing a real bug: this used to check presence only, so whichever
    caller populated a symbol's entry FIRST each day locked in THAT
    caller's window for every other caller for the rest of the day,
    regardless of how much history they actually asked for. Confirmed
    live: daily_review.py's ~12-day window cached HST with 8 bars before
    trail_stops' 40-day request ran, and trail_stops silently got the
    same truncated 8 bars back -- not enough for a 14-day ATR, so it
    skipped placing HST's stop while the position sat genuinely
    unprotected during market hours. A caller now gets a fresh fetch
    whenever the cached entry doesn't reach back far enough for THAT
    call, and the wider result overwrites the cache entry -- so the
    widest window requested on a given day ends up satisfying everyone
    who asks after it, which is what "cached" should have meant from the
    start.
    """
    _CACHE_DIR.mkdir(parents=True, exist_ok=True)
    today = datetime.now().date()
    cache_path = _CACHE_DIR / f"daily_bars_{today.isoformat()}.pkl"

    def _covers(df: pd.DataFrame | None) -> bool:
        return df is not None and not df.empty and df.index.min().date() <= start.date()

    cached: dict[str, pd.DataFrame] = {}
    if cache_path.exists():
        with open(cache_path, "rb") as f:
            cached = pickle.load(f)
        if all(s in cached and _covers(cached[s]) for s in symbols):
            return {s: cached[s] for s in symbols}

    missing = [s for s in symbols if s not in cached or not _covers(cached.get(s))]
    fresh = get_daily_bars(missing, start, end)
    merged = {**cached, **fresh}

    with open(cache_path, "wb") as f:
        pickle.dump(merged, f)

    for old_file in _CACHE_DIR.glob("daily_bars_*.pkl"):
        if old_file != cache_path and (today - datetime.strptime(old_file.stem.removeprefix("daily_bars_"), "%Y-%m-%d").date()).days > 3:
            old_file.unlink()

    return {s: merged[s] for s in symbols if s in merged}


def get_intraday_bars(
    symbols: list[str],
    start: datetime,
    end: datetime,
    minutes: int = 5,
) -> dict[str, pd.DataFrame]:
    """Same shape as get_daily_bars, but intraday -- for the day-trading agent.

    Explicitly requests the free IEX feed: the default SIP feed requires a
    paid market data subscription for anything recent (confirmed via a real
    403 "subscription does not permit querying recent SIP data" against
    this account -- daily bars don't hit this because a "today" bar isn't
    finalized until close, so a daily request never actually touches
    recent data; an intraday request for today always does).
    """
    from alpaca.data.enums import DataFeed
    from alpaca.data.requests import StockBarsRequest
    from alpaca.data.timeframe import TimeFrame, TimeFrameUnit

    client = _get_client()
    request = StockBarsRequest(
        symbol_or_symbols=symbols,
        timeframe=TimeFrame(minutes, TimeFrameUnit.Minute),
        start=start,
        end=end,
        feed=DataFeed.IEX,
    )
    bars = client.get_stock_bars(request)
    df_all = bars.df

    result = {}
    for symbol in symbols:
        if symbol not in df_all.index.get_level_values(0):
            continue
        result[symbol] = df_all.loc[symbol].copy()

    return result


def get_latest_quote(symbol: str) -> dict:
    """Latest quote for a single symbol -- useful for a quick connectivity
    smoke test and for sanity-checking prices before placing an order."""
    from alpaca.data.requests import StockLatestQuoteRequest

    client = _get_client()
    request = StockLatestQuoteRequest(symbol_or_symbols=symbol)
    quote = client.get_stock_latest_quote(request)[symbol]
    return {
        "symbol": symbol,
        "bid": quote.bid_price,
        "ask": quote.ask_price,
        "timestamp": quote.timestamp,
    }


def get_latest_trade_prices(symbols: list[str]) -> dict[str, float]:
    """
    Last EXECUTED trade price per symbol, batched -- confirmed more
    reliable than get_latest_quote's bid/ask for "what is this actually
    worth right now": a real comparison on 2026-07-27 found AMD's NBBO
    quote implying a fake ~5% after-hours move, while the last trade price
    matched Alpaca's own position current_price (which is itself
    trade-based) almost exactly. Prefer this over get_latest_quote
    whenever the goal is "current price," not "what could I transact at."
    """
    from alpaca.data.enums import DataFeed
    from alpaca.data.requests import StockLatestTradeRequest

    client = _get_client()
    request = StockLatestTradeRequest(symbol_or_symbols=symbols, feed=DataFeed.IEX)
    trades = client.get_stock_latest_trade(request)
    return {symbol: float(t.price) for symbol, t in trades.items()}
