"""
Execution loop: the only place pending signals become real (paper) orders.

Design principle -- defense in depth: every signal gets risk-checked AGAIN
here, even though day-trading-agent already checked before writing the
signal. Two reasons:
  1. rd-agent's signals were never risk-checked at all (that gating was
     only added for day-trading-agent) -- this is the first and only
     checkpoint for those.
  2. Time passes between signal creation and execution. Account equity,
     today's P&L, and the day-trade count can all have changed. Re-check
     against CURRENT state, not the state at signal-creation time.

Alpaca's own `pattern_day_trader` flag and `daytrade_count` are treated as
authoritative for PDT status (Alpaca sees everything on the account,
including trades placed outside this system). The local shared/risk.py
counter is a supplementary check.

A signal that fails a risk check is marked 'rejected' with a reason, not
silently dropped -- so you can see what got blocked and why.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from execution import alpaca_client
from shared import db, risk
from shared.config import account_for_strategy, SNEAKY_PIVOT_STRATEGIES


def process_pending_signals(dry_run: bool = True) -> list[dict]:
    """
    dry_run=True (default): evaluates every risk check and logs what WOULD
    happen, but never calls alpaca_client.submit_market_order. Flip to
    False only once you've verified dry-run output looks right.

    Returns a list of result dicts, one per signal processed.

    Each pending signal is routed to its own Alpaca account based on
    account_for_strategy(sig["strategy"]) -- account snapshots (and
    therefore risk limits) are fetched and cached per account, not once
    globally, since Sneaky Pivot got its own paper account on 2026-07-28
    specifically so its equity/PDT/circuit-breaker state stays isolated
    from rd-agent's.
    """
    account_cache: dict[str, alpaca_client.AccountSnapshot] = {}

    def _account(name: str) -> alpaca_client.AccountSnapshot:
        if name not in account_cache:
            account_cache[name] = alpaca_client.get_account_snapshot(account=name)
        return account_cache[name]

    results = []

    with db.db_session() as conn:
        pending = db.get_pending_signals(conn)

        for sig in pending:
            outcome = {"signal_id": sig["id"], "ticker": sig["ticker"], "action": sig["action"]}
            acct = account_for_strategy(sig["strategy"])
            day_trade_scope = (
                {"strategies": list(SNEAKY_PIVOT_STRATEGIES)} if acct == "sneaky_pivot"
                else {"exclude_strategies": list(SNEAKY_PIVOT_STRATEGIES)}
            )

            try:
                account = _account(acct)
                is_pdt_account = account.equity >= 25_000
                limits = risk.RiskLimits(
                    account_equity=account.equity,
                    is_pdt_account=is_pdt_account,
                )

                if sig["action"] == "buy":
                    # Cross-check against Alpaca's OWN day-trade counter,
                    # not just the local one.
                    if not is_pdt_account and account.daytrade_count >= 3:
                        raise risk.RiskViolation(
                            f"Alpaca reports daytrade_count={account.daytrade_count} "
                            f"(>= 3) for a non-PDT account -- blocking new entry."
                        )
                    risk.check_pdt_allows_trade(conn, limits, **day_trade_scope)
                    risk.check_circuit_breaker(conn, limits, account.today_pnl)

                qty = sig["qty"]
                if qty is None:
                    raise risk.RiskViolation(
                        f"Signal #{sig['id']} has no qty set -- refusing to guess a "
                        f"size. Weight-based signals need explicit sizing logic "
                        f"added before this can execute them safely."
                    )

                if sig["action"] == "buy" and sig["stop_price"] is None:
                    raise risk.RiskViolation(
                        f"Signal #{sig['id']} has no stop_price set -- refusing to "
                        f"buy without a real protective stop. Every buy must carry "
                        f"a broker-side stop, not just a sizing assumption."
                    )

                if dry_run:
                    outcome["status"] = "dry_run_would_execute"
                    outcome["qty"] = qty
                    if sig["action"] == "buy":
                        outcome["stop_price"] = sig["stop_price"]
                    db.update_signal_status(conn, sig["id"], "pending")  # unchanged
                else:
                    if sig["action"] == "buy":
                        order = alpaca_client.submit_market_order_with_stop(
                            sig["ticker"], qty, sig["stop_price"], account=acct
                        )
                    else:
                        # Clear any standing protective stop before closing
                        # the position -- otherwise it's left pointing at a
                        # position that's about to no longer exist.
                        alpaca_client.cancel_open_orders_for_symbol(sig["ticker"], account=acct)
                        order = alpaca_client.submit_market_order(sig["ticker"], qty, sig["action"], account=acct)
                    conn.execute(
                        """INSERT INTO orders
                           (signal_id, ticker, side, qty, alpaca_order_id, status, is_paper, notes)
                           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                        (sig["id"], sig["ticker"], sig["action"], qty,
                         order["alpaca_order_id"], order["status"], 1, ""),
                    )
                    db.update_signal_status(conn, sig["id"], "executed")
                    outcome["status"] = "executed"
                    outcome["alpaca_order_id"] = order["alpaca_order_id"]

            except risk.RiskViolation as e:
                db.update_signal_status(conn, sig["id"], "rejected")
                outcome["status"] = "rejected"
                outcome["reason"] = str(e)

            results.append(outcome)

    return results


if __name__ == "__main__":
    dry_run = "--live" not in sys.argv
    if dry_run:
        print("DRY RUN mode (default). Pass --live to actually submit orders.")
    else:
        print("LIVE mode: orders WILL be submitted to Alpaca (paper account, per ALPACA_PAPER config).")

    results = process_pending_signals(dry_run=dry_run)
    for r in results:
        print(r)
