# Trading Desk

A multi-agent personal trading system (Alpaca paper trading). Three
agents, deliberately separated by responsibility, over a shared core:

| Package | Role | Can it touch real/paper trades? |
|---|---|---|
| `signals/` | Strategy research: screeners, backtests, signal generation (swing `rd_mean_reversion` + intraday `sneaky_pivot`) | No — only queues signals |
| `execution/` | Places orders via Alpaca, re-validates every signal against live account state | Yes — the ONLY package that trades |
| `analyst/` | Ingests news/YouTube into research notes; builds the daily review PDF | No — research output requires human review + a coded, backtested strategy before it can ever influence a trade |
| `shared/` | Config, SQLite ledger, risk rules, market data client | Risk rules block, don't just warn |
| `mcp_server.py` | MCP server exposing the desk to AI agents (see below) | No — analysis/simulation only, by hard design |

Restructured 2026-08-02 from the original five agent directories
(`rd-agent`, `day-trading-agent`, `execution-agent`, `learning-agent`) —
same code and strategy names, consolidated layout, no more `sys.path`
gymnastics. Older dates in docstrings refer to the old layout; git
history has the full story.

## Layout

```
shared/        config.py, db.py (SQLite ledger), risk.py (PDT/circuit-breaker/sizing),
               market_data.py (Alpaca bars + cache), benchmark.py (vs SPY)
signals/       screeners/ (mean_reversion, sneaky_pivot), backtest/ (engine + real-data
               backtests), generate_signal.py (queues signals for both strategies)
execution/     alpaca_client.py (the only order-placing module), run_execution_loop.py,
               trail_stops.py, reconcile_orders.py, smoke_test.py
analyst/       ingest/ (news, youtube), extract.py (Claude), ingest_source.py,
               review_notes.py, daily_review.py (PDF report)
scripts/       run_cycle.py (+ .bat, scheduled daily), run_sneaky_pivot_cycle.py,
               seed_account_baselines.py
mcp_server.py  MCP server (stdio); .mcp.json registers it for Claude Code
```

## MCP server

`mcp_server.py` exposes the desk's capabilities as MCP tools so any
MCP-capable agent (Claude Code picks it up automatically via `.mcp.json`)
can work with the system directly:

- `portfolio_status` — equity, P&L, positions, standing stops, alpha vs SPY (portfolio tracker)
- `recent_activity` — signals/orders from the local ledger
- `historical_analysis` — return/volatility/ATR/z-score stats per symbol
- `run_screener` — what each strategy currently sees, analysis-only
- `risk_check` — position sizing + PDT/circuit-breaker gates for a hypothetical entry (risk manager)
- `backtest` — simulate a strategy over real history using the production screener code
- `market_research` / `list_research_notes` — ingest news/YouTube into quarantined research notes

**Hard boundary**: the MCP server can never place, modify, or cancel an
order, and never writes to the `signals` table. The only path to a real
trade remains `execution/run_execution_loop.py --live`.

External market-data MCPs (TradingView screeners, financial statements,
news) can complement these tools; they're configured at the Claude Code
user level, not in this repo.

## Status

- ✅ Both strategies live on separate Alpaca paper accounts:
  `rd_mean_reversion` on the default account (scheduled daily cycle),
  `sneaky_pivot` on its own account (15-min cycle when scheduled; PDT and
  circuit-breaker state isolated by design since 2026-07-28).
- ✅ Every buy carries a real broker-side stop (2xATR), with
  `execution/trail_stops.py` ratcheting stops up and force-converting
  Alpaca's DAY-TIF OTO stop legs to GTC (a real expiry bug found
  2026-07-27 — see `execution/SKILL.md`).
- ✅ Backtested against real data with SPY benchmark comparison
  (`signals/backtest/`, results in the `backtest_runs` table).
- ✅ Daily PDF review (`analyst/daily_review.py`): P&L, positions,
  orders, stop activity, watchlist moves, benchmark, mistakes log.
- 🔲 Portfolio-level backtesting (multi-ticker capital allocation).
- 🔲 Weight-based signal sizing (execution rejects weight-only signals
  rather than guessing).
- 🔲 Short support (long-only end to end, deliberately).

## Full autonomous mode (2026-08-02: continuous market-hours loop)

