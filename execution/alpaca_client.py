"""
Alpaca trading client wrapper.

This is the ONLY module in the whole system that submits orders to Alpaca.
Every other agent produces signals; this is where a signal actually
becomes a trade (paper, per your current config -- ALPACA_PAPER=true).

Written against alpaca-py's documented interface but not run against the
live API (no network in the build environment). Run scripts/smoke_test.py
first against your real paper account before trusting anything else here.
"""
import os
from dataclasses import dataclass

from shared.config import ALPACA_API_KEY, ALPACA_SECRET_KEY, ALPACA_PAPER, require_alpaca_credentials

# Env var prefix per non-default account -- "default" reads
# ALPACA_API_KEY/SECRET_KEY via shared.config; any other account reads
# {PREFIX}ALPACA_API_KEY / {PREFIX}ALPACA_SECRET_KEY. Empty today (single
# account). To give a future strategy its own isolated account, add an
# entry here and route it in shared.config.account_for_strategy.
_ACCOUNT_ENV_PREFIXES: dict[str, str] = {}


def _credentials_for(account: str) -> tuple[str, str]:
    if account == "default":
        require_alpaca_credentials()
        return ALPACA_API_KEY, ALPACA_SECRET_KEY

    prefix = _ACCOUNT_ENV_PREFIXES.get(account)
    if prefix is None:
        raise ValueError(f"Unknown account {account!r}")

    key = os.environ.get(f"{prefix}ALPACA_API_KEY", "")
    secret = os.environ.get(f"{prefix}ALPACA_SECRET_KEY", "")
    if not key or not secret:
        raise RuntimeError(
            f"Missing {prefix}ALPACA_API_KEY / {prefix}ALPACA_SECRET_KEY env "
            f"vars for account {account!r}."
        )
    return key, secret


def _get_trading_client(account: str = "default"):
    from alpaca.trading.client import TradingClient

    key, secret = _credentials_for(account)
    return TradingClient(key, secret, paper=ALPACA_PAPER)


@dataclass
class AccountSnapshot:
    equity: float
    last_equity: float          # previous trading day's closing equity
    today_pnl: float            # equity - last_equity, a simple daily P&L proxy
    buying_power: float
    is_pdt_flagged: bool        # Alpaca's OWN pattern-day-trader flag
    daytrade_count: int         # Alpaca's OWN day-trade counter (last 5 days)


def get_account_snapshot(account: str = "default") -> AccountSnapshot:
    """
    Pulls live account state from Alpaca. IMPORTANT: this includes
    Alpaca's own `pattern_day_trader` flag and `daytrade_count` -- these
    are the authoritative source of truth for PDT status, since Alpaca
    sees ALL activity on the account (including anything placed outside
    this system). Always prefer these over the local shared/risk.py
    counter when they disagree; the local counter is a supplementary
    check, not the primary one.
    """
    client = _get_trading_client(account)
    account = client.get_account()

    equity = float(account.equity)
    last_equity = float(account.last_equity)

    return AccountSnapshot(
        equity=equity,
        last_equity=last_equity,
        today_pnl=equity - last_equity,
        buying_power=float(account.buying_power),
        is_pdt_flagged=bool(account.pattern_day_trader),
        daytrade_count=int(account.daytrade_count or 0),
    )


def get_market_clock(account: str = "default") -> dict:
    """Alpaca's market clock: authoritative on trading days/hours,
    including holidays and early closes -- always prefer this over local
    weekday/time math for any "is the market open" decision."""
    client = _get_trading_client(account)
    clock = client.get_clock()
    return {
        "is_open": bool(clock.is_open),
        "timestamp": clock.timestamp,
        "next_open": clock.next_open,
        "next_close": clock.next_close,
    }


def get_portfolio_history(days: int = 30, account: str = "default") -> dict:
    """Daily equity/P&L history straight from Alpaca. Needed to write a
    report for a PAST session: the account snapshot only ever describes
    right now, so a backfilled daily review would otherwise carry today's
    equity under yesterday's date."""
    from alpaca.trading.requests import GetPortfolioHistoryRequest

    client = _get_trading_client(account)
    history = client.get_portfolio_history(
        GetPortfolioHistoryRequest(period=f"{days}D", timeframe="1D")
    )
    from datetime import datetime as _dt, timezone as _tz
    out = {}
    for i, ts in enumerate(history.timestamp or []):
        day = _dt.fromtimestamp(ts, tz=_tz.utc).date().isoformat()
        equity = (history.equity or [None] * (i + 1))[i]
        pnl = (history.profit_loss or [None] * (i + 1))[i]
        if equity is not None:
            out[day] = {"equity": float(equity), "pnl": float(pnl) if pnl is not None else None}
    return out


def get_open_positions(account: str = "default") -> list[dict]:
    client = _get_trading_client(account)
    positions = client.get_all_positions()
    return [
        {
            "symbol": p.symbol,
            "qty": float(p.qty),
            "avg_entry_price": float(p.avg_entry_price),
            "current_price": float(p.current_price),
            "unrealized_pl": float(p.unrealized_pl),
        }
        for p in positions
    ]


