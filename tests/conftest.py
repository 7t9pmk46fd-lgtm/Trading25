"""
Shared pytest fixtures.

The one rule every fixture here exists to enforce: NOTHING in this test
suite may touch the real production database (data/trading.db) or the
real Alpaca API. Every risk/DB test gets an isolated temp SQLite file;
every execution-loop test mocks the network boundary (execution.
alpaca_client and shared.market_data) rather than calling it.
"""
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from shared import db


@pytest.fixture
def temp_db(tmp_path, monkeypatch):
    """Points shared.db at a fresh temp SQLite file for the duration of
    one test, then restores the real path. shared/db.py binds DB_PATH as
    a module-level name at import time (`from shared.config import
    DB_PATH`), so get_connection() reads shared.db.DB_PATH specifically --
    patching shared.config.DB_PATH alone would not affect it.

    Yields one open connection held for the whole test -- fine for tests
    that only ever touch the DB through that single connection. NOT safe
    for tests that also call a function which opens its OWN db_session()
    (e.g. process_pending_signals): this connection's writes aren't
    committed until teardown, so a second connection to the same file
    won't see them yet. Use `temp_db_path` for those instead."""
    test_db_path = tmp_path / "test_trading.db"
    monkeypatch.setattr(db, "DB_PATH", str(test_db_path))
    db.init_db()
    with db.db_session() as conn:
        yield conn


@pytest.fixture
def temp_db_path(tmp_path, monkeypatch):
    """Same DB_PATH redirection as `temp_db`, but doesn't hold a
    connection open -- each `with db.db_session()` a test (or the code
    under test) opens commits and closes independently, exactly like
    production. Required for anything that calls process_pending_signals,
    which opens its own session internally."""
    test_db_path = tmp_path / "test_trading.db"
    monkeypatch.setattr(db, "DB_PATH", str(test_db_path))
    db.init_db()
    yield str(test_db_path)
