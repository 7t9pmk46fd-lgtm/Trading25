# Execution Agent

## Purpose
The only agent that talks to Alpaca for order placement. Consumes
`pending` signals from the shared `signals` table, re-validates risk
against LIVE account state, and places (paper) orders.

## Run this first
```bash
export ALPACA_API_KEY=...
export ALPACA_SECRET_KEY=...
export ALPACA_PAPER=true
pip install -r ../requirements.txt
python scripts/smoke_test.py
```
This only reads data — no orders placed. Confirms your keys work and
pulls real Alpaca account info + sample historical data before you trust
anything else.

## Accounts

`alpaca_client.py`'s functions all take an `account: str = "default"`
param, `shared.config.KNOWN_ACCOUNTS` lists every live account, and
`shared.config.account_for_strategy(strategy)` decides which account a
strategy's signals route through. One account today, so everything maps
to `default`; the plumbing stays so a future strategy can be isolated on
its own account (separate PDT day-trade count and circuit breaker)
without touching every call site.

A second account existed 2026-07-28 → 2026-08-03 for an intraday
strategy that has since been removed. Worth keeping in mind if one is
ever added again: **positions in separate accounts are invisible to each
other**, so nothing stops two strategies independently holding the same
ticker, and an Alpaca order id is only valid against the account it was
submitted to (`reconcile_orders.py` resolves that per order).

## Running the execution loop
```bash
python scripts/run_execution_loop.py            # DRY RUN (default) — logs what would happen, places nothing
python scripts/run_execution_loop.py --live      # actually submits orders (paper account, per ALPACA_PAPER)
```
Always run dry-run first and read the output. `--live` still only hits
paper trading unless you've explicitly set `ALPACA_PAPER=false`.

## Risk checks — defense in depth
Every signal is re-checked here, even ones a strategy already checked at
creation time:
- Alpaca's own `daytrade_count` and `pattern_day_trader` flag are treated
  as authoritative (Alpaca sees all account activity, not just what this
  system generates).
- The local PDT/circuit-breaker checks in `shared/risk.py` run as a second
  layer.
- Today's P&L is computed from Alpaca's own `equity - last_equity` — a
  live, real number, not a local estimate.
- A signal with no `qty` set is rejected rather than guessed at. Weight-
  based signals (target portfolio %) aren't supported yet — sizing logic
  for those needs to be added before they can execute.
- **A `buy` signal with no `stop_price` set is rejected**, same as a
  missing `qty` — every buy must carry a real protective stop, not just a
  sizing assumption (see "Protective stops" below).

## Protective stops (2026-07-20, corrected 2026-07-27)
Buys are submitted via `alpaca_client.submit_market_order_with_stop()` as
an Alpaca OTO (one-triggers-other) order: the market buy, plus a
stop-loss sell leg that arms automatically once the buy fills.

**Correction**: this used to claim the stop "protects the position
continuously... including on days this system doesn't run at all." That's
false and was never actually verified — confirmed false for real on
2026-07-27 when a live AMD stop expired exactly at market close.
Alpaca's OTO `StopLossRequest` has no `time_in_force` field, so the child
stop leg silently inherits the parent buy order's `DAY` TIF and expires
every day at close. **`scripts/trail_stops.py` is what actually makes
protection durable now**: on a 15-min cron, it force-converts any
DAY-TIF stop to GTC via `alpaca_client.replace_stop_order()` (which does
support setting TIF), independent of whether price movement alone would
trigger a raise, and places a fresh GTC stop via `place_protective_stop()`
on any position it finds with no stop at all. A position is only durably
protected once trail_stops.py has run at least once since it was bought —
a bounded gap (at most one poll interval), not the "always protected"
claim this section used to make.

- `stop_price` is computed by `signals/generate_signal.py` as
  `entry_price - 2 * ATR(14)` (`shared/risk.py::compute_atr`), replacing
  the old flat 1%-below-entry placeholder. A flat 1% is often smaller than
  a stock's normal daily noise and would get stopped out by routine
  volatility, not an actual thesis failure -- 2xATR scales the stop to
  each ticker's own volatility. If ATR can't be computed (insufficient
  history, degenerate zero-range data), the ticker is skipped for that
  cycle (`status: no_stop_data`) rather than falling back to a guessed
  distance.
- On exit, `run_execution_loop.py` calls
  `alpaca_client.cancel_open_orders_for_symbol()` *before* submitting the
  strategy-driven sell, so the standing stop doesn't linger pointed at a
  position that's about to be closed some other way.
- `alpaca_client.place_protective_stop()` is a standalone-stop variant
  (no OTO, just a stop order against an already-open position) for
  retroactively protecting a position that was bought before this existed.
  Used once, 2026-07-20, to protect the pre-existing 145-share NFLX
  position (stop placed at 2xATR below its real avg entry price).
