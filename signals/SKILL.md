# Signals

Strategy research and signal generation — the merge of the old `rd-agent`
(swing/daily) and `day-trading-agent` (intraday) during the 2026-08-02
restructure. This package decides *what* to trade; it can never place an
order. Signals are written to the shared `signals` table and only
`execution/run_execution_loop.py` can turn them into trades.

## Strategies

### ⚠ Trend filter hypothesis tested and REJECTED (2026-08-10)

`backtest/trend_filter_test.py`. Rather than re-tune entry_z/exit_z again
(already proven to fit noise, below) or short (already proven to lose
money, below), this tested a genuinely different mechanism: only take
`enter_long` when price is at/above its own 100-day SMA -- buying a
short-term dip WITHIN a longer-term uptrend, not any statistically
oversold bar regardless of context. `MeanReversionParams.trend_filter_days`
implements it, default `None` (verified byte-identical to no filter via
`tests/test_mean_reversion.py::test_trend_filter_none_is_byte_identical`).
One SMA length (100 days) was fixed BEFORE running, by convention, not
selected by trying several and keeping the best -- that selection process
is exactly the trap the walk-forward result below already demonstrated.

Same live entry_z/exit_z/lookback in both arms, same 2022-08-04 to
2026-07-30 window, current 522-name universe:

| | Total | Sharpe | Max DD | Trades |
|---|---|---|---|---|
| Unfiltered (== live) | +26.5% | 0.75 | -8.2% | 13,724 |
| Trend-filtered (SMA100) | **+9.0%** | 0.77 | -2.7% | 5,276 |
| SPY buy-and-hold | +76.1% | 0.96 | — | — |

**Verdict: the filter does not improve on the unfiltered signal --
+9.0% vs +26.5%, roughly a third of the return, for a flat Sharpe. Do not
adopt.** It does cut max drawdown substantially (-2.7% vs -8.2%), which
is a real effect, not nothing -- but it comes from taking 62% fewer
trades, and the return given up is larger than the risk saved by the
usual risk-adjusted measures. Live default (`trend_filter_days=None`)
stays unchanged.

Caveat worth flagging explicitly: the unfiltered arm's +26.5% here does
NOT match the +49.5% `mean_reversion_fixed_oos` figure from the
2026-08-03 walk-forward, because this test ran on the CURRENT 522-name
universe (post index-scale expansion) while that figure used the
~57-name universe in place at the time -- same window, different
universe, not a contradiction. The A-vs-B comparison within this test
is still apples-to-apples (identical universe, identical window, only
the filter differs), which is what the verdict rests on. Read as a
second data point: even the broader universe with the current fixed
params (the unfiltered arm) trails SPY by more, not less, than the
original 57-name result did.

### ⚠ Short selling was tested and REJECTED (2026-08-03)

`backtest/short_side_test.py`. The screener has always supported shorts
symmetrically (`allow_short`, default False) — entering short when
z >= |entry_z|. Over 5 years and 57 tickers, with everything else held
identical:

| | Total | Annualized | Sharpe | Max DD |
|---|---|---|---|---|
| Long-only (production) | +32.4% | +5.8% | 0.62 | -9.8% |
| Long/short | +3.4% | +0.7% | 0.12 | -16.0% |
| **Short sleeve alone** | **-22.4%** | **-5.0%** | **-0.77** | -28.6% |

Adding shorts cost 29 points of return and half the Sharpe, while
holding short exposure 38% of all ticker-days. It is **not** an artifact
of one threshold — the short sleeve loses at every entry level tested
(z≥1.0 −23.2%, z≥1.5 −22.4%, z≥2.0 −16.9%, z≥2.5 −7.2%, z≥3.0 −0.7%),
with negative Sharpe throughout; the loss only shrinks toward zero
because the trade count does. And in 2022, the one down year in the
sample and the regime shorts exist for, the sleeve returned just +3.3%
while long-only lost only −3.7% anyway.

Mechanically this makes sense: shorting a name because it is 1.5σ above
its 20-day mean is shorting strength, and strength persisted for most of
this sample. **Do not enable `allow_short` on mean reversion.** A
different short thesis (trend/momentum-based, or regime-gated) is not
what was tested and is not ruled out — but it would need its own
strategy, its own walk-forward, and execution-side work that does not
exist: `alpaca_client` is buy-side only, protective stops assume a stop
*below* entry, and `run_execution_loop`'s oversell guard deliberately
blocks any sell beyond what is held.

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

### `rd_mean_reversion` — daily z-score mean reversion (the only live strategy)
- `screeners/mean_reversion.py` + `generate_and_queue_batch()` in
  `generate_signal.py`. Runs in the scheduled daily cycle
  (`scripts/run_cycle.py`, driven by the market loop in
  `scripts/run_trading_day.py`).
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

### Removed strategy: `sneaky_pivot` (deleted 2026-08-03)

An intraday support/resistance breakout strategy lived here from
2026-07-27 to 2026-08-03. It has been **deleted from the working tree**
at the user's direction -- the desk no longer follows that strategy or
format. It is fully recoverable from git if it is ever wanted as a
starting point for a future strategy:

```bash
git show 036d8ce:signals/screeners/sneaky_pivot.py
git show 036d8ce:signals/backtest/backtest_sneaky_pivot.py
git show 036d8ce:scripts/run_sneaky_pivot_cycle.py
```

Why it was dropped, so the mistakes aren't repeated:

- **Never validated.** It backtested at -0.44% and was a fixed-rule
  translation of a method its source presenter described as
  discretionary. It went live on "the logic looks reasonable."
- **It opened two naked shorts** in its only live session (MSFT -25,
  NOK -1119) by re-issuing exits against a stale local fill record.

Both failures are now guarded system-wide, independent of any strategy:
the execution loop refuses any sell exceeding the real broker-side
holding, and the walk-forward result above documents why backtest-based
tuning is not evidence. **Any future intraday strategy inherits two traps
this one found:** a screener's own end-of-day flatten cannot fire if its
cutoff falls after the session's last bar (the final 15-minute bar is
stamped 15:45 ET, so a 15:55 cutoff never triggers), and a dry-run that
leaves signals `pending` can be picked up later by a different live pass.

## Backtests (`backtest/`)
- `engine.py` — simple single-asset engine (equity curve, Sharpe, max DD).
- `backtest_mean_reversion.py` — 2y real-data variant comparison vs SPY;
  results logged to `backtest_runs`.
- `walk_forward.py` — rolling out-of-sample validation. **Run this before
  believing any parameter result.**
- `short_side_test.py` — long-only vs long/short comparison.

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
  open. Every exit must carry a real held qty.
- **Dry-run leakage (2026-07-29)**: a dry-run evaluation used to leave
  signals `pending`, where a separately scheduled always-live cycle could
  (and did) execute one. Any future dry-run path must expire what it
  evaluates rather than leaving it queued.
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
