"""
Does adding the short side to mean reversion actually pay?

The user's instruction was "allow short sells IF gains are reasonable" --
this script exists to answer the conditional before any short-selling
plumbing gets built into execution. Enabling shorts is not a config tweak:
a short's loss is unbounded, its protective stop sits ABOVE entry, and
today's oversell guard (which is what stops the system opening naked
shorts by accident) has to be relaxed to permit them deliberately. That is
only worth doing if the short side earns its risk.

The screener already supports shorts symmetrically and always has
(`MeanReversionParams.allow_short`, default False) -- entering short when
z >= |entry_z| and covering when z <= -exit_z. It has never been tested.

Method: identical parameters, one variable changed.
  A. long-only        (allow_short=False)  -- what runs in production
  B. long/short       (allow_short=True)
  C. B's short sleeve alone -- the direct answer to "do shorts make money"
  D. SPY buy-and-hold -- the benchmark

Returns are computed from the screener's own `position` column as
position.shift(1) * daily_return, which is the same no-lookahead
convention as signals/backtest/engine.py and handles -1 correctly (a short
earns the inverse of the underlying's move). Reported per calendar year as
well as in total, because shorts should earn their keep specifically in
down markets -- a strategy that only works in one regime shows up here.

NOT modeled, and all of it makes real shorting worse than these numbers:
borrow fees (cheap for the liquid large caps here, but never zero),
hard-to-borrow rejections, dividends owed while short, and the margin
interest on short proceeds.

Usage:
    venv/Scripts/python signals/backtest/short_side_test.py
    venv/Scripts/python signals/backtest/short_side_test.py --years 5
"""
import json
import sys
from datetime import datetime, timedelta
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from shared.config import TRADE_UNIVERSE
from shared.db import db_session, init_db, insert_backtest_run
from shared.market_data import get_daily_bars
from signals.screeners.mean_reversion import MeanReversionParams, generate_signals

LIVE = MeanReversionParams()          # production params, unchanged
WARMUP_DAYS = 90


def _sleeve_returns(out: pd.DataFrame, side: str | None = None) -> pd.Series:
    """Daily returns from the screener's position column. side='long'
    keeps only long exposure, 'short' only short, None keeps both."""
    ret = out["close"].pct_change().fillna(0.0)
    pos = out["position"].shift(1).fillna(0.0)
    if side == "long":
        pos = pos.clip(lower=0)
    elif side == "short":
        pos = pos.clip(upper=0)
    return (pos * ret).fillna(0.0)


def _portfolio(bars, tickers, params, side=None, start=None):
    """Equal-weighted daily returns across tickers, plus exposure stats."""
    sleeves, exposure = [], []
    for t in tickers:
        df = bars.get(t)
        if df is None or len(df) < params.lookback + 20:
            continue
        out = generate_signals(df, params)
        r = _sleeve_returns(out, side)
        pos = out["position"]
        if start is not None:
            r, pos = r[r.index >= start], pos[pos.index >= start]
        if r.empty:
            continue
        sleeves.append(r)
        exposure.append(pos)
    if not sleeves:
        return None, None
    return pd.concat(sleeves, axis=1).mean(axis=1), pd.concat(exposure, axis=1)


def _metrics(daily: pd.Series) -> dict:
    if daily is None or daily.empty:
        return {}
    eq = (1 + daily).cumprod()
    total = float(eq.iloc[-1] - 1)
    years = len(daily) / 252
    std = daily.std()
    return {
        "total_return": total,
        "annualized_return": float((1 + total) ** (1 / years) - 1) if years > 0 else None,
        "sharpe": float((daily.mean() / std) * np.sqrt(252)) if std and std > 0 else None,
        "max_drawdown": float(((eq - eq.cummax()) / eq.cummax()).min()),
        "days": len(daily),
    }


