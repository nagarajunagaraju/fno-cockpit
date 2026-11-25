"""Phases 5-6: evening list, Excel, Telegram, accumulation tracker, paper-trade log, weekly report."""
from __future__ import annotations

import datetime as dt
import json
import logging

import httpx
import numpy as np
import pandas as pd

from . import db, model

log = logging.getLogger("smf.daily")
SECRETS = db.OUT / "telegram.json"     # {"bot_token": "...", "chat_id": "..."} - never returned by the API


def _clean(v):
    if isinstance(v, (np.floating, float)):
        return None if np.isnan(v) else round(float(v), 2)
    if isinstance(v, (np.integer,)):
        return int(v)
    if isinstance(v, (np.bool_,)):
        return bool(v)
    return v


def build(cfg: dict | None = None, df: pd.DataFrame | None = None) -> dict:
    """Latest-day lists. Uses ~90 days of data (enough for EMA50 + 20-day windows)."""
    cfg = cfg or model.load_cfg()
    c = db.conn()
    try:
        latest = c.execute("SELECT MAX(d) FROM days WHERE status IN ('OK','PARTIAL')").fetchone()[0]
        if latest is None:
            return {"ok": False, "error": "no data yet: run data update"}
        if df is None:
            since = c.execute("SELECT d FROM days WHERE status IN ('OK','PARTIAL') ORDER BY d DESC LIMIT 1 OFFSET 129").fetchone()
            df = model.load_frame(c, since[0] if since else None)
        ban = set((db.meta_get(c, "ban") or {}).get("list") or [])
    finally:
        c.close()
    df = model.score(model.indicators(df, cfg), cfg)
    day = df[df["d"] == latest].dropna(subset=["bull", "bear"])
    lc = cfg["lists"]
    lists = {}
    for lst, mask in (("FNO", day["is_fno"] & (day["turnover20"] >= lc["fno_min_turnover_cr"]) & ~day["symbol"].isin(ban)),
                      ("INV", day["in_n500"] & (day["turnover20"] >= lc["inv_min_turnover_cr"]))):
        L = day[mask]
        for side, col in (("BULL", "bull"), ("BEAR", "bear")):
            if lst == "INV" and side == "BEAR":
                continue   # investment list = buy side only (cash shares can't be shorted overnight)
            top = L[L[col] >= lc["min_score"]].sort_values([col, "turnover20"], ascending=False).head(lc["top_n"])
            rows = []
            for _, r in top.iterrows():
                lv = model.levels(r, side, cfg)
                rows.append({"symbol": r["symbol"], "score": _clean(r[col]), "phase": r["phase"],
                             "reason": model.reason(r, side), "close": _clean(r["close"]), **lv,
                             "sector": r["sector"], "turnover_cr": _clean(r["turnover20"]),
                             "deliv_per": _clean(r["deliv_per"]), "oi_buildup": r["oi_buildup"],
                             "oi_chg5_pct": _clean(r["oi_chg5_pct"]), "is_fno": bool(r["is_fno"]),
                             "option_hint": (f"{'CE' if side == 'BULL' else 'PE'} ATM/1-OTM, 30-45 days expiry; "
                                             "size so premium SL loss <= 2% capital") if r["is_fno"] else None})
            lists[f"{lst}_{side}"] = rows
    accum = day[(day["phase"] == "ACCUMULATION")]
    rep = {"ok": True, "date": latest, "created_at": dt.datetime.now(model_tz()).isoformat(timespec="seconds"),
           "min_score": lc["min_score"], "lists": lists, "ban": sorted(ban),
           "counts": {"scored": int(len(day)), "accumulation": int(len(accum))},
           "accumulation_symbols": accum.sort_values("bull", ascending=False)["symbol"].head(30).tolist()}
    return rep, df


def model_tz():
    return dt.timezone(dt.timedelta(hours=5, minutes=30))


