"""
Tests for the log-parsing helpers in analyst/daily_review.py.

These functions turn heterogeneous JSONL log shapes (single-account vs.
multi-account, `results`/`signals`/`result`-nested) into the flat rows the
daily review report is built from. Each shape quirk covered below is a
documented real bug: _flatten_results silently iterating a dict's string
keys instead of its per-ticker rows (missed after trail_stops.py went
multi-account), and _step_results only reading the old `results`/`signals`
keys after the market loop became the primary driver (silently reported
zero errors/stop events on days that had plenty). A backfilled report
reads _reconstruct_positions instead of live Alpaca state, so its
quantity-weighted averaging is covered too.
"""
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from analyst import daily_review as dr
from shared import db


# --------------------------------------------------- _read_todays_log_entries

def test_read_todays_log_entries_missing_file_returns_empty(tmp_path):
    assert dr._read_todays_log_entries(tmp_path / "nope.jsonl", "2026-08-19") == []


def test_read_todays_log_entries_filters_by_date_prefix(tmp_path):
    path = tmp_path / "log.jsonl"
    path.write_text(
        '{"event": "a", "logged_at": "2026-08-19T10:00:00Z"}\n'
        '{"event": "b", "logged_at": "2026-08-18T10:00:00Z"}\n'
        '{"event": "c", "logged_at": "2026-08-19T15:00:00Z"}\n'
    )

    entries = dr._read_todays_log_entries(path, "2026-08-19")

    assert [e["event"] for e in entries] == ["a", "c"]


def test_read_todays_log_entries_skips_malformed_and_blank_lines(tmp_path):
    path = tmp_path / "log.jsonl"
    path.write_text(
        "\n"
        "not json\n"
        '{"event": "a", "logged_at": "2026-08-19T10:00:00Z"}\n'
    )

    entries = dr._read_todays_log_entries(path, "2026-08-19")

    assert [e["event"] for e in entries] == ["a"]


def test_read_todays_log_entries_missing_logged_at_excluded(tmp_path):
    path = tmp_path / "log.jsonl"
    path.write_text('{"event": "a"}\n')
    assert dr._read_todays_log_entries(path, "2026-08-19") == []


# --------------------------------------------------------- _flatten_results

def test_flatten_results_plain_list():
    raw = [{"ticker": "AAPL", "status": "queued"}, {"ticker": "MSFT", "status": "error"}]
    assert dr._flatten_results(raw) == raw


def test_flatten_results_list_drops_non_dict_rows():
    raw = [{"ticker": "AAPL"}, "not a dict", 42]
    assert dr._flatten_results(raw) == [{"ticker": "AAPL"}]


def test_flatten_results_per_account_dict_tags_account():
    # Regression guard: trail_stops.py's multi-account refactor logs
    # {"<account>": [rows]}, not a plain list.
    raw = {
        "default": [{"ticker": "NVDA", "status": "raised_stop"}],
        "secondary": [{"ticker": "CCI", "status": "placed_initial_stop"}],
    }

    flat = dr._flatten_results(raw)

    assert {"ticker": "NVDA", "status": "raised_stop", "account": "default"} in flat
    assert {"ticker": "CCI", "status": "placed_initial_stop", "account": "secondary"} in flat
    assert len(flat) == 2


def test_flatten_results_dict_with_scalar_values_ignored():
    # The market loop's `clock` step logs {"is_open": true, ...} -- a
    # dict whose values are scalars, not per-account row lists. Must not
    # be misread as one-row-per-key.
    raw = {"is_open": True, "timestamp": "2026-08-19T14:00:00Z"}
    assert dr._flatten_results(raw) == []


def test_flatten_results_mixed_dict_some_scalar_some_list():
    raw = {"is_open": True, "default": [{"ticker": "AAPL"}]}
    flat = dr._flatten_results(raw)
    assert flat == [{"ticker": "AAPL", "account": "default"}]


def test_flatten_results_other_types_return_empty():
    assert dr._flatten_results(None) == []
    assert dr._flatten_results("a string") == []
    assert dr._flatten_results(42) == []


# ----------------------------------------------------------- _step_results

def test_step_results_reads_results_key():
    entry = {"results": [{"ticker": "AAPL", "status": "queued"}]}
    assert dr._step_results(entry) == [{"ticker": "AAPL", "status": "queued"}]


def test_step_results_reads_signals_key():
    entry = {"signals": [{"ticker": "MSFT", "status": "queued"}]}
    assert dr._step_results(entry) == [{"ticker": "MSFT", "status": "queued"}]


def test_step_results_prefers_results_over_signals_when_both_present():
    entry = {"results": [{"ticker": "A"}], "signals": [{"ticker": "B"}]}
    assert dr._step_results(entry) == [{"ticker": "A"}]


