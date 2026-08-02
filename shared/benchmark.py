"""
Portfolio-vs-S&P-500 benchmark comparison.

Added 2026-07-28 when the user set an explicit goal for all agents:
generate more income, or at minimum beat the S&P 500. This module answers
"are we actually beating it" -- it does not change any trading behavior by
itself.

Each Alpaca paper account gets one row in shared.db's `account_baselines`
table: the equity and real SPY closing price on the date that account's
tracked performance window begins. See scripts/seed_account_baselines.py
for how those starting points were picked -- they're an approximation
(calendar date, not exact account-creation timestamp), since Alpaca's
paper accounts don't expose their own creation time via the API.
"""
from dataclasses import dataclass
from datetime import datetime

from shared.db import get_account_baseline


@dataclass
class BenchmarkComparison:
    account: str
    start_date: str
    start_equity: float
    current_equity: float
    portfolio_return_pct: float
    benchmark_symbol: str
    benchmark_start_price: float
    benchmark_current_price: float
    benchmark_return_pct: float
    alpha_pct: float  # portfolio_return_pct - benchmark_return_pct; positive = beating it


def compute_benchmark_comparison(
    conn, account: str, current_equity: float, benchmark_current_price: float
) -> BenchmarkComparison | None:
    """Returns None if this account has no seeded baseline yet (run
    scripts/seed_account_baselines.py first).

    Deliberately takes current_equity and benchmark_current_price as
    arguments rather than fetching them itself -- callers already have (or
    are about to fetch) both via alpaca_client/alpaca_data, and which
    agent's data-fetching module is importable depends on that caller's own
    sys.path setup, which this shared module shouldn't have to know about.
    """
    baseline = get_account_baseline(conn, account)
    if baseline is None:
        return None

    portfolio_return_pct = (current_equity / baseline["start_equity"] - 1) * 100
    benchmark_return_pct = (benchmark_current_price / baseline["benchmark_start_price"] - 1) * 100

    return BenchmarkComparison(
        account=account,
        start_date=baseline["start_date"],
        start_equity=baseline["start_equity"],
        current_equity=current_equity,
        portfolio_return_pct=portfolio_return_pct,
        benchmark_symbol=baseline["benchmark_symbol"],
        benchmark_start_price=baseline["benchmark_start_price"],
        benchmark_current_price=benchmark_current_price,
        benchmark_return_pct=benchmark_return_pct,
        alpha_pct=portfolio_return_pct - benchmark_return_pct,
    )
