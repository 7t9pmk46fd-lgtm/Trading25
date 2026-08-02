"""
Mean-reversion screener.

Strategy logic (classic z-score mean reversion):
  - For each ticker, compute a rolling mean and rolling std of closing price
    over `lookback` days.
  - z_score = (price - rolling_mean) / rolling_std
  - Go LONG when z_score <= entry_z (price is unusually far below its
    recent average -> bet it reverts upward).
  - EXIT the long when z_score >= exit_z (price has reverted back toward
    or above the mean).
  - (Optional) SHORT entries are supported symmetrically but disabled by
    default — flip `allow_short=True` to enable.

This is intentionally a simple, well-understood baseline strategy. It's a
starting point to validate the R&D -> execution pipeline end-to-end, not a
claim that it's profitable. Real viability has to come from backtesting on
real data, out-of-sample testing, and paper trading — not from the logic
looking reasonable on paper.
"""
from dataclasses import dataclass

import numpy as np
import pandas as pd


@dataclass
class MeanReversionParams:
    lookback: int = 20          # rolling window, in trading days
    entry_z: float = -1.5       # enter long when z-score drops to/below this.
                                 # Changed from -2.0 on 2026-07-28 after the first-ever
                                 # real backtest (rd-agent/scripts/backtest_mean_reversion.py,
                                 # 2y of real daily bars, 14-ticker watchlist): -1.5 beat
                                 # -2.0 on total return (+26.9% vs +14.96%), sharpe (0.82 vs
                                 # 0.57), and win rate (73.7% vs 69.9%), at the cost of a
                                 # slightly deeper max drawdown (-12.5% vs -10.7%). Only this
                                 # single change was tested in isolation and adopted -- not a
                                 # full grid search across combinations. Note that even this
                                 # tuned value still trailed SPY buy-and-hold (+36.0%) over
                                 # the same window; this is an improvement, not proof the
                                 # strategy beats the index.
    exit_z: float = 0.0         # exit long when z-score rises to/above this
    allow_short: bool = False   # mirror logic for short entries
    min_volume: float = 500_000  # skip illiquid names (avg daily $ volume proxy)


def compute_zscore(close: pd.Series, lookback: int) -> pd.Series:
    rolling_mean = close.rolling(window=lookback, min_periods=lookback).mean()
    rolling_std = close.rolling(window=lookback, min_periods=lookback).std(ddof=0)
    # avoid division by zero on flat/illiquid series
    z = (close - rolling_mean) / rolling_std.replace(0, np.nan)
    return z


def generate_signals(df: pd.DataFrame, params: MeanReversionParams = MeanReversionParams()) -> pd.DataFrame:
    """
    df: DataFrame with at least columns ['close', 'volume'], indexed by date,
        for a single ticker, sorted ascending by date.

    Returns a copy of df with added columns:
        zscore, position (1 = long, -1 = short, 0 = flat), signal
        (one of 'enter_long', 'exit_long', 'enter_short', 'exit_short', None)
    """
    out = df.copy()
    out["zscore"] = compute_zscore(out["close"], params.lookback)

    position = 0
    positions = []
    signals = []

    for z in out["zscore"]:
        signal = None
        if np.isnan(z):
            positions.append(0)
            signals.append(None)
            continue

        if position == 0:
            if z <= params.entry_z:
                position = 1
                signal = "enter_long"
            elif params.allow_short and z >= abs(params.entry_z):
                position = -1
                signal = "enter_short"
        elif position == 1:
            if z >= params.exit_z:
                position = 0
                signal = "exit_long"
        elif position == -1:
            if z <= -params.exit_z:
                position = 0
                signal = "exit_short"

        positions.append(position)
        signals.append(signal)

    out["position"] = positions
    out["signal"] = signals
    return out


def latest_signal(df_with_signals: pd.DataFrame) -> dict | None:
    """Pull the most recent actionable signal (if the last row has one).
    Uses pd.isna(), not `is None` -- pandas can silently convert None to
    NaN in this column, which `is None` alone would miss (see the same
    fix in day-trading-agent's get_last_signal)."""
    if df_with_signals.empty:
        return None
    last = df_with_signals.iloc[-1]
    if pd.isna(last["signal"]):
        return None
    return {
        "date": df_with_signals.index[-1],
        "signal": last["signal"],
        "zscore": last["zscore"],
        "close": last["close"],
    }
