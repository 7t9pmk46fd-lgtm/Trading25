"""
Signal generation: fetch real bars, run the screener, and queue
actionable signals to the shared `signals` table. Nothing here places an
order -- execution/run_execution_loop.py is the only thing that does.

One strategy today: rd_mean_reversion (swing/daily). Its signals are NOT
risk-gated at generation time; per shared/risk.py and the execution
agent's defense-in-depth design they get their first risk check at
execution time, against live account state rather than stale state from
whenever the signal was written.

Usage:
    python signals/generate_signal.py TICKER
"""
import sys
from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from shared import db
from shared.config import MAX_CONCURRENT_POSITIONS
from shared.risk import RiskLimits, compute_position_size, compute_atr
from shared.market_data import get_daily_bars
from signals.screeners.mean_reversion import generate_signals, latest_signal, MeanReversionParams

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

    # --- Pass 1: evaluate every ticker, write nothing ---------------------
    # Deliberately split from queueing. With a 500+ name universe the
    # scan routinely produces more entry candidates than there are free
    # position slots, and queueing inline would fill those slots in
    # whatever order the tickers happen to be listed -- i.e. alphabetically.
    # The universe would then be decorative: only names near the start of
    # the alphabet could ever trade.
    plans = []
    for ticker in tickers:
        try:
            plans.append(_plan_one(ticker, all_bars, _account, _held_positions, _held_qty))
        except Exception as e:
            # A bug in one ticker (e.g. the unrecognized-signal guard
            # tripping) must not cost the rest of an unattended batch its
            # signals -- isolate per-ticker.
            plans.append({"ticker": ticker, "status": "error", "error": str(e)})

    # --- Pass 2: queue exits unconditionally, then the best entries -------
    results = []
    entries = []
    for plan in plans:
        if plan["status"] != "actionable":
            results.append(plan)
        elif plan["action"] == "sell":
            # Exits are NEVER rationed. Blocking an exit is far more
            # dangerous than missing an entry.
            results.append(_queue(plan))
        else:
            entries.append(plan)

    # Rank entries by conviction: the most negative z-score is the most
    # stretched below its mean, which is precisely what this strategy
    # claims an edge on. Ties broken by ticker for determinism.
    entries.sort(key=lambda p: (p["zscore"], p["ticker"]))

    capacity = MAX_CONCURRENT_POSITIONS - len(_held_positions())
    for rank, plan in enumerate(entries):
        if rank < capacity:
            queued = _queue(plan)
            queued["rank"] = rank + 1
            results.append(queued)
        else:
            results.append({"ticker": plan["ticker"], "status": "skipped_position_limit",
                            "zscore": plan["zscore"], "rank": rank + 1,
                            "limit": MAX_CONCURRENT_POSITIONS})

    return results


def _queue(plan: dict) -> dict:
    """Write one planned signal to the queue. Split from planning so the
    decision of WHICH signals to take is made with the whole scan in view."""
    with db.db_session() as conn:
        signal_id = db.insert_signal(
            conn, ticker=plan["ticker"], action=plan["action"], strategy="rd_mean_reversion",
            qty=plan["qty"], stop_price=plan.get("stop_price"),
            confidence=None, reasoning=plan["reasoning"],
        )
    return {"ticker": plan["ticker"], "status": "queued", "signal_id": signal_id,
            "action": plan["action"], "qty": plan["qty"],
            "stop_price": plan.get("stop_price"), "zscore": plan["zscore"]}


def _plan_one(ticker, all_bars, get_account, get_held_positions, get_held_qty) -> dict:
    """Decide what this ticker WOULD do. Never writes to the DB -- the
    caller ranks every plan against the others before anything is queued.
    Returns status 'actionable' with the full order shape, or a terminal
    status explaining why not."""
    if ticker not in all_bars or all_bars[ticker].empty:
        return {"ticker": ticker, "status": "no_data"}

    result = generate_signals(all_bars[ticker], MeanReversionParams())
    sig = latest_signal(result)

    if sig is None:
        last_z = result["zscore"].iloc[-1]
        return {"ticker": ticker, "status": "no_signal",
                "zscore": None if pd.isna(last_z) else float(last_z)}

    if sig["signal"] not in KNOWN_SIGNALS:
        raise ValueError(
            f"Unrecognized signal value {sig['signal']!r} for {ticker} -- "
            f"refusing to guess buy/sell. This should never happen; treat "
            f"it as a bug."
        )

    action = "buy" if sig["signal"] in ("enter_long", "exit_short") else "sell"
    zscore = float(sig["zscore"])
    reasoning = f"signal={sig['signal']}, zscore={zscore:.2f}, close={sig['close']:.2f}"
    stop_price = None

    if action == "buy":
        # The screener simulates its position from scratch off price
        # history each run -- it has no memory of a real held position.
        # Without this check, a lookback-window rollover could replay a
        # "new" enter_long for a ticker we already hold, stacking exposure
        # past the intended per-trade cap. Real position state always wins.
        if ticker in get_held_positions():
            return {"ticker": ticker, "status": "skipped_already_holding", "zscore": zscore}

        entry_price = sig["close"]
        atr = compute_atr(all_bars[ticker])
        if pd.isna(atr) or atr <= 0:
            # Not enough history (or degenerate zero-range data) to place
            # a real stop -- refuse rather than fall back to a made-up
            # distance. A buy with no defensible stop doesn't get queued.
            return {"ticker": ticker, "status": "no_stop_data", "zscore": zscore}

        stop_price = entry_price - 2 * atr  # standard 2xATR stop distance
        if stop_price <= 0:
            return {"ticker": ticker, "status": "no_stop_data", "zscore": zscore}

        account = get_account()
        limits = RiskLimits(account_equity=account.equity)
        sizing = compute_position_size(limits, entry_price, stop_price)
        qty = sizing["qty"]
        if qty <= 0:
            return {"ticker": ticker, "status": "size_too_small", "zscore": zscore}
        reasoning += f", atr={atr:.2f}, stop_price={stop_price:.2f}, sizing={sizing}"
    else:
        # The screener has no memory of whether a real position exists.
        # Without this, every exit signal left qty=None, which
        # run_execution_loop.py unconditionally rejects: found 2026-07-28
        # after a real exit_long for NFLX was silently rejected and the
        # position stayed open despite the strategy wanting to close it.
        qty = get_held_qty().get(ticker)
        if not qty:
            return {"ticker": ticker, "status": "skipped_not_held", "zscore": zscore}

    return {"ticker": ticker, "status": "actionable", "action": action, "qty": qty,
            "stop_price": stop_price, "reasoning": reasoning, "zscore": zscore}


def generate_and_queue(ticker: str) -> dict:
    """Single-ticker convenience wrapper around generate_and_queue_batch."""
    return generate_and_queue_batch([ticker])[0]


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
