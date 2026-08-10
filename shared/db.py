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
    qty             REAL NOT NULL,       -- ORDERED qty, set at submission. For a
                                         -- partially_filled order this is NOT
                                         -- the amount that actually filled --
                                         -- see filled_qty below.
    order_type      TEXT NOT NULL DEFAULT 'market',
    alpaca_order_id TEXT,               -- Alpaca's own order id, for reconciliation
    status          TEXT NOT NULL DEFAULT 'submitted',
    is_paper        INTEGER NOT NULL DEFAULT 1,  -- 1 = paper trade, 0 = live
    submitted_at    TEXT NOT NULL DEFAULT (datetime('now')),
    filled_at       TEXT,
    fill_price      REAL,
    filled_qty      REAL,               -- Added 2026-08-10, populated by
                                         -- reconcile_orders from Alpaca's own
                                         -- record. NULL for orders reconciled
                                         -- before this column existed, and for
                                         -- anything still unreconciled -- callers
                                         -- that need "how much actually filled"
                                         -- should COALESCE(filled_qty, qty) and
                                         -- treat the fallback as approximate.
                                         -- Confirmed to matter for real: AMD's
                                         -- 2026-08-04 sell recorded qty=13 (the
                                         -- order placed) while Alpaca had only
                                         -- filled 11 -- a gap `qty` alone can't see.
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
    # Table may already exist from before a column was added to SCHEMA --
    # CREATE TABLE IF NOT EXISTS alone would never pick that up. Run on
    # every connection (not just init_db()) since most callers go straight
    # to db_session() and never call init_db() explicitly. _migrate no-ops
    # if `orders` doesn't exist yet, so this is safe on a brand-new DB too.
    _migrate(conn)
    return conn


def _migrate(conn: sqlite3.Connection) -> None:
    """Lightweight schema migrations for columns added after the DB file
    already existed. CREATE TABLE IF NOT EXISTS (in SCHEMA) never alters an
    existing table, so a new column needs an explicit, idempotent
    ALTER TABLE here. No-ops entirely if `orders` doesn't exist yet (a
    brand-new DB, about to get the full current schema from SCHEMA
    instead) -- ALTER TABLE ADD COLUMN has no IF NOT EXISTS form in
    SQLite, and PRAGMA table_info on a missing table just returns nothing,
    so check sqlite_master first rather than let ALTER fail on a table
    that was never created."""
    table_exists = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='orders'"
    ).fetchone()
    if not table_exists:
        return
    existing = {row["name"] for row in conn.execute("PRAGMA table_info(orders)").fetchall()}
    if "filled_qty" not in existing:
        conn.execute("ALTER TABLE orders ADD COLUMN filled_qty REAL")
        conn.commit()


def init_db() -> None:
    with get_connection() as conn:
        conn.executescript(SCHEMA)
        _migrate(conn)


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
    """Orders not yet in a TERMINAL state locally -- candidates for
    reconciliation against Alpaca's own order status. Includes the
    originating strategy (joined from signals) so callers can route the
    lookup to the right Alpaca account via shared.config.account_for_strategy
    -- an order id is only valid against the account it was actually
    submitted to.

    Gates on status, not `filled_at IS NULL` (changed 2026-08-10). Alpaca
    sets filled_at on the FIRST partial fill, not only on full completion
    -- a 'partially_filled' order already has a non-null filled_at, so the
    old filled_at-based check dropped it from this list the moment ANY
    portion filled and never looked at it again, permanently missing
    later fills of the remainder. Confirmed for real: AMD's 2026-08-04
    sell was recorded partially_filled at 11/13 shares and stayed that
    way in the local ledger for six days, even though Alpaca had it fully
    filled at 13/13 within the same session -- caught only by a manual
    one-off re-check, not by this function. 'filled' is the only status
    that means truly done; every other non-terminal status still needs a
    look."""
    return conn.execute(
        """
        SELECT o.*, s.strategy AS strategy
        FROM orders o
        LEFT JOIN signals s ON o.signal_id = s.id
        WHERE o.alpaca_order_id IS NOT NULL
          AND o.status NOT IN ('filled', 'canceled', 'replaced', 'expired', 'rejected')
        """
    ).fetchall()


def update_order_fill(
    conn: sqlite3.Connection,
    alpaca_order_id: str,
    status: str,
    filled_at: str | None,
    fill_price: float | None,
    filled_qty: float | None = None,
) -> None:
    """filled_qty added 2026-08-10 -- optional so existing callers that
    don't pass it (there were none, but keep the signature backward
    compatible) don't break. Always pass it going forward; `qty` alone is
    the ordered amount and can silently diverge from what actually filled
    on a partial fill (confirmed for real on AMD's 2026-08-04 sell)."""
    conn.execute(
        "UPDATE orders SET status = ?, filled_at = ?, fill_price = ?, filled_qty = ? WHERE alpaca_order_id = ?",
        (status, filled_at, fill_price, filled_qty, alpaca_order_id),
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
