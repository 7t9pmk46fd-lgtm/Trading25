---
name: trading-desk
description: Operating manual for the trading desk at A:\trading-desk. Use at the start of ANY session touching this project — trading, strategy, dashboard, reports, or repo work. Encodes the safety rules (paper-only, no GitHub push without command), the sync workflow (iCloud mirror), the automation roster, and where to look first (dashboard attention panel).
---

# Trading Desk — operating manual

An autonomous Alpaca PAPER trading system. Three packages: `signals/`
(strategy screeners + backtests + signal generation), `execution/` (the
ONLY code that places orders), `analyst/` (research ingestion, daily
review PDF, dashboard), over `shared/` (config, SQLite ledger, risk
rules, market data). `mcp_server.py` exposes read-only/simulation MCP
tools. **One** Alpaca paper account: `default`, running the swing
mean-reversion strategy. Credentials in gitignored `.env`.

One live strategy: `rd_mean_reversion` (swing, daily bars), scanning an
index-scale universe (S&P 500 + NASDAQ-100 + Dow 30 union, ~522 tradeable
names as of 2026-08-04, see `data/universe.json` / `scripts/build_universe.py`),
ranked by z-score so the strongest signals fill the position cap first
(not ticker-alphabetical). Max 20 concurrent positions at 5% of equity
each. Scans every weekday (widened from Mon-Wed 2026-08-06). **Long-only**
— short selling was tested and rejected (-22.4% short sleeve); see
`signals/SKILL.md`.

An intraday strategy was removed entirely on 2026-08-03 along with its
paper account. Do not reintroduce it or its scaffolding unless the user
asks. Its code is recoverable from git commit `036d8ce`.

## Check first, before touching anything trading-related

1. Dashboard attention panel: GET http://127.0.0.1:8787/api/state
   (`attention` key) — or the page itself. Every item there encodes a
   real past failure (stuck pending signals, unprotected positions,
   DAY-TIF/wedged stops, silent loop, unreconciled orders).
2. `git status` — the working tree should be clean between tasks.
3. If the market is open, remember the loop is LIVE: unattended paper
   orders fire from `scripts/run_trading_day.py`.

## Hard rules (non-negotiable)

- **Paper only.** Never set `ALPACA_PAPER=false`, never weaken the
  paper guard in `run_trading_day.py`. Autonomy is scoped to paper.
- **Never push to GitHub unless the user explicitly commands it.**
  Local commits are expected after each change; the remote
  (https://github.com/7t9pmk46fd-lgtm/Trading25) is the user's
  known-good revert baseline, not a live mirror.
- **Mirror to iCloud after any file change**: run
  `scripts/mirror_to_icloud.bat` (a 15-min scheduled task also runs it).
- **Never run anything with `--live` manually**, and never "test" any
  code path that can reach `process_pending_signals` or order
  submission against the real API — verify assumptions via the DB and
  read-only endpoints first. (A test EOD flatten once cancelled a real
  protective stop and left a pending sell primed to fire at open.)
- **Strategy changes require human review.** R&D tasks file proposals
  into `research_notes`; nothing auto-applies. Promotion path:
  `analyst/review_notes.py` → user approval → code change → backtest.
- **Exits are never risk-blocked; every buy needs qty + stop_price**
  (execution rejects otherwise). Don't change these invariants.
- **The broker's position is the truth about what is held.** Never decide
  "do I still hold this?" from local fill records — reconcile lags a
  cycle, and acting on a stale ledger opened two naked shorts on
  2026-08-03. `run_execution_loop.py` now hard-refuses any sell larger
  than the real holding; never weaken that guard.
- **This system is long-only.** It has no code path that closes a short.
  If one appears, it's a bug — surface it to the user immediately; they
  must close it manually (Claude never places orders).
- **Backtest results are not evidence for a parameter change.** Walk-
  forward showed re-tuning underperforms leaving parameters alone. A new
  *hypothesis* needs its own walk-forward written before it goes live.

## Automation roster (don't double-run these by hand)

| Task | When | What |
|---|---|---|
| `TradingDeskMarketLoop` (Windows) | weekdays 8:20 AM CT | full-session loop: swing scan once/day (every weekday after 9:45 ET) + trail_stops every 15 min; paper guard + instance lock. Skips a swing scan already logged today, so a mid-session restart is safe |
| `TradingDeskDashboard` (Windows) | at logon | read-only dashboard :8787 |
| `TradingDeskICloudMirror` (Windows) | every 15 min | robocopy /MIR to iCloud |
| `trading-desk-daily-review` (Claude) | weekdays 3:15 PM CT | gather → narrative → PDF in Reports/ |
| `trading-desk-nightly-rd` (Claude) | weekdays 5:30 PM CT | operational health check. **Barred from proposing parameter changes** — walk-forward proved that inference is noise |
| `trading-desk-weekly-rd` (Claude) | Sat 10 AM CT | weekly synthesis report |

## Where things live

- Ledger: `data/trading.db` (signals, orders, backtest_runs,
  research_notes, account_baselines). Strategy names in the DB
  (`rd_mean_reversion`) are load-bearing — never rename. Historical rows
  reference strategies that no longer exist; that is expected.
- Logs (JSONL, gitignored): `data/trading_day_log.jsonl` (loop),
  `cycle_log`, `trail_stop_log`, `rd_nightly_log`.
- Reports: `Reports/` (gitignored). Dashboard serves them at /reports/.
- Entry points live in `scripts/`; keep `.bat` paths stable — Task
  Scheduler references them.
- Machine is Central Time; market hours are ET. All market-hours logic
  must use `execution.alpaca_client.get_market_clock()`, never local
  weekday math.

## MCP

`.mcp.json` registers `trading-desk` (stdio, `mcp_server.py`): 8
read-only/simulation tools (portfolio_status, run_screener, risk_check,
backtest, …). The server must never gain an order-placing or
signal-writing tool — that boundary is the design's core principle.