def main():
    args = sys.argv
    years = int(args[args.index("--years") + 1]) if "--years" in args else 5

    init_db()
    end = datetime.now()
    start = end - timedelta(days=int(365.25 * years))

    print(f"Fetching {years}y of daily bars for {len(TRADE_UNIVERSE)} tickers + SPY...")
    bars = get_daily_bars(TRADE_UNIVERSE + ["SPY"], start - timedelta(days=WARMUP_DAYS), end)
    have = [t for t in TRADE_UNIVERSE if t in bars and not bars[t].empty]
    missing = [t for t in TRADE_UNIVERSE if t not in have]
    if missing:
        print(f"WARNING: no data for {missing} -- excluded")
    print(f"Universe: {len(have)} tickers\n")

    spy = bars["SPY"]
    win_start = pd.Timestamp(start)

    long_only = MeanReversionParams(lookback=LIVE.lookback, entry_z=LIVE.entry_z,
                                    exit_z=LIVE.exit_z, allow_short=False)
    long_short = MeanReversionParams(lookback=LIVE.lookback, entry_z=LIVE.entry_z,
                                     exit_z=LIVE.exit_z, allow_short=True)

    lo, lo_pos = _portfolio(bars, have, long_only, None, win_start)
    ls, ls_pos = _portfolio(bars, have, long_short, None, win_start)
    sh, _ = _portfolio(bars, have, long_short, "short", win_start)
    lg, _ = _portfolio(bars, have, long_short, "long", win_start)

    spy_win = spy[spy.index >= win_start]
    spy_daily = spy_win["close"].pct_change().fillna(0.0)

    arms = [
        ("A. Long-only (production)", lo),
        ("B. Long/short", ls),
        ("C.   ...short sleeve only", sh),
        ("D.   ...long sleeve only", lg),
        ("E. SPY buy-and-hold", spy_daily),
    ]

    print(f"{'':<30}{'TOTAL':>10}{'ANNUALIZED':>13}{'SHARPE':>9}{'MAX DD':>10}")
    print("-" * 72)
    for name, series in arms:
        m = _metrics(series)
        if not m:
            print(f"{name:<30}{'no data':>10}")
            continue
        print(f"{name:<30}{m['total_return']:>+10.2%}{(m['annualized_return'] or 0):>+13.2%}"
              f"{(m['sharpe'] or 0):>9.2f}{m['max_drawdown']:>+10.2%}")

    # Per-year: shorts should earn their keep in down years specifically.
    print(f"\nBY CALENDAR YEAR")
    print(f"{'YEAR':<8}{'LONG-ONLY':>12}{'LONG/SHORT':>13}{'SHORT SLEEVE':>15}{'SPY':>10}")
    print("-" * 58)
    for year in sorted(set(lo.index.year)):
        def yr(s):
            sl = s[s.index.year == year]
            return float((1 + sl).prod() - 1) if len(sl) else 0.0
        print(f"{year:<8}{yr(lo):>+12.2%}{yr(ls):>+13.2%}{yr(sh):>+15.2%}{yr(spy_daily):>+10.2%}")

    # Exposure: how much of the time was anything actually short?
    short_days = int((ls_pos == -1).sum().sum())
    long_days = int((ls_pos == 1).sum().sum())
    total_slots = int(ls_pos.notna().sum().sum())
    print(f"\nEXPOSURE (ticker-days)")
    print(f"  long   {long_days:>7,} ({long_days / total_slots:.1%} of slots)")
    print(f"  short  {short_days:>7,} ({short_days / total_slots:.1%} of slots)")
    print(f"  flat   {total_slots - long_days - short_days:>7,} "
          f"({(total_slots - long_days - short_days) / total_slots:.1%})")

    short_pos_years = sum(1 for y in sorted(set(sh.index.year))
                          if float((1 + sh[sh.index.year == y]).prod() - 1) > 0)
    n_years = len(set(sh.index.year))
    m_sh, m_lo, m_ls = _metrics(sh), _metrics(lo), _metrics(ls)

    print(f"\nVERDICT INPUTS")
    print(f"  short sleeve total return : {m_sh['total_return']:+.2%}")
    print(f"  short sleeve profitable in: {short_pos_years}/{n_years} calendar years")
    print(f"  adding shorts changed total return by "
          f"{m_ls['total_return'] - m_lo['total_return']:+.2%} "
          f"and Sharpe by {(m_ls['sharpe'] or 0) - (m_lo['sharpe'] or 0):+.2f}")

    with db_session() as conn:
        for name, m, note in (
            ("mean_reversion_long_only", m_lo, "Long-only, live params, expanded universe."),
            ("mean_reversion_long_short", m_ls, "allow_short=True, otherwise identical to live params."),
            ("mean_reversion_short_sleeve", m_sh, "Short exposure in isolation. Excludes borrow fees, dividends owed, margin interest."),
        ):
            if not m:
                continue
            insert_backtest_run(
                conn, strategy=name,
                params_json=json.dumps({"lookback": LIVE.lookback, "entry_z": LIVE.entry_z,
                                        "exit_z": LIVE.exit_z, "years": years}),
                start_date=str(start.date()), end_date=str(end.date()),
                universe=",".join(sorted(have)),
                total_return=m["total_return"], sharpe=m["sharpe"],
                max_drawdown=m["max_drawdown"], num_trades=0, win_rate=None, notes=note,
            )
    print("\nLogged to backtest_runs.")


if __name__ == "__main__":
    main()
