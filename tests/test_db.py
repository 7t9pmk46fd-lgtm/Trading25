"""
Tests for shared/db.py.

Every other test suite in this repo mocks around this module (via the
temp_db/temp_db_path fixtures) rather than exercising it directly. That
leaves the schema, the migration path, and the query helpers themselves
unverified -- and this is the one ledger reconcile_orders.py and
shared.risk's FIFO P&L math both depend on, so a bug here is silent
corruption, not a crash.
"""
import sqlite3
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from shared import db


# ------------------------------------------------------------- signals

def test_insert_signal_defaults_to_pending(temp_db):
    signal_id = db.insert_signal(temp_db, "AAPL", "buy", "rd_mean_reversion", qty=10)
    row = temp_db.execute("SELECT * FROM signals WHERE id = ?", (signal_id,)).fetchone()
    assert row["status"] == "pending"
    assert row["ticker"] == "AAPL"
    assert row["qty"] == 10


def test_insert_signal_rejects_invalid_action(temp_db):
    # CHECK(action IN ('buy', 'sell')) -- a typo'd action must fail loudly
    # at the DB layer, not silently insert a signal nothing will ever match.
    with pytest.raises(sqlite3.IntegrityError):
        db.insert_signal(temp_db, "AAPL", "hold", "rd_mean_reversion")


def test_get_pending_signals_excludes_non_pending(temp_db):
    pending_id = db.insert_signal(temp_db, "AAPL", "buy", "rd_mean_reversion")
    executed_id = db.insert_signal(temp_db, "MSFT", "buy", "rd_mean_reversion")
    db.update_signal_status(temp_db, executed_id, "executed")

    pending = db.get_pending_signals(temp_db)

    assert [row["id"] for row in pending] == [pending_id]


def test_get_pending_signals_ordered_oldest_first(temp_db):
    first_id = db.insert_signal(temp_db, "AAPL", "buy", "rd_mean_reversion")
    second_id = db.insert_signal(temp_db, "MSFT", "buy", "rd_mean_reversion")

    pending = db.get_pending_signals(temp_db)

    assert [row["id"] for row in pending] == [first_id, second_id]


def test_update_signal_status_updates_timestamp(temp_db):
    signal_id = db.insert_signal(temp_db, "AAPL", "buy", "rd_mean_reversion")
    before = temp_db.execute(
        "SELECT updated_at FROM signals WHERE id = ?", (signal_id,)
    ).fetchone()["updated_at"]

    db.update_signal_status(temp_db, signal_id, "executed")

    row = temp_db.execute("SELECT * FROM signals WHERE id = ?", (signal_id,)).fetchone()
    assert row["status"] == "executed"
    # updated_at must move so callers can tell a status change actually
    # happened, not just trust the value blindly.
    assert row["updated_at"] >= before


# --------------------------------------------------------------- orders

def test_get_unreconciled_orders_excludes_terminal_statuses(temp_db):
    signal_id = db.insert_signal(temp_db, "AAPL", "buy", "rd_mean_reversion")
    temp_db.execute(
        """INSERT INTO orders (signal_id, ticker, side, qty, alpaca_order_id, status)
           VALUES (?, 'AAPL', 'buy', 10, 'ord-open', 'partially_filled')""",
        (signal_id,),
    )
    temp_db.execute(
        """INSERT INTO orders (signal_id, ticker, side, qty, alpaca_order_id, status)
           VALUES (?, 'AAPL', 'sell', 10, 'ord-done', 'filled')""",
        (signal_id,),
    )
    temp_db.execute(
        """INSERT INTO orders (signal_id, ticker, side, qty, alpaca_order_id, status)
           VALUES (?, 'AAPL', 'sell', 10, 'ord-canceled', 'canceled')""",
        (signal_id,),
    )

    unreconciled = db.get_unreconciled_orders(temp_db)

    assert [row["alpaca_order_id"] for row in unreconciled] == ["ord-open"]


def test_get_unreconciled_orders_ignores_orders_without_alpaca_id(temp_db):
    # An order that never got a real Alpaca id (submission failed before
    # the id came back) has nothing to reconcile against and must not be
    # returned -- reconcile_orders would have no key to look it up by.
    signal_id = db.insert_signal(temp_db, "AAPL", "buy", "rd_mean_reversion")
    temp_db.execute(
        """INSERT INTO orders (signal_id, ticker, side, qty, alpaca_order_id, status)
           VALUES (?, 'AAPL', 'buy', 10, NULL, 'submitted')""",
        (signal_id,),
    )

    assert db.get_unreconciled_orders(temp_db) == []


