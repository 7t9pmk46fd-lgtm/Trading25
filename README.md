# Trading Desk

A multi-agent personal trading system (Alpaca paper trading). Three
agents, deliberately separated by responsibility, over a shared core:

| Package | Role | Can it touch real/paper trades? |
|---|---|---|
| `signals/` | Strategy research: screeners, backtests, signal generation. Live strategy: swing `rd_mean_reversion`. `signals/research/` holds quarantined hypotheses that are tested but not wired to anything live. | No — only queues signals |
| `execution/` | Places orders via Alpaca, re-validates every signal against live account state, protects every position with a trailing stop, keeps the local ledger in sync with what actually happened on the broker | Yes — the ONLY package that trades |
| `analyst/` | Ingests news/YouTube into research notes; builds the daily review PDF; serves the live dashboard | No — research output requires human review + a coded, backtested strategy before it can ever influence a trade; the dashboard is read-only by construction |
| `shared/` | Config, SQLite ledger, risk rules, market data client, sector/industry reference data | Risk rules block, don't just warn |
| `mcp_server.py` | MCP server exposing the desk to AI agents (see below) | No — analysis/simulation only, by hard design |

Restructured 2026-08-02 from the original five agent directories
(`rd-agent`, `day-trading-agent`, `execution-agent`, `learning-agent`) —
same code and strategy names, consolidated layout, no more `sys.path`
gymnastics. Older dates in docstrings refer to the old layout; git
history has the full story.

## Layout

