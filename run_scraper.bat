@echo off
REM Double-click to scrape all KL pages into Supabase.
REM Only NEW listings get their detail page opened, so daily runs are much faster than the first.
cd /d "%~dp0"
.venv\Scripts\python scraper.py
pause
