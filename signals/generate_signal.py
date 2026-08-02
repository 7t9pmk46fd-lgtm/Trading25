"""
Signal generation for both strategies: fetch real bars, run a screener,
and queue actionable signals to the shared `signals` table.

Two strategies live here (merged from the old rd-agent and
day-trading-agent during the 2026-08-02 restructure):

  - rd_mean_reversion (swing/daily): NOT risk-gated at generation time --
    per shared/risk.py and the execution agent's defense-in-depth design,
    these get their first risk check at execution time.
  - sneaky_pivot (intraday): MUST pass PDT + circuit-breaker checks before
    being written at all -- a day-trading signal that violates PDT should
    never even reach the queue, not be caught later.

Usage:
    python signals/generate_signal.py TICKER
"""
import sys
from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from shared import db, risk
from shared.risk import RiskLimits, compute_position_size, compute_atr
from shared.market_data import get_daily_bars
from signals.screeners.mean_reversion import generate_signals, latest_signal, MeanReversionParams
from signals.screeners.sneaky_pivot import compute_levels, evaluate_today, SneakyPivotParams

KNOWN_SIGNALS = {"enter_long", "exit_long", "enter_short", "exit_short"}


def generate_and_queue_batch(tickers: list[str]) -> list[dict]:
    """
    Same semantics as generate_and_queue, for a whole ticker list -- but
    fetches bars in a single Alpaca call (alpaca_data.get_daily_bars
    already accepts a symbol list) and fetches account/position state at
    most once per batch instead of once per ticker. On a watchlist scan,
    this turns N+ network round-trips into 1-3.
    """
    tickers = [t.upper() for t in tickers]
    end = datetime.now()
    start = end - timedelta(days=90)  # comfortably covers a 20-trading-day lookback

    all_bars = get_daily_bars(tickers, start, end)

    # Lazy + cached: a cycle with zero buy signals never touches Alpaca's
    # account/position endpoints at all.
    _cache: dict = {}

    def _account():
        if "account" not in _cache:
            from execution import alpaca_client
            _cache["account"] = alpaca_client.get_account_snapshot()
        return _cache["account"]

    def _held_positions():
        # Includes symbols with an open (unfilled) buy order, not just
        # filled positions -- a day-TIF order submitted after close (or
        # still filling) doesn't appear in get_open_positions() yet, so
        # checking positions alone let a second scan duplicate the buy
        # before the first one filled.
        if "positions" not in _cache:
            from execution import alpaca_client
            held = {p["symbol"] for p in alpaca_client.get_open_positions()}
            pending_buys = alpaca_client.get_open_buy_order_symbols()
            _cache["positions"] = held | pending_buys
        return _cache["positions"]

    def _held_qty():
        # Real filled qty per symbol, for sizing exit signals. Separate
        # from _held_positions() (which also counts unfilled pending buys,
        # with no real qty to sell yet).
        if "held_qty" not in _cache:
            from execution import alpaca_client
            _cache["held_qty"] = {p["symbol"]: p["qty"] for p in alpaca_client.get_open_positions()}
        return _cache["held_qty"]

    results = []
    for ticker in tickers:
        try:
            results.append(_screen_one(ticker, all_bars, _account, _held_positions, _held_qty))
        except Exception as e:
            # A bug in one ticker (e.g. the unrecognized-signal guard
            # tripping) must not cost the rest of an unattended batch its
            # signals -- isolate per-ticker like the original per-ticker
            # loop did, now that fetching is batched instead of the
            # processing.
            results.append({"ticker": ticker, "status": "error", "error": str(e)})

    return results


def _screen_one(ticker, all_bars, get_account, get_held_positions, get_held_qty) -> dict:
    if ticker not in all_bars or all_bars[ticker].empty:
        return {"ticker": ticker, "status": "no_data"}

    result = generate_signals(all_bars[ticker], MeanReversionParams())
    sig = latest_signal(result)

    if sig is None:
        last_z = result["zscore"].iloc[-1]
        return {"ticker": ticker, "status": "no_signal", "zscore": float(last_z)}

    if sig["signal"] not in KNOWN_SIGNALS:
        raise ValueError(
            f"Unrecognized signal value {sig['signal']!r} for {ticker} -- "
            f"refusing to guess buy/sell. This should never happen; treat "
            f"it as a bug."
        )

    action = "buy" if sig["signal"] in ("enter_long", "exit_short") else "sell"
    reasoning = f"signal={sig['signal']}, zscore={sig['zscore']:.2f}, close={sig['close']:.2f}"
    qty = None

    if action == "buy":
        # The screener simulates its position from scratch off price
        # history each run -- it has no memory of a real held position.
        # Without this check, a lookback-window rollover could replay a
        # "new" enter_long for a ticker we already hold, stacking exposure
        # past the intended per-trade cap. Real position state always wins.
        # Safe to cache across the batch: queuing a signal doesn't change
        # Alpaca's real positions, only executing one does.
        if ticker in get_held_positions():
            return {"ticker": ticker, "status": "skipped_already_holding"}

        entry_price = sig["close"]
        atr = compute_atr(all_bars[ticker])
        if pd.isna(atr) or atr <= 0:
            # Not enough history (or degenerate zero-range data) to place
            # a real stop -- refuse rather than fall back to a made-up
            # distance. A buy with no defensible stop doesn't get queued.
            return {"ticker": ticker, "status": "no_stop_data"}

        stop_price = entry_price - 2 * atr  # standard 2xATR stop distance
        if stop_price <= 0:
            return {"ticker": ticker, "status": "no_stop_data"}

        account = get_account()
        limits = RiskLimits(account_equity=account.equity)
        sizing = compute_position_size(limits, entry_price, stop_price)
        qty = sizing["qty"]
        reasoning += f", atr={atr:.2f}, stop_price={stop_price:.2f}, sizing={sizing}"
    else:
        # The screener simulates its position from scratch off price
        # history alone -- it has no memory of whether a real position
        # exists. Without this, every exit signal left qty=None, which
        # run_execution_loop.py unconditionally rejects: found 2026-07-28
        # after a real exit_long for NFLX (z-score reverted to 0.27) was
        # silently rejected and the position stayed open despite the
        # strategy's own logic wanting to close it. Same bug class already
        # fixed in day-trading-agent's generate_and_queue_signal.
        qty = get_held_qty().get(ticker)
        if not qty:
            return {"ticker": ticker, "status": "skipped_not_held"}

    with db.db_session() as conn:
        signal_id = db.insert_signal(
            conn, ticker=ticker, action=action, strategy="rd_mean_reversion",
            qty=qty, stop_price=stop_price if action == "buy" else None,
            confidence=None, reasoning=reasoning,
        )

    return {"ticker": ticker, "status": "queued", "signal_id": signal_id, "action": action, "qty": qty,
            "stop_price": stop_price if action == "buy" else None}


