@echo off
REM Used by Windows Task Scheduler. Writes a log per run to the logs folder.
cd /d "%~dp0"
if not exist logs mkdir logs
for /f %%i in ('powershell -NoProfile -Command "Get-Date -Format yyyyMMdd_HHmmss"') do set STAMP=%%i
set PYTHONUNBUFFERED=1
.venv\Scripts\python scraper.py > "logs\scrape_%STAMP%.log" 2>&1
