---
name: trading-desk
description: Operating manual for the trading desk at A:\trading-desk. Use at the start of ANY session touching this project — trading, strategy, dashboard, reports, or repo work. Encodes the safety rules (paper-only, no GitHub push without command), the sync workflow (iCloud mirror), the automation roster, and where to look first (dashboard attention panel).
---

# Trading Desk — operating manual

Autonomous Alpaca **PAPER** trading system. Packages: `signals/`
(screeners, backtests, signal generation — never places orders),
`execution/` (the ONLY code that places orders), `analyst/` (research
ingestion, daily review PDF, dashboard), `shared/` (config, SQLite
ledger, risk rules, market data). `mcp_server.py` gives Claude 8
read-only/simulation MCP tools. One Alpaca account: `default`.
Credentials in gitignored `.env`.

Live strategy: `rd_mean_reversion` (swing, daily bars) over an
index-scale universe (~522 names, S&P 500 + NASDAQ-100 + Dow 30 union,
`data/universe.json` / `scripts/build_universe.py`), ranked by z-score
so the strongest signals fill the 20-position cap first (not
alphabetical). 5% of equity per position. Scans every weekday.
**Long-only, no margin** — see hard rules.

An intraday strategy (`sneaky_pivot`) was removed entirely along with
its paper account (git commit `036d8ce` if ever revisited). Do not
reintroduce it unless asked.

## Check first, every session

1. Dashboard attention panel — `GET http://127.0.0.1:8787/api/state`
   (`attention` key) or the page itself. Dashboard is manual-start:
   `venv/Scripts/python analyst/dashboard.py` (its logon auto-start is
   disabled, not deleted — don't re-enable without asking).
2. `git status` — should be clean between tasks.
3. If the market is open, the loop is LIVE — unattended paper orders
   fire from `scripts/run_trading_day.py`.

## Hard rules (non-negotiable)

- **Paper only.** Never set `ALPACA_PAPER=false`; never weaken the
  paper guard in `run_trading_day.py`.
- **Never push to GitHub unless explicitly told to.** Local commits
  after each change are expected; the remote
  (github.com/7t9pmk46fd-lgtm/Trading25) is the revert baseline.
- **Mirror to iCloud after any file change**: `scripts/mirror_to_icloud.bat`.
  No scheduled task does this anymore (user deleted it) — there is no
  safety net if you forget.
- **Never run anything with `--live` manually, and never exercise
  `process_pending_signals` or order submission against the real API to
  "test" something.** Verify via the DB and read-only endpoints first.
- **Strategy changes require human review.** R&D tasks file proposals
  into `research_notes`; nothing auto-applies.
- **Every buy needs qty + stop_price; exits are never risk-blocked.**
  Execution rejects otherwise — don't change these invariants.
- **The broker's position is the truth about what is held.** Never
  infer "do I hold this?" from local fill records — reconcile lags a
  cycle. The oversell guard (execution refuses any sell larger than the
  real holding) exists because of a real incident; never weaken it.
- **Long-only.** No code path closes a short. If one appears, it's a
  bug — tell the user immediately; only they place orders.
- **No margin.** `shared.risk.check_cash_floor` refuses any buy that
  would push cash below $0. Don't weaken without the user explicitly
  re-opening the question (margin-call awareness and interest tracking
  are still missing regardless).
- **Backtest results alone are never evidence for a strategy change.**
  Re-tuning existing parameters and a trend-filter hypothesis were both
  tested and rejected via walk-forward (`signals/SKILL.md`) — a new
  hypothesis needs its own walk-forward before it goes live.
- **Buys use a plain order + standalone GTC stop, not Alpaca's OTO
  bracket** (retired — the bracket's stop leg couldn't have its
  time-in-force converted, a recurring source of unprotected
  positions). Don't reintroduce `submit_market_order_with_stop`.
- **Run `pytest tests/` after touching `shared/risk.py`,
  `execution/run_execution_loop.py`, or the mean-reversion screener.**
  Every test pins a specific past production bug.

## Automation roster

| Task | When | What |
|---|---|---|
| `TradingDeskMarketLoop` (Windows) | weekdays 8:20 AM CT | swing scan once/day (after 9:45 ET) + trail_stops every 15 min. Paper guard + instance lock; skips a scan already logged today, so a mid-session restart is safe |
| `trading-desk-daily-review` (Claude) | weekdays 3:15 PM CT | gather → narrative → PDF in `Reports/` |
| `trading-desk-nightly-rd` (Claude) | weekdays 5:30 PM CT | operational health check only — **never proposes parameter changes** |
| `trading-desk-weekly-rd` (Claude) | Sat 10 AM CT | weekly synthesis report |

## Where things live

- Ledger: `data/trading.db` (signals, orders, backtest_runs,
  research_notes, account_baselines). Strategy name `rd_mean_reversion`
  is load-bearing — never rename. Rows referencing removed strategies
  are expected.
- Logs (JSONL, gitignored): `data/trading_day_log.jsonl` (loop),
  `cycle_log`, `trail_stop_log`, `rd_nightly_log`.
- Reports: `Reports/` (gitignored, served at `/reports/` by the dashboard).
- Entry points in `scripts/` — keep `.bat` paths stable, Task Scheduler
  references them directly.
- Machine is Central Time; market hours are ET. Always use
  `execution.alpaca_client.get_market_clock()` for market-open logic,
  never local weekday math.

## MCP

`.mcp.json` registers `trading-desk` (stdio, `mcp_server.py`): 8
read-only/simulation tools (portfolio_status, run_screener, risk_check,
backtest, …). Must never gain an order-placing or signal-writing tool —
that boundary is the design's core principle.