- Confirmed end-to-end against the real paper API: OTO structure (buy +
  held stop leg that arms on fill), standalone retroactive stop, and the
  cancel-before-exit cleanup, all verified with real orders and cleaned
  up afterward.
- **Still no defense against a gap below the stop** (e.g. an overnight
  news-driven drop that opens well under the stop price) — Alpaca's stop
  order becomes a market order once triggered, so a large enough gap can
  still fill meaningfully worse than the stop price. This bounds losses,
  it does not eliminate them.

## Tested (mocked Alpaca client, since no network in the build environment)
- Healthy account: buy/sell signals execute (dry-run), missing-qty signal
  correctly rejected with a clear reason.
- Alpaca-reported `daytrade_count >= 3` on a non-PDT account: new entry
  correctly blocked.
- Circuit breaker: a real -3.09% daily P&L correctly blocks new entries.
- PDT-exempt account (equity ≥ $25k): day-trade-count check correctly
  skipped, only circuit breaker still applies.

## Confirmed against the real Alpaca paper API (2026-07-20)
`smoke_test.py` passed end-to-end against a live paper account: account
snapshot, open positions, and historical bars all fetched successfully.
One real bug turned up in the process — Alpaca returns `daytrade_count:
null` (not `0`) for an account with no trade history yet, and
`get_account_snapshot()` did `int(account.daytrade_count)` with no null
check, so the very first real call crashed. Fixed in `alpaca_client.py` to
`int(account.daytrade_count or 0)`, matching how `pattern_day_trader`
(also nullable) was already handled. Worth remembering: any other
`int()`/`float()` cast on an Alpaca field should assume it can come back
`null` on a fresh or inactive account, not just `0`.

The dry-run execution loop and `--live` order submission have *not* been
exercised against the real API yet — only the read-only smoke test has.
Run `run_execution_loop.py` (no `--live`) next and read its output before
trusting the live path.

## Order reconciliation
`scripts/reconcile_orders.py` polls Alpaca for every local order that
isn't marked filled yet and writes status/filled_at/fill_price back to the
`orders` table. Confirmed working 2026-07-20 against a real filled NFLX
order. Called automatically at the end of `scripts/run_cycle.py` and once
more by the market loop after the close — no longer purely manual, though
nothing runs it standalone on its own schedule.

**Timing trap worth knowing** (it caused two naked shorts on 2026-08-03):
reconcile runs at the END of a cycle, and an order submitted seconds
earlier is usually still `accepted`, not `filled` — so its fill is not
recorded locally until the NEXT cycle. Never treat the local ledger as
the truth about what is currently held; ask the broker.

## Trailing stops (2026-07-27)
`scripts/trail_stops.py` — runs independently of any strategy, on every
held position regardless of which strategy opened it. Each cycle: raises
a position's stop toward `current_price - 2*ATR` if that's higher than
its current stop (never lowers one), places an initial GTC stop on any
position found with none, and force-converts any DAY-TIF stop to GTC
(see "Protective stops" above for why that last part is load-bearing, not
optional). Uses `alpaca_client.get_open_stop_orders()` and
`replace_stop_order()`, both added the same day.

A real duplicate-order bug was found and fixed here 2026-07-27: when a
symbol has more than one open order (e.g. a replace that's still
mid-flight, stuck in `pending_replace`), naively keying by symbol picked
whichever order the API happened to return last — which could be the
stale one, causing every subsequent replace attempt to fail against an
order that was already being replaced. Fixed to prefer a stable order
status (`new`/`accepted`) over a transitional one.

A second, harder issue turned up the same day and is NOT fully fixable
from this side: a replace chain can get wedged in Alpaca's paper
environment (an ancestor order stuck in `pending_replace` for 5+ hours,
likely from submitting replaces faster than the paper simulator
reconciles them — reproduced during rapid manual testing, unlikely under
normal 15-min cadence). Once wedged, Alpaca rejects both further replaces
(`"order chain not fully replaced"`) AND cancellation of the stuck order
(`"original order pending replacement"`) — confirmed there is no client-
side recovery action available; it can only be Alpaca's backend
resolving it. `trail_stops.py` reports this as
`stop_replace_blocked_still_protected` and moves on. Important: the
position is NOT unprotected in this state — the last-known-good stop
order is still live and enforced by the broker, it just can't be raised
further until the chain clears.

## Known limitations
- No handling yet for partial fills, rejected orders from Alpaca's side
  (e.g. insufficient buying power), or market-closed submission attempts.
  Treat the current version as a first pass, not a finished system —
  exercise it carefully in dry-run and paper mode, and watch actual
  Alpaca dashboard behavior closely before increasing size or trust.
- Daily bars used for ATR (here and in signal generation) are cached on
  disk once per calendar day via `shared.market_data.get_daily_bars_cached`
  — added 2026-07-27 to stop a 15-minute cycle from re-fetching 40-70
  days of identical daily history every pass.
