"""
Sneaky Pivot screener, adapted from a YouTube day-trading tutorial (Doug,
"Sneaky Pivot" strategy built around the "Rumers Magic Lines" support/
resistance levels) at the user's request on 2026-07-27.

IMPORTANT CONTEXT FOR ANYONE (HUMAN OR AGENT) READING THIS LATER: the
source strategy is explicitly discretionary. The presenter's own words:
"technical analysis isn't literal," a drawn line is "just a range," and in
his two live examples he adjusts his own rules mid-trade (substituting a
tighter opening-candle range when the normal range/swing lines don't get
tested). What follows is ONE reasonable, but arbitrary, translation of
that judgment into fixed numeric rules -- not a faithful reproduction of
what an experienced discretionary trader would actually do looking at the
same chart. It has never been backtested. Treat any results from it with
real skepticism, same as any other untested strategy in this repo, but
more so given the source material itself warns against mechanical use.

Rules as coded here (15-minute bars, one trading day at a time):

  1. Levels: range_high/range_low = previous trading day's high/low.
     swing_high/swing_low = the closest prior day (scanning backward
     through `lookback_days` of daily history) whose high/low exceeds the
     range high/low. If no such day exists in the window, that swing
     level is unavailable and only the range line is used.

  2. Zone tolerance: a price is considered "at" a line if it comes within
     0.25x the 14-day ATR of it -- translating the video's repeated
     "it's not literal, it's a range" into a fixed band.

  3. Setup (long only -- see note below): the first 15-minute bar of the
     session ("opening range candle") must trade into the lower zone
     (range_low or swing_low, whichever it reaches). The second bar
     ("sneaky candle") must close green (close > open), confirming buyers
     stepped in.

  4. Entry trigger: the first subsequent bar (within
     `entry_search_bars` bars of the sneaky candle) whose price closes
     above the sneaky candle's high. If the setup's stop level (see below)
     is breached before that happens, the setup is invalidated for the
     day.

  5. Stop: just below the lower of the opening-range and sneaky candles'
     lows (the "tested" low), minus a small ATR-based buffer -- the
     video's "stop underneath the big buyer."

  6. Exit target: not a resting limit order (this system deliberately
     never submits take-profit legs -- see alpaca_client's docstring).
     Instead, `evaluate_today` returns "exit_long" once price reaches the
     range_high zone (the video's "trade it back to the top of the
     range") while a Sneaky-Pivot position is open.

  7. End-of-day flatten: any open Sneaky Pivot position is force-closed
     `force_flatten_minutes_before_close` minutes before market close,
     same hard rule as intraday_mean_reversion.py.

Long-only, on purpose: the video demonstrates selling at the upper lines
too (shorting), but alpaca_client.py has no short-entry helper -- it's
buy-side only by design ("sells only ever close an existing position").
Extending execution to support shorts is a separate, larger decision than
adding this screener; short setups are simply not generated here.
"""
from dataclasses import dataclass
from datetime import datetime, time
from zoneinfo import ZoneInfo

import pandas as pd

ET = ZoneInfo("America/New_York")


@dataclass
class SneakyPivotParams:
    lookback_days: int = 30            # how far back to scan for swing lines
    zone_tolerance_atr_mult: float = 0.25
    stop_buffer_atr_mult: float = 0.1
    entry_search_bars: int = 6         # ~1.5 hours after the sneaky candle
    no_new_entries_minutes_before_close: int = 30
    force_flatten_minutes_before_close: int = 5


def compute_levels(daily_bars: pd.DataFrame, params: SneakyPivotParams = SneakyPivotParams()) -> dict | None:
    """
    daily_bars: ascending-by-date daily OHLC bars, NOT including today
    (i.e. the most recent row is yesterday's completed session).
    Returns None if there isn't even a single completed prior day.
    """
    if daily_bars.empty:
        return None

    range_high = float(daily_bars["high"].iloc[-1])
    range_low = float(daily_bars["low"].iloc[-1])

    history = daily_bars.iloc[:-1].tail(params.lookback_days)

    highs_above = history[history["high"] > range_high]["high"]
    swing_high = float(highs_above.iloc[-1]) if not highs_above.empty else None

    lows_below = history[history["low"] < range_low]["low"]
    swing_low = float(lows_below.iloc[-1]) if not lows_below.empty else None

    return {
        "range_high": range_high,
        "range_low": range_low,
        "swing_high": swing_high,
        "swing_low": swing_low,
    }


