# Signals

Strategy research and signal generation — the merge of the old `rd-agent`
(swing/daily) and `day-trading-agent` (intraday) during the 2026-08-02
restructure. This package decides *what* to trade; it can never place an
order. Signals are written to the shared `signals` table and only
`execution/run_execution_loop.py` can turn them into trades.

## Strategies

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

### `sneaky_pivot` — intraday support/resistance breakout (live, own account)
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

## Removed in the restructure
`intraday_mean_reversion` (the original day-trading strategy) was deleted
2026-08-02: never wired into any cycle, placeholder flat-1% stop, only
synthetic-data tested. It's in git history if ever wanted again.
