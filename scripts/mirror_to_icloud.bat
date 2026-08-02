@echo off
REM Mirrors A:\trading-desk to iCloud Drive for safekeeping. Excludes the
REM recreatable venvs, __pycache__, the daily bar cache, and lock files.
REM /MIR makes iCloud an exact replica (deletions propagate too).
REM Run automatically every 15 min by the TradingDeskICloudMirror task and
REM after any change made by the assistant.
robocopy "A:\trading-desk" "C:\Users\knank\iCloudDrive\trading-desk" /MIR ^
  /XD venv tradingview-mcp-venv __pycache__ cache /XF *.lock ^
  /R:2 /W:5 /NFL /NDL /NP
REM robocopy exit codes 0-7 all mean success (0=no change, 1=copied, ...)
if %ERRORLEVEL% LSS 8 exit /b 0
exit /b %ERRORLEVEL%
