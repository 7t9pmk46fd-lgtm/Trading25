"""
Real backtest of the Sneaky Pivot screener, closing the "never backtested"
gap flagged in this strategy's own screener docstring and carried in
project memory since it was built (2026-07-27) -- it went live purely on
"this is one reasonable translation of the video's rules," with an
explicit warning to treat it skeptically until validated. Added 2026-07-28
as part of setting an explicit "beat the S&P 500" goal for the agents.

Reuses the ACTUAL production functions (compute_levels, evaluate_today)
from screeners/sneaky_pivot.py rather than a re-implementation or a
canned proxy strategy -- this is what makes the result trustworthy: it's
testing the exact code path that runs live, not an approximation of it.
For each (ticker, trading day), evaluate_today is called with an
incrementally expanding window of bars, one call per completed 15-min bar,
mirroring exactly how the live 15-minute cron polls it -- this matters
because evaluate_today's own logic deliberately only acts on the single
most-recently-completed bar (see its docstring: a real bug was caught and
fixed here on 2026-07-27 where a full-history rescan produced a stale,
already-invalid entry).

Usage:
    venv/Scripts/python day-trading-agent/scripts/backtest_sneaky_pivot.py
"""
import json
import sys
from datetime import datetime, timedelta, time
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "rd-agent"))
sys.path.insert(0, str(ROOT / "day-trading-agent"))

from data.alpaca_data import get_daily_bars, get_intraday_bars
from screeners.sneaky_pivot import SneakyPivotParams, compute_levels, evaluate_today
from shared.config import WATCHLIST
from shared.db import db_session, init_db, insert_backtest_run
from shared.risk import compute_atr

ET = ZoneInfo("America/New_York")
CALENDAR_DAYS = 90  # ~60 trading days
TRADE_UNIVERSE = [t for t in WATCHLIST if t not in ("SPY", "QQQ")]
PARAMS = SneakyPivotParams()  # current production defaults -- no variants tested here,
                              # the source strategy is too specific/discretionary to
                              # justify grid-searching invented parameter sets


def _daily_context(daily_bars: pd.DataFrame, as_of_date) -> tuple[dict | None, float | None]:
    """Levels + ATR computed using ONLY data strictly before as_of_date --
    no lookahead. Mirrors what the live system would actually have known
    at that day's market open."""
    prior = daily_bars[daily_bars.index.date < as_of_date]
    if len(prior) < 15:
        return None, None
    levels = compute_levels(prior, PARAMS)
    atr = compute_atr(prior, period=14)
    if levels is None or np.isnan(atr):
        return None, None
    return levels, atr


def simulate_ticker(ticker: str, daily_bars: pd.DataFrame, intraday_bars: pd.DataFrame) -> dict:
    idx = intraday_bars.index
    if idx.tz is None:
        idx = idx.tz_localize("UTC")
    intraday_bars = intraday_bars.copy()
    intraday_bars.index = idx.tz_convert(ET)
    intraday_bars = intraday_bars.sort_index()

    trading_days = sorted(set(intraday_bars.index.date))
    trade_returns = []
    daily_pnl_pct = {}  # date -> % return that day (0 if flat all day)

    for day in trading_days:
        todays = intraday_bars[intraday_bars.index.date == day]
        todays = todays[(todays.index.time >= time(9, 30)) & (todays.index.time < time(16, 0))]
        if len(todays) < 2:
            continue

        levels, atr = _daily_context(daily_bars, day)
        if levels is None:
            continue

        session_close = pd.Timestamp.combine(day, time(16, 0)).tz_localize(ET)

        has_position = False
        entry_price = None
        stop_price = None
        day_return = 0.0

        for i in range(1, len(todays) + 1):
            window = todays.iloc[:i]
            result = evaluate_today(window, levels, atr, session_close, has_position, PARAMS)
            signal = result.get("signal")

            if signal == "enter_long" and not has_position:
                has_position = True
                entry_price = result["entry_price"]
                stop_price = result["stop_price"]
            elif signal in ("exit_long", "force_flatten_eod") and has_position:
                exit_price = float(window.iloc[-1]["close"])
                # A real stop would have capped the loss at stop_price intraday;
                # this bar-close simulation can't see intrabar stop hits, so as a
                # conservative approximation, floor the exit at the stop price
                # whenever the bar's low breached it -- avoids overstating losses
                # that the real broker-side stop would have prevented.
                bar_low = float(window.iloc[-1]["low"])
                if stop_price is not None and bar_low <= stop_price:
                    exit_price = stop_price
                trade_return = (exit_price / entry_price) - 1
                trade_returns.append(trade_return)
                day_return = trade_return
                has_position = False
                entry_price = None
                stop_price = None

        if has_position:
            # still open at session end w/o an explicit flatten signal in the
            # window (shouldn't happen given force_flatten_eod, but guard anyway)
            exit_price = float(todays.iloc[-1]["close"])
            trade_return = (exit_price / entry_price) - 1
            trade_returns.append(trade_return)
            day_return = trade_return

        daily_pnl_pct[pd.Timestamp(day)] = day_return

    return {"ticker": ticker, "daily_returns": pd.Series(daily_pnl_pct), "trade_returns": trade_returns}


