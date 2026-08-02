"""
Shared SQLite database: the contract between the R&D agent and the
execution agent.

Flow:
  1. R&D agent writes rows to `signals` with status='pending'.
  2. Execution agent polls for status='pending' signals, attempts to place
     the order via Alpaca, then updates status to 'executed' or 'rejected'
     and records the resulting order/fill info.

Design choices:
  - SQLite because this is a single-user, single-machine system — no need
    for a client-server DB. Easy to inspect with any SQLite browser.
  - Every table has created_at/updated_at for auditability. For a system
    that (eventually) touches real money, you want a clear trail of what
    was proposed, when, and what happened to it.
"""
import sqlite3
from contextlib import contextmanager
from pathlib import Path

from shared.config import DB_PATH

SCHEMA = """
CREATE TABLE IF NOT EXISTS signals (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker          TEXT NOT NULL,
    action          TEXT NOT NULL CHECK(action IN ('buy', 'sell')),
    strategy        TEXT NOT NULL,
    qty             REAL,               -- share quantity, if fixed
    weight          REAL,               -- target portfolio weight, if used instead of qty
    stop_price      REAL,               -- protective stop level a 'buy' was sized against;
                                         -- execution-agent places a real broker-side stop at
                                         -- this price, not just a sizing assumption
    confidence      REAL,               -- 0-1, strategy's own confidence score
    reasoning       TEXT,               -- human-readable explanation
    status          TEXT NOT NULL DEFAULT 'pending'
                        CHECK(status IN ('pending', 'executed', 'rejected', 'expired')),
    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at      TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS orders (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    signal_id       INTEGER REFERENCES signals(id),
    ticker          TEXT NOT NULL,
    side            TEXT NOT NULL CHECK(side IN ('buy', 'sell')),
    qty             REAL NOT NULL,
    order_type      TEXT NOT NULL DEFAULT 'market',
    alpaca_order_id TEXT,               -- Alpaca's own order id, for reconciliation
    status          TEXT NOT NULL DEFAULT 'submitted',
    is_paper        INTEGER NOT NULL DEFAULT 1,  -- 1 = paper trade, 0 = live
    submitted_at    TEXT NOT NULL DEFAULT (datetime('now')),
    filled_at       TEXT,
    fill_price      REAL,
    notes           TEXT
);

CREATE TABLE IF NOT EXISTS research_notes (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    source_type     TEXT NOT NULL CHECK(source_type IN ('news_article', 'youtube_video')),
    source_url      TEXT NOT NULL,
    source_title    TEXT,
    published_at    TEXT,               -- publish date of the source, if known
    strategy_summary TEXT,              -- Claude's plain-language summary of the idea
    tickers_mentioned TEXT,             -- comma-separated tickers/sectors mentioned
    specificity     TEXT CHECK(specificity IN ('vague', 'moderate', 'concrete')),
                        -- how actionable/concrete the claim actually is
    raw_excerpt     TEXT,               -- short excerpt Claude pulled as support (not full reproduction)
    status          TEXT NOT NULL DEFAULT 'unreviewed'
                        CHECK(status IN ('unreviewed', 'reviewed_promising', 'reviewed_discarded', 'promoted_to_strategy')),
    reviewer_notes  TEXT,               -- your own notes after reviewing
    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at      TEXT NOT NULL DEFAULT (datetime('now'))
    -- IMPORTANT: rows in this table are NEVER read by the execution agent.
    -- They exist purely for human review. An idea only becomes tradeable
    -- once a person codes it up as an actual screener/strategy that gets
    -- backtested like any other, at which point it's promoted and this
    -- row's status is updated for traceability.
);

CREATE TABLE IF NOT EXISTS account_baselines (
    account              TEXT PRIMARY KEY,   -- matches shared.config.KNOWN_ACCOUNTS
    start_date           TEXT NOT NULL,      -- calendar date this account's tracked
                                              -- performance window begins (approximate --
                                              -- see seed_account_baselines.py for how it
                                              -- was picked for each account)
    start_equity         REAL NOT NULL,
    benchmark_symbol      TEXT NOT NULL DEFAULT 'SPY',
    benchmark_start_price REAL NOT NULL,     -- SPY's real close on start_date
    created_at            TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS backtest_runs (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    strategy        TEXT NOT NULL,
    params_json     TEXT,               -- JSON blob of the parameters used
    start_date      TEXT,
    end_date        TEXT,
    universe        TEXT,               -- e.g. "sp500"
    total_return    REAL,
    sharpe          REAL,
    max_drawdown    REAL,
    num_trades      INTEGER,
    win_rate        REAL,
    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
    notes           TEXT
);
"""


def get_connection() -> sqlite3.Connection:
    Path(DB_PATH).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON;")
    return conn


