@echo off
REM Optional: test the phone app against this PC on home Wi-Fi (http://<PC-IP>:8000 + app token).
REM Token is in data\app_token.txt . Windows firewall will ask to allow Python - allow on Private networks only.
cd /d %~dp0
if not exist .venv ( python -m venv .venv && .venv\Scripts\pip install -r requirements.txt )
ipconfig | findstr /i "IPv4"
type data\app_token.txt 2>nul
.venv\Scripts\python -m uvicorn app.main:app --host 0.0.0.0 --port 8000 --workers 1
