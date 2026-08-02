@echo off
REM Entry point for Windows Task Scheduler. Credentials come from
REM A:\trading-desk\.env (loaded by shared/config.py), not from this
REM script or the task's own environment.
"A:\trading-desk\venv\Scripts\python.exe" "A:\trading-desk\scripts\run_cycle.py"