def test_step_results_falls_back_to_nested_result_signals():
    # The market loop nests the swing cycle's summary inside `result`, under
    # its own `signals`/`execution`/`reconciliation` keys. NOTE: because
    # entry["result"] is itself a dict with a list-valued key, the PRIMARY
    # loop's generic _flatten_results(entry["result"]) already matches it --
    # treating "signals" as if it were an account name and tagging each row
    # with account="signals" -- before the explicit `inner.get("signals")`
    # fallback below ever runs. That makes the documented fallback
    # (checking inner.get(key) for signals/execution/reconciliation)
    # unreachable in practice, and the resulting rows carry a spurious
    # "account" key. Pinning the actual behavior here so a refactor of
    # either function doesn't change it silently.
    entry = {"result": {"signals": [{"ticker": "AAPL", "status": "queued"}]}}
    assert dr._step_results(entry) == [{"ticker": "AAPL", "status": "queued", "account": "signals"}]


def test_step_results_nested_result_execution_key():
    entry = {"result": {"execution": [{"ticker": "AAPL", "status": "error", "error": "boom"}]}}
    assert dr._step_results(entry) == [{"ticker": "AAPL", "status": "error", "error": "boom", "account": "execution"}]


def test_step_results_nested_result_reconciliation_key():
    entry = {"result": {"reconciliation": [{"ticker": "AAPL", "status": "reconciled"}]}}
    assert dr._step_results(entry) == [{"ticker": "AAPL", "status": "reconciled", "account": "reconciliation"}]


def test_step_results_no_recognized_keys_returns_empty():
    assert dr._step_results({"event": "clock", "result": {"is_open": True}}) == []


def test_step_results_empty_entry_returns_empty():
    assert dr._step_results({}) == []


# ---------------------------------------------------------- _extract_errors

def test_extract_errors_step_level_error():
    entries = [{"event": "swing_cycle", "status": "error", "error": "ConnectionError: timeout",
                "logged_at": "2026-08-19T10:00:00Z"}]

    errors = dr._extract_errors("market_loop", entries)

    assert len(errors) == 1
    assert errors[0]["source"] == "market_loop"
    assert errors[0]["ticker"] is None
    assert "swing_cycle" in errors[0]["error"]
    assert "timeout" in errors[0]["error"]


def test_extract_errors_top_level_execution_error_key():
    entries = [{"execution_error": "insufficient buying power", "logged_at": "2026-08-19T10:00:00Z"}]
    errors = dr._extract_errors("cycle", entries)
    assert errors == [{"source": "cycle", "ticker": None,
                        "error": "insufficient buying power", "logged_at": "2026-08-19T10:00:00Z"}]


def test_extract_errors_nested_result_execution_error():
    entries = [{"result": {"execution_error": "PDT blocked"}, "logged_at": "2026-08-19T10:00:00Z"}]
    errors = dr._extract_errors("market_loop", entries)
    assert errors == [{"source": "market_loop", "ticker": None,
                        "error": "PDT blocked", "logged_at": "2026-08-19T10:00:00Z"}]


def test_extract_errors_per_ticker_error_status():
    entries = [{"results": [
        {"ticker": "AAPL", "status": "queued"},
        {"ticker": "MSFT", "status": "error", "error": "rejected by broker"},
    ], "logged_at": "2026-08-19T10:00:00Z"}]

    errors = dr._extract_errors("cycle", entries)

    assert errors == [{"source": "cycle", "ticker": "MSFT",
                        "error": "rejected by broker", "logged_at": "2026-08-19T10:00:00Z"}]


def test_extract_errors_no_errors_returns_empty():
    entries = [{"results": [{"ticker": "AAPL", "status": "queued"}], "logged_at": "2026-08-19T10:00:00Z"}]
    assert dr._extract_errors("cycle", entries) == []


def test_extract_errors_multiple_sources_combine():
    entries = [
        {"status": "error", "error": "boom1", "event": "clock", "logged_at": "t1"},
        {"execution_error": "boom2", "logged_at": "t2"},
    ]
    errors = dr._extract_errors("market_loop", entries)
    assert len(errors) == 2


# ------------------------------------------------------ _extract_stop_events

def test_extract_stop_events_captures_recognized_statuses():
    entries = [{"results": [
        {"ticker": "NVDA", "account": "default", "status": "raised_stop",
         "existing_stop": 180.0, "new_stop": 185.0},
        {"ticker": "CCI", "account": "default", "status": "no_change"},
    ], "logged_at": "2026-08-19T10:00:00Z"}]

    events = dr._extract_stop_events(entries)

    assert events == [{
        "ticker": "NVDA", "account": "default", "status": "raised_stop",
        "old_stop": 180.0, "new_stop": 185.0, "logged_at": "2026-08-19T10:00:00Z",
    }]


def test_extract_stop_events_falls_back_to_stop_price_field():
    entries = [{"results": [
        {"ticker": "AAPL", "status": "placed_initial_stop", "stop_price": 149.0},
    ], "logged_at": "t1"}]

    events = dr._extract_stop_events(entries)

    assert events[0]["new_stop"] == 149.0


def test_extract_stop_events_ignores_unrecognized_statuses():
    entries = [{"results": [{"ticker": "AAPL", "status": "some_new_status"}], "logged_at": "t1"}]
    assert dr._extract_stop_events(entries) == []


