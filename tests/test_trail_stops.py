"""
Tests for execution/trail_stops.py's orphaned-stop cleanup.

Confirmed live 2026-08-14: a full exit on CCI left an 18-share GTC stop
resting on the broker after the position hit 0 (race between the exit's
pre-sell cancel + un-awaited market sell, and a trail_stops cycle landing
mid-fill and re-protecting the still-open remainder). A stop resting with
no position behind it is a real long-only risk -- if ever triggered, a
stop-SELL against a shortable symbol with zero shares held would open a
short. These pin the sweep that cancels any stop whose symbol isn't in the
live position list, every cycle, including when every position has closed.
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from execution import alpaca_client, trail_stops


def test_orphaned_stop_is_cancelled(monkeypatch):
    monkeypatch.setattr(alpaca_client, "get_open_positions", lambda account="default": [
        {"symbol": "NVDA", "qty": 10.0, "current_price": 200.0, "avg_entry_price": 190.0, "unrealized_pl": 100.0},
    ])
    monkeypatch.setattr(alpaca_client, "get_open_stop_orders", lambda account="default": {
        "NVDA": {"alpaca_order_id": "keep-1", "stop_price": 180.0, "qty": 10.0, "time_in_force": "gtc"},
        "CCI": {"alpaca_order_id": "orphan-1", "stop_price": 70.0, "qty": 18.0, "time_in_force": "gtc"},
    })
    cancelled = []
    monkeypatch.setattr(alpaca_client, "cancel_open_orders_for_symbol",
                         lambda symbol, account="default": cancelled.append(symbol) or ["orphan-1"])
    monkeypatch.setattr(trail_stops, "get_daily_bars_cached", lambda symbols, start, end: {})

    results = trail_stops.check_and_trail(dry_run=False, account="default")

    assert cancelled == ["CCI"]
    orphan_result = next(r for r in results if r.get("ticker") == "CCI")
    assert orphan_result["status"] == "cancelled_orphaned_stop"
    assert orphan_result["cancelled_order_ids"] == ["orphan-1"]
    # NVDA's live stop must never be touched by the sweep.
    assert "NVDA" not in cancelled


def test_orphaned_stop_is_cancelled_even_with_no_open_positions(monkeypatch):
    monkeypatch.setattr(alpaca_client, "get_open_positions", lambda account="default": [])
    monkeypatch.setattr(alpaca_client, "get_open_stop_orders", lambda account="default": {
        "CCI": {"alpaca_order_id": "orphan-1", "stop_price": 70.0, "qty": 18.0, "time_in_force": "gtc"},
    })
    cancelled = []
    monkeypatch.setattr(alpaca_client, "cancel_open_orders_for_symbol",
                         lambda symbol, account="default": cancelled.append(symbol) or ["orphan-1"])

    results = trail_stops.check_and_trail(dry_run=False, account="default")

    assert cancelled == ["CCI"]
    assert any(r.get("status") == "cancelled_orphaned_stop" and r.get("ticker") == "CCI" for r in results)
    assert any(r.get("status") == "no_positions" for r in results)


def test_no_orphans_means_nothing_cancelled(monkeypatch):
    monkeypatch.setattr(alpaca_client, "get_open_positions", lambda account="default": [
        {"symbol": "NVDA", "qty": 10.0, "current_price": 200.0, "avg_entry_price": 190.0, "unrealized_pl": 100.0},
    ])
    monkeypatch.setattr(alpaca_client, "get_open_stop_orders", lambda account="default": {
        "NVDA": {"alpaca_order_id": "keep-1", "stop_price": 180.0, "qty": 10.0, "time_in_force": "gtc"},
    })
    cancelled = []
    monkeypatch.setattr(alpaca_client, "cancel_open_orders_for_symbol",
                         lambda symbol, account="default": cancelled.append(symbol) or [])
    monkeypatch.setattr(trail_stops, "get_daily_bars_cached", lambda symbols, start, end: {})

    trail_stops.check_and_trail(dry_run=False, account="default")

    assert cancelled == []


# --- Stop qty drift (BA, 2026-08-24): _wait_for_fill in run_execution_loop.py
# can return on a partial fill, sizing the initial protective stop below the
# eventual full position. These pin trail_stops noticing and correcting that
# drift, independent of whether the stop price also needs to move.

def _flat_bars(n=20):
    # compute_atr is monkeypatched directly in these tests, so the bars
    # content is irrelevant -- only len(bars) >= 15 needs to hold to clear
    # the "insufficient bars" guard.
    return list(range(n))


def test_undersized_stop_qty_is_topped_up(monkeypatch):
    monkeypatch.setattr(alpaca_client, "get_open_positions", lambda account="default": [
        {"symbol": "BA", "qty": 26.0, "current_price": 210.0, "avg_entry_price": 210.17, "unrealized_pl": -4.4},
    ])
    monkeypatch.setattr(alpaca_client, "get_open_stop_orders", lambda account="default": {
        "BA": {"alpaca_order_id": "stop-1", "stop_price": 199.89, "qty": 23.0, "time_in_force": "gtc", "status": "new"},
    })
    monkeypatch.setattr(alpaca_client, "cancel_open_orders_for_symbol",
                         lambda symbol, account="default": [])
    monkeypatch.setattr(trail_stops, "get_daily_bars_cached", lambda symbols, start, end: {"BA": _flat_bars()})
    # ATR chosen so candidate_stop (210 - 2*atr) stays below the existing
    # 199.89 stop -- isolates the qty fix from any price raise.
    monkeypatch.setattr(trail_stops, "compute_atr", lambda bars: 5.5)

    replace_calls = []

    def _fake_replace(alpaca_order_id, new_stop_price, qty=None, account="default"):
        replace_calls.append({"id": alpaca_order_id, "stop_price": new_stop_price, "qty": qty})
        return {"alpaca_order_id": alpaca_order_id, "symbol": "BA", "status": "replaced",
                "stop_price": new_stop_price, "qty": qty}

    monkeypatch.setattr(alpaca_client, "replace_stop_order", _fake_replace)

    results = trail_stops.check_and_trail(dry_run=False, account="default")

    assert len(replace_calls) == 1
    assert replace_calls[0]["qty"] == 26.0
    assert replace_calls[0]["stop_price"] == 199.89  # price unchanged, qty-only fix

    ba_result = next(r for r in results if r.get("ticker") == "BA")
    assert ba_result["status"] == "fixed_qty"
    assert ba_result["new_qty"] == 26.0


def test_matching_stop_qty_is_left_unchanged(monkeypatch):
    monkeypatch.setattr(alpaca_client, "get_open_positions", lambda account="default": [
        {"symbol": "BA", "qty": 26.0, "current_price": 210.0, "avg_entry_price": 210.17, "unrealized_pl": -4.4},
    ])
    monkeypatch.setattr(alpaca_client, "get_open_stop_orders", lambda account="default": {
        "BA": {"alpaca_order_id": "stop-1", "stop_price": 199.89, "qty": 26.0, "time_in_force": "gtc", "status": "new"},
    })
    monkeypatch.setattr(alpaca_client, "cancel_open_orders_for_symbol",
                         lambda symbol, account="default": [])
    monkeypatch.setattr(trail_stops, "get_daily_bars_cached", lambda symbols, start, end: {"BA": _flat_bars()})
    monkeypatch.setattr(trail_stops, "compute_atr", lambda bars: 5.5)

    replace_calls = []
    monkeypatch.setattr(alpaca_client, "replace_stop_order",
                         lambda *a, **kw: replace_calls.append((a, kw)))

    results = trail_stops.check_and_trail(dry_run=False, account="default")

    assert replace_calls == []
    ba_result = next(r for r in results if r.get("ticker") == "BA")
    assert ba_result["status"] == "unchanged"


def test_undersized_stop_qty_fixed_together_with_a_price_raise(monkeypatch):
    monkeypatch.setattr(alpaca_client, "get_open_positions", lambda account="default": [
        {"symbol": "BA", "qty": 26.0, "current_price": 230.0, "avg_entry_price": 210.17, "unrealized_pl": 515.6},
    ])
    monkeypatch.setattr(alpaca_client, "get_open_stop_orders", lambda account="default": {
        "BA": {"alpaca_order_id": "stop-1", "stop_price": 199.89, "qty": 23.0, "time_in_force": "gtc", "status": "new"},
    })
    monkeypatch.setattr(alpaca_client, "cancel_open_orders_for_symbol",
                         lambda symbol, account="default": [])
    monkeypatch.setattr(trail_stops, "get_daily_bars_cached", lambda symbols, start, end: {"BA": _flat_bars()})
    # candidate_stop = 230 - 2*5.5 = 219.0, above the existing 199.89 --
    # price should raise AND qty should be corrected in the same call.
    monkeypatch.setattr(trail_stops, "compute_atr", lambda bars: 5.5)

    replace_calls = []

    def _fake_replace(alpaca_order_id, new_stop_price, qty=None, account="default"):
        replace_calls.append({"stop_price": new_stop_price, "qty": qty})
        return {"alpaca_order_id": alpaca_order_id, "symbol": "BA", "status": "replaced",
                "stop_price": new_stop_price, "qty": qty}

    monkeypatch.setattr(alpaca_client, "replace_stop_order", _fake_replace)

    results = trail_stops.check_and_trail(dry_run=False, account="default")

    assert len(replace_calls) == 1
    assert replace_calls[0]["qty"] == 26.0
    assert replace_calls[0]["stop_price"] == 219.0

    ba_result = next(r for r in results if r.get("ticker") == "BA")
    assert ba_result["status"] == "fixed_qty_and_raised_stop"
