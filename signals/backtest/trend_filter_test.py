"""
Does a longer-term trend filter improve the mean-reversion signal?

WHY THIS EXISTS: the walk-forward validation (walk_forward.py, 2026-08-03)
found the live strategy trails SPY specifically -- not that it loses
money, but that unconditional mean reversion has no way to tell a
healthy pullback (buy the dip, price was already trending up) from a
falling knife (price is trending down and the "dip" is just further
decline that hasn't stopped). That is a mechanism gap, not a threshold
gap -- re-tuning entry_z/exit_z again was already proven to just fit
noise (walk_forward.py: re-tuned +38.7% vs fixed +49.5%). This tests a
genuinely different idea: only take enter_long when price is at or above
its own 100-day moving average, i.e. buying the dip WITHIN a longer-term
uptrend rather than any statistically oversold bar regardless of context.

Method, deliberately NOT a repeat of the walk-forward's train/test
selection: the filter's one parameter (100-day SMA) is fixed up front by
convention (a standard trend-following lookback), not chosen by testing
several and keeping the best -- that selection process is exactly what
walk_forward.py showed produces noise, not signal. So there is nothing
to select here; this is a straight A/B comparison, same live entry_z/
exit_z/lookback in both arms, over the SAME real out-of-sample window
already used for the existing fixed-params-vs-SPY comparison
(2022-08-04 to 2026-07-30) so the numbers are directly comparable to
numbers already on record, not a new cherry-picked window.

Three things compared:
  A. Unfiltered (== today's live logic, trend_filter_days=None)
  B. Trend-filtered (trend_filter_days=100)
  C. SPY buy-and-hold over the identical window

Usage:
    venv/Scripts/python signals/backtest/trend_filter_test.py
    venv/Scripts/python signals/backtest/trend_filter_test.py --sma 50
"""
import json
import sys
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from shared.config import TRADE_UNIVERSE
from shared.db import db_session, init_db, insert_backtest_run
from shared.market_data import get_daily_bars
from signals.backtest.backtest_mean_reversion import compute_metrics, simulate_ticker
from signals.screeners.mean_reversion import MeanReversionParams

# Same window as the persisted mean_reversion_fixed_oos / spy_oos_benchmark
# rows from the 2026-08-03 walk-forward run -- numbers are meant to line
# up against those, not a freshly chosen period.
WINDOW_START = datetime(2022, 8, 4)
WINDOW_END = datetime(2026, 7, 30)
WARMUP_DAYS = 130  # covers a 100-day SMA plus the 20-day zscore lookback


def main():
    args = sys.argv
    sma_days = int(args[args.index("--sma") + 1]) if "--sma" in args else 100

    init_db()
    print(f"Fetching daily bars for {len(TRADE_UNIVERSE)} tickers + SPY, "
          f"{WINDOW_START.date()} to {WINDOW_END.date()} (+{WARMUP_DAYS}d warmup)...")
    bars = get_daily_bars(TRADE_UNIVERSE + ["SPY"], WINDOW_START - pd.Timedelta(days=WARMUP_DAYS), WINDOW_END)
    have = [t for t in TRADE_UNIVERSE if t in bars and not bars[t].empty]
    missing = [t for t in TRADE_UNIVERSE if t not in have]
    if missing:
        print(f"WARNING: no data for {missing} -- excluded")
    print(f"Universe: {len(have)} tickers\n")

    unfiltered_params = MeanReversionParams(trend_filter_days=None)
    filtered_params = MeanReversionParams(trend_filter_days=sma_days)

    def run(params):
        per_ticker_daily, all_trades = {}, []
        for t in have:
            df = bars[t]
            if len(df) < params.lookback + 20:
                continue
            daily_r, trade_r, _ = simulate_ticker(df, params)
            daily_r = daily_r[daily_r.index >= WINDOW_START]
            if daily_r.empty:
                continue
            per_ticker_daily[t] = daily_r
            all_trades.extend(trade_r)
        if not per_ticker_daily:
            return None, {}
        portfolio_daily = pd.concat(per_ticker_daily, axis=1).mean(axis=1)
        return portfolio_daily, compute_metrics(portfolio_daily, all_trades)

    unfiltered_daily, unfiltered_metrics = run(unfiltered_params)
    filtered_daily, filtered_metrics = run(filtered_params)

    spy = bars["SPY"]
    spy_window = spy[spy.index >= WINDOW_START]
    spy_total = float(spy_window["close"].iloc[-1] / spy_window["close"].iloc[0] - 1)
    spy_daily = spy_window["close"].pct_change().fillna(0.0)
    spy_sharpe = float((spy_daily.mean() / spy_daily.std()) * np.sqrt(252))

    def row(name, m):
        if not m:
            return f"{name:<32}{'no data':>10}"
        return (f"{name:<32}{m['total_return']:>+10.2%}{(m.get('sharpe') or 0):>9.2f}"
                f"{m['max_drawdown']:>+10.2%}{m['num_trades']:>9d}")

    print(f"{'':<32}{'TOTAL':>10}{'SHARPE':>9}{'MAX DD':>10}{'TRADES':>9}")
    print("-" * 70)
    print(row("A. Unfiltered (== live today)", unfiltered_metrics))
    print(row(f"B. Trend-filtered (SMA{sma_days})", filtered_metrics))
    print(f"{'C. SPY buy-and-hold':<32}{spy_total:>+10.2%}{spy_sharpe:>9.2f}")

    a_beats_spy = unfiltered_metrics.get("total_return", -999) > spy_total
    b_beats_spy = filtered_metrics.get("total_return", -999) > spy_total
    b_beats_a = filtered_metrics.get("total_return", -999) > unfiltered_metrics.get("total_return", -999)

    print(f"\nA beats SPY: {a_beats_spy}  |  B beats SPY: {b_beats_spy}  |  B beats A: {b_beats_a}")
    if b_beats_a and not b_beats_spy:
        print("VERDICT: filter helps relative to the unfiltered signal, but neither beats SPY.")
    elif b_beats_a and b_beats_spy:
        print("VERDICT: filter both improves on the unfiltered signal AND beats SPY over this window.")
    elif not b_beats_a:
        print("VERDICT: filter does NOT improve on the unfiltered signal. Do not adopt.")

    with db_session() as conn:
        for name, m, note in (
            ("mean_reversion_trend_filter_off", unfiltered_metrics,
             f"A/B baseline arm, trend_filter_days=None (== live). Window matches the "
             f"2026-08-03 walk-forward's fixed_oos figure -- should read close to it."),
            (f"mean_reversion_trend_filter_sma{sma_days}", filtered_metrics,
             f"A/B test arm, trend_filter_days={sma_days}. Same live entry_z/exit_z/lookback "
             f"as the unfiltered arm -- only the trend gate differs. Not re-tuned, not selected "
             f"from multiple SMA lengths -- {sma_days} was fixed before running."),
        ):
            if not m:
                continue
            insert_backtest_run(
                conn, strategy=name, params_json=json.dumps({"sma_days": sma_days if "sma" in name else None}),
                start_date=str(WINDOW_START.date()), end_date=str(WINDOW_END.date()),
                universe=",".join(sorted(have)), total_return=m["total_return"], sharpe=m["sharpe"],
                max_drawdown=m["max_drawdown"], num_trades=m["num_trades"], win_rate=m["win_rate"], notes=note,
            )
    print("\nLogged both arms to backtest_runs.")


if __name__ == "__main__":
    main()
