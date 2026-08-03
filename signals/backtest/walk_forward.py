"""
Walk-forward validation of the mean-reversion strategy.

WHY THIS EXISTS: the live parameter set (entry_z=-1.5) was chosen on
2026-07-28 by running five variants over one fixed 2-year window and
adopting the winner -- then evaluated on that same window. That is
in-sample selection, and it cannot distinguish "this parameter is
better" from "this parameter happened to fit this stretch of history."
Every strategy has a best-looking parameter in hindsight.

Walk-forward answers the honest question instead:

    Pick parameters using ONLY data available at the time, then measure
    on data the selection never saw. Roll forward. Repeat.

Each fold: select the best variant over a TRAIN window (by total return,
mirroring how the live value was actually picked), then apply that
choice, untouched, to the immediately following TEST window. Only test
windows are scored, and no test window overlaps its own selection data.
Concatenating every test window gives one continuous out-of-sample
record.

Three things get compared over that identical out-of-sample period:
  1. walk-forward   -- re-tuned each fold, as above
  2. fixed live     -- today's production params, never re-tuned
  3. SPY            -- buy-and-hold, the benchmark this desk exists to beat

Also reported: how often the "best" parameters change between folds.
Parameters that jump around every window are noise being fitted, not a
signal being found -- that diagnostic matters as much as the returns.

Uses the production screener (signals.screeners.mean_reversion) and the
production per-ticker simulator, so this measures the code that actually
trades, not a re-implementation.

Usage:
    venv/Scripts/python signals/backtest/walk_forward.py
    venv/Scripts/python signals/backtest/walk_forward.py --years 5 --train 12 --test 3
"""
import json
import sys
from datetime import datetime, timedelta
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from shared.config import TRADE_UNIVERSE as WATCHLIST
from shared.db import db_session, init_db, insert_backtest_run
from shared.market_data import get_daily_bars
from signals.backtest.backtest_mean_reversion import simulate_ticker
from signals.screeners.mean_reversion import MeanReversionParams

TRADE_UNIVERSE = list(WATCHLIST)  # shared.config.TRADE_UNIVERSE already excludes benchmarks
WARMUP_DAYS = 90          # calendar days of history before a window, so the
                          # rolling z-score is warm on the window's first bar
LIVE_PARAMS = MeanReversionParams()   # whatever production is running today

# The selection grid. Deliberately includes the live value and the value it
# replaced, so the test can say something about that specific decision.
GRID = [
    MeanReversionParams(lookback=lb, entry_z=ez, exit_z=xz)
    for lb in (10, 20, 30)
    for ez in (-1.0, -1.5, -2.0, -2.5)
    for xz in (0.0, 0.25, 0.5)
]


def _label(p: MeanReversionParams) -> str:
    return f"lb{p.lookback}/entry{p.entry_z}/exit{p.exit_z}"


def _portfolio_returns(bars, tickers, params, win_start, win_end):
    """Equal-weighted daily returns across tickers for one window.

    Bars are sliced from WARMUP_DAYS before win_start so the z-score is
    warm on day one, then returns are trimmed to the window itself -- the
    warm-up contributes state, never score.
    """
    per_ticker = []
    trades = []
    for ticker in tickers:
        df = bars.get(ticker)
        if df is None:
            continue
        sl = df[(df.index >= win_start - timedelta(days=WARMUP_DAYS)) & (df.index <= win_end)]
        if len(sl) < params.lookback + 20:
            continue
        daily, trade_returns, _ = simulate_ticker(sl, params)
        daily = daily[daily.index >= win_start]
        if daily.empty:
            continue
        per_ticker.append(daily)
        trades.extend(trade_returns)
    if not per_ticker:
        return None, []
    return pd.concat(per_ticker, axis=1).mean(axis=1), trades


def _total_return(daily: pd.Series) -> float:
    return float((1 + daily).prod() - 1) if daily is not None and len(daily) else 0.0