def submit_market_order(symbol: str, qty: float, side: str, account: str = "default") -> dict:
    """
    side: 'buy' or 'sell'. Submits a market order with day time-in-force.
    Returns a dict with the key fields needed to record the order locally.

    Does NOT do any risk checking itself -- callers (the execution loop)
    are responsible for calling shared/risk.py checks BEFORE calling this.
    This function's only job is placing the order Alpaca is told to place.
    """
    from alpaca.trading.enums import OrderSide, TimeInForce
    from alpaca.trading.requests import MarketOrderRequest

    client = _get_trading_client(account)
    order_side = OrderSide.BUY if side == "buy" else OrderSide.SELL

    request = MarketOrderRequest(
        symbol=symbol,
        qty=qty,
        side=order_side,
        time_in_force=TimeInForce.DAY,
    )
    order = client.submit_order(request)

    return {
        "alpaca_order_id": str(order.id),
        "symbol": order.symbol,
        "side": side,
        "qty": float(order.qty),
        "status": order.status.value if hasattr(order.status, "value") else str(order.status),
        "submitted_at": str(order.submitted_at),
    }


def submit_market_order_with_stop(symbol: str, qty: float, stop_price: float, account: str = "default") -> dict:
    """
    Buy entry + a real broker-side protective stop, submitted as one OTO
    (one-triggers-other) order: the stop-loss leg only arms once the
    market buy actually fills.

    IMPORTANT, confirmed against the real API (2026-07-27): the armed stop
    leg is NOT GTC despite earlier assumptions here -- Alpaca's OTO/bracket
    StopLossRequest has no time_in_force field at all, so the child leg
    silently inherits the PARENT market order's DAY time_in_force. That
    means this stop expires at market close the same day it's placed,
    every time, unless something later converts it. Confirmed via a real
    order that expired at 20:00:40 UTC (market close) leaving a position
    briefly unprotected. execution-agent/scripts/trail_stops.py is what
    actually makes this durable: every cycle it checks each open stop's
    time_in_force and force-converts DAY to GTC via replace_stop_order(),
    independent of whether the price has moved enough to otherwise raise
    it. That means a position bought and closed to market close BEFORE
    trail_stops.py gets a chance to run even once is still briefly exposed
    -- a small, bounded window (at most one poll interval), not the
    unbounded one this function used to (incorrectly) claim didn't exist.

    No take-profit leg on purpose: the mean-reversion exit signal is the
    intended profit-taking path, not a fixed target. Buy-side only --
    there is no matching helper for sells, since sells only ever close an
    existing (already-protected-or-not) position.
    """
    from alpaca.trading.enums import OrderSide, TimeInForce, OrderClass
    from alpaca.trading.requests import MarketOrderRequest, StopLossRequest

    client = _get_trading_client(account)
    request = MarketOrderRequest(
        symbol=symbol,
        qty=qty,
        side=OrderSide.BUY,
        time_in_force=TimeInForce.DAY,
        order_class=OrderClass.OTO,
        stop_loss=StopLossRequest(stop_price=round(stop_price, 2)),
    )
    order = client.submit_order(request)

    return {
        "alpaca_order_id": str(order.id),
        "symbol": order.symbol,
        "side": "buy",
        "qty": float(order.qty),
        "status": order.status.value if hasattr(order.status, "value") else str(order.status),
        "submitted_at": str(order.submitted_at),
        "stop_price": stop_price,
    }


def place_protective_stop(symbol: str, qty: float, stop_price: float, account: str = "default") -> dict:
    """
    Attaches a standalone stop-sell order to a position that's already
    open (i.e. wasn't bought via submit_market_order_with_stop). Used to
    retroactively protect a position, not part of the normal buy flow.
    """
    from alpaca.trading.enums import OrderSide, TimeInForce
    from alpaca.trading.requests import StopOrderRequest

    client = _get_trading_client(account)
    request = StopOrderRequest(
        symbol=symbol,
        qty=qty,
        side=OrderSide.SELL,
        time_in_force=TimeInForce.GTC,
        stop_price=round(stop_price, 2),
    )
    order = client.submit_order(request)

    return {
        "alpaca_order_id": str(order.id),
        "symbol": order.symbol,
        "status": order.status.value if hasattr(order.status, "value") else str(order.status),
        "stop_price": stop_price,
    }


def get_open_buy_order_symbols(account: str = "default") -> set[str]:
    """
    Returns symbols with at least one open (unfilled) BUY order. A day-TIF
    order submitted after market close, or one still filling, does not
    show up in get_open_positions() until it actually fills -- so a
    "don't re-buy something we already hold" check based on positions
    alone can be fooled into duplicating a buy while the first one is
    still pending. Callers guarding against duplicate entries should treat
    a symbol as already exposed if it's in either this set or held
    positions.
    """
    from alpaca.trading.enums import QueryOrderStatus, OrderSide
    from alpaca.trading.requests import GetOrdersRequest

    client = _get_trading_client(account)
    open_orders = client.get_orders(
        GetOrdersRequest(status=QueryOrderStatus.OPEN, side=OrderSide.BUY)
    )
    return {o.symbol for o in open_orders}


