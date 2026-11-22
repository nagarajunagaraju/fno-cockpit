"""SQLite trade journal + settings store."""
from __future__ import annotations

import datetime as dt
import json
import sqlite3
from pathlib import Path

DB = Path(__file__).resolve().parent.parent / "cockpit.db"

MISTAKE_TAGS = ["Revenge trade", "FOMO entry", "Against trend", "Moved SL", "No SL",
                "Oversized", "Early exit", "Held loser too long", "Late entry", "No plan"]


def conn():
    c = sqlite3.connect(DB)
    c.row_factory = sqlite3.Row
    return c


def init():
    with conn() as c:
        c.execute("""CREATE TABLE IF NOT EXISTS trades(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            opened_at TEXT, symbol TEXT, side TEXT, entry REAL, sl REAL, t1 REAL, t2 REAL,
            lots INTEGER, lot_size INTEGER, qty INTEGER, risk_amount REAL,
            score INTEGER, bias TEXT, checklist TEXT, rules_ok INTEGER, reason TEXT,
            underlying TEXT, opt_type TEXT, strike REAL, expiry TEXT, delta REAL, iv REAL,
            u_sl REAL, u_t1 REAL,
            status TEXT DEFAULT 'OPEN', exit_price REAL, pnl REAL, r_multiple REAL,
            mistakes TEXT, notes TEXT, closed_at TEXT)""")
        c.execute("CREATE TABLE IF NOT EXISTS settings(k TEXT PRIMARY KEY, v TEXT)")


def get_rules(defaults: dict) -> dict:
    with conn() as c:
        row = c.execute("SELECT v FROM settings WHERE k='rules'").fetchone()
    rules = dict(defaults)
    if row:
        rules.update(json.loads(row["v"]))
    return rules


def save_rules(rules: dict):
    with conn() as c:
        c.execute("INSERT OR REPLACE INTO settings(k, v) VALUES('rules', ?)", (json.dumps(rules),))


def add_trade(plan: dict, checklist: dict, reason: str) -> int:
    rules_ok = int(all(checklist.values())) if checklist else 0
    with conn() as c:
        cur = c.execute(
            """INSERT INTO trades(opened_at, symbol, side, entry, sl, t1, t2, lots, lot_size, qty,
               risk_amount, score, bias, checklist, rules_ok, reason,
               underlying, opt_type, strike, expiry, delta, iv, u_sl, u_t1)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (dt.datetime.now().isoformat(timespec="seconds"), plan["symbol"], plan["side"], plan["entry"],
             plan["sl"], plan["t1"], plan["t2"], plan["lots"], plan["lot_size"], plan["qty"],
             plan["risk_amount"], plan.get("score"), plan.get("bias"), json.dumps(checklist), rules_ok, reason,
             plan.get("underlying"), plan.get("opt_type"), plan.get("strike"), plan.get("expiry"),
             plan.get("delta"), plan.get("iv"), plan.get("u_sl"), plan.get("u_t1")))
        return cur.lastrowid


def close_trade(tid: int, exit_price: float, mistakes: list[str], notes: str) -> dict:
    with conn() as c:
        t = c.execute("SELECT * FROM trades WHERE id=?", (tid,)).fetchone()
        if not t or t["status"] != "OPEN":
            raise ValueError("Trade not found or already closed")
        sign = -1 if t["side"] == "SHORT" else 1
        pnl = sign * (exit_price - t["entry"]) * t["qty"]
        risk = abs(t["entry"] - t["sl"]) * t["qty"]
        r_mult = pnl / risk if risk else 0
        c.execute("""UPDATE trades SET status='CLOSED', exit_price=?, pnl=?, r_multiple=?, mistakes=?, notes=?,
                     closed_at=? WHERE id=?""",
                  (exit_price, round(pnl, 2), round(r_mult, 2), json.dumps(mistakes), notes,
                   dt.datetime.now().isoformat(timespec="seconds"), tid))
    return {"pnl": round(pnl, 2), "r_multiple": round(r_mult, 2)}


def rows(sql: str, args=()) -> list[dict]:
    with conn() as c:
        return [dict(r) for r in c.execute(sql, args).fetchall()]


def open_trades():
    return rows("SELECT * FROM trades WHERE status='OPEN' ORDER BY opened_at")


def today_trades():
    return rows("SELECT * FROM trades WHERE substr(opened_at,1,10)=? ORDER BY opened_at",
                (dt.date.today().isoformat(),))


def realized_today():
    r = rows("SELECT COALESCE(SUM(pnl),0) s FROM trades WHERE status='CLOSED' AND substr(closed_at,1,10)=?",
             (dt.date.today().isoformat(),))
    return r[0]["s"]


def last_loss_at():
    r = rows("SELECT closed_at FROM trades WHERE status='CLOSED' AND pnl<0 ORDER BY closed_at DESC LIMIT 1")
    return r[0]["closed_at"] if r else None


def all_trades(limit=200):
    return rows("SELECT * FROM trades ORDER BY opened_at DESC LIMIT ?", (limit,))


def stats():
    closed = rows("SELECT * FROM trades WHERE status='CLOSED'")
    n = len(closed)
    if not n:
        return {"closed": 0}
    wins = [t for t in closed if t["pnl"] > 0]
    by_tag: dict[str, dict] = {}
    for t in closed:
        for tag in json.loads(t["mistakes"] or "[]"):
            d = by_tag.setdefault(tag, {"tag": tag, "count": 0, "pnl": 0.0})
            d["count"] += 1
            d["pnl"] += t["pnl"]
    followed = [t for t in closed if t["rules_ok"]]
    broke = [t for t in closed if not t["rules_ok"]]

    def avg_r(ts):
        return round(sum(t["r_multiple"] for t in ts) / len(ts), 2) if ts else None

    return {
        "closed": n,
        "win_rate": round(len(wins) / n * 100, 1),
        "total_pnl": round(sum(t["pnl"] for t in closed), 2),
        "avg_r": avg_r(closed),
        "rules_followed": {"count": len(followed), "avg_r": avg_r(followed),
                           "pnl": round(sum(t["pnl"] for t in followed), 2)},
        "rules_broken": {"count": len(broke), "avg_r": avg_r(broke),
                         "pnl": round(sum(t["pnl"] for t in broke), 2)},
        "mistakes": sorted(({**d, "pnl": round(d["pnl"], 2)} for d in by_tag.values()),
                           key=lambda d: d["pnl"]),
    }
