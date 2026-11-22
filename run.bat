@echo off
REM F&O Cockpit on your own Windows PC (live Kite). Local browser needs no token.
cd /d %~dp0
if not exist .venv ( python -m venv .venv && .venv\Scripts\pip install -r requirements.txt )
set COCKPIT_LOCAL=1
start "" http://127.0.0.1:8000/static/signals.html
.venv\Scripts\python -m uvicorn app.main:app --host 127.0.0.1 --port 8000 --workers 1