def init_db() -> None:
    with get_connection() as conn:
        conn.executescript(SCHEMA)


@contextmanager
def db_session():
    conn = get_connection()
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def insert_signal(
    conn: sqlite3.Connection,
    ticker: str,
    action: str,
    strategy: str,
    qty: float | None = None,
    weight: float | None = None,
    stop_price: float | None = None,
    confidence: float | None = None,
    reasoning: str = "",
) -> int:
    cur = conn.execute(
        """INSERT INTO signals (ticker, action, strategy, qty, weight, stop_price, confidence, reasoning)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (ticker, action, strategy, qty, weight, stop_price, confidence, reasoning),
    )
    return cur.lastrowid


def get_pending_signals(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM signals WHERE status = 'pending' ORDER BY created_at ASC"
    ).fetchall()


def update_signal_status(conn: sqlite3.Connection, signal_id: int, status: str) -> None:
    conn.execute(
        "UPDATE signals SET status = ?, updated_at = datetime('now') WHERE id = ?",
        (status, signal_id),
    )


def insert_research_note(
    conn: sqlite3.Connection,
    source_type: str,
    source_url: str,
    source_title: str = "",
    published_at: str | None = None,
    strategy_summary: str = "",
    tickers_mentioned: str = "",
    specificity: str = "vague",
    raw_excerpt: str = "",
) -> int:
    cur = conn.execute(
        """INSERT INTO research_notes
           (source_type, source_url, source_title, published_at, strategy_summary,
            tickers_mentioned, specificity, raw_excerpt)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (source_type, source_url, source_title, published_at, strategy_summary,
         tickers_mentioned, specificity, raw_excerpt),
    )
    return cur.lastrowid


def get_unreconciled_orders(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """Orders that have an Alpaca order id but haven't been recorded as
    filled locally yet -- candidates for reconciliation against Alpaca's
    own order status. Includes the originating strategy (joined from
    signals) so callers can route the lookup to the right Alpaca account
    via shared.config.account_for_strategy -- an order id is only valid
    against the account it was actually submitted to."""
    return conn.execute(
        """
        SELECT o.*, s.strategy AS strategy
        FROM orders o
        LEFT JOIN signals s ON o.signal_id = s.id
        WHERE o.alpaca_order_id IS NOT NULL AND o.filled_at IS NULL
          AND o.status NOT IN ('canceled', 'replaced', 'expired', 'rejected')
        """
    ).fetchall()


def update_order_fill(
    conn: sqlite3.Connection,
    alpaca_order_id: str,
    status: str,
    filled_at: str | None,
    fill_price: float | None,
) -> None:
    conn.execute(
        "UPDATE orders SET status = ?, filled_at = ?, fill_price = ? WHERE alpaca_order_id = ?",
        (status, filled_at, fill_price, alpaca_order_id),
    )


def get_unreviewed_notes(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM research_notes WHERE status = 'unreviewed' ORDER BY created_at DESC"
    ).fetchall()


def set_account_baseline(
    conn: sqlite3.Connection,
    account: str,
    start_date: str,
    start_equity: float,
    benchmark_start_price: float,
    benchmark_symbol: str = "SPY",
) -> None:
    """Upsert -- re-running the seed script with corrected inputs should
    overwrite, not duplicate-error."""
    conn.execute(
        """INSERT INTO account_baselines
               (account, start_date, start_equity, benchmark_symbol, benchmark_start_price)
           VALUES (?, ?, ?, ?, ?)
           ON CONFLICT(account) DO UPDATE SET
               start_date = excluded.start_date,
               start_equity = excluded.start_equity,
               benchmark_symbol = excluded.benchmark_symbol,
               benchmark_start_price = excluded.benchmark_start_price""",
        (account, start_date, start_equity, benchmark_symbol, benchmark_start_price),
    )


def get_account_baseline(conn: sqlite3.Connection, account: str) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM account_baselines WHERE account = ?", (account,)
    ).fetchone()


def insert_backtest_run(
    conn: sqlite3.Connection,
    strategy: str,
    params_json: str,
    start_date: str,
    end_date: str,
    universe: str,
    total_return: float | None,
    sharpe: float | None,
    max_drawdown: float | None,
    num_trades: int,
    win_rate: float | None,
    notes: str = "",
) -> int:
    cur = conn.execute(
        """INSERT INTO backtest_runs
               (strategy, params_json, start_date, end_date, universe,
                total_return, sharpe, max_drawdown, num_trades, win_rate, notes)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (strategy, params_json, start_date, end_date, universe,
         total_return, sharpe, max_drawdown, num_trades, win_rate, notes),
    )
    return cur.lastrowid


if __name__ == "__main__":
    init_db()
    print(f"Initialized database at {DB_PATH}")
