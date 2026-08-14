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
