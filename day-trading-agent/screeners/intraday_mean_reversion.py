"""
Intraday mean-reversion screener.

Same z-score logic as rd-agent's mean_reversion.py, adapted for intraday
bars (e.g. 1-min or 5-min) with two day-trading-specific additions:

  1. Mandatory end-of-day flatten: any open position MUST be closed before
     market close, regardless of z-score. Day trading means no overnight
     exposure by definition here -- holding overnight silently turns a
     "day trade" into a swing trade with none of the day-trading risk
     controls (and defeats the point of the PDT-aware sizing).
  2. A no-new-entries cutoff some number of minutes before close, so a
     freshly opened position isn't immediately forced to flatten before
     the trade thesis had any chance to play out.

This reuses rd-agent's z-score math rather than duplicating it -- see
rd-agent/screeners/mean_reversion.py for the underlying logic and its
validation against synthetic data.
"""
import sys
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent / "rd-agent"))
from screeners.mean_reversion import compute_zscore  # noqa: E402


@dataclass
class IntradayParams:
    lookback_bars: int = 20        # rolling window, in bars (not days)
    entry_z: float = -2.0
    exit_z: float = 0.0
    no_new_entries_minutes_before_close: int = 30
    force_flatten_minutes_before_close: int = 5


def generate_intraday_signals(
    df: pd.DataFrame,
    session_close: pd.Timestamp,
    params: IntradayParams = IntradayParams(),
) -> pd.DataFrame:
    """
    df: intraday bars for a single ticker, indexed by timestamp (same day),
        with a 'close' column, sorted ascending.
    session_close: timestamp of today's market close (e.g. 16:00 ET).
    """
    out = df.copy()
    out["zscore"] = compute_zscore(out["close"], params.lookback_bars)

    no_entry_cutoff = session_close - pd.Timedelta(minutes=params.no_new_entries_minutes_before_close)
    force_flatten_cutoff = session_close - pd.Timedelta(minutes=params.force_flatten_minutes_before_close)

    position = 0
    positions = []
    signals = []

    for ts, z in out["zscore"].items():
        signal = None

        # Hard rule: force flatten near close, overriding everything else.
        if ts >= force_flatten_cutoff and position != 0:
            position = 0
            signal = "force_flatten_eod"
            positions.append(position)
            signals.append(signal)
            continue

        if np.isnan(z):
            positions.append(position)
            signals.append(None)
            continue

        if position == 0:
            # Block new entries once inside the no-new-entries window.
            if ts < no_entry_cutoff and z <= params.entry_z:
                position = 1
                signal = "enter_long"
        elif position == 1:
            if z >= params.exit_z:
                position = 0
                signal = "exit_long"

        positions.append(position)
        signals.append(signal)

    out["position"] = positions
    out["signal"] = signals
    return out


def get_last_signal(df_with_signals: pd.DataFrame):
    """Safely get the last signal, treating both None and NaN as 'no signal'.
    pandas can silently convert None to NaN when a column mixes None with
    strings under its string dtype inference -- `x is None` alone misses
    that case, so always use pd.isna() when checking this column."""
    if df_with_signals.empty:
        return None
    last_signal = df_with_signals.iloc[-1]["signal"]
    if pd.isna(last_signal):
        return None
    return last_signal
