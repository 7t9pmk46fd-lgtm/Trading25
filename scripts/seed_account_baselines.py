"""
One-time (idempotent) seed of shared.db's account_baselines table -- the
starting point each account's return is measured from for the S&P 500
benchmark comparison (shared/benchmark.py).

Start dates are approximations, not exact Alpaca account-creation
timestamps (the API doesn't expose those): the default account is pegged
to 2026-07-20, the day real trading actually began on it per the project's
own session history; the sneaky_pivot account to 2026-07-28, the day it
was created. Both accounts start from $100,000 paper equity, Alpaca's
standard paper-account default, which matches what was actually observed
on each account's first real balance check.

Re-run any time to correct the seeded values -- set_account_baseline
upserts rather than erroring on a second run.

Usage: venv/Scripts/python scripts/seed_account_baselines.py
"""
import sys
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from shared.market_data import get_daily_bars
from shared.db import db_session, init_db, set_account_baseline

BASELINES = {
    "default": {"start_date": "2026-07-20", "start_equity": 100_000.0},
    "sneaky_pivot": {"start_date": "2026-07-28", "start_equity": 100_000.0},
}


def spy_close_on(date_str: str) -> float:
    day = datetime.strptime(date_str, "%Y-%m-%d")
    bars = get_daily_bars(["SPY"], day - timedelta(days=7), day + timedelta(days=1))
    df = bars["SPY"]
    df = df[df.index.date <= day.date()]
    if df.empty:
        raise RuntimeError(f"No SPY bar on or before {date_str}")
    return float(df.iloc[-1]["close"])


def main():
    init_db()
    with db_session() as conn:
        for account, cfg in BASELINES.items():
            spy_price = spy_close_on(cfg["start_date"])
            set_account_baseline(
                conn,
                account=account,
                start_date=cfg["start_date"],
                start_equity=cfg["start_equity"],
                benchmark_start_price=spy_price,
            )
            print(
                f"{account}: baseline set to {cfg['start_date']}, "
                f"equity ${cfg['start_equity']:,.2f}, SPY close ${spy_price:.2f}"
            )


if __name__ == "__main__":
    main()
