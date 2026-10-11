@echo off
REM Double-click to open the dashboard in your browser.
cd /d "%~dp0"
.venv\Scripts\streamlit run app.py