# ------------------------------------------------------------------ tracker
def track(df: pd.DataFrame, date: str) -> list[dict]:
    """ACCUMULATION stocks are tracked daily until breakout (MARKUP) or failure (close below base low).
    Returns alerts for breakouts / failures that happened on `date`."""
    c = db.conn()
    alerts = []
    try:
        today = df[df["d"] == date].set_index("symbol")
        for sym, r in today[today["phase"] == "ACCUMULATION"].iterrows():
            c.execute("INSERT OR IGNORE INTO watch VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                      (sym, "BULL", "FNO" if r["is_fno"] else "INV", date, _clean(r["bull"]), _clean(r["l20"]),
                       _clean(r["h20"]), "WATCHING", date, None, model.reason(r, "BULL")))
        for w in c.execute("SELECT * FROM watch WHERE status='WATCHING'").fetchall():
            w = dict(w)
            if w["symbol"] not in today.index or w["start_d"] == date:
                continue
            r = today.loc[w["symbol"]]
            age = int(((pd.to_datetime(date) - pd.to_datetime(w["start_d"])).days))
            new = None
            if r["phase"] == "MARKUP" or r["close"] > (w["base_high"] or 1e18):
                new, txt = "BROKE_OUT", (f"BREAKOUT {w['symbol']}: close {r['close']:.2f} above base high "
                                         f"{w['base_high']:.2f}, vol {r['vol_ratio']:.1f}x (accumulation since {w['start_d']})")
            elif r["close"] < (w["base_low"] or -1):
                new, txt = "FAILED", f"FAILED {w['symbol']}: closed below base low {w['base_low']:.2f}"
            elif age > 45:
                new, txt = "EXPIRED", None
            if new:
                c.execute("UPDATE watch SET status=?, end_d=?, last_d=? WHERE symbol=? AND side=? AND start_d=?",
                          (new, date, date, w["symbol"], w["side"], w["start_d"]))
                if txt:
                    alerts.append({"kind": new, "symbol": w["symbol"], "text": txt})
            else:
                c.execute("UPDATE watch SET last_d=? WHERE symbol=? AND side=? AND start_d=?",
                          (date, w["symbol"], w["side"], w["start_d"]))
        c.commit()
    finally:
        c.close()
    return alerts


# ------------------------------------------------------------------ paper log
def paper_add(rep: dict):
    c = db.conn()
    try:
        for key, rows in rep["lists"].items():
            lst, side = key.split("_")
            for r in rows:
                c.execute("INSERT OR IGNORE INTO paper (signal_d,symbol,side,list,score,phase,entry,sl,t1,t2,sector,status)"
                          " VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                          (rep["date"], r["symbol"], side, lst, r["score"], r["phase"], r["entry"], r["sl"],
                           r["t1"], r["t2"], r["sector"], "PENDING"))
        c.commit()
    finally:
        c.close()


def paper_update(df: pd.DataFrame, cfg: dict) -> int:
    """Walk each open paper trade through the daily bars after its signal day (same rules as backtest)."""
    c = db.conn()
    n = 0
    hold = cfg["trade"]["max_hold_days"]
    try:
        bars = {s: g.sort_values("d").reset_index(drop=True) for s, g in df.groupby("symbol")}
        for p in c.execute("SELECT * FROM paper WHERE status IN ('PENDING','OPEN','HALF')").fetchall():
            p = dict(p)
            g = bars.get(p["symbol"])
            if g is None:
                continue
            after = g[g["d"] > p["signal_d"]].reset_index(drop=True)
            if after.empty:
                continue
            bull = p["side"] == "BULL"
            status, entry_d = p["status"], p["entry_d"]
            if status == "PENDING":
                b = after.iloc[0]
                if (bull and b["high"] >= p["entry"]) or (not bull and b["low"] <= p["entry"]):
                    status, entry_d = "OPEN", b["d"]
                else:
                    c.execute("UPDATE paper SET status='NOT_TRIGGERED', exit_d=? WHERE id=?", (b["d"], p["id"]))
                    n += 1
                    continue
            run = after[after["d"] >= entry_d].reset_index(drop=True)
            risk = abs(p["entry"] - p["sl"])
            half, stop = False, p["sl"]    # replay from the entry day every evening (stateless, no drift)
            done = None
            for j, b in run.iterrows():
                hit_sl = b["low"] <= stop if bull else b["high"] >= stop
                if hit_sl:
                    leg = (stop - p["entry"]) if bull else (p["entry"] - stop)
                    res = 0.5 * abs(p["t1"] - p["entry"]) + 0.5 * leg if half else leg
                    done = (b["d"], stop, res / risk, "SL" if not half else "BE after T1")
                    break
                if not half and (b["high"] >= p["t1"] if bull else b["low"] <= p["t1"]):
                    half, stop = True, p["entry"]
                if half and (b["high"] >= p["t2"] if bull else b["low"] <= p["t2"]):
                    done = (b["d"], p["t2"], (0.5 * abs(p["t1"] - p["entry"]) + 0.5 * abs(p["t2"] - p["entry"])) / risk, "T2")
                    break
                if j + 1 >= hold:
                    leg = (b["close"] - p["entry"]) if bull else (p["entry"] - b["close"])
                    res = 0.5 * abs(p["t1"] - p["entry"]) + 0.5 * leg if half else leg
                    done = (b["d"], b["close"], res / risk, "TIME")
                    break
            if done:
                c.execute("UPDATE paper SET status='CLOSED', entry_d=?, exit_d=?, exit_price=?, r=?, note=? WHERE id=?",
                          (entry_d, done[0], round(done[1], 2), round(done[2], 3), done[3], p["id"]))
            else:
                c.execute("UPDATE paper SET status=?, entry_d=? WHERE id=?", ("HALF" if half else "OPEN", entry_d, p["id"]))
            n += 1
        c.commit()
    finally:
        c.close()
    return n


