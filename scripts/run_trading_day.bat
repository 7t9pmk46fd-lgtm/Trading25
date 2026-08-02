@echo off
REM Entry point for the TradingDeskMarketLoop scheduled task. Credentials
REM come from A:\trading-desk\.env (loaded by shared/config.py). The loop
REM refuses to run unless ALPACA_PAPER=true.
"A:\trading-desk\venv\Scripts\python.exe" "A:\trading-desk\scripts\run_trading_day.py"