def generate_and_queue(ticker: str) -> dict:
    """Single-ticker convenience wrapper around generate_and_queue_batch."""
    return generate_and_queue_batch([ticker])[0]


def generate_and_queue_sneaky_pivot_signal(
    ticker: str,
    daily_bars: pd.DataFrame,
    intraday_bars_today: pd.DataFrame,
    session_close,
    account_equity: float,
    is_pdt_account: bool,
    current_daily_pnl: float,
    held_qty: float | None,
    params: SneakyPivotParams = SneakyPivotParams(),
) -> int | None:
    """
    Sneaky Pivot strategy (see screeners/sneaky_pivot.py for the full
    ruleset and caveats -- it's a coded approximation of a discretionary
    YouTube strategy, added 2026-07-27).

    held_qty: current real position size in this ticker (None/0 if flat).
    Exit signals here carry a real qty (the held amount) rather than
    leaving it None -- run_execution_loop rejects ANY signal with
    qty=None, entry or exit, so an exit signal without a real qty would
    silently never execute.

    Returns the new signal id, or None if no actionable signal / blocked.
    Raises risk.RiskViolation if a risk rule blocks an attempted entry.
    """
    limits = risk.RiskLimits(account_equity=account_equity, is_pdt_account=is_pdt_account)

    levels = compute_levels(daily_bars, params)
    if levels is None:
        return None

    atr = risk.compute_atr(daily_bars)
    if pd.isna(atr):
        return None

    has_open_position = bool(held_qty and held_qty > 0)
    outcome = evaluate_today(
        intraday_bars_today, levels, atr, session_close, has_open_position, params
    )
    signal = outcome["signal"]
    if signal is None:
        return None

    SNEAKY_KNOWN_SIGNALS = {"enter_long", "exit_long", "force_flatten_eod"}
    if signal not in SNEAKY_KNOWN_SIGNALS:
        raise ValueError(
            f"Unrecognized signal value {signal!r} for {ticker} -- refusing "
            f"to guess buy/sell. This should never happen; treat it as a bug."
        )

    with db.db_session() as conn:
        if signal == "enter_long":
            risk.check_pdt_allows_trade(conn, limits)
            risk.check_circuit_breaker(conn, limits, current_daily_pnl)

            entry_price = outcome["entry_price"]
            stop_price = outcome["stop_price"]
            sizing = risk.compute_position_size(limits, entry_price, stop_price)
            qty = sizing["qty"]
            if qty <= 0:
                return None
            action = "buy"
        else:
            # Exits (including force_flatten_eod) are never risk-gated --
            # closing a position must always be allowed through.
            action = "sell"
            qty = held_qty
            stop_price = None

        signal_id = db.insert_signal(
            conn,
            ticker=ticker,
            action=action,
            strategy="sneaky_pivot",
            qty=qty,
            stop_price=stop_price,
            confidence=None,
            reasoning=outcome.get("reasoning", signal),
        )

    return signal_id


def main():
    if len(sys.argv) < 2:
        print("Usage: python signals/generate_signal.py TICKER")
        sys.exit(1)

    result = generate_and_queue(sys.argv[1])
    if result["status"] == "no_data":
        print(f"No data returned for {result['ticker']}.")
    elif result["status"] == "no_signal":
        print(f"No actionable signal for {result['ticker']}. Latest z-score: {result['zscore']:.2f} "
              f"(entry threshold: {MeanReversionParams().entry_z}, "
              f"exit threshold: {MeanReversionParams().exit_z})")
    else:
        print(f"Queued signal #{result['signal_id']}: {result['action']} {result['ticker']}"
              + (f" qty={result['qty']}" if result['qty'] else ""))


if __name__ == "__main__":
    main()
