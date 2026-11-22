"""SQLite persistence for the signal engine (signals, events, paper P&L, IV history, OI baselines)."""
from __future__ import annotations

import json
import os
import sqlite3
import threading
from pathlib import Path

DATA = Path(os.environ.get("COCKPIT_DATA_DIR") or Path(__file__).resolve().parents[2] / "data")
DATA.mkdir(parents=True, exist_ok=True)
DB = DATA / "engine.db"
_lock = threading.RLock()

SCHEMA = """
CREATE TABLE IF NOT EXISTS signals(
  id INTEGER PRIMARY KEY AUTOINCREMENT, day TEXT, symbol TEXT, bias TEXT, status TEXT, score REAL,
  setup_key TEXT, trigger_level REAL, created_at TEXT, updated_at TEXT, closed_at TEXT,
  plan TEXT, explanation TEXT, evaluation TEXT, selection TEXT, mode TEXT,
  fill_price REAL, filled_at TEXT, qty INTEGER, remaining_qty INTEGER, sl_current REAL, t1_done INTEGER DEFAULT 0,
  exits TEXT DEFAULT '[]', gross_pnl REAL, charges REAL, net_pnl REAL, last_bid REAL, outcome TEXT);
CREATE INDEX IF NOT EXISTS ix_signals_day ON signals(day);
CREATE TABLE IF NOT EXISTS events(
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, signal_id INTEGER, kind TEXT, title TEXT, body TEXT, payload TEXT);
CREATE TABLE IF NOT EXISTS option_oi_baseline(day TEXT, ts TEXT, oi REAL, kind TEXT, at TEXT, PRIMARY KEY(day, ts));
CREATE TABLE IF NOT EXISTS iv_history(symbol TEXT, day TEXT, atm_iv REAL, PRIMARY KEY(symbol, day));
CREATE TABLE IF NOT EXISTS kv(k TEXT PRIMARY KEY, v TEXT);
CREATE TABLE IF NOT EXISTS backtests(id INTEGER PRIMARY KEY AUTOINCREMENT, created_at TEXT, params TEXT, result TEXT);
"""


def conn():
    c = sqlite3.connect(DB, timeout=30, check_same_thread=False)
    c.row_factory = sqlite3.Row
    return c


def init():
    with _lock, conn() as c:
        c.executescript(SCHEMA)
        c.execute("PRAGMA journal_mode=WAL")


def q(sql, args=()):
    with _lock, conn() as c:
        return [dict(r) for r in c.execute(sql, args).fetchall()]


def x(sql, args=()):
    with _lock, conn() as c:
        cur = c.execute(sql, args)
        return cur.lastrowid


def kv_get(k, default=None):
    r = q("SELECT v FROM kv WHERE k=?", (k,))
    return json.loads(r[0]["v"]) if r else default


def kv_set(k, v):
    x("INSERT OR REPLACE INTO kv(k, v) VALUES(?, ?)", (k, json.dumps(v)))


JSON_COLS = ("plan", "explanation", "evaluation", "selection", "exits")


def decode(row: dict) -> dict:
    for k in JSON_COLS:
        if row.get(k):
            try:
                row[k] = json.loads(row[k])
            except Exception:
                pass
    return row


init()
