# Trading Desk

A multi-agent personal trading system, replacing Ledger Desk with a fresh
build. Three agents, deliberately separated by responsibility:

| Agent | Role | Can it touch real/paper trades? |
|---|---|---|
| `rd-agent` | Screens/backtests quantitative strategies (swing/daily timeframe) | No — only produces signals |
| `day-trading-agent` | Intraday mean-reversion, PDT-aware, risk-gated | No — only produces signals |
| `execution-agent` | Places orders via Alpaca | Yes — this is the only agent that trades |
| `learning-agent` | Ingests news/YouTube, extracts research notes | No — output requires human review + promotion into a coded strategy before it can ever influence a trade |
| `shared/risk.py` | Not an agent — the risk rules enforced on any entry signal | Blocks, doesn't just warn |

## Status (as of this build)

- ✅ `shared/db.py` — SQLite schema (`signals`, `orders`, `backtest_runs`,
  `research_notes`) and helper functions. Tested locally.
- ✅ `shared/config.py` — env-var based config for Alpaca + Anthropic credentials.
- ✅ `rd-agent/screeners/mean_reversion.py` — z-score mean-reversion signal
  logic. Validated against synthetic data (correctly catches a clean
  dip-and-recovery; correctly produces losses on a mismatched-period sine
  wave, confirming no lookahead bias / no artificial inflation).
- ✅ `rd-agent/backtest/engine.py` — single-asset backtest engine (equity
  curve, Sharpe, max drawdown, win rate). Validated against synthetic data.
- ✅ `learning-agent/` — news + YouTube ingestion, Claude-based extraction,
  research note storage and review CLI. Logic validated where possible
  without network access (URL parsing, import wiring); the actual
  fetch/extract calls need to run in your environment with real credentials.
- ✅ `day-trading-agent/` — intraday mean-reversion screener with mandatory
  EOD flatten, PDT-aware and circuit-breaker-gated signal generation.
  Validated against synthetic data, including a real bug found and fixed
  during testing (see `day-trading-agent/SKILL.md`).
- ✅ `shared/risk.py` — PDT rule enforcement, daily circuit breaker,
  stop-loss-aware position sizing. Validated with deliberate failure tests
  (PDT breach, circuit breaker trip, invalid inputs).
- ✅ `rd-agent/data/alpaca_data.py` — Alpaca historical/intraday data client.
  Confirmed working against the real paper API as of 2026-07-20 (see
  `execution-agent/SKILL.md`) — `get_daily_bars` successfully pulled real
  AAPL bars.
- ✅ `execution-agent/` — order placement via Alpaca, defense-in-depth risk
  re-checking against LIVE account state (Alpaca's own PDT flag/day-trade
  count treated as authoritative). Tested with a mocked Alpaca client
  covering healthy/PDT-blocked/circuit-breaker-blocked/PDT-exempt cases.
  `smoke_test.py` (read-only) also confirmed against a real paper account
  on 2026-07-20, which caught and fixed a null-handling bug in
  `alpaca_client.get_account_snapshot` (see `execution-agent/SKILL.md`).
  The dry-run execution loop and `--live` order path are still untested
  against the real API. Defaults to dry-run; `--live` required to actually
  submit orders.
- 🔲 Portfolio-level backtesting (multiple tickers, capital allocation).
  Currently single-ticker only.
- ✅ `execution-agent/scripts/reconcile_orders.py` — polls Alpaca for
  unreconciled local orders and writes fill status/price back. Confirmed
  working against a real filled order (2026-07-20). Manual/on-demand only
  — not scheduled automatically.
- 🔲 Weight-based (vs fixed-qty) signal sizing — not supported by the
  execution agent yet; those signals are rejected rather than guessed at.

## Important limitation on this build

Everything above was originally built and validated in a sandboxed
environment with **no network access**. That means:
- Backtest/screener *logic* is genuinely tested (via synthetic data).
- Anything requiring live API calls (Alpaca, Anthropic, YouTube, news
  sites) was written against the documented APIs but had not been run
  against real endpoints as of the initial build.