def compute_metrics(portfolio_daily_returns: pd.Series, all_trade_returns: list[float]) -> dict:
    equity_curve = (1 + portfolio_daily_returns).cumprod()
    total_return = float(equity_curve.iloc[-1] - 1) if len(equity_curve) else 0.0
    running_max = equity_curve.cummax()
    drawdown = (equity_curve - running_max) / running_max
    max_drawdown = float(drawdown.min()) if len(drawdown) else 0.0
    std = portfolio_daily_returns.std()
    sharpe = float((portfolio_daily_returns.mean() / std) * np.sqrt(252)) if std and std > 0 else None
    num_trades = len(all_trade_returns)
    win_rate = float(sum(1 for r in all_trade_returns if r > 0) / num_trades) if num_trades else None
    return {"total_return": total_return, "sharpe": sharpe, "max_drawdown": max_drawdown,
            "num_trades": num_trades, "win_rate": win_rate}


def main():
    init_db()
    end = datetime.now()
    start = end - timedelta(days=CALENDAR_DAYS)
    daily_start = start - timedelta(days=90)  # extra lookback so compute_levels/ATR have history from day 1

    print(f"Fetching ~{CALENDAR_DAYS}d of 15-min bars + daily context for {len(TRADE_UNIVERSE)} tickers + SPY benchmark...")
    daily_bars_all = get_daily_bars(TRADE_UNIVERSE + ["SPY"], daily_start, end)
    intraday_bars_all = get_intraday_bars(TRADE_UNIVERSE, start, end, minutes=15)

    spy = daily_bars_all["SPY"]
    spy_window = spy[spy.index.date >= start.date()]
    spy_total_return = float(spy_window["close"].iloc[-1] / spy_window["close"].iloc[0] - 1)
    spy_daily_returns = spy_window["close"].pct_change().dropna()
    spy_sharpe = float((spy_daily_returns.mean() / spy_daily_returns.std()) * np.sqrt(252))
    print(f"\nSPY buy-and-hold over the same window: {spy_total_return:+.2%} (sharpe {spy_sharpe:.2f})\n")

    per_ticker_daily = {}
    all_trade_returns = []
    missing = []
    for ticker in TRADE_UNIVERSE:
        daily_df = daily_bars_all.get(ticker)
        intraday_df = intraday_bars_all.get(ticker)
        if daily_df is None or intraday_df is None or intraday_df.empty:
            missing.append(ticker)
            continue
        r = simulate_ticker(ticker, daily_df, intraday_df)
        if not r["daily_returns"].empty:
            per_ticker_daily[ticker] = r["daily_returns"]
        all_trade_returns.extend(r["trade_returns"])
        print(f"  {ticker}: {len(r['trade_returns'])} trades")

    if missing:
        print(f"WARNING: no usable data for {missing}, excluded from this run")

    if not per_ticker_daily:
        print("No trades/data produced anything to score -- aborting without logging a run.")
        return

    portfolio_daily = pd.concat(per_ticker_daily, axis=1).fillna(0.0).mean(axis=1)
    metrics = compute_metrics(portfolio_daily, all_trade_returns)

    with db_session() as conn:
        insert_backtest_run(
            conn, strategy="sneaky_pivot", params_json=json.dumps(vars(PARAMS)),
            start_date=str(start.date()), end_date=str(end.date()),
            universe=",".join(sorted(per_ticker_daily.keys())),
            total_return=metrics["total_return"], sharpe=metrics["sharpe"],
            max_drawdown=metrics["max_drawdown"], num_trades=metrics["num_trades"],
            win_rate=metrics["win_rate"],
            notes=(
                "First-ever backtest of this strategy against real data (closes the "
                "never-backtested gap flagged since 2026-07-27). Simulated using the "
                "actual production compute_levels/evaluate_today functions, polled bar-"
                "by-bar to match live cron behavior. Exit prices floored at stop_price "
                "on any bar whose low breached it (approximates the real broker-side "
                "stop; can't see true intrabar fills from bar data alone)."
            ),
        )

    beat_spy = "BEATS SPY" if metrics["total_return"] > spy_total_return else "trails SPY"
    print(
        f"\nsneaky_pivot (current params)  total_return={metrics['total_return']:+.2%}  "
        f"sharpe={metrics['sharpe']}  max_dd={metrics['max_drawdown']:.2%}  "
        f"trades={metrics['num_trades']}  win_rate={metrics['win_rate']}  [{beat_spy}]"
    )
    print("Logged to backtest_runs table.")


if __name__ == "__main__":
    main()
