# Signals

Strategy research and signal generation — the merge of the old `rd-agent`
(swing/daily) and `day-trading-agent` (intraday) during the 2026-08-02
restructure. This package decides *what* to trade; it can never place an
order. Signals are written to the shared `signals` table and only
`execution/run_execution_loop.py` can turn them into trades.

## Strategies

### ⚠ Walk-forward result (2026-08-03) — read before tuning anything

`backtest/walk_forward.py` ran 16 quarterly folds over ~4 years of real
out-of-sample data (12-month train → 3-month test, rolling). Verdict:

| | Total | Annualized | Sharpe | Max DD |
|---|---|---|---|---|
| Walk-forward (re-tuned each fold) | +38.7% | +8.5% | 0.62 | -12.3% |
| Fixed live params | +49.5% | +10.5% | 0.74 | -14.0% |
| **SPY buy-and-hold** | **+76.1%** | **+15.4%** | **0.96** | -19.0% |

Three conclusions, all uncomfortable and all load-bearing:

1. **Re-tuning made it worse** (+38.7% vs +49.5% for never touching the
   parameters). The selection is fitting noise, and acting on it costs
   ~11 points. **Do not tune this strategy on backtest results.**
2. **The parameter instability is total**: 10 distinct winners across 16
   folds, changing in 11 of 15 consecutive folds. And the live set
   (`lb20/entry-1.5/exit0.0`) was the training winner in **0 of 16**
   folds — the 2026-07-28 decision that adopted it was in-sample
   selection on a single window, exactly the artifact this test exists
   to detect.
3. **It does not beat its benchmark**, on absolute return or Sharpe, and
   won only 6 of 16 quarters.

Fair caveats: the test window (2022-08 → 2026-07) was a strong bull
market, which structurally disadvantages mean reversion; the strategy
did lose less than SPY in the one down quarter, and carried a smaller
drawdown throughout; and the simulation pays 0% on cash while flat,
understating real returns by roughly 2-3%/yr at recent money-market
rates. None of that closes a 27-point gap.

### `rd_mean_reversion` — daily z-score mean reversion (live, default account)
- `screeners/mean_reversion.py` + `generate_and_queue_batch()` in
  `generate_signal.py`. Runs in the scheduled daily cycle
  (`scripts/run_cycle.py`, Windows task `TradingDeskDailyCycle`).
- NOT risk-gated at generation time — first risk check happens at
  execution time (defense-in-depth; see `execution/SKILL.md`).
- Params tuned 2026-07-28 from the first real 2-year backtest: entry_z
  moved -2.0 → -1.5 (better return/sharpe/win-rate, slightly deeper
  drawdown). Even tuned, it trailed SPY buy-and-hold over that window —
  an improvement, not proof of edge.
- Known gap: the screener recomputes position state purely from price
  history each run. Real position state always wins (already-held tickers
  are skipped; exits use real held qty), but if a position outlives the
  ~90-day lookback window, the strategy may never generate its exit —
  the broker-side 2xATR stop is what bounds that risk.

### `sneaky_pivot` — DISABLED 2026-08-03 (code retained, inert)

**Do not re-enable without reading this.** Killed after exactly one live
session. Two failures, one fatal:

1. **It opened two naked shorts** (MSFT -25, NOK -1119) in a long-only
   system, by re-issuing an exit against a stale local fill record. The
   underlying oversell bug is fixed in three layers (see below), but it
   only surfaced because this strategy exits on a 15-minute cadence.
2. **It was never validated.** Its own backtest returned -0.44%; it is a
   fixed-rule translation of a method its source presenter calls
   discretionary.

Disabled via `shared.config.SNEAKY_PIVOT_ENABLED` (default false), which
short-circuits `scripts/run_sneaky_pivot_cycle.py::run_cycle` — covering
the scheduled loop AND any manual `--live` run — and makes the loop skip
the step entirely. **Its dedicated paper account was closed by the user
the same day**, so re-enabling needs a new account plus new
`SNEAKY_PIVOT_*` credentials, and `KNOWN_ACCOUNTS` / `account_for_strategy`
would have to be pointed back at it.

Original description follows, for whoever revisits it:

### `sneaky_pivot` — intraday support/resistance breakout
- `screeners/sneaky_pivot.py` + `generate_and_queue_sneaky_pivot_signal()`.
  Cycle entry point: `scripts/run_sneaky_pivot_cycle.py` (dry-run by
  default; `--live` to submit).
- A fixed-rule translation of an explicitly discretionary YouTube
  strategy (2026-07-27) — treat results skeptically; the source material
  itself warns against mechanical use. Long-only (execution has no short
  support).
- Risk-gated at generation time: PDT + circuit-breaker checks run before
  a signal is even written. Exits are never blocked.
- Runs against its own Alpaca paper account (`SNEAKY_PIVOT_*` env vars)
  since 2026-07-28, isolating its PDT count and circuit breaker from the
  swing account.
- Hard rules: EOD force-flatten 5 min before close; no new entries within
  30 min of close; only the most recently completed bar can trigger an
  entry (a real stale-signal bug was caught here — a full-history rescan
  once resurrected an already-invalidated breakout).

## Backtests (`backtest/`)
- `engine.py` — simple single-asset engine (equity curve, Sharpe, max DD).
- `backtest_mean_reversion.py` — 2y real-data variant comparison vs SPY;
  results logged to `backtest_runs`.
- `backtest_sneaky_pivot.py` — bar-by-bar replay using the actual
  production `compute_levels`/`evaluate_today` functions, polled exactly
  like the live cycle polls them.

## Bug history worth remembering
- **NaN vs None (original build)**: pandas silently converts `None` to
  `NaN` under string dtype inference. A check written as `x is None`
  missed `NaN`, letting a "no signal" bar fall through to a default
  `action="sell"` without passing risk checks. Fixed with `pd.isna()`
  everywhere signals are checked, plus a known-signal allowlist that
  raises loudly on anything unrecognized.
- **qty=None on exits (2026-07-27/28)**: exit signals generated without a
  real held quantity are unconditionally rejected by the execution loop —
  a real NFLX exit was silently rejected this way and the position stayed
  open. Both strategies now pass real held qty on every exit.
- **Dry-run leakage (2026-07-29)**: a dry-run evaluation used to leave
  signals `pending`, where a separately scheduled always-live cycle could
  (and did) execute one. Dry-run cycles now expire what they evaluate —
  see `scripts/run_sneaky_pivot_cycle.py`.
- **Oversell → naked short (2026-08-03, twice)**: `reconcile()` runs at
  the END of a cycle, so an order submitted seconds earlier is still
  `accepted`, not `filled` — its fill lands one cycle late. The next
  cycle read its own stale ledger, believed the position was still open,
  and sold again, opening a short with no stop and no code path that
  would ever close it. **The broker's position is authoritative; never
  decide "do I hold this?" from local fill records alone.** Fixed at
  three layers: the cycle takes `min(ledger, broker position)`, the EOD
  flatten does the same, and — most importantly —
  `run_execution_loop.py` now refuses ANY sell exceeding the real
  holding, for every strategy present or future.

## Removed in the restructure
`intraday_mean_reversion` (the original day-trading strategy) was deleted
2026-08-02: never wired into any cycle, placeholder flat-1% stop, only
synthetic-data tested. It's in git history if ever wanted again.