```
shared/        config.py, db.py (SQLite ledger), risk.py (PDT/circuit-breaker/sizing),
               market_data.py (Alpaca bars + cache), benchmark.py (vs SPY),
               sector_data.py (sector/industry tags, concentration analysis --
               not wired into any live path)
signals/       screeners/ (mean_reversion), backtest/ (engine, real-data backtests,
               walk_forward, short_side_test), generate_signal.py (queues signals),
               research/ (quarantined hypotheses: scale_in.py -- unit-tested,
               zero imports from any live path, see signals/SKILL.md)
execution/     alpaca_client.py (the only order-placing module), run_execution_loop.py,
               trail_stops.py (stop ratcheting, orphan-stop cleanup, qty-drift
               correction), reconcile_orders.py (fill-status polling +
               discovers broker-side fills the local ledger never learned
               about), smoke_test.py
analyst/       ingest/ (news, youtube), extract.py (Claude), ingest_source.py,
               review_notes.py, daily_review.py (PDF report, supports backfilling
               a missed day from Alpaca's own portfolio history), dashboard.py
               (live read-only dashboard + go-live readiness meter, localhost:8787)
scripts/       run_trading_day.py (+ .bat, the market-hours loop), run_cycle.py
               (the swing cycle it calls), seed_account_baselines.py,
               mirror_to_icloud.bat, run_dashboard.bat, measure_coverage.py
               (risk-critical test coverage -> data/coverage_report.json),
               measure_sizing_viability.py (full-watchlist small-capital
               sizing check -> data/sizing_viability_report.json)
tests/         pytest suite, 237 tests -- see Status below
.github/       workflows/tests.yml: pytest + coverage measurement on every
               push/PR to main (no live credentials configured or needed)
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

## Dashboard & go-live readiness meter

```bash
venv/Scripts/python analyst/dashboard.py     # http://127.0.0.1:8787, manual-start only
```

Read-only by construction — no order placement, no signal writes, no
config changes anywhere in this file. Two things live here:

- **Attention panel** — derived anomaly checks (unprotected positions,
  DAY-TIF stops, a silent loop during market hours, unreconciled orders,
  pending R&D proposals). Each one is a failure mode this system has
  actually hit in production, not a hypothetical.
- **Readiness meter** — 14 evidence-based checks scoring *engineering*
  readiness for real money, explicitly **not** investment advice (the
  dashboard's own disclaimer says so). The single heaviest check is
  "beats benchmark out-of-sample" (30/105 weight) — a flawlessly engineered
  strategy with no edge still isn't worth funding. Other checks: live
  paper session count, position protection, session reliability, test
  coverage on risk-critical code specifically (not "do test files exist"),
  code stability (days since risk-critical code last changed), tail risk
  (walk-forward max drawdown), credential hygiene, small-capital sizing
  viability, and whether the weekly R&D synthesis is actually producing
  output (added 2026-08-19 after confirming it was scheduled and firing
  but never once completing).

Two of those checks read cached measurements rather than recomputing
live on every ~20s page poll (network/subprocess cost, and the numbers
don't change between test runs):

```bash
venv/Scripts/python scripts/measure_coverage.py           # test coverage
venv/Scripts/python scripts/measure_sizing_viability.py   # small-capital sizing (needs live credentials)
```

Neither runs automatically — rerun after a real code change if you want
the meter to reflect it.

## Status

- ⚠️ **`rd_mean_reversion` does not beat its benchmark out of sample.**
  Walk-forward validation (`signals/backtest/walk_forward.py`, 16
  quarterly folds over 4 years, 2026-08-03): +49.5% with fixed live
  params vs **SPY +76.1%**, Sharpe 0.74 vs 0.96. Re-tuning each fold made
  it *worse* (+38.7%), and the live parameter set was never the training
  winner in any fold — the tuning it came from was in-sample selection.
  **Do not tune this strategy on backtest results**; that's the specific
  thing measured and found to be noise. It remains live on paper as a
  working pipeline, not as a demonstrated edge. (Live paper performance
  since 2026-07-20 has tracked well ahead of SPY — a good stretch is not
  evidence against a 4-year walk-forward; see the readiness meter.)
- ✅ Live on the `default` paper account via the scheduled market loop.
- 🗑️ An intraday strategy (`sneaky_pivot`) ran 2026-07-27 → 2026-08-03 and
  was **removed entirely**: never validated (-0.44% backtest), and it
  opened two naked shorts in its only live session via a stale-fill
  oversell bug. Recoverable from git (see `signals/SKILL.md`); the
  system-wide guard it prompted — **the execution loop refuses any sell
  exceeding the real broker-side holding** — is permanent.
- ⛔ **A trend filter hypothesis was tested and rejected** (2026-08-10,
  `signals/backtest/trend_filter_test.py`): only entering within a
  100-day uptrend cut the return to a third of the unfiltered signal
  (+9.0% vs +26.5% over the same window/universe) for a flat Sharpe.
  Live default (`trend_filter_days=None`) unchanged.
- 🧪 **A scale-in (add-to-position) hypothesis is quarantined, not live**
  (`signals/research/scale_in.py`, 2026-08-25) — a pure decision function,
  unit-tested, zero imports from any live path. Not backtested or
  walk-forward validated. Exists to prove the logic works in isolation
  before any question of wiring it in.
- ✅ **This system does not use margin** — `shared.risk.check_cash_floor`
  (2026-08-10) refuses any buy that would draw cash negative. Added
  after confirming the live account was running -$4,467 of cash with
  nothing previously checking it.
- ✅ **Buys no longer use Alpaca's OTO bracket order** (2026-08-10) —
  retired after Alpaca stopped allowing the bracket stop leg's
  time-in-force to be converted at all. A buy now submits plain, polls
  for the fill, then places a standalone GTC stop from the start.
- ✅ **Trailing stops are volatility-scaled but bounded to 5-8% of price**
  (2026-08-26) — distance is still ATR-derived (each stock's own recent
  volatility, not a guess), but clamped into a 5-8% band rather than left
  to float freely. Confirmed live before the change: unclamped 2xATR
  ranged 4.3%-14.65% of price across real positions — some names were
  giving back far more unrealized gain than necessary before a stop would
  ever trigger. Never lowers a stop once raised, regardless of price
  action, and self-heals two other real failure modes every cycle: a
  stop left orphaned by a completed exit (2026-08-14 — risks opening a
  short if ever triggered against zero shares held) and a stop sized off
  a partial fill that never got topped up once the rest filled
  (2026-08-25, confirmed real on BA).
- ✅ **The local order ledger stays in sync with the broker** — a
  triggered protective stop used to leave no local record at all (it
  fills outside the path that normally writes one), silently corrupting
  realized P&L and causing backfilled reports to invent phantom
  positions. `execution/reconcile_orders.reconcile_missing_fills`
  (2026-08-17) detects and backfills any broker-side fill missing
  locally; runs automatically at the end of every trading session.
- ✅ **Automated test suite** (`tests/`, pytest, **237 tests**) — covers
  the exact bug classes that have hit this system in production: the
  oversell guard, NaN-vs-None signal handling, PDT/circuit-breaker
  gating, FIFO realized P&L, the cash floor, orphaned/undersized stops,
  ledger reconciliation, and the MCP server's tool surface. Test
  coverage on risk-critical modules specifically (not the whole repo,
  which includes plenty of code that can't be meaningfully unit-tested,
  like anything talking to the real Alpaca API) sits around 75%; tracked
  live on the dashboard readiness meter, not just at commit time.
  `.github/workflows/tests.yml` runs the full suite plus coverage
  measurement on every push/PR to `main`.
- ✅ **Real realized P&L attribution** (`shared.risk.get_today_realized_pnl`)
  — replaced a hardcoded placeholder with FIFO lot matching over the
  order ledger. Surfaced on the dashboard readiness meter, not wired into
  the circuit breaker (which correctly already uses Alpaca's live equity).
- ✅ **Sector/industry concentration data** (`shared/sector_data.py`,
  2026-08-25) — pulled via FMP for current holdings, cached to
  `data/sector_tags.json` (regenerated manually; no FMP key exists for
  the unattended loop). Confirmed a real concentration finding: Energy
  alone was 47.6% of total unrealized P&L across just 3 of 20 positions.
  Not wired into the dashboard or any live path yet.
- 🔲 Portfolio-level backtesting (multi-ticker capital allocation).
- 🔲 Weight-based signal sizing (execution rejects weight-only signals
  rather than guessing).
- 🔲 Margin-call awareness and interest-cost tracking — the cash floor
  above stops NEW margin usage, but doesn't yet monitor maintenance
  margin on positions already held or account for interest accrual.
- 🔲 Small-capital sizing is a known, quantified gap, not yet decided —
  at a $1,000 reference balance and the current 5%-of-equity position
  cap, only ~13% of the full watchlist prices low enough to buy even one
  share (`scripts/measure_sizing_viability.py`). Fix is a real tradeoff
  (raise the per-position cap vs. shrink the tradeable universe), not a
  bug with one obvious answer.

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
- **every hour** until close (`CYCLE_SECONDS` in `run_trading_day.py`;
  changed from 15 minutes on 2026-08-18): `trail_stops` — stop ratcheting
  within the 5-8% band above, DAY→GTC conversion, orphaned-stop cleanup,
  and stop-qty drift correction;
- after close: final reconciliation (`reconcile_orders.reconcile`, then
  `reconcile_missing_fills`), then exits until the next morning.

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

- **trading-desk-daily-review** (weekdays 3:16 PM CT): gathers the day's
  data, writes the narrative, builds the PDF in `Reports/`. Also runs the
  operational health check that used to be a separate nightly task (see
  below).
- **trading-desk-weekly-rd** (Saturdays 10:05 AM CT): synthesises the
  week, cross-checks entries and top holdings against an independent
  TradingView technical read (2026-08-19), writes
  `Reports/weekly_rd_<date>.md`, and files concrete improvement
  proposals as research notes. **Proposals only — it never edits
  strategy code or parameters**; promotion still goes through human
  review (`analyst/review_notes.py`), per design principle 2. Rewritten
  2026-08-19 after confirming it was firing on schedule but silently
  never completing — the fix was the same one that worked for
  daily-review: fewer, more mechanical, token-bounded steps.
- **trading-desk-nightly-rd** (weekdays 5:30 PM CT): re-enabled as of
  2026-08-25, intentionally — a deliberate second independent health
  check alongside daily-review's folded-in one, not a leftover. (Was
  disabled 2026-08-10 when its check was first folded into daily-review
  to cut token cost.) Barred from proposing parameter changes since the
  walk-forward showed that kind of inference is noise.

## Risk rules (enforced in code, not just documented)

- **PDT**: under $25k equity, max 3 day trades per rolling 5 days —
  blocks, doesn't warn. Alpaca's own `daytrade_count`/PDT flag treated as
  authoritative; the local counter is a second layer.
- **Daily circuit breaker**: new entries blocked once today's P&L hits
  -2.5% of equity. Exits are NEVER blocked by any risk rule.
- **Entry sizing**: positions sized so a full stop-out risks ~1% of
  equity, capped at 5% of equity per position, with a hard ceiling of 20
  concurrent positions (`MAX_CONCURRENT_POSITIONS`).
- **Trailing-stop distance**: ATR-derived, clamped to 5-8% of price
  (`execution/trail_stops.py`) — a separate mechanism from entry sizing
  above; only ever ratchets up, never down, regardless of price action.
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

Two more things worth doing after setup, not strictly required:

```bash
venv/Scripts/python analyst/dashboard.py   # dashboard + readiness meter, manual-start only
scripts/mirror_to_icloud.bat               # sync the whole repo to iCloud, no scheduled task does this automatically
```

## Design principles

1. **Separation of concerns** — an agent that decides *what* to do never
   also has the authority to *do it*. Only `execution/` talks to Alpaca
   for order placement; the MCP server inherits the same boundary.
2. **No silent trust of unreliable input** — analyst output is
   quarantined from the trading pipeline until a human promotes it into a
   coded, backtested strategy. Same boundary applies to `signals/research/`
   — quarantined until explicitly wired in, not just backtested.
3. **Same data path in backtest and live** — Alpaca's data API for both,
   and backtests reuse the exact production screener functions.
4. **Honest backtesting** — no lookahead bias, synthetic edge-case tests,
   and SPY buy-and-hold reported alongside every result.
5. **Fail loudly** — unknown signal values raise, missing qty/stop
   rejects, blocked trades are logged with reasons, every unattended run
   writes a durable JSONL record.
6. **Trust nothing that isn't checked** — the readiness meter and CI
   exist because "it's scheduled" turned out not to mean "it's running,"
   and "the code looks right" turned out not to mean "the tests cover
   it." Measure, don't assume.
