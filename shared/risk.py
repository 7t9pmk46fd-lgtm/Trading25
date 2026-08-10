"""
Risk management shared by any agent that can generate or execute trades.

This module is deliberately NOT strategy-specific -- it's the layer that
sits between "a strategy wants to trade" and "an order actually gets
submitted," so risk rules are enforced consistently no matter which
strategy (mean-reversion, day-trading, whatever comes next) is asking.

Covers four things:
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
  4. Cash floor (added 2026-08-10) -- refuses a buy that would draw the
     account's cash negative. This system does not use margin. Sizing is
     still purely equity-%-based (position sizing and margin usage are
     separate concerns), so this is a backstop that catches the case
     where equity-based sizing and actual available cash have diverged
     (e.g. several concurrent buys, or cash tied up in unsettled trades).

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
    for the case where a strategy runs on its own Alpaca account: without
    scoping, one account's day-trade count would wrongly count against
    another account's local PDT check, even though each account has its
    own real PDT status with Alpaca. Unused while a single account runs.
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
    """
    Sum of realized P&L from round-trips CLOSED today, computed by FIFO
    matching every filled buy/sell in the local order ledger, per ticker.

    Implemented 2026-08-10, replacing a hardcoded `return 0.0` placeholder.
    NOT wired into check_circuit_breaker -- that already uses Alpaca's own
    account.today_pnl (equity-based, includes unrealized moves too, and is
    the authoritative live figure). This function exists for reporting
    (dashboard, daily review) where "what did we actually lock in today,
    separate from paper moves on positions still held" is the useful
    number and wasn't available anywhere before.

    Method: walk every filled order across ALL history (not just today) in
    fill order, maintaining a per-ticker FIFO queue of open buy lots. A
    sell consumes the oldest lots first; the realized P&L on each matched
    portion is (sell_price - buy_price) * matched_qty. Full history is
    required to build accurate queue state even though only today's sells
    are counted -- a sell today can close a lot bought last week.

    Uses COALESCE(filled_qty, qty): filled_qty (added 2026-08-10) is the
    broker-confirmed actual fill amount and is preferred whenever present.
    Rows reconciled before that column existed have filled_qty = NULL and
    fall back to qty (the ordered amount) -- exact for a full fill, an
    overstatement for a partial one. Confirmed for real on AMD's
    2026-08-04 sell: 13 ordered, only 11 actually filled.
    """
    rows = conn.execute(
        """SELECT ticker, side, COALESCE(filled_qty, qty) AS qty, fill_price, filled_at
           FROM orders
           WHERE filled_at IS NOT NULL AND fill_price IS NOT NULL
           ORDER BY filled_at ASC"""
    ).fetchall()

    today = datetime.now().strftime("%Y-%m-%d")
    open_lots: dict[str, list[list[float]]] = {}  # ticker -> [[qty, price], ...] oldest first
    realized_today = 0.0

    for row in rows:
        ticker, side, qty, price = row["ticker"], row["side"], row["qty"], row["fill_price"]
        lots = open_lots.setdefault(ticker, [])

        if side == "buy":
            lots.append([qty, price])
            continue

        # side == "sell": consume oldest lots first (FIFO).
        remaining = qty
        is_today = str(row["filled_at"]).startswith(today)
        while remaining > 1e-9 and lots:
            lot_qty, lot_price = lots[0]
            matched = min(remaining, lot_qty)
            if is_today:
                realized_today += matched * (price - lot_price)
            lot_qty -= matched
            remaining -= matched
            if lot_qty <= 1e-9:
                lots.pop(0)
            else:
                lots[0][0] = lot_qty
        # A sell with no matching lot (shouldn't happen -- the oversell
        # guard prevents it going forward) is silently ignored rather than
        # raising, since this function serves reporting, not a live gate.

    return realized_today


def check_cash_floor(cash: float, order_cost: float, cash_floor: float = 0.0) -> None:
    """
    Raises RiskViolation if a buy would push settled cash below `cash_floor`
    (default: $0 -- no margin debit at all).

    Added 2026-08-10. Before this, no risk check anywhere read the
    account's cash balance -- compute_position_size sizes purely off a %
    of equity, and Alpaca's buying_power (which the account had plenty of)
    already includes margin, so a buy would happily execute using
    borrowed money with nothing in this codebase aware it had happened.
    Confirmed on the live paper account: cash was -$4,467.25 while fully
    invested, with no code path that tracks margin interest, monitors
    maintenance-margin health, or has any repayment plan -- a real gap for
    a system that might someday run real money. This is the minimum fix:
    refuse the buy outright rather than silently draw on margin. It does
    not, by itself, make margin usage safe to enable -- see shared/risk.py
    module docstring and execution/SKILL.md for what's still missing
    (margin-call awareness, interest tracking) if that's ever revisited.

    order_cost: qty * entry_price for the buy being evaluated, i.e. the
    cash this specific order would consume if filled at that price.
    """
    if cash - order_cost < cash_floor:
        raise RiskViolation(
            f"Buy would use margin: cash is ${cash:,.2f}, this order costs "
            f"${order_cost:,.2f}, leaving ${cash - order_cost:,.2f} -- below "
            f"the ${cash_floor:,.2f} floor. This system does not use margin; "
            f"refusing rather than silently drawing on it."
        )


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