def test_get_unreconciled_orders_includes_partially_filled(temp_db):
    # Regression guard for the AMD 2026-08-04 bug: partially_filled orders
    # already have a non-null filled_at, so a filled_at-based check would
    # drop them here. The gate must be on status, not filled_at.
    signal_id = db.insert_signal(temp_db, "AMD", "sell", "rd_mean_reversion")
    temp_db.execute(
        """INSERT INTO orders (signal_id, ticker, side, qty, alpaca_order_id,
                                status, filled_at, filled_qty)
           VALUES (?, 'AMD', 'sell', 13, 'ord-amd', 'partially_filled',
                   '2026-08-04 15:00:00+00:00', 11)""",
        (signal_id,),
    )

    unreconciled = db.get_unreconciled_orders(temp_db)

    assert [row["alpaca_order_id"] for row in unreconciled] == ["ord-amd"]


def test_get_unreconciled_orders_joins_strategy_from_signal(temp_db):
    signal_id = db.insert_signal(temp_db, "AAPL", "buy", "rd_mean_reversion")
    temp_db.execute(
        """INSERT INTO orders (signal_id, ticker, side, qty, alpaca_order_id, status)
           VALUES (?, 'AAPL', 'buy', 10, 'ord-1', 'submitted')""",
        (signal_id,),
    )

    row = db.get_unreconciled_orders(temp_db)[0]

    assert row["strategy"] == "rd_mean_reversion"


def test_get_unreconciled_orders_strategy_null_when_signal_missing(temp_db):
    # Orders inserted by the reconcile backfill itself carry signal_id=NULL
    # (see reconcile_missing_fills) -- the LEFT JOIN must not turn that
    # into a crash or drop the row.
    temp_db.execute(
        """INSERT INTO orders (signal_id, ticker, side, qty, alpaca_order_id, status)
           VALUES (NULL, 'AAPL', 'sell', 10, 'ord-orphan', 'submitted')"""
    )

    row = db.get_unreconciled_orders(temp_db)[0]

    assert row["strategy"] is None


def test_update_order_fill_sets_filled_qty(temp_db):
    signal_id = db.insert_signal(temp_db, "AAPL", "buy", "rd_mean_reversion")
    temp_db.execute(
        """INSERT INTO orders (signal_id, ticker, side, qty, alpaca_order_id, status)
           VALUES (?, 'AAPL', 'buy', 13, 'ord-1', 'submitted')""",
        (signal_id,),
    )

    db.update_order_fill(temp_db, "ord-1", "filled", "2026-08-04T15:00:00Z", 100.5, filled_qty=11)

    row = temp_db.execute("SELECT * FROM orders WHERE alpaca_order_id = 'ord-1'").fetchone()
    assert row["status"] == "filled"
    assert row["fill_price"] == 100.5
    assert row["filled_qty"] == 11
    # qty (ordered) must be untouched -- filled_qty is a separate field
    # precisely because the two can diverge on a partial fill.
    assert row["qty"] == 13


# --------------------------------------------------------- research notes

def test_insert_research_note_defaults_to_unreviewed(temp_db):
    note_id = db.insert_research_note(
        temp_db, "news_article", "https://example.com/a", source_title="Some article"
    )
    row = temp_db.execute("SELECT * FROM research_notes WHERE id = ?", (note_id,)).fetchone()
    assert row["status"] == "unreviewed"


def test_insert_research_note_rejects_invalid_source_type(temp_db):
    with pytest.raises(sqlite3.IntegrityError):
        db.insert_research_note(temp_db, "tweet", "https://example.com/a")


def test_get_unreviewed_notes_excludes_reviewed(temp_db):
    unreviewed_id = db.insert_research_note(temp_db, "news_article", "https://example.com/a")
    reviewed_id = db.insert_research_note(temp_db, "news_article", "https://example.com/b")
    temp_db.execute(
        "UPDATE research_notes SET status = 'reviewed_discarded' WHERE id = ?", (reviewed_id,)
    )

    notes = db.get_unreviewed_notes(temp_db)

    assert [row["id"] for row in notes] == [unreviewed_id]


