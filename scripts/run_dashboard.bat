@echo off
REM Starts the read-only local dashboard at http://127.0.0.1:8787
REM (TradingDeskDashboard scheduled task runs this at logon).
"A:\trading-desk\venv\Scripts\python.exe" "A:\trading-desk\analyst\dashboard.py"