def paper_report(days: int = 7) -> dict:
    c = db.conn()
    try:
        since = (dt.date.today() - dt.timedelta(days=days)).isoformat()
        rows = [dict(r) for r in c.execute("SELECT * FROM paper ORDER BY signal_d DESC")]
    finally:
        c.close()
    closed = [r for r in rows if r["status"] == "CLOSED"]

    def summ(rs):
        if not rs:
            return {"trades": 0}
        rr = [r["r"] for r in rs]
        return {"trades": len(rr), "win_pct": round(sum(x > 0 for x in rr) / len(rr) * 100, 1),
                "avg_r": round(sum(rr) / len(rr), 2), "total_r": round(sum(rr), 2),
                "best": max(rs, key=lambda r: r["r"])["symbol"], "worst": min(rs, key=lambda r: r["r"])["symbol"]}
    week = [r for r in closed if (r["exit_d"] or "") >= since]
    sectors = {}
    for r in closed:
        sectors.setdefault(r["sector"] or "Other", []).append(r)
    return {"all_time": summ(closed), "last_7_days": summ(week),
            "by_sector": {k: summ(v) for k, v in sorted(sectors.items())},
            "by_list": {k: summ([r for r in closed if f"{r['list']}_{r['side']}" == k])
                        for k in ("FNO_BULL", "FNO_BEAR", "INV_BULL")},
            "open": [r for r in rows if r["status"] in ("PENDING", "OPEN", "HALF")],
            "recent_closed": closed[:30]}


# ------------------------------------------------------------------ Excel / Telegram
def excel(rep: dict) -> str:
    from openpyxl import Workbook
    from openpyxl.styles import Font, PatternFill
    wb = Workbook()
    wb.remove(wb.active)
    cols = ["symbol", "score", "phase", "reason", "close", "entry", "sl", "t1", "t2", "risk_pct", "cash_qty",
            "lot", "fut_risk_per_lot", "fut_lots", "oi_buildup", "oi_chg5_pct", "deliv_per", "turnover_cr", "sector"]
    names = {"FNO_BULL": "F&O Bullish", "FNO_BEAR": "F&O Bearish", "INV_BULL": "Investment (N500)"}
    for key, rows in rep["lists"].items():
        ws = wb.create_sheet(names.get(key, key))
        ws.append([f"SMF {names.get(key, key)} - {rep['date']} (min score {rep['min_score']})"])
        ws["A1"].font = Font(bold=True, size=13)
        ws.append(cols)
        for cell in ws[2]:
            cell.font = Font(bold=True, color="FFFFFF")
            cell.fill = PatternFill("solid", fgColor="1F4E79" if "BULL" in key else "7B2C2C")
        for r in rows:
            ws.append([r.get(k) for k in cols])
        for col, w in zip("ABCDEFGHIJKLMNOPQRS", (12, 7, 14, 60, 10, 10, 10, 10, 10, 8, 9, 7, 12, 8, 12, 10, 9, 11, 22)):
            ws.column_dimensions[col].width = w
        ws.freeze_panes = "B3"
    path = db.OUT / f"SMF_{rep['date']}.xlsx"
    wb.save(path)
    return str(path)


def telegram_text(rep: dict, alerts: list[dict]) -> str:
    lines = [f"SMF {rep['date']} (score >= {rep['min_score']})"]
    names = {"FNO_BULL": "F&O BULLISH", "FNO_BEAR": "F&O BEARISH", "INV_BULL": "INVESTMENT"}
    for key, rows in rep["lists"].items():
        lines.append(f"\n{names[key]}:")
        if not rows:
            lines.append("  none")
        for r in rows[:10]:
            lines.append(f"  {r['symbol']} {r['score']} {r['phase']} | E {r['entry']} SL {r['sl']} T1 {r['t1']} T2 {r['t2']}")
            lines.append(f"    {r['reason']}")
    for a in alerts:
        lines.append(f"\n{a['text']}")
    lines.append("\nResearch only. Not advice.")
    return "\n".join(lines)


def telegram_send(text: str) -> str | None:
    if not SECRETS.exists():
        return "telegram not configured"
    s = json.loads(SECRETS.read_text())
    try:
        for i in range(0, len(text), 3800):
            r = httpx.post(f"https://api.telegram.org/bot{s['bot_token']}/sendMessage",
                           json={"chat_id": s["chat_id"], "text": text[i:i + 3800]}, timeout=20)
            r.raise_for_status()
        return None
    except Exception as e:
        return f"telegram failed: {type(e).__name__}"   # never log the token


def run_daily(progress=None) -> dict:
    """Full evening run on already-downloaded data."""
    cfg = model.load_cfg()
    res = build(cfg)
    if isinstance(res, dict):
        return res
    rep, df = res
    if progress: progress("tracker + paper log")
    alerts = track(df, rep["date"])
    paper_add(rep)
    paper_update(df, cfg)
    rep["alerts"] = alerts
    rep["excel"] = excel(rep)
    rep["telegram"] = telegram_send(telegram_text(rep, alerts)) or "sent"
    c = db.conn()
    try:
        db.meta_set(c, "today", rep)
    finally:
        c.close()
    return rep
