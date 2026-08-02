"""
Generate a day-trading signal, gated by risk checks.

This does NOT place any order -- like rd-agent, it only writes to the
shared `signals` table. The execution agent remains the only thing that
talks to Alpaca. What's different from rd-agent's signals is that these
carry strategy='day_trading_intraday' and MUST pass PDT + circuit-breaker
checks before being written at all -- a day-trading signal that violates
PDT should never even reach the queue, not be caught later at execution
time.

Usage (per ticker, run on a live/near-live intraday bar feed):
    python scripts/generate_signal.py TICKER

Requires ALPACA_API_KEY/ALPACA_SECRET_KEY for real intraday bars once the
Alpaca data client exists (not yet built -- see rd-agent/data/, still
pending). Until then this script's data-fetch step is a placeholder.
"""
import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "day-trading-agent"))

from shared import db, risk
from screeners.intraday_mean_reversion import generate_intraday_signals, get_last_signal, IntradayParams
from screeners.sneaky_pivot import compute_levels, evaluate_today, SneakyPivotParams


def generate_and_queue_signal(
    ticker: str,
    bars_df,
    session_close,
    account_equity: float,
    is_pdt_account: bool,
    current_daily_pnl: float,
    held_qty: float | None = None,
    params: IntradayParams = IntradayParams(),
) -> int | None:
    """
    Returns the new signal id, or None if no actionable signal / blocked
    by a risk check. Raises risk.RiskViolation if a risk rule is violated
    on an attempted entry (callers should catch this and log/alert, not
    swallow it silently).

    held_qty: current real position size in this ticker, required for any
    exit (exit_long/force_flatten_eod) to carry a real qty.
    run_execution_loop.py rejects ANY signal with qty=None, entry or exit
    -- found and fixed as a real latent bug on 2026-07-27 (this function
    previously left qty=None for every exit, which would have silently
    made every exit signal unexecutable the moment this got wired into a
    real cycle -- same bug class already avoided in
    generate_and_queue_sneaky_pivot_signal).
    """
    limits = risk.RiskLimits(
        account_equity=account_equity,
        is_pdt_account=is_pdt_account,
    )

    result = generate_intraday_signals(bars_df, session_close, params)
    signal = get_last_signal(result)
    last = result.iloc[-1]

    if signal is None:
        return None

    KNOWN_SIGNALS = {"enter_long", "exit_long", "force_flatten_eod"}
    if signal not in KNOWN_SIGNALS:
        raise ValueError(
            f"Unrecognized signal value {signal!r} for {ticker} -- refusing "
            f"to guess buy/sell. This should never happen; treat it as a bug."
        )

    with db.db_session() as conn:
        # Only entries are gated by PDT/circuit-breaker -- exits (including
        # force_flatten_eod) must ALWAYS be allowed through, since blocking
        # an exit is far more dangerous than blocking an entry.
        if signal == "enter_long":
            risk.check_pdt_allows_trade(conn, limits)
            risk.check_circuit_breaker(conn, limits, current_daily_pnl)

        action = "buy" if signal == "enter_long" else "sell"
        sizing = None
        qty = None
        if action == "buy":
            # Placeholder stop-loss: 1% below entry. Real stop placement
            # should come from the strategy (e.g. below recent swing low),
            # not a flat percentage -- refine before trusting this sizing.
            entry_price = last["close"]
            stop_price = entry_price * 0.99
            sizing = risk.compute_position_size(limits, entry_price, stop_price)
            qty = sizing["qty"]
        else:
            qty = held_qty

        signal_id = db.insert_signal(
            conn,
            ticker=ticker,
            action=action,
            strategy="day_trading_intraday",
            qty=qty,
            confidence=None,
            reasoning=f"signal={signal}, zscore={last['zscore']:.2f}"
            + (f", sizing={sizing}" if sizing else ""),
        )

    return signal_id


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
    YouTube strategy, unvalidated, added 2026-07-27).

    held_qty: current real position size in this ticker (None/0 if flat).
    Unlike generate_and_queue_signal above, exit signals here carry a real
    qty (the held amount) rather than leaving it None -- run_execution_loop
    rejects ANY signal with qty=None, entry or exit, so an exit signal
    without a real qty would silently never execute.

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

    KNOWN_SIGNALS = {"enter_long", "exit_long", "force_flatten_eod"}
    if signal not in KNOWN_SIGNALS:
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


if __name__ == "__main__":
    print(
        "This script requires a live intraday bar feed to run for real. "
        "The Alpaca data client (rd-agent/data/) isn't built yet -- wire "
        "that up first, then call generate_and_queue_signal() per ticker "
        "on a polling loop during market hours."
    )