def _metrics(daily: pd.Series, trades: list) -> dict:
    if daily is None or daily.empty:
        return {}
    equity = (1 + daily).cumprod()
    total = float(equity.iloc[-1] - 1)
    dd = float(((equity - equity.cummax()) / equity.cummax()).min())
    std = daily.std()
    sharpe = float((daily.mean() / std) * np.sqrt(252)) if std and std > 0 else None
    years = len(daily) / 252
    annualized = float((1 + total) ** (1 / years) - 1) if years > 0 else None
    wins = [t for t in trades if t > 0]
    return {
        "total_return": total,
        "annualized_return": annualized,
        "sharpe": sharpe,
        "max_drawdown": dd,
        "num_trades": len(trades),
        "win_rate": (len(wins) / len(trades)) if trades else None,
        "days": len(daily),
    }


def main():
    args = sys.argv
    years = int(args[args.index("--years") + 1]) if "--years" in args else 5
    train_months = int(args[args.index("--train") + 1]) if "--train" in args else 12
    test_months = int(args[args.index("--test") + 1]) if "--test" in args else 3

    init_db()
    end = datetime.now()
    start = end - timedelta(days=int(365.25 * years))

    print(f"Fetching {years}y of daily bars for {len(TRADE_UNIVERSE)} tickers + SPY...")
    bars = get_daily_bars(TRADE_UNIVERSE + ["SPY"], start - timedelta(days=WARMUP_DAYS), end)
    have = [t for t in TRADE_UNIVERSE if t in bars and not bars[t].empty]
    missing = [t for t in TRADE_UNIVERSE if t not in have]
    if missing:
        print(f"WARNING: no data for {missing} -- excluded")
    spy = bars["SPY"]
    first_bar = min(bars[t].index[0] for t in have)
    print(f"History available from {first_bar.date()} to {spy.index[-1].date()}\n")

    train_delta = timedelta(days=int(30.44 * train_months))
    test_delta = timedelta(days=int(30.44 * test_months))

    # First fold can only begin once there's warm-up + a full train window.
    fold_train_start = max(start, first_bar.to_pydatetime() + timedelta(days=WARMUP_DAYS))

    folds = []
    oos_chunks = []       # walk-forward (re-tuned) out-of-sample daily returns
    fixed_chunks = []     # live params over the same out-of-sample days
    oos_trades, fixed_trades = [], []

    while fold_train_start + train_delta + test_delta <= end:
        train_end = fold_train_start + train_delta
        test_end = min(train_end + test_delta, end)

        # --- selection: TRAIN window only ---
        best_params, best_score = None, -np.inf
        for params in GRID:
            daily, _ = _portfolio_returns(bars, have, params, fold_train_start, train_end)
            if daily is None or daily.empty:
                continue
            score = _total_return(daily)     # mirrors how the live value was picked
            if score > best_score:
                best_score, best_params = score, params
        if best_params is None:
            fold_train_start += test_delta
            continue

        # --- evaluation: TEST window, never seen by the selection ---
        oos_daily, oos_tr = _portfolio_returns(bars, have, best_params, train_end, test_end)
        fixed_daily, fixed_tr = _portfolio_returns(bars, have, LIVE_PARAMS, train_end, test_end)
        spy_window = spy[(spy.index >= train_end) & (spy.index <= test_end)]
        spy_ret = float(spy_window["close"].iloc[-1] / spy_window["close"].iloc[0] - 1) if len(spy_window) > 1 else 0.0

        if oos_daily is not None and not oos_daily.empty:
            oos_chunks.append(oos_daily)
            oos_trades.extend(oos_tr)
        if fixed_daily is not None and not fixed_daily.empty:
            fixed_chunks.append(fixed_daily)
            fixed_trades.extend(fixed_tr)

        folds.append({
            "train": f"{fold_train_start.date()}->{train_end.date()}",
            "test": f"{train_end.date()}->{test_end.date()}",
            "chosen": _label(best_params),
            "train_return": best_score,
            "oos_return": _total_return(oos_daily),
            "fixed_return": _total_return(fixed_daily),
            "spy_return": spy_ret,
        })
        fold_train_start += test_delta

    if not folds:
        print("Not enough history for even one fold.")
        return

    print(f"{'TEST WINDOW':<26}{'CHOSEN ON TRAIN':<24}{'TRAIN':>9}{'OOS':>9}{'FIXED':>9}{'SPY':>9}")
    print("-" * 86)
    for f in folds:
        print(f"{f['test']:<26}{f['chosen']:<24}"
              f"{f['train_return']:>+8.2%}{f['oos_return']:>+9.2%}"
              f"{f['fixed_return']:>+9.2%}{f['spy_return']:>+9.2%}")

    wf_daily = pd.concat(oos_chunks) if oos_chunks else None
    fx_daily = pd.concat(fixed_chunks) if fixed_chunks else None
    wf, fx = _metrics(wf_daily, oos_trades), _metrics(fx_daily, fixed_trades)

    oos_start = folds[0]["test"].split("->")[0]
    oos_end = folds[-1]["test"].split("->")[1]
    spy_oos = spy[(spy.index >= pd.Timestamp(oos_start)) & (spy.index <= pd.Timestamp(oos_end))]
    spy_daily = spy_oos["close"].pct_change().dropna()
    spy_total = float(spy_oos["close"].iloc[-1] / spy_oos["close"].iloc[0] - 1)
    spy_equity = (1 + spy_daily).cumprod()
    spy_metrics = {
        "total_return": spy_total,
        "annualized_return": float((1 + spy_total) ** (252 / len(spy_daily)) - 1),
        "sharpe": float((spy_daily.mean() / spy_daily.std()) * np.sqrt(252)),
        "max_drawdown": float(((spy_equity - spy_equity.cummax()) / spy_equity.cummax()).min()),
    }

    def row(name, m):
        return (f"{name:<26}{m.get('total_return', 0):>+9.2%}"
                f"{(m.get('annualized_return') or 0):>+12.2%}"
                f"{(m.get('sharpe') or 0):>9.2f}{m.get('max_drawdown', 0):>+10.2%}")

    print(f"\nOUT-OF-SAMPLE TOTALS  ({oos_start} -> {oos_end}, {len(folds)} folds)")
    print(f"{'':<26}{'TOTAL':>9}{'ANNUALIZED':>12}{'SHARPE':>9}{'MAX DD':>10}")
    print("-" * 66)
    print(row("Walk-forward (re-tuned)", wf))
    print(row("Fixed live params", fx))
    print(row("SPY buy-and-hold", spy_metrics))

    print(f"\nTrades: walk-forward {wf.get('num_trades')} (win rate "
          f"{(wf.get('win_rate') or 0):.1%}) | fixed {fx.get('num_trades')} "
          f"(win rate {(fx.get('win_rate') or 0):.1%})")

    chosen = [f["chosen"] for f in folds]
    unique = sorted(set(chosen))
    switches = sum(1 for a, b in zip(chosen, chosen[1:]) if a != b)
    print(f"\nPARAMETER STABILITY")
    print(f"  distinct winners across {len(folds)} folds: {len(unique)}")
    print(f"  changed between consecutive folds: {switches}/{len(folds) - 1}")
    live_label = _label(LIVE_PARAMS)
    print(f"  folds where training picked the live set ({live_label}): "
          f"{sum(1 for c in chosen if c == live_label)}")
    print(f"  winners: {', '.join(unique)}")

    beat = sum(1 for f in folds if f["oos_return"] > f["spy_return"])
    print(f"\n  folds where walk-forward beat SPY: {beat}/{len(folds)}")
    print(f"  folds where fixed live beat SPY:   "
          f"{sum(1 for f in folds if f['fixed_return'] > f['spy_return'])}/{len(folds)}")

    with db_session() as conn:
        for name, m, note in (
            ("mean_reversion_walkforward", wf, f"Walk-forward OOS, {train_months}m train / {test_months}m test, {len(folds)} folds, re-tuned each fold on total return."),
            ("mean_reversion_fixed_oos", fx, f"Live params ({live_label}) over the identical OOS window, never re-tuned."),
        ):
            if not m:
                continue
            insert_backtest_run(
                conn, strategy=name, params_json=json.dumps({"train_months": train_months, "test_months": test_months, "folds": len(folds)}),
                start_date=oos_start, end_date=oos_end, universe=",".join(sorted(have)),
                total_return=m["total_return"], sharpe=m["sharpe"], max_drawdown=m["max_drawdown"],
                num_trades=m["num_trades"], win_rate=m["win_rate"], notes=note,
            )
    print("\nLogged walk-forward and fixed-params runs to backtest_runs.")


if __name__ == "__main__":
    main()
