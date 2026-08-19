"""
Tests for shared/benchmark.py -- portfolio-vs-SPY comparison math.
"""
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from shared import db
from shared.benchmark import compute_benchmark_comparison


def test_compute_benchmark_comparison_returns_none_without_baseline(temp_db):
    result = compute_benchmark_comparison(temp_db, "default", current_equity=110_000, benchmark_current_price=460.0)
    assert result is None


def test_compute_benchmark_comparison_computes_returns_and_alpha(temp_db):
    db.set_account_baseline(temp_db, "default", "2026-08-01", 100_000.0, 450.0)

    result = compute_benchmark_comparison(temp_db, "default", current_equity=110_000.0, benchmark_current_price=459.0)

    assert result.portfolio_return_pct == pytest.approx(10.0)
    assert result.benchmark_return_pct == pytest.approx(2.0)
    assert result.alpha_pct == pytest.approx(8.0)
    assert result.account == "default"
    assert result.start_equity == 100_000.0
    assert result.benchmark_symbol == "SPY"


def test_compute_benchmark_comparison_negative_alpha_when_trailing(temp_db):
    db.set_account_baseline(temp_db, "default", "2026-08-01", 100_000.0, 450.0)

    result = compute_benchmark_comparison(temp_db, "default", current_equity=101_000.0, benchmark_current_price=468.0)

    # Portfolio +1%, SPY +4% -- alpha must read negative, not clamped to 0.
    assert result.portfolio_return_pct == pytest.approx(1.0)
    assert result.benchmark_return_pct == pytest.approx(4.0)
    assert result.alpha_pct == pytest.approx(-3.0)


def test_compute_benchmark_comparison_zero_return_when_flat(temp_db):
    db.set_account_baseline(temp_db, "default", "2026-08-01", 100_000.0, 450.0)

    result = compute_benchmark_comparison(temp_db, "default", current_equity=100_000.0, benchmark_current_price=450.0)

    assert result.portfolio_return_pct == 0.0
    assert result.benchmark_return_pct == 0.0
    assert result.alpha_pct == 0.0
