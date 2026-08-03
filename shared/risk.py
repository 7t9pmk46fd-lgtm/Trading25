"""
Risk management shared by any agent that can generate or execute trades.

This module is deliberately NOT strategy-specific -- it's the layer that
sits between "a strategy wants to trade" and "an order actually gets
submitted," so risk rules are enforced consistently no matter which
strategy (mean-reversion, day-trading, whatever comes next) is asking.

Covers three things:
  1. PDT (Pattern Day Trader) rule enforcement -- FINRA requires accounts
     under $25,000 equity to be limited to 3 "day trades" (opening and
     closing the same position on the same day) within any rolling 5
     business day window, on margin accounts. Exceeding this can get an
     account restricted by the broker. This is a hard regulatory
     constraint, not a preference -- the code blocks rather than warns.
  2. Daily loss circuit breaker -- once realized + open unrealized losses
     for the current trading day hit the configured threshold, no new
     entries are allowed for the rest of the day.
  3. Position sizing -- caps position size as a % of account equity, and
     provides stop-loss-aware sizing so a full stop-out corresponds to a
     bounded % account risk rather than an arbitrary dollar amount.

IMPORTANT: this module tracks state via the shared `orders` table. It is
only as accurate as the orders actually recorded there. If orders are ever
placed directly through Alpaca's dashboard/app outside of this system, the
PDT counter here will be wrong (undercounting) -- treat it as a safety net
on top of Alpaca's own PDT flag, not a replacement for it. Alpaca will
itself reject/flag trades that violate PDT, but you should not rely on
that as your primary defense.
"""
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta

import pandas as pd


def compute_atr(bars: pd.DataFrame, period: int = 14) -> float:
    """
    Average True Range over `period` days: true range = max(high-low,
    |high-prev_close|, |low-prev_close|), then a simple rolling mean.
    Returns the latest value, or NaN if there isn't enough history yet --
    callers must check for NaN rather than treating it as a valid (zero)
    stop distance.

    Used for stop-loss placement instead of a flat percentage, since a
    fixed % (e.g. 1%) is often smaller than a stock's normal daily noise
    and would get hit by routine volatility, not an actual thesis failure.
    """
    high, low, close = bars["high"], bars["low"], bars["close"]
    prev_close = close.shift(1)
    true_range = pd.concat([
        high - low,
        (high - prev_close).abs(),
        (low - prev_close).abs(),
    ], axis=1).max(axis=1)
    return float(true_range.rolling(window=period, min_periods=period).mean().iloc[-1])


@dataclass
class RiskLimits:
    account_equity: float
    is_pdt_account: bool = False        # True if equity >= $25,000 (PDT rule doesn't apply)
    max_day_trades_per_5_days: int = 3  # FINRA PDT limit for non-PDT accounts
    daily_loss_limit_pct: float = 0.025  # 2.5% -- circuit breaker trips here
    max_position_pct: float = 0.05       # 5% of equity, max per single position.
                                          # Halved from 10% on 2026-08-03 when the
                                          # watchlist went from 16 to ~57 names: at a
                                          # 10% cap the account could only ever hold
                                          # ~10 positions, so a wider universe would
                                          # not have produced a wider portfolio. At 5%
                                          # up to 20 can be held concurrently. Note the
                                          # aggregate-risk trade-off: 20 positions each
                                          # risking target_risk_per_trade_pct to their
                                          # stop is 20% of equity at risk if everything
                                          # stops out together (correlated selloff),
                                          # versus 10% before. Lower
                                          # target_risk_per_trade_pct if that is too hot.
    target_risk_per_trade_pct: float = 0.01  # 1% -- used for stop-loss-aware sizing


class RiskViolation(Exception):
    """Raised when a proposed trade would violate a hard risk rule.
    Callers (execution agent) should catch this and refuse to place the
    order, not silently downsize/skip -- a blocked trade should be loud."""
    pass


def count_recent_day_trades(
    conn: sqlite3.Connection,
    lookback_days: int = 5,
    strategies: list[str] | None = None,
    exclude_strategies: list[str] | None = None,
) -> int:
    """
    Count 'day trades' in the orders table over the last `lookback_days`
    calendar days (a reasonable proxy for 5 business days for a first
    version -- swap in a real trading-calendar library if precision near
    the boundary matters to you).

    A day trade = a buy and a sell of the SAME ticker with fills on the
    SAME calendar date.

    strategies/exclude_strategies: scope the count to (or away from) a set
    of strategy names, via a join to the signals table. Added 2026-07-28
    when Sneaky Pivot moved to its own Alpaca account -- without this, an
    intraday day-trade count from one account's strategy would wrongly
    count against the OTHER account's local PDT check, even though the
    two accounts have entirely separate real PDT status with Alpaca.
    """
    cutoff = (datetime.now() - timedelta(days=lookback_days)).strftime("%Y-%m-%d")

    query = "SELECT o.ticker, date(o.filled_at) as fill_date, o.side, COUNT(*) as n FROM orders o"
    params: list = [cutoff]
    if strategies or exclude_strategies:
        query += " JOIN signals s ON o.signal_id = s.id"
    query += " WHERE o.filled_at IS NOT NULL AND date(o.filled_at) >= ?"
    if strategies:
        query += f" AND s.strategy IN ({','.join('?' for _ in strategies)})"
        params += list(strategies)
    if exclude_strategies:
        query += f" AND s.strategy NOT IN ({','.join('?' for _ in exclude_strategies)})"
        params += list(exclude_strategies)
    query += " GROUP BY o.ticker, fill_date, o.side"

    rows = conn.execute(query, params).fetchall()

    # group by (ticker, date) and check both a buy and a sell happened
    by_ticker_date = {}
    for row in rows:
        key = (row["ticker"], row["fill_date"])
        by_ticker_date.setdefault(key, set()).add(row["side"])

    day_trades = sum(1 for sides in by_ticker_date.values() if {"buy", "sell"} <= sides)
    return day_trades


