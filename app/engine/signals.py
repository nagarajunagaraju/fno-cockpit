"""Signal lifecycle, duplicate prevention, risk controls, paper execution and notification events.

Lifecycle: (WATCHING in memory) -> TRIGGERED -> ACTIVE -> TARGET_HIT / STOP_LOSS_HIT / EXPIRED
           TRIGGERED -> INVALIDATED (spot broke the stop before fill) / EXPIRED (missed fill)
Paper trading is the only execution mode. No broker orders are ever placed.
"""
from __future__ import annotations

import datetime as dt
import json
import logging
import math

from . import store
from .charges import option_round_trip
from .config import IST, hm
from .sectors import risk_group

log = logging.getLogger("cockpit.signals")
OPEN_STATES = ("TRIGGERED", "ACTIVE")


def _now_s(now):
    return now.astimezone(IST).isoformat(timespec="seconds")


class SignalManager:
    def __init__(self, cfg_getter):
        self.cfg = cfg_getter
        self.watch: dict[str, dict] = {}

    # ------------------------------------------------------------ events
    def emit(self, now, kind, title, body, signal_id=None, payload=None):
        eid = store.x("INSERT INTO events(ts, signal_id, kind, title, body, payload) VALUES(?,?,?,?,?,?)",
                      (_now_s(now), signal_id, kind, title, body, json.dumps(payload or {})))
        log.info("event %s %s: %s", eid, kind, title)
        return eid

    def events(self, after: int = 0, limit: int = 100):
        rows = store.q("SELECT * FROM events WHERE id > ? ORDER BY id ASC LIMIT ?", (after, limit))
        for r in rows:
            r["payload"] = json.loads(r["payload"] or "{}")
        return rows

    # ------------------------------------------------------------ queries
    def get(self, sid):
        r = store.q("SELECT * FROM signals WHERE id=?", (sid,))
        return store.decode(r[0]) if r else None

    def list(self, day=None, status=None, limit=200):
        sql, args = "SELECT * FROM signals WHERE 1=1", []
        if day:
            sql += " AND day=?"; args.append(day)
        if status:
            sql += f" AND status IN ({','.join('?' * len(status))})"; args += list(status)
        sql += " ORDER BY id DESC LIMIT ?"; args.append(limit)
        return [store.decode(r) for r in store.q(sql, args)]

    def open_signals(self):
        return self.list(status=OPEN_STATES)

    # ------------------------------------------------------------ risk
    def risk_state(self, now) -> dict:
        cfg = self.cfg()
        day = now.astimezone(IST).date().isoformat()
        rows = self.list(day=day, limit=1000)
        realized = sum(r["net_pnl"] or 0 for r in rows if r["status"] not in OPEN_STATES)
        unreal, exposure, open_n = 0.0, 0.0, 0
        for r in rows:
            if r["status"] in OPEN_STATES:
                open_n += 1
                p = r["plan"]
                if r["status"] == "ACTIVE":
                    exposure += (r["fill_price"] or 0) * (r["remaining_qty"] or 0)
                    if r["last_bid"]:
                        unreal += (r["last_bid"] - r["fill_price"]) * (r["remaining_qty"] or 0) + self._realized_part(r)
                else:
                    exposure += p["premium_outlay"]
        day_pnl = realized + unreal
        max_loss = cfg["capital"] * cfg["max_daily_loss_pct"] / 100
        return {"day": day, "realized": round(realized, 2), "unrealized": round(unreal, 2),
                "day_pnl": round(day_pnl, 2), "max_daily_loss": round(max_loss, 2),
                "locked": day_pnl <= -max_loss, "open_positions": open_n,
                "max_open_positions": cfg["max_open_positions"], "exposure": round(exposure, 2),
                "max_exposure": round(cfg["capital"] * cfg["max_exposure_pct"] / 100, 2),
                "signals_today": len(rows), "mode": "PAPER"}

    @staticmethod
    def _realized_part(r):
        return sum((e["price"] - r["fill_price"]) * e["qty"] for e in (r["exits"] or []))

    def can_take(self, plan, now) -> tuple[bool, str]:
        cfg = self.cfg()
        rs = self.risk_state(now)
        if rs["locked"]:
            return False, f"Daily loss limit hit (P&L {rs['day_pnl']:,.0f} <= -{rs['max_daily_loss']:,.0f}). Trading locked today."
        if rs["open_positions"] >= cfg["max_open_positions"]:
            return False, f"Max simultaneous positions reached ({rs['open_positions']}/{cfg['max_open_positions']})."
        if rs["exposure"] + plan["premium_outlay"] > rs["max_exposure"]:
            return False, (f"Exposure limit: open Rs {rs['exposure']:,.0f} + new Rs {plan['premium_outlay']:,.0f} "
                           f"> Rs {rs['max_exposure']:,.0f}.")
        return True, ""

    # ------------------------------------------------------------ dedupe
    def duplicate_reason(self, symbol, bias, trigger_level, now) -> str | None:
        cfg = self.cfg()
        day = now.astimezone(IST).date().isoformat()
        rows = store.q("SELECT * FROM signals WHERE day=? AND symbol=? AND bias=? ORDER BY id", (day, symbol, bias))
        if any(r["status"] in OPEN_STATES for r in rows):
            return "same setup already open"
        if len(rows) >= cfg["max_signals_per_symbol_day"]:
            return f"already {len(rows)} signals today for {symbol} {bias}"
        if rows:
            last = rows[-1]
            closed = dt.datetime.fromisoformat(last["closed_at"] or last["created_at"])
            if (now - closed).total_seconds() < cfg["resignal_cooldown_min"] * 60:
                return "re-signal cooldown"
            up = bias == "BULL"
            if (up and trigger_level <= last["trigger_level"]) or (not up and trigger_level >= last["trigger_level"]):
                return "no new extreme since last signal"
        return None

    # ------------------------------------------------------------ create
    def daily_count(self, now) -> int:
        day = now.astimezone(IST).date().isoformat()
        return store.q("SELECT COUNT(*) AS n FROM signals WHERE day=?", (day,))[0]["n"]

    def cap_reason(self, symbol, now) -> str | None:
        cfg = self.cfg()
        if self.daily_count(now) >= cfg.get("max_alerts_per_day", 99):
            return f"daily alert cap reached ({cfg['max_alerts_per_day']})"
        day = now.astimezone(IST).date().isoformat()
        if store.q("SELECT 1 FROM signals WHERE day=? AND symbol=? LIMIT 1", (day, symbol)):
            return "already signalled this stock today"
        grp = risk_group(symbol)
        same = [r for r in self.open_signals() if risk_group(r["symbol"]) == grp]
        if len(same) >= cfg.get("max_per_sector", 99):
            return f"sector cap: already {len(same)} open position(s) in {grp}"
        return None

    def on_trigger(self, ev, sel, plan, explanation, now, mode="LIVE") -> dict:
        lvl = ev["trigger"]["level"]
        if not ev.get("own_limits"):      # the Sensex brain enforces its own per-day limits
            dup = self.duplicate_reason(ev["symbol"], ev["direction"], lvl, now)
            if dup:
                return {"created": False, "reason": dup}
            cap = self.cap_reason(ev["symbol"], now)
            if cap:
                return {"created": False, "reason": cap}
        ok, why = self.can_take(plan, now)
        if not ok:
            day = now.astimezone(IST).date().isoformat()
            key = f"risk_block:{day}:{why.split('(')[0]}"
            if not store.kv_get(key):
                store.kv_set(key, True)
                self.emit(now, "RISK_LOCK", "Risk control: new signals blocked", why)
            return {"created": False, "reason": why}
        day = now.astimezone(IST).date().isoformat()
        sid = store.x(
            """INSERT INTO signals(day, symbol, bias, status, score, setup_key, trigger_level, created_at, updated_at,
               plan, explanation, evaluation, selection, mode, qty, remaining_qty, sl_current)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (day, ev["symbol"], ev["direction"], "TRIGGERED", ev["score"], f"{day}:{ev['symbol']}:{ev['direction']}",
             lvl, _now_s(now), _now_s(now), json.dumps(plan), json.dumps(explanation), json.dumps(ev),
             json.dumps({k: v for k, v in sel.items() if k != "candidates"} | {"candidates": sel["candidates"]}),
             mode, plan["qty"], plan["qty"], plan["sl_premium"]))
        g = ev.get("grade")
        title = ev.get("title") or (f"{'[' + g + '] ' if g else ''}BUY {plan['tradingsymbol']} | "
                                    f"{ev['symbol']} {plan['direction']} | Score {ev['score']:g}/{ev.get('max_score', 10)}")
        body = (f"Entry {plan['entry_range'][0]}-{plan['entry_range'][1]} | SL {plan['sl_premium']} | "
                f"T1 {plan['t1_premium']} | T2 {plan['t2_premium']} | {plan['lots']} lot(s), "
                f"max loss Rs {plan['max_loss']:,.0f}")
        self.emit(now, "TRIGGERED", title, body, sid, {"plan": plan})
        return {"created": True, "id": sid}

    # ------------------------------------------------------------ lifecycle / paper
    def update(self, now, quotes: dict, spot_close: dict):
        """quotes: {tradingsymbol: {ltp, bid, ask}}; spot_close: {symbol: last completed 1-min close}."""
        cfg = self.cfg()
        sq = hm(cfg["square_off_time"])
        for r in self.open_signals():
            p = r["plan"]
            qd = quotes.get(p["tradingsymbol"]) or {}
            bid, ask = qd.get("bid"), qd.get("ask")
            spot = spot_close.get(r["symbol"])
            up = r["bias"] == "BULL"
            spot_broken = spot is not None and ((up and spot < p["spot_sl"]) or (not up and spot > p["spot_sl"]))
            t_local = now.astimezone(IST).time()
            if r["status"] == "TRIGGERED":
                age = (now - dt.datetime.fromisoformat(r["created_at"])).total_seconds()
                if spot_broken:
                    self._close_unfilled(r, now, "INVALIDATED", f"Spot closed beyond {p['spot_sl']} before entry fill")
                elif ask and ask > 0 and ask <= p["entry_range"][1]:
                    fill = min(round(ask * (1 + cfg["paper_slippage_pct"] / 100), 2), p["entry_range"][1])
                    store.x("UPDATE signals SET status='ACTIVE', fill_price=?, filled_at=?, updated_at=?, last_bid=? WHERE id=?",
                            (fill, _now_s(now), _now_s(now), bid, r["id"]))
                    self.emit(now, "ACTIVE", f"Paper entry filled: {p['tradingsymbol']} @ {fill}",
                              f"{p['qty']} qty | SL {p['sl_premium']} | T1 {p['t1_premium']} | T2 {p['t2_premium']}", r["id"])
                elif age > cfg["fill_window_minutes"] * 60 or t_local >= sq:
                    why = (f"Premium ask {ask} stayed above entry range {p['entry_range'][1]}" if ask
                           else "No ask quote") + f" for {cfg['fill_window_minutes']} min"
                    self._close_unfilled(r, now, "EXPIRED", f"Missed fill: {why}")
                continue
            # ACTIVE
            if bid is not None:
                store.x("UPDATE signals SET last_bid=?, updated_at=? WHERE id=?", (bid, _now_s(now), r["id"]))
                r["last_bid"] = bid
            exit_px = round(bid * (1 - cfg["paper_slippage_pct"] / 100), 2) if bid else None
            if t_local >= sq:
                px = exit_px or qd.get("ltp") or r["fill_price"]
                self._exit(r, now, r["remaining_qty"], px, "Time exit (square-off)", final="EXPIRED")
            elif (bid is not None and bid <= r["sl_current"]) or spot_broken:
                px = exit_px if exit_px is not None else (qd.get("ltp") or r["sl_current"])
                reason = "Premium stop hit" if (bid is not None and bid <= r["sl_current"]) else f"Spot closed beyond {p['spot_sl']}"
                booked = any("Target" in e["reason"] for e in (r["exits"] or []))
                final = "TARGET_HIT" if booked else "STOP_LOSS_HIT"
                if r["t1_done"]:
                    reason += " (after T1, stop moved to cost)"
                self._exit(r, now, r["remaining_qty"], px, reason, final=final)
            elif (bid is not None and not r["t1_done"] and cfg.get("time_stop_minutes") and r["filled_at"]
                  and (now - dt.datetime.fromisoformat(r["filled_at"])).total_seconds() >= cfg["time_stop_minutes"] * 60
                  and bid < r["fill_price"] + cfg["time_stop_r"] * (r["fill_price"] - p["sl_premium"])):
                self._exit(r, now, r["remaining_qty"], exit_px,
                           f"Time stop: no +{cfg['time_stop_r']}R move in {cfg['time_stop_minutes']} min", final="EXPIRED")
            elif bid is not None and bid >= p["t2_premium"]:
                self._exit(r, now, r["remaining_qty"], exit_px, "Target 2 hit", final="TARGET_HIT")
            elif bid is not None and bid >= p["t1_premium"] and not r["t1_done"]:
                lots = r["remaining_qty"] // p["lot_size"]
                book = math.floor(lots * cfg["t1_book_fraction"]) * p["lot_size"]
                store.x("UPDATE signals SET t1_done=1, sl_current=? WHERE id=?", (r["fill_price"], r["id"]))
                r["t1_done"], r["sl_current"] = 1, r["fill_price"]
                if book > 0:
                    self._exit(r, now, book, exit_px, "Target 1 hit - partial booked, stop moved to cost", final=None)
                else:
                    self.emit(now, "T1", f"T1 hit: {p['tradingsymbol']} @ {bid}",
                              "1 lot only: holding for T2 with stop moved to entry", r["id"])

    def _close_unfilled(self, r, now, status, why):
        store.x("UPDATE signals SET status=?, closed_at=?, updated_at=?, outcome=?, gross_pnl=0, charges=0, net_pnl=0 WHERE id=?",
                (status, _now_s(now), _now_s(now), why, r["id"]))
        self.emit(now, status, f"{r['symbol']} signal {status.lower()}", why, r["id"])

    def _exit(self, r, now, qty, price, reason, final):
        cfg = self.cfg()
        p = r["plan"]
        exits = (r["exits"] or []) + [{"time": _now_s(now), "price": price, "qty": qty, "reason": reason}]
        remaining = r["remaining_qty"] - qty
        sold_val = sum(e["price"] * e["qty"] for e in exits)
        sold_qty = sum(e["qty"] for e in exits)
        gross = sum((e["price"] - r["fill_price"]) * e["qty"] for e in exits)
        ch = option_round_trip(r["fill_price"], sold_val / sold_qty if sold_qty else 0, sold_qty, cfg,
                               orders=1 + len(exits))["total"]
        status = final if (final and remaining <= 0) else r["status"]
        store.x("""UPDATE signals SET exits=?, remaining_qty=?, gross_pnl=?, charges=?, net_pnl=?, status=?,
                   updated_at=?, closed_at=?, outcome=? WHERE id=?""",
                (json.dumps(exits), remaining, round(gross, 2), ch, round(gross - ch, 2), status, _now_s(now),
                 _now_s(now) if remaining <= 0 else None, reason if remaining <= 0 else r["outcome"], r["id"]))
        r.update(exits=exits, remaining_qty=remaining, status=status)
        kind = {"TARGET_HIT": "TARGET_HIT", "STOP_LOSS_HIT": "STOP_LOSS_HIT", "EXPIRED": "EXPIRED"}.get(status, "T1")
        self.emit(now, kind, f"{p['tradingsymbol']}: {reason}",
                  f"Exit {qty} @ {price} | Net P&L so far Rs {gross - ch:,.0f}", r["id"])

    # ------------------------------------------------------------ stats
    def stats(self, day_from=None):
        rows = [r for r in self.list(limit=5000) if r["status"] not in OPEN_STATES and r["fill_price"]]
        if day_from:
            rows = [r for r in rows if r["day"] >= day_from]
        return trade_stats([{"pnl": r["net_pnl"] or 0, "symbol": r["symbol"], "hour": (r["filled_at"] or "")[11:13],
                             "score": r["score"], "side": r["plan"]["option_type"]} for r in rows])


def trade_stats(trades: list[dict]) -> dict:
    n = len(trades)
    if not n:
        return {"trades": 0}
    wins = [t["pnl"] for t in trades if t["pnl"] > 0]
    losses = [t["pnl"] for t in trades if t["pnl"] <= 0]
    eq, peak, mdd = 0.0, 0.0, 0.0
    for t in trades:
        eq += t["pnl"]; peak = max(peak, eq); mdd = min(mdd, eq - peak)

    def group(key):
        g = {}
        for t in trades:
            k = str(t.get(key))
            d = g.setdefault(k, {"trades": 0, "pnl": 0.0, "wins": 0})
            d["trades"] += 1; d["pnl"] = round(d["pnl"] + t["pnl"], 2); d["wins"] += t["pnl"] > 0
        for d in g.values():
            d["win_rate"] = round(d["wins"] / d["trades"] * 100, 1)
        return g
    gross_win, gross_loss = sum(wins), -sum(losses)
    return {"trades": n, "win_rate": round(len(wins) / n * 100, 1),
            "avg_profit": round(gross_win / len(wins), 2) if wins else 0,
            "avg_loss": round(-gross_loss / len(losses), 2) if losses else 0,
            "profit_factor": round(gross_win / gross_loss, 2) if gross_loss else None,
            "max_drawdown": round(mdd, 2), "expectancy": round(sum(t["pnl"] for t in trades) / n, 2),
            "net": round(sum(t["pnl"] for t in trades), 2),
            "by_symbol": group("symbol"), "by_hour": group("hour"), "by_score": group("score"), "by_side": group("side")}