Windows Task Scheduler task `TradingDeskMarketLoop` starts
`scripts/run_trading_day.bat` every weekday at 8:20 AM CT (9:20 ET). The
loop (`scripts/run_trading_day.py`) then covers the whole session with no
per-trade human approval:

- waits for the open using **Alpaca's market clock** (holidays and early
  closes handled by the broker, not local weekday math);
- once per day at/after 9:45 ET, Mon–Wed (preserving the old daily-cycle
  schedule): swing mean-reversion scan → live execution → reconciliation;
- every 15 minutes until close: sneaky_pivot intraday cycle (live) +
  `trail_stops` across both accounts;
- after close: final reconciliation, then exits until the next morning.

Safety rails enforced in code: **refuses to start unless
`ALPACA_PAPER=true`** (the no-approval autonomy is scoped to paper
trading only), an OS-level single-instance lock so a manual run and the
scheduled task can't double-trade, and per-step exception isolation with
every step logged to `data/trading_day_log.jsonl`.

```powershell
Get-ScheduledTask -TaskName TradingDeskMarketLoop       # check status
Disable-ScheduledTask -TaskName TradingDeskMarketLoop    # pause
Unregister-ScheduledTask -TaskName TradingDeskMarketLoop # remove
```

Manual runs of `scripts/run_sneaky_pivot_cycle.py` remain dry-run by
default (`--live` required); a dry-run expires every signal it evaluates
so no other live cycle can pick one up (a real cross-cycle execution
bug, fixed 2026-07-29). The old one-shot `TradingDeskDailyCycle` task
was removed in favor of the loop; `scripts/run_cycle.py` is still the
swing-cycle implementation the loop calls.

Two Claude scheduled tasks (run locally by Claude Code while the app is
open; a missed run fires on next launch) complete the unattended day:

- **trading-desk-daily-review** (weekdays 3:15 PM CT): runs
  `analyst/daily_review.py gather`, writes the narrative, builds the PDF
  in `Reports/`.
- **trading-desk-weekly-rd** (Saturdays 10 AM CT): re-runs both
  backtests, mines the week's logs and fills, writes
  `Reports/weekly_rd_<date>.md`, and files concrete improvement
  proposals as research notes. **Proposals only — it never edits
  strategy code or parameters**; promotion still goes through human
  review (`analyst/review_notes.py`), per design principle 2.

## Risk rules (enforced in code, not just documented)

- **PDT**: under $25k equity, max 3 day trades per rolling 5 days —
  blocks, doesn't warn. Alpaca's own `daytrade_count`/PDT flag treated as
  authoritative; the local counter is a second layer.
- **Daily circuit breaker**: new entries blocked once today's P&L hits
  -2.5% of equity. Exits are NEVER blocked by any risk rule.
- **Sizing**: positions sized so a full stop-out risks ~1% of equity,
  capped at 10% of equity per position.
- **Every buy needs a real stop**: signals without `stop_price` (or
  without `qty`) are rejected at execution, never guessed.
- A stop bounds losses, it doesn't eliminate them — an overnight gap can
  fill below the stop price.

## Setup

```bash
python -m venv venv
venv\Scripts\activate            # Windows
pip install -r requirements.txt

copy .env.example .env           # then fill in real keys (never committed)
python -m shared.db              # initialize the database
python execution/smoke_test.py   # read-only connectivity check — run this first
```

Credentials come from `A:\trading-desk\.env` (gitignored), loaded by
`shared/config.py` for scheduled/unattended runs. `ALPACA_PAPER=true` is
the default; the sneaky_pivot account uses `SNEAKY_PIVOT_ALPACA_API_KEY`
/ `SNEAKY_PIVOT_ALPACA_SECRET_KEY`.

## Design principles

1. **Separation of concerns** — an agent that decides *what* to do never
   also has the authority to *do it*. Only `execution/` talks to Alpaca
   for order placement; the MCP server inherits the same boundary.
2. **No silent trust of unreliable input** — analyst output is
   quarantined from the trading pipeline until a human promotes it into a
   coded, backtested strategy.
3. **Same data path in backtest and live** — Alpaca's data API for both,
   and backtests reuse the exact production screener functions.
4. **Honest backtesting** — no lookahead bias, synthetic edge-case tests,
   and SPY buy-and-hold reported alongside every result.
5. **Fail loudly** — unknown signal values raise, missing qty/stop
   rejects, blocked trades are logged with reasons, every unattended run
   writes a durable JSONL record.