@pytest.mark.parametrize("status", [
    "raised_stop", "placed_initial_stop", "converted_to_gtc", "recovered_from_stuck_chain",
])
def test_extract_stop_events_covers_all_recognized_statuses(status):
    entries = [{"results": [{"ticker": "AAPL", "status": status}], "logged_at": "t1"}]
    events = dr._extract_stop_events(entries)
    assert len(events) == 1
    assert events[0]["status"] == status


# ------------------------------------------------------ _reconstruct_positions

def _insert_order(conn, ticker, side, qty, fill_price, filled_at, signal_id=None):
    conn.execute(
        """INSERT INTO orders (signal_id, ticker, side, qty, alpaca_order_id, status, filled_at, fill_price)
           VALUES (?, ?, ?, ?, ?, 'filled', ?, ?)""",
        (signal_id, ticker, side, qty, f"ord-{ticker}-{filled_at}", filled_at, fill_price),
    )


def test_reconstruct_positions_single_buy(temp_db):
    _insert_order(temp_db, "AAPL", "buy", 10, 150.0, "2026-08-19 14:30:00+00:00")

    positions = dr._reconstruct_positions(temp_db, "2026-08-19", {"AAPL": 155.0})

    assert len(positions) == 1
    assert positions[0]["symbol"] == "AAPL"
    assert positions[0]["qty"] == 10.0
    assert positions[0]["avg_entry_price"] == 150.0
    assert positions[0]["current_price"] == 155.0
    assert positions[0]["unrealized_pl"] == pytest.approx(50.0)  # (155-150)*10


def test_reconstruct_positions_fully_closed_excluded(temp_db):
    _insert_order(temp_db, "AAPL", "buy", 10, 150.0, "2026-08-19 14:30:00+00:00")
    _insert_order(temp_db, "AAPL", "sell", 10, 160.0, "2026-08-19 15:30:00+00:00")

    positions = dr._reconstruct_positions(temp_db, "2026-08-19", {"AAPL": 160.0})

    assert positions == []


def test_reconstruct_positions_partial_sell_keeps_avg_entry(temp_db):
    # Buy 10 @ 100, sell 4 @ 120 -> avg entry must still read 100 for the
    # remaining 6 shares, not distorted by the sell's fill price.
    _insert_order(temp_db, "AAPL", "buy", 10, 100.0, "2026-08-19 14:30:00+00:00")
    _insert_order(temp_db, "AAPL", "sell", 4, 120.0, "2026-08-19 15:00:00+00:00")

    positions = dr._reconstruct_positions(temp_db, "2026-08-19", {"AAPL": 110.0})

    assert len(positions) == 1
    assert positions[0]["qty"] == 6.0
    assert positions[0]["avg_entry_price"] == pytest.approx(100.0)


def test_reconstruct_positions_excludes_sneaky_pivot_strategy(temp_db):
    # Regression guard: orders from the retired sneaky_pivot strategy's
    # separate paper account must never resurrect a phantom position in a
    # backfilled report for the default account.
    signal_id = db.insert_signal(temp_db, "AMD", "buy", "sneaky_pivot")
    _insert_order(temp_db, "AMD", "buy", 10, 100.0, "2026-08-19 14:30:00+00:00", signal_id=signal_id)

    positions = dr._reconstruct_positions(temp_db, "2026-08-19", {"AMD": 105.0})

    assert positions == []


def test_reconstruct_positions_orders_after_day_excluded(temp_db):
    _insert_order(temp_db, "AAPL", "buy", 10, 100.0, "2026-08-20 14:30:00+00:00")
    positions = dr._reconstruct_positions(temp_db, "2026-08-19", {"AAPL": 105.0})
    assert positions == []


def test_reconstruct_positions_no_close_falls_back_to_avg_entry(temp_db):
    _insert_order(temp_db, "AAPL", "buy", 10, 100.0, "2026-08-19 14:30:00+00:00")
    positions = dr._reconstruct_positions(temp_db, "2026-08-19", {})
    assert positions[0]["current_price"] == 100.0
    assert positions[0]["unrealized_pl"] == 0.0


def test_reconstruct_positions_dust_qty_excluded(temp_db):
    # A position that rounds down to ~0 shares (e.g. from float drift on
    # matched buy/sell qtys) must not show up as a phantom holding.
    _insert_order(temp_db, "AAPL", "buy", 10.0000001, 100.0, "2026-08-19 14:30:00+00:00")
    _insert_order(temp_db, "AAPL", "sell", 10.0, 110.0, "2026-08-19 15:00:00+00:00")

    positions = dr._reconstruct_positions(temp_db, "2026-08-19", {"AAPL": 110.0})

    assert positions == []


def test_reconstruct_positions_sorted_by_ticker(temp_db):
    _insert_order(temp_db, "MSFT", "buy", 5, 300.0, "2026-08-19 14:30:00+00:00")
    _insert_order(temp_db, "AAPL", "buy", 5, 100.0, "2026-08-19 14:31:00+00:00")

    positions = dr._reconstruct_positions(temp_db, "2026-08-19", {"AAPL": 100.0, "MSFT": 300.0})

    assert [p["symbol"] for p in positions] == ["AAPL", "MSFT"]
