# Day-Trading Agent

## Your risk parameters (as configured)
- Account equity: currently ~$100k paper, **above the $25,000 PDT
  threshold** — the PDT day-trade limit does NOT currently apply
  (`is_pdt_account = account.equity >= 25_000`, checked fresh each cycle
  against Alpaca's real equity, not hardcoded). The line below only
  applies if equity ever drops under $25k.
  - Under $25,000: PDT rule applies (max 3 day trades / rolling 5 days)
- Daily loss circuit breaker: **2.5% of account equity**
- Max position size: **10% of equity per trade**, sized so a full stop-out
  risks ~1% of equity where possible (tight stops will often be capped
  below 1% risk by the 10% position ceiling — see README for why)
- Execution mode: **fully autonomous, paper trading only**

## Hard rules enforced in code (not just documented)
1. **PDT limit** — `shared/risk.py::check_pdt_allows_trade` blocks a new
   entry if 3 day trades already happened in the last 5 days. This is a
   FINRA rule; the code refuses rather than warns.
2. **Daily circuit breaker** — blocks new entries once today's P&L hits
   -2.5% of equity. **Exits are never blocked** by either rule — an open
   position must always be closeable.
3. **End-of-day flatten** — any open position is force-closed 5 minutes
   before market close regardless of strategy state. A 30-minute
   no-new-entries window before that prevents opening a position with no
   real time to play out.

## Components — two strategies at very different maturity levels

### `sneaky_pivot` — live, wired, real-money-shaped (still paper)
- `screeners/sneaky_pivot.py` + `scripts/run_sneaky_pivot_cycle.py`. Added
  2026-07-27 at the user's request, translating a YouTube day-trading
  tutorial into fixed rules — **explicitly NOT a faithful reproduction**,
  the source strategy is discretionary by the presenter's own admission.
  See the module docstring and `research_notes` id 1/2 for full caveats.
- **Actually wired end-to-end and live-tested against the real API**:
  generates signals AND executes them (`--live` submits real paper
  orders), reconciles fills, real ATR/level-based stops (not a
  placeholder), long-only (execution-agent has no short support).
- Cross-strategy safe: skips any ticker already held by another strategy
  (e.g. rd-agent mean-reversion) rather than stacking into or selling out
  from under it — tracked via `signals.strategy` joined against filled
  orders, not Alpaca's raw position.
- Runs on a 15-min cron. Daily bars (ATR/levels) are cached on disk
  per-day via `alpaca_data.get_daily_bars_cached` — added 2026-07-27 after
  finding this cycle was re-fetching 40-70 days of daily history from the
  API every 15 minutes for data that only changes once a day.

### `intraday_mean_reversion` — original build, still never wired live
- `screeners/intraday_mean_reversion.py` + the OTHER half of
  `scripts/generate_signal.py` (`generate_and_queue_signal`, distinct
  from sneaky_pivot's `generate_and_queue_sneaky_pivot_signal`). z-score
  mean reversion on intraday bars, reusing rd-agent's core math, plus EOD
  flatten logic.
- **Still not wired into any cycle script** — nothing currently calls
  `generate_and_queue_signal` outside of tests. Only tested on synthetic
  data, never against the real API.
- Stop-loss placement is still a flat-1%-below-entry placeholder (unlike
  sneaky_pivot's real ATR/level-based stops) — needs real testing before
  use, same caveat as when this was originally built.
- **Bug fixed 2026-07-27**: `generate_and_queue_signal` left `qty=None`
  on every exit signal (`exit_long`/`force_flatten_eod`) — `qty=None` on
  any signal, entry or exit, is unconditionally rejected by
  `run_execution_loop.py`. This function would have silently made every
  exit unexecutable the moment someone wired it into a real cycle. Added
  a `held_qty` parameter, same pattern already used in
  `generate_and_queue_sneaky_pivot_signal`.

## What's been tested against the real API (sneaky_pivot only)
- Full cycle (signal generation, live execution, reconciliation) run
  successfully multiple times against the real paper account, 2026-07-27.
- Cross-strategy skip logic confirmed correct against real held positions.
- A real stale-signal bug was caught and fixed before it could execute: an
  earlier version scanned a fixed historical bar window on every call
  instead of only the current bar, which would have resurrected an
  already-invalidated breakout as if it were live the first time this ran
  mid-session. See `screeners/sneaky_pivot.py` for the fix.

## What's been tested (synthetic data only — intraday_mean_reversion)
- EOD force-flatten correctly closes a position that never naturally
  reverted (confirmed: a losing position gets closed, not held overnight).
- No-new-entries window correctly blocks late-day entries.
- PDT check correctly blocks a 4th day trade within 5 days.
- Circuit breaker correctly blocks entries beyond the loss limit, and
  correctly allows losses within it.
- Exits/force-flattens correctly bypass both risk gates (verified even
  with a tripped circuit breaker).

## A bug found and fixed during the original build
Pandas silently converts `None` to `NaN` in a mixed-type column under its
string dtype inference. A signal check written as `x is None` missed
`NaN` values, which meant a "no signal" bar could fall through to a
default `action="sell"` branch **without going through the PDT/circuit-breaker
check at all** (that check only ran on `"enter_long"`). Fixed by using
`pd.isna()` everywhere signal values are checked, and by making
`generate_and_queue_signal` explicitly validate against a known-signal
allowlist and raise loudly on anything unrecognized, rather than silently
guessing an action. Worth knowing about since it's exactly the kind of bug
that stays invisible until a specific data shape triggers it in production.

## Known limitations / what's still needed
- **intraday_mean_reversion still isn't wired into a live cycle** and its
  stop-loss is still a flat-percentage placeholder — see above.
  sneaky_pivot doesn't have either problem.
- **P&L tracking is a placeholder.** `get_today_realized_pnl` in
  `shared/risk.py` returns 0.0 — the circuit breaker needs real P&L fed in
  from Alpaca's account/position data (both strategies share this gap).
- **PDT day-trade counting relies on local order records.** If you ever
  place trades manually through Alpaca's own app, this system's PDT
  counter won't see them and will under-count. Treat it as a safety net
  on top of Alpaca's own PDT enforcement, not a replacement for it.
- **A real, systemic stop-loss bug affects BOTH strategies' entries**:
  `execution-agent/alpaca_client.py::submit_market_order_with_stop`'s OTO
  stop-loss leg silently inherits the parent buy order's `DAY`
  time_in_force (confirmed via a real order that expired at market close,
  2026-07-27) — Alpaca's bracket-order API has no field to set the leg's
  TIF independently. `execution-agent/scripts/trail_stops.py` is what
  actually makes protection durable: every 15-min cycle it force-converts
  any DAY-TIF stop to GTC via a replace, independent of whether price
  movement alone would trigger a raise. This means a position bought and
  then market-closed before trail_stops.py gets even one cycle to run is
  briefly unprotected (a bounded window, not the unbounded one earlier
  documentation incorrectly assumed didn't exist) — see that function's
  docstring for the full mechanism.
- **sneaky_pivot has never traded live** — deployed mid-session on
  2026-07-27 (after its ~9:30-11:30am ET entry window had already
  elapsed for the day), so it's real-API-tested but has zero live entries
  or exits to its name yet. First real test is whatever session next
  covers a full market open.