Since then, `execution-agent/scripts/smoke_test.py`, the live execution
loop (dry-run and `--live`), and order reconciliation have all been run
successfully against a real Alpaca paper account (2026-07-20) — see
`execution-agent/SKILL.md`. Two real bugs were caught and fixed in the
process (a null `daytrade_count` on a fresh account, and a NaN-vs-None
signal bug that produced a false "sell" signal). Learning-agent's
Anthropic/news/YouTube calls are still untested against real endpoints.

## Performance

`rd-agent/scripts/generate_signal.py` exposes `generate_and_queue_batch()`,
used by `run_cycle.py` for watchlist scans. It fetches daily bars for the
whole watchlist in a single Alpaca call (`alpaca_data.get_daily_bars`
already accepts a symbol list) and fetches account/open-position state at
most once per cycle (lazily, so a cycle with zero buy signals never
touches those endpoints at all), instead of once per ticker. Benchmarked
2026-07-20 on the default 11-ticker watchlist: 6.60s (sequential
per-ticker calls) → 1.88s (batched), same results. Per-ticker fault
isolation was preserved (one ticker's exception is caught and recorded as
`status: error` without aborting the rest of the batch) and verified with
a simulated failure.

## Full autonomous mode

`scripts/run_cycle.py` chains rd-agent signal generation (across
`shared.config.WATCHLIST`) → live risk-gated execution → order
reconciliation into one unattended cycle, logging every run to
`data/cycle_log.jsonl` (JSONL, one line per run — this is the only
after-the-fact visibility, since nothing here waits for approval).

As of 2026-07-20, a Windows Task Scheduler task (`TradingDeskDailyCycle`)
runs `scripts/run_cycle.bat` Monday–Wednesday at 9:45 AM (adjusted same
day to exclude Thursday/Friday), submitting real (paper) orders with no
per-trade human approval. Manage it with:
```powershell
Get-ScheduledTask -TaskName TradingDeskDailyCycle       # check status
Disable-ScheduledTask -TaskName TradingDeskDailyCycle    # pause
Unregister-ScheduledTask -TaskName TradingDeskDailyCycle # remove
```
Credentials come from `A:\trading-desk\.env` (gitignored — copy
`.env.example` and fill in real values; never commit it).

**Known gap in this mode**: `rd-agent/screeners/mean_reversion.py`
recomputes its position purely from price history each run, with no
awareness of your actual Alpaca holdings. `generate_signal.py` guards
against the most acute risk (re-entering a ticker you already hold), but
if a real position stays open longer than the ~90-day/60-trading-day
lookback window fetched each run, the original entry bar could roll out
of that window before an exit condition ever fires — meaning the
strategy might silently never generate an exit for a real open position.
This is why every buy also carries a real broker-side stop (see below) —
it bounds the downside even if the strategy's own exit signal never
fires. Watch open positions periodically regardless; don't assume the
automation will always close them on its own initiative.

**Protective stops** (added 2026-07-20): every buy is now submitted with
a real stop-loss order placed directly with Alpaca (2xATR below entry,
see `execution-agent/SKILL.md`), not just a locally-computed number —
this protects the position continuously, independent of whether this
system's schedule ever fires again. The pre-existing NFLX position (open
before this existed) was retroactively protected the same way. This
bounds losses; it doesn't eliminate them — a large enough overnight gap
can still fill worse than the stop price.

## Setup

```bash
python -m venv venv
source venv/bin/activate   # or venv\Scripts\activate on Windows
pip install -r requirements.txt

export ALPACA_API_KEY=...
export ALPACA_SECRET_KEY=...
export ALPACA_PAPER=true
export ANTHROPIC_API_KEY=...

python -m shared.db   # initializes the database
```

## Design principles carried through this build
1. **Separation of concerns** — an agent that decides *what* to do never
   also has the authority to *do it* to a live account. Only
   `execution-agent` talks to Alpaca for order placement.
2. **No silent trust of unreliable input** — learning-agent output is
   quarantined from the trading pipeline until a human promotes it.
3. **Same data path in backtest and live** — using Alpaca's data API for
   both, wherever practical, to avoid backtest/live data mismatches.
4. **Honest backtesting** — no lookahead bias (signals use prior-day
   positions), synthetic edge-case tests included to catch bugs before
   they hide inside a plausible-looking equity curve.