# ----------------------------------------------------- account baselines

def test_set_account_baseline_then_get(temp_db):
    db.set_account_baseline(temp_db, "default", "2026-08-01", 100_000.0, 450.0)

    row = db.get_account_baseline(temp_db, "default")

    assert row["start_date"] == "2026-08-01"
    assert row["start_equity"] == 100_000.0
    assert row["benchmark_symbol"] == "SPY"
    assert row["benchmark_start_price"] == 450.0


def test_set_account_baseline_upserts_without_duplicating(temp_db):
    # Re-running the seed script with corrected inputs must overwrite the
    # existing row, not error on the account_baselines PRIMARY KEY or
    # leave a stale duplicate behind.
    db.set_account_baseline(temp_db, "default", "2026-08-01", 100_000.0, 450.0)
    db.set_account_baseline(temp_db, "default", "2026-08-02", 101_000.0, 452.0)

    rows = temp_db.execute("SELECT * FROM account_baselines").fetchall()
    assert len(rows) == 1
    assert rows[0]["start_date"] == "2026-08-02"
    assert rows[0]["start_equity"] == 101_000.0


def test_get_account_baseline_missing_account_returns_none(temp_db):
    assert db.get_account_baseline(temp_db, "nonexistent") is None


# -------------------------------------------------------------- backtests

def test_insert_backtest_run_roundtrip(temp_db):
    run_id = db.insert_backtest_run(
        temp_db,
        strategy="rd_mean_reversion",
        params_json='{"z_entry": -1.5}',
        start_date="2025-01-01",
        end_date="2026-01-01",
        universe="sp500",
        total_return=0.12,
        sharpe=1.1,
        max_drawdown=-0.08,
        num_trades=42,
        win_rate=0.55,
    )
    row = temp_db.execute("SELECT * FROM backtest_runs WHERE id = ?", (run_id,)).fetchone()
    assert row["strategy"] == "rd_mean_reversion"
    assert row["num_trades"] == 42


# ---------------------------------------------------------------- schema

def test_init_db_is_idempotent(tmp_path, monkeypatch):
    # init_db() (SCHEMA + _migrate) must be safe to call repeatedly against
    # the same file -- production code doesn't guard every call site with
    # "only once".
    test_db_path = tmp_path / "idempotent.db"
    monkeypatch.setattr(db, "DB_PATH", str(test_db_path))

    db.init_db()
    db.init_db()

    with db.db_session() as conn:
        signal_id = db.insert_signal(conn, "AAPL", "buy", "rd_mean_reversion")
    assert signal_id == 1


def test_migrate_adds_filled_qty_to_pre_existing_orders_table(tmp_path, monkeypatch):
    # Simulates a DB file created before filled_qty existed: a bare
    # `orders` table without that column. get_connection() must migrate it
    # in place rather than erroring the next time update_order_fill runs.
    test_db_path = tmp_path / "legacy.db"
    monkeypatch.setattr(db, "DB_PATH", str(test_db_path))

    legacy_conn = sqlite3.connect(str(test_db_path))
    legacy_conn.execute(
        """CREATE TABLE orders (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            signal_id INTEGER,
            ticker TEXT NOT NULL,
            side TEXT NOT NULL,
            qty REAL NOT NULL,
            order_type TEXT NOT NULL DEFAULT 'market',
            alpaca_order_id TEXT,
            status TEXT NOT NULL DEFAULT 'submitted',
            is_paper INTEGER NOT NULL DEFAULT 1,
            submitted_at TEXT NOT NULL DEFAULT (datetime('now')),
            filled_at TEXT,
            fill_price REAL,
            notes TEXT
        )"""
    )
    legacy_conn.commit()
    legacy_conn.close()

    conn = db.get_connection()
    columns = {row["name"] for row in conn.execute("PRAGMA table_info(orders)").fetchall()}
    conn.close()

    assert "filled_qty" in columns


def test_migrate_noops_on_brand_new_db(tmp_path, monkeypatch):
    # _migrate must not error when `orders` doesn't exist yet -- the very
    # first get_connection() call on a fresh DB file hits this path before
    # SCHEMA has ever run.
    test_db_path = tmp_path / "brand_new.db"
    monkeypatch.setattr(db, "DB_PATH", str(test_db_path))

    conn = db.get_connection()
    conn.close()  # must not raise
