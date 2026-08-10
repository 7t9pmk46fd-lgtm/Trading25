"""
Tests for signals/screeners/mean_reversion.py.

The NaN-vs-None tests below target a real bug class from the original
build: pandas can silently convert None to NaN in a mixed-type column,
and `x is None` alone misses that -- a "no signal" bar fell through to a
default sell branch without ever passing a risk check. The fix was
pd.isna() everywhere the signal column is read; these tests exist so a
future edit can't quietly reintroduce `is None`.
"""
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from signals.screeners.mean_reversion import (
    MeanReversionParams,
    compute_zscore,
    generate_signals,
    latest_signal,
)


def _flat_then_dip_then_recover(n_flat=25, dip=-5.0, n_dip=3, n_recover=5):
    """A synthetic price series: flat (to warm up the rolling window),
    then a clean dip, then a recovery back to the flat level -- exactly
    the shape a mean-reversion strategy is supposed to catch."""
    flat = [100.0] * n_flat
    dip = [100.0 + dip] * n_dip
    recover = [100.0] * n_recover
    closes = flat + dip + recover
    idx = pd.date_range("2026-01-01", periods=len(closes), freq="D")
    return pd.DataFrame({"close": closes, "volume": [1_000_000] * len(closes)}, index=idx)


def test_zscore_is_nan_before_lookback_warms_up():
    close = pd.Series([100.0] * 5)
    z = compute_zscore(close, lookback=20)
    assert z.isna().all()


def test_zscore_is_zero_for_a_perfectly_flat_series_once_warm():
    close = pd.Series([100.0] * 25)
    z = compute_zscore(close, lookback=20)
    # Flat series -> rolling std is 0 -> division guarded to NaN, not an error.
    assert z.iloc[-1] is not None
    assert pd.isna(z.iloc[-1]) or z.iloc[-1] == 0


def test_enter_long_fires_on_a_real_dip():
    df = _flat_then_dip_then_recover()
    params = MeanReversionParams(lookback=20, entry_z=-1.5, exit_z=0.0)
    out = generate_signals(df, params)
    assert "enter_long" in out["signal"].values


def test_exit_long_fires_after_recovery():
    df = _flat_then_dip_then_recover()
    params = MeanReversionParams(lookback=20, entry_z=-1.5, exit_z=0.0)
    out = generate_signals(df, params)
    assert "exit_long" in out["signal"].values
    # And it must come strictly after the entry.
    entry_idx = out.index[out["signal"] == "enter_long"][0]
    exit_idx = out.index[out["signal"] == "exit_long"][0]
    assert exit_idx > entry_idx


def test_no_signal_when_price_never_deviates():
    df = pd.DataFrame({
        "close": [100.0] * 30,
        "volume": [1_000_000] * 30,
    }, index=pd.date_range("2026-01-01", periods=30, freq="D"))
    out = generate_signals(df, MeanReversionParams())
    assert out["signal"].isin(["enter_long", "exit_long"]).sum() == 0


def test_short_disabled_by_default():
    # A symmetric spike (mirror of the dip) must NOT enter a short unless
    # allow_short is explicitly set -- long-only is a system-wide invariant,
    # not just an execution-layer guard.
    flat = [100.0] * 25
    spike = [105.0] * 3
    idx = pd.date_range("2026-01-01", periods=len(flat + spike), freq="D")
    df = pd.DataFrame({"close": flat + spike, "volume": [1_000_000] * len(flat + spike)}, index=idx)
    out = generate_signals(df, MeanReversionParams(allow_short=False))
    assert "enter_short" not in out["signal"].values


def test_latest_signal_returns_none_on_nan_not_crash():
    """The historical bug: a signal column can hold NaN (not None) for a
    'no signal' row, and `x is None` alone would miss it. latest_signal
    must use pd.isna() and return a clean None, not raise or misclassify."""
    df = pd.DataFrame({
        "close": [100.0, 101.0],
        "zscore": [np.nan, np.nan],
        "signal": [np.nan, np.nan],  # pandas' own representation of "no signal" in a mixed column
    }, index=pd.date_range("2026-01-01", periods=2, freq="D"))
    result = latest_signal(df)
    assert result is None