def _session_bars_only(bars: pd.DataFrame) -> pd.DataFrame:
    """Filters to regular-session bars (9:30-16:00 ET) for the most recent
    date present, sorted ascending. Bars must have a tz-aware or naive
    (assumed UTC) DatetimeIndex."""
    idx = bars.index
    if idx.tz is None:
        idx = idx.tz_localize("UTC")
    bars = bars.copy()
    bars.index = idx.tz_convert(ET)
    bars = bars.sort_index()

    if bars.empty:
        return bars

    today = bars.index[-1].date()
    todays = bars[bars.index.date == today]
    return todays[
        (todays.index.time >= time(9, 30)) & (todays.index.time < time(16, 0))
    ]


def evaluate_today(
    intraday_bars_today: pd.DataFrame,
    levels: dict,
    atr: float,
    session_close: pd.Timestamp,
    has_open_position: bool,
    params: SneakyPivotParams = SneakyPivotParams(),
) -> dict:
    """
    intraday_bars_today: today's 15-min bars (any tz, will be normalized to
    ET and filtered to regular session).
    session_close: today's market close as a tz-aware pd.Timestamp.
    has_open_position: whether we already hold a Sneaky-Pivot-originated
    long in this ticker (exits are evaluated instead of entries).

    Returns a dict with at least a "signal" key: None, "enter_long",
    "exit_long", or "force_flatten_eod".
    """
    bars = _session_bars_only(intraday_bars_today)
    result = {"signal": None}

    if bars.empty:
        return result

    now = bars.index[-1]
    if session_close.tzinfo is None:
        session_close = session_close.tz_localize(ET)
    force_flatten_cutoff = session_close - pd.Timedelta(minutes=params.force_flatten_minutes_before_close)
    no_entry_cutoff = session_close - pd.Timedelta(minutes=params.no_new_entries_minutes_before_close)

    if has_open_position and now >= force_flatten_cutoff:
        result["signal"] = "force_flatten_eod"
        return result

    zone_tolerance = params.zone_tolerance_atr_mult * atr
    stop_buffer = params.stop_buffer_atr_mult * atr

    if has_open_position:
        current_price = float(bars.iloc[-1]["close"])
        target_zone = levels["range_high"] - zone_tolerance
        if current_price >= target_zone:
            result["signal"] = "exit_long"
        return result

    if len(bars) < 2 or now >= no_entry_cutoff:
        return result

    opening_bar = bars.iloc[0]
    sneaky_bar = bars.iloc[1]

    # "at the lower zone" means the opening bar's low dipped into one of
    # the two buy lines (range_low is always defined; swing_low may not
    # be, if no qualifying prior day was found).
    touched_range_low = opening_bar["low"] <= levels["range_low"] + zone_tolerance
    touched_swing_low = (
        levels["swing_low"] is not None
        and opening_bar["low"] <= levels["swing_low"] + zone_tolerance
    )
    if not (touched_range_low or touched_swing_low):
        return result

    sneaky_confirmed = sneaky_bar["close"] > sneaky_bar["open"]
    if not sneaky_confirmed:
        return result

    stop_price = min(opening_bar["low"], sneaky_bar["low"]) - stop_buffer

    # Only the CURRENT (most recently completed) bar can trigger an entry --
    # this function is meant to be polled repeatedly through the day, and
    # must never resurrect a breakout from several bars ago just because
    # this happens to be the first time it's been called since then. That
    # would mean buying at today's current price against a setup whose
    # thesis may have already reversed, with no guarantee the computed
    # stop is even still above the market.
    window_start = 2
    window_end = 2 + params.entry_search_bars
    latest_bar_pos = len(bars) - 1

    if latest_bar_pos < window_start or latest_bar_pos >= window_end:
        # Either no entry-eligible bar has formed yet, or the window during
        # which this setup was valid has already fully elapsed.
        return result

    prior_window_bars = bars.iloc[window_start:latest_bar_pos]
    if (prior_window_bars["low"] <= stop_price).any():
        return result  # setup already invalidated earlier in the window

    latest_bar = bars.iloc[-1]
    if latest_bar["low"] <= stop_price:
        return result  # invalidated on this very bar

    if latest_bar["close"] > sneaky_bar["high"]:
        result["signal"] = "enter_long"
        result["entry_price"] = float(latest_bar["close"])
        result["stop_price"] = float(stop_price)
        result["reasoning"] = (
            f"opening bar low {opening_bar['low']:.2f} tagged buy zone "
            f"(range_low={levels['range_low']:.2f}, swing_low={levels['swing_low']}), "
            f"sneaky bar confirmed green, entry on breakout above "
            f"{sneaky_bar['high']:.2f}"
        )

    return result