def cancel_open_orders_for_symbol(symbol: str, account: str = "default") -> list[str]:
    """
    Cancels any open orders (e.g. a standing protective stop) for a
    symbol. Called before submitting a strategy-driven sell, so an exit
    doesn't leave a now-meaningless stop order sitting against a position
    that's no longer open. Returns the list of cancelled order ids.
    """
    from alpaca.trading.enums import QueryOrderStatus
    from alpaca.trading.requests import GetOrdersRequest

    client = _get_trading_client(account)
    open_orders = client.get_orders(
        GetOrdersRequest(status=QueryOrderStatus.OPEN, symbols=[symbol])
    )
    cancelled = []
    for order in open_orders:
        client.cancel_order_by_id(order.id)
        cancelled.append(str(order.id))
    return cancelled


def get_open_stop_orders(account: str = "default") -> dict[str, dict]:
    """
    Returns {symbol: {alpaca_order_id, stop_price, qty, time_in_force}}
    for every open SELL stop order -- covers both a standalone
    place_protective_stop() order and an OTO buy's stop-loss leg once it's
    armed (Alpaca exposes the armed leg as its own open order with
    order_type='stop'). A symbol with no entry here has no broker-side
    protection right now.

    time_in_force is included so callers can detect a still-DAY order (an
    OTO stop leg that's never been converted) -- see
    submit_market_order_with_stop's docstring for why that matters.

    Alpaca's OPEN status filter includes transitional states like
    pending_replace, not just genuinely resting orders -- confirmed for
    real when a replace left the OLD order stuck in pending_replace
    indefinitely (never finalized to 'replaced') while a separate NEW
    order with the updated price was already live. Naively keying by
    symbol picks whichever happens to come last in the API response,
    which can silently select the stale pending order -- then every
    future replace attempt targets an order that's already mid-replace
    and fails. When a symbol has more than one open stop, this prefers a
    stable status (new/accepted) over a transitional one, and the more
    recently submitted order as a tiebreaker.
    """
    from alpaca.trading.enums import QueryOrderStatus, OrderSide
    from alpaca.trading.requests import GetOrdersRequest

    STABLE_STATUSES = {"new", "accepted"}

    client = _get_trading_client(account)
    open_orders = client.get_orders(
        GetOrdersRequest(status=QueryOrderStatus.OPEN, side=OrderSide.SELL)
    )
    result = {}
    for o in open_orders:
        order_type = o.order_type.value if hasattr(o.order_type, "value") else str(o.order_type)
        if "stop" not in order_type:
            continue
        tif = o.time_in_force.value if hasattr(o.time_in_force, "value") else str(o.time_in_force)
        status = o.status.value if hasattr(o.status, "value") else str(o.status)
        candidate = {
            "alpaca_order_id": str(o.id),
            "stop_price": float(o.stop_price),
            "qty": float(o.qty),
            "time_in_force": tif,
            "status": status,
            "submitted_at": o.submitted_at,
        }

        existing = result.get(o.symbol)
        if existing is None:
            result[o.symbol] = candidate
            continue

        existing_stable = existing["status"] in STABLE_STATUSES
        candidate_stable = status in STABLE_STATUSES
        if candidate_stable and not existing_stable:
            result[o.symbol] = candidate
        elif candidate_stable == existing_stable and candidate["submitted_at"] > existing["submitted_at"]:
            result[o.symbol] = candidate

    return result


def replace_stop_order(alpaca_order_id: str, new_stop_price: float, account: str = "default") -> dict:
    """
    Raises the stop price on an existing open stop order in place, instead
    of cancel-then-resubmit -- avoids the brief window where a
    cancel+place approach would leave the position with no standing
    protection at all if a fill happened to land in between.

    Always forces time_in_force=GTC, regardless of the order's current
    TIF -- this is what actually fixes a DAY-inherited OTO stop leg (see
    submit_market_order_with_stop's docstring). ReplaceOrderRequest is the
    only place in the API that can change an order's TIF after the fact;
    the original StopLossRequest bracket leg has no such field.
    """
    from alpaca.trading.enums import TimeInForce
    from alpaca.trading.requests import ReplaceOrderRequest

    client = _get_trading_client(account)
    request = ReplaceOrderRequest(stop_price=round(new_stop_price, 2), time_in_force=TimeInForce.GTC)
    order = client.replace_order_by_id(alpaca_order_id, request)

    return {
        "alpaca_order_id": str(order.id),
        "symbol": order.symbol,
        "status": order.status.value if hasattr(order.status, "value") else str(order.status),
        "stop_price": new_stop_price,
    }


def get_order_status(alpaca_order_id: str, account: str = "default") -> dict:
    client = _get_trading_client(account)
    order = client.get_order_by_id(alpaca_order_id)
    return {
        "alpaca_order_id": str(order.id),
        "status": order.status.value if hasattr(order.status, "value") else str(order.status),
        "filled_qty": float(order.filled_qty) if order.filled_qty else 0.0,
        "filled_avg_price": float(order.filled_avg_price) if order.filled_avg_price else None,
        "filled_at": str(order.filled_at) if order.filled_at else None,
    }
