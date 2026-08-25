"""
Scale-in (add-to-an-existing-position) decision logic -- QUARANTINED.

Started 2026-08-25 at the user's explicit request: develop this as an
isolated, independently-testable module and prove it's *functional*
before any question of wiring it into the live desk comes up. Nothing in
this file is called by generate_signal.py, run_cycle.py,
scripts/run_trading_day.py, or any other live path, and nothing in it
writes to the `signals` table, calls alpaca_client, or touches the
broker. It is pure decision logic: given a position already held and a
fresh price/z-score, decide whether to add and what the resulting
position/stop would look like. A human has to explicitly wire this in
later -- it does not happen by this file existing.

Why this is worth quarantining, not just writing carefully: today the
desk NEVER adds to a position it already holds -- generate_signal.py
skips any ticker already in get_held_positions() unconditionally
(status "skipped_already_holding"). That's a deliberate simplicity
choice, not an oversight, and averaging into a losing position is a
well-known way to turn a small mistake into a large one if the sizing or
stop logic has a bug. This module existing and being unit-tested is NOT
evidence the underlying idea is a good one -- per signals/SKILL.md's
hard rule, a genuinely new hypothesis needs its own walk-forward
backtest before it's anything but a proposal, and adding a second
untested numeric knob purely fits noise exactly like re-tuning
entry_z/exit_z did (SKILL.md, rejected 2026-08-03). The walk-forward is
a separate, later step from what this file does.

Design, and why:
  - Trigger: only at a MATERIALLY deeper z-score than the original
    entry (entry_zscore - add_z_gap), not the same entry threshold
    firing again. Refiring the same threshold would just be leverage on
    the existing signal, not a distinct claim worth testing.
  - Frequency: capped at `max_adds` per position (default 1).
    Unbounded averaging-down into a falling name is the standard,
    well-documented failure mode of this class of strategy -- default
    conservative on purpose.
  - Sizing: reuses shared.risk.compute_position_size, the same
    stop-distance-aware sizing every live entry already uses. An add is
    a same-sized second bet, not a bigger one.
  - Stop: recomputed at the same ATR multiple trail_stops.py already
    uses, from the NEW blended average price -- but never loosened
    below whatever stop is already resting. This needs calling out
    explicitly: an add happens at a LOWER price than the original
    entry (that's what the trigger requires), so a naive "ATR from
    current price" recompute would produce a stop BELOW the existing
    one -- i.e. would loosen protection on a shrinking-confidence
    trade. trail_stops.py's own rule is "only ratchets up, never down";
    this mirrors that rule rather than fighting it.
"""
from dataclasses import dataclass

from shared.risk import RiskLimits, compute_position_size


@dataclass
class ScaleInParams:
    add_z_gap: float = 1.0
    # Trigger an add only when the current z-score has fallen at least
    # this much FURTHER below the position's original entry z-score --
    # e.g. entry_zscore=-1.5, add_z_gap=1.0 means the add threshold is
    # -2.5, not -1.5 again.
    max_adds: int = 1
    # Hard cap on adds per position lifetime.
    atr_stop_multiple: float = 2.0
    # Same distance execution/trail_stops.py already uses (its
    # ATR_STOP_MULTIPLE), so an add's stop story is consistent with the
    # rest of the desk rather than introducing a second convention.


@dataclass
class PositionState:
    symbol: str
    qty: float
    avg_entry_price: float
    entry_zscore: float
    # The z-score recorded at the ORIGINAL entry. Never updated by an
    # add -- the trigger is always measured against where the position
    # started, not against itself after a previous add, otherwise
    # max_adds=1 could still be circumvented by a threshold that keeps
    # moving with the position.
    adds_so_far: int = 0
    current_stop_price: float | None = None


def evaluate_add(
    position: PositionState,
    current_price: float,
    current_zscore: float,
    atr: float,
    risk_limits: RiskLimits,
    params: ScaleInParams = ScaleInParams(),
) -> dict:
    """
    Pure decision function -- no I/O, no side effects. Returns a dict
    with a 'status' key, mirroring the status-dict convention used
    throughout the rest of this codebase (generate_signal.py,
    trail_stops.py, run_execution_loop.py) rather than raising or
    returning None, so a caller can log every outcome uniformly.

    risk_limits: the account's real shared.risk.RiskLimits (it already
    carries account_equity), so the add sizes consistently with live
    entries -- no separate account_equity parameter, to avoid two
    sources of truth for the same number.
    """
    if position.adds_so_far >= params.max_adds:
        return {"status": "skipped_max_adds_reached", "adds_so_far": position.adds_so_far}

    add_threshold_z = position.entry_zscore - params.add_z_gap
    if current_zscore > add_threshold_z:
        return {
            "status": "no_add_zscore_not_deep_enough",
            "current_zscore": current_zscore,
            "add_threshold_z": add_threshold_z,
        }

    if atr != atr or atr <= 0:  # atr != atr is the NaN check (avoids importing numpy/pandas here)
        return {"status": "skipped_invalid_atr", "atr": atr}

    candidate_stop = current_price - params.atr_stop_multiple * atr
    if candidate_stop <= 0:
        return {"status": "skipped_invalid_stop", "candidate_stop": candidate_stop}

    sizing = compute_position_size(risk_limits, current_price, candidate_stop)
    add_qty = sizing["qty"]
    if add_qty <= 0:
        return {"status": "skipped_zero_qty", "sizing": sizing}

    new_total_qty = position.qty + add_qty
    new_avg_entry_price = (
        position.qty * position.avg_entry_price + add_qty * current_price
    ) / new_total_qty

    # Never loosen protection: an add fires at a price BELOW the
    # original entry, so a naive ATR-from-current-price stop would sit
    # below whatever's already resting. Keep the tighter of the two,
    # same "only ratchets up" rule trail_stops.py already enforces.
    if position.current_stop_price is not None:
        new_stop_price = max(candidate_stop, position.current_stop_price)
    else:
        new_stop_price = candidate_stop

    return {
        "status": "would_add",
        "add_qty": add_qty,
        "add_price": current_price,
        "new_total_qty": new_total_qty,
        "new_avg_entry_price": new_avg_entry_price,
        "new_stop_price": new_stop_price,
        "sizing": sizing,
        "reasoning": (
            f"zscore {current_zscore:.2f} <= threshold {add_threshold_z:.2f} "
            f"(entry {position.entry_zscore:.2f} - gap {params.add_z_gap}); "
            f"add #{position.adds_so_far + 1} of max {params.max_adds}"
        ),
    }
