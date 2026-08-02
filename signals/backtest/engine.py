"""
Simple single-asset-at-a-time backtest engine.

Takes a DataFrame already annotated with `position` (from a screener like
mean_reversion.generate_signals) and simulates trading it with a fixed
starting capital, producing an equity curve and summary stats.

Assumptions (all simplifications worth knowing about):
  - Trades execute at the closing price of the signal day (no slippage,
    no bid/ask spread modeled). Real fills will be worse than this.
  - No transaction costs/commissions included by default (Alpaca is
    commission-free for stocks, but spread/slippage still apply in reality).
  - Whole position sizing: either fully in (100% of allocated capital) or
    flat. No partial sizing/scaling in-and-out.
  - Single ticker per run. Portfolio-level backtesting (many tickers,
    capital allocation across them) is a natural next step once this is
    validated, not built here yet.
"""
from dataclasses import dataclass

import numpy as np
import pandas as pd


@dataclass
class BacktestResult:
    equity_curve: pd.Series
    total_return: float
    annualized_return: float
    sharpe: float
    max_drawdown: float
    num_trades: int
    win_rate: float
    trades: pd.DataFrame


def run_backtest(
    df_with_signals: pd.DataFrame,
    starting_capital: float = 10_000.0,
    trading_days_per_year: int = 252,
) -> BacktestResult:
    """
    df_with_signals: output of a screener's generate_signals(), must have
    'close' and 'position' columns (position: 1 = long, 0 = flat, -1 = short).
    """
    df = df_with_signals.dropna(subset=["position"]).copy()

    # daily return of holding the position established at the *previous*
    # close (position.shift(1) avoids lookahead bias — you can't trade on
    # today's close using today's own signal).
    df["daily_return"] = df["close"].pct_change()
    df["strategy_return"] = df["position"].shift(1).fillna(0) * df["daily_return"]

    equity_curve = starting_capital * (1 + df["strategy_return"]).cumprod()
    equity_curve.iloc[0] = starting_capital  # anchor start

    total_return = equity_curve.iloc[-1] / starting_capital - 1

    n_days = len(df)
    years = n_days / trading_days_per_year
    annualized_return = (
        (1 + total_return) ** (1 / years) - 1 if years > 0 else np.nan
    )

    daily_std = df["strategy_return"].std(ddof=0)
    sharpe = (
        (df["strategy_return"].mean() / daily_std) * np.sqrt(trading_days_per_year)
        if daily_std and daily_std > 0
        else np.nan
    )

    running_max = equity_curve.cummax()
    drawdown = (equity_curve - running_max) / running_max
    max_drawdown = drawdown.min()

    trades = _extract_trades(df)
    num_trades = len(trades)
    win_rate = (trades["pnl_pct"] > 0).mean() if num_trades > 0 else np.nan

    return BacktestResult(
        equity_curve=equity_curve,
        total_return=total_return,
        annualized_return=annualized_return,
        sharpe=sharpe,
        max_drawdown=max_drawdown,
        num_trades=num_trades,
        win_rate=win_rate,
        trades=trades,
    )


def _extract_trades(df: pd.DataFrame) -> pd.DataFrame:
    """Pair up enter/exit signals into discrete round-trip trades with P&L."""
    trades = []
    entry_price = None
    entry_date = None
    direction = None

    for date, row in df.iterrows():
        sig = row.get("signal")
        if sig in ("enter_long", "enter_short"):
            entry_price = row["close"]
            entry_date = date
            direction = 1 if sig == "enter_long" else -1
        elif sig in ("exit_long", "exit_short") and entry_price is not None:
            exit_price = row["close"]
            pnl_pct = direction * (exit_price / entry_price - 1)
            trades.append(
                {
                    "entry_date": entry_date,
                    "exit_date": date,
                    "direction": "long" if direction == 1 else "short",
                    "entry_price": entry_price,
                    "exit_price": exit_price,
                    "pnl_pct": pnl_pct,
                }
            )
            entry_price = None
            entry_date = None
            direction = None

    return pd.DataFrame(trades)


def print_summary(result: BacktestResult, ticker: str = "") -> None:
    label = f" ({ticker})" if ticker else ""
    print(f"Backtest summary{label}")
    print(f"  Total return:       {result.total_return:.2%}")
    print(f"  Annualized return:  {result.annualized_return:.2%}")
    print(f"  Sharpe ratio:       {result.sharpe:.2f}")
    print(f"  Max drawdown:       {result.max_drawdown:.2%}")
    print(f"  Number of trades:   {result.num_trades}")
    print(f"  Win rate:           {result.win_rate:.2%}" if result.num_trades else "  Win rate:           n/a")
