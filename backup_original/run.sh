#!/usr/bin/env bash
# Usage: ./run.sh          (live, needs config.json)
#        ./run.sh demo     (demo data, no login)
cd "$(dirname "$0")"
[ -d .venv ] || { python3 -m venv .venv && .venv/bin/pip install -r requirements.txt; }
[ "$1" = "demo" ] && export COCKPIT_DEMO=1
.venv/bin/python -m uvicorn app.main:app --host 127.0.0.1 --port 8000
