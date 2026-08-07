# Trading Desk

A multi-agent personal trading system (Alpaca paper trading). Three
agents, deliberately separated by responsibility, over a shared core:

| Package | Role | Can it touch real/paper trades? |
|---|---|---|
| `signals/` | Strategy research: screeners, backtests, signal generation. Live strategy: swing `rd_mean_reversion` | No — only queues signals |
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
signals/       screeners/ (mean_reversion), backtest/ (engine, real-data backtests,
               walk_forward, short_side_test), generate_signal.py (queues signals)
execution/     alpaca_client.py (the only order-placing module), run_execution_loop.py,
               trail_stops.py, reconcile_orders.py, smoke_test.py
analyst/       ingest/ (news, youtube), extract.py (Claude), ingest_source.py,
               review_notes.py, daily_review.py (PDF report)
scripts/       run_trading_day.py (+ .bat, the market-hours loop), run_cycle.py
               (the swing cycle it calls), seed_account_baselines.py,
               mirror_to_icloud.bat, run_dashboard.bat
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

- ⚠️ **`rd_mean_reversion` does not beat its benchmark out of sample.**
  Walk-forward validation (`signals/backtest/walk_forward.py`, 16
  quarterly folds over 4 years, 2026-08-03): +49.5% with fixed live
  params vs **SPY +76.1%**, Sharpe 0.74 vs 0.96. Re-tuning each fold made
  it *worse* (+38.7%), and the live parameter set was never the training
  winner in any fold — the tuning it came from was in-sample selection.
  **Do not tune this strategy on backtest results**; that's the specific
  thing measured and found to be noise. It remains live on paper as a
  working pipeline, not as a demonstrated edge.
- ✅ Live on the `default` paper account via the scheduled market loop.
- 🗑️ An intraday strategy (`sneaky_pivot`) ran 2026-07-27 → 2026-08-03 and
  was **removed entirely**: never validated (-0.44% backtest), and it
  opened two naked shorts in its only live session via a stale-fill
  oversell bug. Recoverable from git (see `signals/SKILL.md`); the
  system-wide guard it prompted — **the execution loop refuses any sell
  exceeding the real broker-side holding** — is permanent.
- 🔲 Portfolio-level backtesting (multi-ticker capital allocation).
- 🔲 Weight-based signal sizing (execution rejects weight-only signals
  rather than guessing).

## Full autonomous mode (2026-08-02: continuous market-hours loop)

Windows Task Scheduler task `TradingDeskMarketLoop` starts
`scripts/run_trading_day.bat` every weekday at 8:20 AM CT (9:20 ET). The
loop (`scripts/run_trading_day.py`) then covers the whole session with no
per-trade human approval:

- waits for the open using **Alpaca's market clock** (holidays and early
  closes handled by the broker, not local weekday math);
- once per day at/after 9:45 ET, every weekday: swing mean-reversion scan
  → live execution → reconciliation. A restart mid-session won't repeat
  it — the loop checks its own log;
- every 15 minutes until close: `trail_stops` (stop ratcheting + DAY→GTC
  conversion);
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

The old one-shot `TradingDeskDailyCycle` task was removed in favor of the
loop; `scripts/run_cycle.py` is still the swing-cycle implementation the
loop calls. There is no end-of-day flatten step — the only live strategy
holds across sessions by design.

Two Claude scheduled tasks (run locally by Claude Code while the app is
open; a missed run fires on next launch) complete the unattended day:

- **trading-desk-daily-review** (weekdays 3:15 PM CT): runs
  `analyst/daily_review.py gather`, writes the narrative, builds the PDF
  in `Reports/`.
- **trading-desk-nightly-rd** (weekdays 5:30 PM CT): operational health
  check — loop uptime, unprotected positions, stuck signals. **Barred
  from proposing parameter changes**; walk-forward showed that inference
  is noise.
- **trading-desk-weekly-rd** (Saturdays 10 AM CT): synthesises the week,
  mines the logs and fills, writes
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
  capped at 5% of equity per position, with a hard ceiling of 20
  concurrent positions (`MAX_CONCURRENT_POSITIONS`).
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
the default. One Alpaca account (`default`); the per-account plumbing is
retained so a future strategy can be isolated on its own.

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
