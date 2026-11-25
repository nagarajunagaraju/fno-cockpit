"""SQLite storage for SMF (separate file from the intraday engine: data/smf.db)."""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from ..engine.store import DATA

DB = Path(DATA) / "smf.db"
OUT = Path(DATA) / "smf"
OUT.mkdir(parents=True, exist_ok=True)

SCHEMA = """
CREATE TABLE IF NOT EXISTS equity (d TEXT, symbol TEXT, open REAL, high REAL, low REAL, close REAL,
    prev_close REAL, volume REAL, turnover_cr REAL, trades REAL, deliv_qty REAL, deliv_per REAL,
    PRIMARY KEY (d, symbol));
CREATE TABLE IF NOT EXISTS fo (d TEXT, symbol TEXT, fut_oi REAL, fut_oi_chg REAL, near_exp TEXT,
    fut_close REAL, lot REAL, PRIMARY KEY (d, symbol));
CREATE TABLE IF NOT EXISTS idx (d TEXT PRIMARY KEY, nifty REAL);
CREATE TABLE IF NOT EXISTS days (d TEXT PRIMARY KEY, status TEXT, equity_rows INT, fo_rows INT,
    note TEXT, fetched_at TEXT);
CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v TEXT);
CREATE TABLE IF NOT EXISTS watch (symbol TEXT, side TEXT, list TEXT, start_d TEXT, start_score REAL,
    base_low REAL, base_high REAL, status TEXT, last_d TEXT, end_d TEXT, note TEXT,
    PRIMARY KEY (symbol, side, start_d));
CREATE TABLE IF NOT EXISTS paper (id INTEGER PRIMARY KEY AUTOINCREMENT, signal_d TEXT, symbol TEXT,
    side TEXT, list TEXT, score REAL, phase TEXT, entry REAL, sl REAL, t1 REAL, t2 REAL, sector TEXT,
    status TEXT, entry_d TEXT, exit_d TEXT, exit_price REAL, r REAL, note TEXT,
    UNIQUE (signal_d, symbol, side));
"""


def conn() -> sqlite3.Connection:
    c = sqlite3.connect(DB, timeout=30)
    c.row_factory = sqlite3.Row
    c.executescript(SCHEMA)
    return c


def meta_get(c, k, default=None):
    r = c.execute("SELECT v FROM meta WHERE k=?", (k,)).fetchone()
    return json.loads(r["v"]) if r else default


def meta_set(c, k, v):
    c.execute("INSERT OR REPLACE INTO meta VALUES (?,?)", (k, json.dumps(v, default=str)))
    c.commit()
