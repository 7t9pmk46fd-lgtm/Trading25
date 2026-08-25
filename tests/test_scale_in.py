"""
Tests for signals/research/scale_in.py -- the QUARANTINED scale-in decision
logic. These prove the pure decision function is functional in isolation,
per the user's explicit request (2026-08-25) to develop and verify it apart
from the live desk before any question of wiring it in. They say nothing
about whether adding to positions is a good idea -- that's a walk-forward
backtest question, not a unit test question, and is deliberately not
attempted here (see the module docstring).
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from shared.risk import RiskLimits
from signals.research.scale_in import PositionState, ScaleInParams, evaluate_add

LIMITS = RiskLimits(account_equity=100_000.0)


def _position(**overrides):
    base = dict(symbol="BA", qty=26.0, avg_entry_price=210.0, entry_zscore=-1.83, adds_so_far=0,
                current_stop_price=199.89)
    base.update(overrides)
    return PositionState(**base)


def test_no_add_when_zscore_not_deep_enough():
    # entry_zscore=-1.83, default add_z_gap=1.0 -> threshold is -2.83.
    # -2.0 hasn't crossed it, even though it would have qualified as a
    # fresh entry on its own.
    result = evaluate_add(_position(), current_price=205.0, current_zscore=-2.0, atr=5.0,
                           risk_limits=LIMITS)
    assert result["status"] == "no_add_zscore_not_deep_enough"


def test_add_triggers_past_threshold():
    result = evaluate_add(_position(), current_price=195.0, current_zscore=-2.9, atr=5.0,
                           risk_limits=LIMITS)
    assert result["status"] == "would_add"
    assert result["add_qty"] > 0
    # Blended average must land strictly between the two prices, closer
    # to whichever side has more shares.
    assert 195.0 < result["new_avg_entry_price"] < 210.0
    assert result["new_total_qty"] == 26.0 + result["add_qty"]


def test_max_adds_blocks_a_second_add():
    result = evaluate_add(_position(adds_so_far=1), current_price=180.0, current_zscore=-3.5,
                           atr=5.0, risk_limits=LIMITS, params=ScaleInParams(max_adds=1))
    assert result["status"] == "skipped_max_adds_reached"


def test_trigger_is_measured_against_original_entry_not_a_moving_target():
    # Even with adds_so_far=0 still allowed by max_adds, the threshold
    # must stay anchored to entry_zscore (-1.83), not silently reset to
    # wherever the position happens to be now.
    deep_position = _position(entry_zscore=-1.83)
    threshold_result = evaluate_add(deep_position, current_price=200.0, current_zscore=-2.82,
                                     atr=5.0, risk_limits=LIMITS)
    assert threshold_result["status"] == "no_add_zscore_not_deep_enough"
    just_past_result = evaluate_add(deep_position, current_price=200.0, current_zscore=-2.84,
                                     atr=5.0, risk_limits=LIMITS)
    assert just_past_result["status"] == "would_add"


def test_stop_never_loosens_below_existing_stop():
    # Add fires at a lower price than entry (195 vs 210 avg_entry), so a
    # naive ATR-from-current-price stop (195 - 2*5 = 185) would sit BELOW
    # the existing 199.89 stop -- i.e. would loosen protection. The
    # decision must keep the tighter (higher) of the two.
    position = _position(current_stop_price=199.89)
    result = evaluate_add(position, current_price=195.0, current_zscore=-2.9, atr=5.0,
                           risk_limits=LIMITS)
    assert result["status"] == "would_add"
    naive_candidate_stop = 195.0 - 2.0 * 5.0  # 185.0
    assert result["new_stop_price"] == 199.89
    assert result["new_stop_price"] > naive_candidate_stop


def test_stop_uses_candidate_when_no_existing_stop_on_record():
    position = _position(current_stop_price=None)
    result = evaluate_add(position, current_price=195.0, current_zscore=-2.9, atr=5.0,
                           risk_limits=LIMITS)
    assert result["status"] == "would_add"
    assert result["new_stop_price"] == 195.0 - 2.0 * 5.0


def test_invalid_atr_is_refused_not_guessed():
    result = evaluate_add(_position(), current_price=195.0, current_zscore=-2.9, atr=float("nan"),
                           risk_limits=LIMITS)
    assert result["status"] == "skipped_invalid_atr"


def test_zero_qty_from_sizing_is_reported_not_silently_dropped():
    # A tiny account can legitimately size an add down to 0 whole shares.
    tiny_limits = RiskLimits(account_equity=10.0)
    result = evaluate_add(_position(), current_price=195.0, current_zscore=-2.9, atr=5.0,
                           risk_limits=tiny_limits)
    assert result["status"] == "skipped_zero_qty"


def test_custom_add_z_gap_is_respected():
    wide_gap = ScaleInParams(add_z_gap=2.0)  # threshold becomes -3.83
    result = evaluate_add(_position(), current_price=195.0, current_zscore=-2.9, atr=5.0,
                           risk_limits=LIMITS, params=wide_gap)
    assert result["status"] == "no_add_zscore_not_deep_enough"
    assert result["add_threshold_z"] == -3.83