def check_pdt_allows_trade(
    conn: sqlite3.Connection,
    limits: RiskLimits,
    strategies: list[str] | None = None,
    exclude_strategies: list[str] | None = None,
) -> None:
    """Raises RiskViolation if placing another day trade would violate PDT."""
    if limits.is_pdt_account:
        return  # rule doesn't apply above $25k equity

    recent = count_recent_day_trades(
        conn, lookback_days=5, strategies=strategies, exclude_strategies=exclude_strategies
    )
    if recent >= limits.max_day_trades_per_5_days:
        raise RiskViolation(
            f"PDT limit reached: {recent} day trades in the last 5 days "
            f"(limit is {limits.max_day_trades_per_5_days} for accounts "
            f"under $25,000 equity). No new day trades allowed until the "
            f"rolling window clears. This is a FINRA rule, not a "
            f"configurable preference."
        )


def get_today_realized_pnl(conn: sqlite3.Connection) -> float:
    """Sum of realized P&L from orders filled today. Requires fill_price
    to be populated and a matching entry order to compute P&L -- this is a
    simplified placeholder; a real implementation should reconcile against
    Alpaca's own position/activity data rather than recomputing from local
    order records alone."""
    today = datetime.now().strftime("%Y-%m-%d")
    rows = conn.execute(
        "SELECT * FROM orders WHERE date(filled_at) = ? AND fill_price IS NOT NULL",
        (today,),
    ).fetchall()
    # Placeholder: real P&L reconciliation belongs in the execution agent,
    # which has access to Alpaca's actual position/activity records.
    return 0.0


def check_circuit_breaker(conn: sqlite3.Connection, limits: RiskLimits, current_daily_pnl: float) -> None:
    """
    current_daily_pnl: realized + unrealized P&L for today, as a raw dollar
    amount (negative = loss). Callers should compute this from Alpaca's
    live account/position data, not derive it purely from local records.
    """
    loss_limit_dollars = -abs(limits.account_equity * limits.daily_loss_limit_pct)
    if current_daily_pnl <= loss_limit_dollars:
        raise RiskViolation(
            f"Daily circuit breaker tripped: P&L today is ${current_daily_pnl:,.2f}, "
            f"limit is ${loss_limit_dollars:,.2f} "
            f"({limits.daily_loss_limit_pct:.1%} of ${limits.account_equity:,.2f} equity). "
            f"No new entries allowed for the rest of the trading day."
        )


def compute_position_size(
    limits: RiskLimits,
    entry_price: float,
    stop_loss_price: float,
) -> dict:
    """
    Stop-loss-aware position sizing: sizes the position so that if the
    stop-loss is hit, the loss equals target_risk_per_trade_pct of account
    equity -- then caps it at max_position_pct of equity regardless.

    Returns a dict with the recommended share quantity and which
    constraint (risk-based or cap-based) actually bound the size.
    """
    if entry_price <= 0 or stop_loss_price <= 0:
        raise ValueError("entry_price and stop_loss_price must be positive")
    if entry_price == stop_loss_price:
        raise ValueError("stop_loss_price cannot equal entry_price (zero risk-per-share)")

    risk_per_share = abs(entry_price - stop_loss_price)
    risk_budget_dollars = limits.account_equity * limits.target_risk_per_trade_pct
    risk_based_qty = risk_budget_dollars / risk_per_share

    max_position_dollars = limits.account_equity * limits.max_position_pct
    cap_based_qty = max_position_dollars / entry_price

    final_qty = min(risk_based_qty, cap_based_qty)
    binding_constraint = "risk_per_trade" if risk_based_qty <= cap_based_qty else "max_position_pct"

    return {
        "qty": int(final_qty),  # whole shares
        "position_dollars": int(final_qty) * entry_price,
        "position_pct_of_equity": (int(final_qty) * entry_price) / limits.account_equity,
        "max_loss_if_stopped": int(final_qty) * risk_per_share,
        "binding_constraint": binding_constraint,
    }
