"""
Smoke test: run this FIRST after setting up your Alpaca API keys, before
running anything else in this project. It only reads data -- no orders
are placed. If any step fails, fix that before moving on.

Usage:
    export ALPACA_API_KEY=...
    export ALPACA_SECRET_KEY=...
    export ALPACA_PAPER=true
    pip install -r requirements.txt
    python execution-agent/scripts/smoke_test.py
"""
import sys
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "execution-agent"))
sys.path.insert(0, str(ROOT / "rd-agent"))


def main():
    print("1. Checking credentials are set...")
    from shared.config import ALPACA_API_KEY, ALPACA_SECRET_KEY, ALPACA_PAPER
    if not ALPACA_API_KEY or not ALPACA_SECRET_KEY:
        print("   FAILED: ALPACA_API_KEY / ALPACA_SECRET_KEY not set.")
        sys.exit(1)
    print(f"   OK. Paper mode: {ALPACA_PAPER}")

    print("2. Connecting to Alpaca trading API (account info)...")
    try:
        import alpaca_client
        account = alpaca_client.get_account_snapshot()
        print(f"   OK. Equity: ${account.equity:,.2f} | "
              f"PDT flagged: {account.is_pdt_flagged} | "
              f"Day trades used: {account.daytrade_count}")
        if not ALPACA_PAPER:
            print("   *** WARNING: ALPACA_PAPER is not true -- this is a LIVE account. ***")
    except Exception as e:
        print(f"   FAILED: {e}")
        sys.exit(1)

    print("3. Checking open positions...")
    try:
        positions = alpaca_client.get_open_positions()
        print(f"   OK. {len(positions)} open position(s).")
    except Exception as e:
        print(f"   FAILED: {e}")
        sys.exit(1)

    print("4. Fetching sample historical data (AAPL, last 30 days)...")
    try:
        from data.alpaca_data import get_daily_bars
        end = datetime.now()
        start = end - timedelta(days=30)
        bars = get_daily_bars(["AAPL"], start, end)
        if "AAPL" in bars and len(bars["AAPL"]) > 0:
            print(f"   OK. Got {len(bars['AAPL'])} daily bars for AAPL.")
        else:
            print("   FAILED: no bars returned for AAPL.")
            sys.exit(1)
    except Exception as e:
        print(f"   FAILED: {e}")
        sys.exit(1)

    print()
    print("All smoke tests passed. Real API connectivity confirmed.")
    print("Still do NOT run the execution loop with --live until you've")
    print("reviewed dry-run output carefully.")


if __name__ == "__main__":
    main()
