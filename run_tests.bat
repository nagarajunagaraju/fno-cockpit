@echo off
cd /d %~dp0
if not exist .venv ( python -m venv .venv && .venv\Scripts\pip install -r requirements.txt )
.venv\Scripts\pip install pytest httpx -q
.venv\Scripts\python -m pytest -q tests
pause
