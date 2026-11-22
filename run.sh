#!/usr/bin/env bash
# ./run.sh        -> live (needs config.json)        ./run.sh demo -> simulated data
cd "$(dirname "$0")"
[ -d .venv ] || { python3 -m venv .venv && .venv/bin/pip install -r requirements.txt; }
export COCKPIT_LOCAL=1
[ "${1:-}" = "demo" ] && export COCKPIT_DEMO=1 COCKPIT_SIM_SPEED=30
exec .venv/bin/python -m uvicorn app.main:app --host 127.0.0.1 --port 8000 --workers 1