def test_latest_signal_returns_none_on_empty_dataframe():
    df = pd.DataFrame(columns=["close", "zscore", "signal"])
    assert latest_signal(df) is None


def test_trend_filter_none_is_byte_identical_to_no_filter():
    """trend_filter_days=None (the default, and what every live signal
    uses) must produce IDENTICAL output to before the field existed. This
    pins that contract so the 2026-08-10 trend-filter hypothesis can never
    silently change live behavior -- only an explicit, deliberate
    trend_filter_days=N opts into the new code path."""
    df = _flat_then_dip_then_recover()
    baseline = generate_signals(df, MeanReversionParams())
    explicit_none = generate_signals(df, MeanReversionParams(trend_filter_days=None))
    pd.testing.assert_frame_equal(baseline, explicit_none)


def test_trend_filter_blocks_entry_when_price_below_long_term_average():
    # Price sits well below its own 100-day average for the whole series
    # (a structural downtrend), then has a short-term dip-and-recover on
    # top of that -- the exact "falling knife" case the filter exists to
    # exclude. Long history of decline (dragging the SMA down with price,
    # but staying above the dip low) plus a dip near the end.
    n = 120
    decline = [200.0 - i * 0.8 for i in range(n)]       # steady downtrend
    dip = [decline[-1] - 5.0] * 3
    closes = decline + dip
    idx = pd.date_range("2026-01-01", periods=len(closes), freq="D")
    df = pd.DataFrame({"close": closes, "volume": [1_000_000] * len(closes)}, index=idx)

    unfiltered = generate_signals(df, MeanReversionParams(lookback=20, entry_z=-1.0, trend_filter_days=None))
    filtered = generate_signals(df, MeanReversionParams(lookback=20, entry_z=-1.0, trend_filter_days=100))

    assert "enter_long" in unfiltered["signal"].values   # confirms the dip alone WOULD trigger
    assert "enter_long" not in filtered["signal"].values  # but the trend filter blocks it


def test_trend_filter_allows_entry_within_an_uptrend():
    # Steady uptrend, then a short-term dip-and-recover -- the case the
    # filter is supposed to keep letting through.
    n = 120
    uptrend = [100.0 + i * 0.5 for i in range(n)]
    dip = [uptrend[-1] - 8.0] * 3
    recover = [uptrend[-1]] * 5
    closes = uptrend + dip + recover
    idx = pd.date_range("2026-01-01", periods=len(closes), freq="D")
    df = pd.DataFrame({"close": closes, "volume": [1_000_000] * len(closes)}, index=idx)

    filtered = generate_signals(df, MeanReversionParams(lookback=20, entry_z=-1.0, trend_filter_days=100))
    assert "enter_long" in filtered["signal"].values


def test_trend_filter_refuses_rather_than_guesses_before_warmup():
    # Fewer bars than trend_filter_days -- SMA is NaN throughout, and the
    # filter must fail closed (no entries), not silently pass everything.
    df = _flat_then_dip_then_recover(n_flat=25)  # 33 bars total
    filtered = generate_signals(df, MeanReversionParams(lookback=20, entry_z=-1.5, trend_filter_days=100))
    assert "enter_long" not in filtered["signal"].values


def test_latest_signal_returns_dict_when_signal_present():
    df = pd.DataFrame({
        "close": [100.0, 95.0],
        "zscore": [0.0, -2.0],
        "signal": [None, "enter_long"],
    }, index=pd.date_range("2026-01-01", periods=2, freq="D"))
    result = latest_signal(df)
    assert result is not None
    assert result["signal"] == "enter_long"
    assert result["close"] == 95.0
