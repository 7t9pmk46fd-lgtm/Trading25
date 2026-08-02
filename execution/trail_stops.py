"""
Trailing-stop check: raises the protective stop on each open position as
its price rises, so unrealized gains get progressively locked in instead
of being fully exposed to a round-trip back to entry. Never lowers a
stop -- only ratchets up.

Intended to run frequently (e.g. every 15 min) alongside, not instead of,
the daily entry/execution cycle (run_cycle.py) -- this script never opens
or closes a position, it only tightens existing protection. A position
held with no stop order at all gets one placed here rather than left
unprotected, using the same 2xATR distance as a normal entry.

Usage:
    python scripts/trail_stops.py           # applies changes
    python scripts/trail_stops.py --dry-run # logs what it would do only
"""
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from execution import alpaca_client
from shared.market_data import get_daily_bars_cached
from shared.config import KNOWN_ACCOUNTS
from shared.risk import compute_atr

LOG_PATH = ROOT / "data" / "trail_stop_log.jsonl"
ATR_STOP_MULTIPLE = 2.0
MIN_STOP_INCREASE = 0.01  # skip replacing on sub-cent noise


def _log(entry: dict) -> None:
    entry["logged_at"] = datetime.now(timezone.utc).isoformat()
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(LOG_PATH, "a") as f:
        f.write(json.dumps(entry, default=str) + "\n")
    print(json.dumps(entry, default=str))


def check_and_trail(dry_run: bool = False, account: str = "default") -> list[dict]:
    positions = alpaca_client.get_open_positions(account=account)
    if not positions:
        return [{"status": "no_positions"}]

    open_stops = alpaca_client.get_open_stop_orders(account=account)

    symbols = [p["symbol"] for p in positions]
    bars_by_symbol = get_daily_bars_cached(
        symbols,
        start=datetime.now() - timedelta(days=40),
        end=datetime.now(),
    )

    results = []
    for pos in positions:
        symbol = pos["symbol"]
        outcome = {
            "ticker": symbol,
            "qty": pos["qty"],
            "current_price": pos["current_price"],
            "avg_entry_price": pos["avg_entry_price"],
            "unrealized_pl": pos["unrealized_pl"],
        }
        try:
            bars = bars_by_symbol.get(symbol)
            if bars is None or len(bars) < 15:
                outcome["status"] = "skipped_insufficient_bars"
                results.append(outcome)
                continue

            atr = compute_atr(bars)
            candidate_stop = pos["current_price"] - ATR_STOP_MULTIPLE * atr
            existing = open_stops.get(symbol)

            if existing is None:
                # Held position with no broker-side protection at all --
                # place one now rather than leave it exposed until the
                # next daily cycle.
                if candidate_stop <= 0:
                    outcome["status"] = "skipped_invalid_stop"
                    results.append(outcome)
                    continue
                if dry_run:
                    outcome["status"] = "dry_run_would_place_initial_stop"
                    outcome["stop_price"] = candidate_stop
                else:
                    order = alpaca_client.place_protective_stop(
                        symbol, pos["qty"], candidate_stop, account=account
                    )
                    outcome["status"] = "placed_initial_stop"
                    outcome["stop_price"] = order["stop_price"]
                    outcome["alpaca_order_id"] = order["alpaca_order_id"]
                results.append(outcome)
                continue

            if existing.get("status") not in ("new", "accepted"):
                # A replace is already in flight for this order (or some
                # other transitional state) -- attempting another replace
                # now would just fail against Alpaca. Next cycle will
                # pick up whatever it settles into.
                outcome["status"] = "skipped_order_still_settling"
                outcome["order_status"] = existing.get("status")
                results.append(outcome)
                continue

            new_stop = max(existing["stop_price"], candidate_stop)
            outcome["existing_stop"] = existing["stop_price"]
            needs_gtc_conversion = existing.get("time_in_force") == "day"

            if new_stop - existing["stop_price"] < MIN_STOP_INCREASE:
                if not needs_gtc_conversion:
                    outcome["status"] = "unchanged"
                    results.append(outcome)
                    continue
                # Price hasn't moved enough to justify a raise, but this
                # stop is still DAY-TIF (an OTO leg that's never been
                # touched by a replace) and will expire at market close
                # today if left alone -- force a same-price replace just
                # to convert it to GTC. See submit_market_order_with_stop's
                # docstring for why this matters.
                new_stop = existing["stop_price"]

            if new_stop >= pos["current_price"]:
                # Would violate Alpaca's stop-below-market requirement for
                # a sell stop -- can happen on a fast intraday spike vs.
                # a wide ATR band; skip this cycle rather than error.
                outcome["status"] = "skipped_stop_would_be_above_market"
                results.append(outcome)
                continue

            raised_price = new_stop > existing["stop_price"]
            if dry_run:
                outcome["status"] = "dry_run_would_raise_stop" if raised_price else "dry_run_would_convert_to_gtc"
                outcome["new_stop"] = new_stop
            else:
                try:
                    order = alpaca_client.replace_stop_order(
                        existing["alpaca_order_id"], new_stop, account=account
                    )
                    outcome["status"] = "raised_stop" if raised_price else "converted_to_gtc"
                    outcome["new_stop"] = order["stop_price"]
                except Exception as e:
                    # Confirmed real 2026-07-27: a replace chain can get
                    # wedged in Alpaca's paper environment (an ancestor
                    # order stuck in pending_replace for hours), which
                    # blocks replacing any descendant with "order chain
                    # not fully replaced". Cancelling doesn't help either
                    # -- confirmed the cancel itself fails the same way
                    # ("original order pending replacement"), so there is
                    # no recovery action available from this side; it can
                    # only be Alpaca's backend resolving the chain on its
                    # own. IMPORTANT: the position is NOT unprotected in
                    # this state -- existing["alpaca_order_id"] is still a
                    # live, working stop at its last-known price, it just
                    # can't be raised or replaced until the chain clears.
                    # Report clearly and move on; next cycle retries.
                    outcome["status"] = "stop_replace_blocked_still_protected"
                    outcome["existing_stop_still_active"] = existing["stop_price"]
                    outcome["error"] = f"{type(e).__name__}: {e}"

        except Exception as e:
            outcome["status"] = "error"
            outcome["error"] = f"{type(e).__name__}: {e}"

        results.append(outcome)

    return results


def main():
    dry_run = "--dry-run" in sys.argv
    # Runs against every known account in one invocation (rather than
    # needing a separate cron per account) so Sneaky Pivot's positions get
    # the same stop-raising/DAY-to-GTC protection as rd-agent's, once it
    # starts trading live in its own account (added 2026-07-28).
    results_by_account = {
        account: check_and_trail(dry_run=dry_run, account=account)
        for account in KNOWN_ACCOUNTS
    }
    _log({"dry_run": dry_run, "results": results_by_account})


if __name__ == "__main__":
    main()
