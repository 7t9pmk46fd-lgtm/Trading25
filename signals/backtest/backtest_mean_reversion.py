"""
Real backtest of the mean-reversion screener against real historical daily
bars, benchmarked against real SPY buy-and-hold over the same window.

Added 2026-07-28 as part of setting an explicit "beat the S&P 500" goal
for the trading-desk agents -- this had never been backtested against real
data before; the strategy went live purely because the logic "looked
reasonable" (mean_reversion.py's own docstring already flagged this gap).

Per-ticker simulation, kept deliberately simple:
  - One notional position per ticker at a time (no pyramiding), long only.
  - Buy at the close of the bar an 'enter_long' signal appears on, sell at
    the close of the bar an 'exit_long' appears on -- ignores slippage/
    commission, which real fills already showed are small relative to this
    strategy's ~5%+ typical z-score moves (see execution-agent's real
    fills), but is still an optimistic simplification worth remembering
    when reading the numbers.
  - Each ticker is treated as its own fully-invested-or-flat sleeve; the
    portfolio-level curve is an equal-weighted average of each ticker's
    daily returns (flat/0% on days it holds no position). This approximates
    a diversified multi-name deployment without building a full position-
    sizing/capital-allocation simulator, which isn't needed to answer the
    question this script exists to answer: does this strategy, as coded,
    beat SPY buy-and-hold over the same real window?

Usage:
    venv/Scripts/python signals/backtest/backtest_mean_reversion.py
"""
import json
import sys
from datetime import datetime, timedelta
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from shared.market_data import get_daily_bars
from signals.screeners.mean_reversion import MeanReversionParams, generate_signals
from shared.config import TRADE_UNIVERSE as WATCHLIST
from shared.db import db_session, init_db, insert_backtest_run

LOOKBACK_YEARS = 2
TRADE_UNIVERSE = list(WATCHLIST)  # shared.config.TRADE_UNIVERSE already excludes benchmarks

VARIANTS = {
    "current_default": MeanReversionParams(lookback=20, entry_z=-2.0, exit_z=0.0),
    "looser_entry_-1.5": MeanReversionParams(lookback=20, entry_z=-1.5, exit_z=0.0),
    "tighter_entry_-2.5": MeanReversionParams(lookback=20, entry_z=-2.5, exit_z=0.0),
    "earlier_exit_0.25": MeanReversionParams(lookback=20, entry_z=-2.0, exit_z=0.25),
    "shorter_lookback_10": MeanReversionParams(lookback=10, entry_z=-2.0, exit_z=0.0),
}


def simulate_ticker(df: pd.DataFrame, params: MeanReversionParams) -> tuple[pd.Series, list[float], int]:
    """Returns (daily_return_series, list_of_trade_returns, num_open_trades_at_end)."""
    out = generate_signals(df, params)
    daily_returns = pd.Series(0.0, index=out.index)
    trade_returns = []

    position_open = False
    entry_price = None
    prev_close = None

    for i, (date, row) in enumerate(out.iterrows()):
        if position_open and prev_close is not None:
            daily_returns.loc[date] = (row["close"] / prev_close) - 1

        if row["signal"] == "enter_long" and not position_open:
            position_open = True
            entry_price = row["close"]
        elif row["signal"] == "exit_long" and position_open:
            trade_returns.append((row["close"] / entry_price) - 1)
            position_open = False
            entry_price = None

        prev_close = row["close"]

    open_at_end = 1 if position_open else 0
    return daily_returns, trade_returns, open_at_end


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

    return {
        "total_return": total_return,
        "sharpe": sharpe,
        "max_drawdown": max_drawdown,
        "num_trades": num_trades,
        "win_rate": win_rate,
    }


def main():
    init_db()
    end = datetime.now()
    start = end - timedelta(days=int(365.25 * LOOKBACK_YEARS))

    print(f"Fetching {LOOKBACK_YEARS}y of daily bars for {len(TRADE_UNIVERSE)} tickers + SPY benchmark...")
    bars = get_daily_bars(TRADE_UNIVERSE + ["SPY"], start, end)
    missing = [t for t in TRADE_UNIVERSE if t not in bars]
    if missing:
        print(f"WARNING: no data returned for {missing}, excluded from this run")

    spy = bars["SPY"]
    spy_total_return = float(spy["close"].iloc[-1] / spy["close"].iloc[0] - 1)
    spy_daily_returns = spy["close"].pct_change().dropna()
    spy_sharpe = float((spy_daily_returns.mean() / spy_daily_returns.std()) * np.sqrt(252))
    print(f"\nSPY buy-and-hold over the same window: {spy_total_return:+.2%} (sharpe {spy_sharpe:.2f})\n")

    results = {}
    with db_session() as conn:
        insert_backtest_run(
            conn, strategy="spy_buy_and_hold", params_json="{}",
            start_date=str(start.date()), end_date=str(end.date()), universe="SPY",
            total_return=spy_total_return, sharpe=spy_sharpe, max_drawdown=None,
            num_trades=1, win_rate=None,
            notes="Benchmark row for mean-reversion variant comparison.",
        )

        for variant_name, params in VARIANTS.items():
            per_ticker_daily = {}
            all_trade_returns = []
            open_at_end_count = 0

            for ticker in TRADE_UNIVERSE:
                df = bars.get(ticker)
                if df is None or len(df) < params.lookback + 5:
                    continue
                daily_r, trade_r, open_at_end = simulate_ticker(df, params)
                per_ticker_daily[ticker] = daily_r
                all_trade_returns.extend(trade_r)
                open_at_end_count += open_at_end

            if not per_ticker_daily:
                continue

            portfolio_daily = pd.concat(per_ticker_daily, axis=1).mean(axis=1)
            metrics = compute_metrics(portfolio_daily, all_trade_returns)
            results[variant_name] = metrics

            insert_backtest_run(
                conn, strategy="mean_reversion", params_json=json.dumps(vars(params)),
                start_date=str(start.date()), end_date=str(end.date()),
                universe=",".join(sorted(per_ticker_daily.keys())),
                total_return=metrics["total_return"], sharpe=metrics["sharpe"],
                max_drawdown=metrics["max_drawdown"], num_trades=metrics["num_trades"],
                win_rate=metrics["win_rate"],
                notes=f"{open_at_end_count} ticker(s) still holding an open position at window end (excluded from trade_returns/win_rate).",
            )

            beat_spy = "BEATS SPY" if metrics["total_return"] > spy_total_return else "trails SPY"
            print(
                f"{variant_name:22s} total_return={metrics['total_return']:+.2%}  "
                f"sharpe={metrics['sharpe']:.2f}  max_dd={metrics['max_drawdown']:.2%}  "
                f"trades={metrics['num_trades']:3d}  win_rate={metrics['win_rate']:.1%}  [{beat_spy}]"
            )

    print(f"\nAll variants + SPY benchmark logged to backtest_runs table.")


if __name__ == "__main__":
    main()
