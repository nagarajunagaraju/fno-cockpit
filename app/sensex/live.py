"""Live Sensex commentary engine: polls Kite (quote API), builds exact 1-min bars, feeds the Brain,
picks the BSE option contract for signals and tracks them in paper mode through the signal manager."""
from __future__ import annotations

import datetime as dt
import logging
import threading
import time
import traceback

from .. import options
from ..engine import store
from .brain import Brain, fmt, hm

log = logging.getLogger("cockpit.sensex")
IST = dt.timezone(dt.timedelta(hours=5, minutes=30))
SPOT, NIFTY, VIX = "BSE:SENSEX", "NSE:NIFTY 50", "NSE:INDIA VIX"


def ist_now():
    return dt.datetime.now(IST)


class SensexLive:
    def __init__(self, src, manager, cfg_getter):
        self.src, self.manager, self.cfg = src, manager, cfg_getter
        self.day = None
        self.brain: Brain | None = None
        self.comm: list[dict] = []
        self.status = {"stage": "waiting for Kite login", "error": None, "updated": None}
        self.tokens: dict = {}
        self.chain: dict = {}
        self.quotes: dict = {}
        self.cur: dict | None = None
        self.prev_close: dict = {}
        self.lock = threading.Lock()
        self._fails = 0

    def start(self):
        threading.Thread(target=self._loop, daemon=True, name="sensex").start()

    # ------------------------------------------------------------------ loop
    def _loop(self):
        while True:
            try:
                self.tick()
                self._fails = 0
                time.sleep(5)
            except Exception as e:
                self._fails = min(self._fails + 1, 6)
                self.status["error"] = f"{type(e).__name__}: {e}"
                log.warning("sensex tick: %s\n%s", e, traceback.format_exc()[-800:])
                time.sleep(10 * self._fails)

    def tick(self):
        now = ist_now()
        if now.weekday() >= 5:
            self.status["stage"] = "weekend - market closed"
            return
        kite = getattr(self.src, "kite", None)
        if not kite or not self.src.logged_in():
            self.status["stage"] = "waiting for Kite login (login every morning ~09:00)"
            return
        t = now.hour * 60 + now.minute
        if t < hm("09:00") or t > hm("15:35"):
            self.status["stage"] = "market closed"
            return
        today = now.date().isoformat()
        if self.day != today:
            self._init_day(kite, now)
        self._poll(kite, now)

    # ------------------------------------------------------------------ day setup
    def _init_day(self, kite, now):
        self.status["stage"] = "loading Sensex levels"
        q = kite.quote([SPOT, NIFTY, VIX])
        self.tokens = {k: v["instrument_token"] for k, v in q.items()}
        self.prev_close = {k: (v.get("ohlc") or {}).get("close") for k, v in q.items()}
        today = now.date()
        tok = self.tokens[SPOT]
        daily = [c for c in kite.historical_data(tok, today - dt.timedelta(days=12), today, "day") if c["date"].date() < today]
        if not daily:
            raise RuntimeError("no Sensex daily history from Kite")
        p = daily[-1]
        levels = {"pdh": p["high"], "pdl": p["low"], "pdc": p["close"]}
        five = kite.historical_data(tok, today - dt.timedelta(days=8), today - dt.timedelta(days=1), "5minute")
        seed = [c["close"] for c in five]
        self._load_chain(kite, today)
        with self.lock:
            self.brain = Brain(levels, seed)
            self.comm = []
            self.cur = None
            self.day = today.isoformat()
        rep = store.kv_get("eod_report") or {}
        fii = (rep.get("fii") or {}).get("mood") if rep.get("for_date") == today.isoformat() else None
        self._add({"t": now.strftime("%H:%M"), "kind": "INFO", "important": False,
                   "text": f"Good morning. Ninna Sensex high {fmt(levels['pdh'])}, low {fmt(levels['pdl'])}, close "
                           f"{fmt(levels['pdc'])}. Pivot {fmt(self.brain.lv['pivot'])}, R1 {fmt(self.brain.lv['r1'])}, "
                           f"S1 {fmt(self.brain.lv['s1'])}." + (f" Ninna FII mood: {fii}." if fii else "")
                           + f" Options expiry vaadedi: {self.chain.get('expiry', 'n/a')}."})
        # started late: replay today's completed minutes so levels, EMA and state are exact
        if now.hour * 60 + now.minute > hm("09:16"):
            self.status["stage"] = "catching up today's candles"
            bars = kite.historical_data(tok, now.replace(hour=9, minute=15, second=0, microsecond=0).replace(tzinfo=None),
                                        now.replace(tzinfo=None), "minute")
            nb = {c["date"].strftime("%H:%M"): c["close"]
                  for c in kite.historical_data(self.tokens[NIFTY], now.replace(hour=9, minute=15, second=0, microsecond=0).replace(tzinfo=None),
                                                now.replace(tzinfo=None), "minute")}
            npc = self.prev_close.get(NIFTY)
            done = [c for c in bars if c["date"].strftime("%H:%M") < now.strftime("%H:%M")]
            for c in done:
                tt = c["date"].strftime("%H:%M")
                nc = nb.get(tt)
                for e in self.brain.on_bar({"t": tt, "o": c["open"], "h": c["high"], "l": c["low"], "c": c["close"]},
                                           nifty_chg=(nc - npc) / npc * 100 if nc and npc else None):
                    e["replayed"] = True
                    self._add(e, notify=False)
            if done:
                self._add({"t": now.strftime("%H:%M"), "kind": "INFO", "important": False,
                           "text": f"App late ga start ayyindi: 09:15 nunchi {len(done)} candles replay chesanu. "
                                   f"Paina unna old signals ki ippudu entry cheyaku."})

    def _load_chain(self, kite, today):
        ins = [i for i in kite.instruments("BFO") if i.get("name") == "SENSEX" and i["instrument_type"] in ("CE", "PE")]
        exps = sorted({i["expiry"] for i in ins if i["expiry"] >= today})
        if self.cfg().get("sensex_avoid_expiry_day", True):
            exps = [e for e in exps if e > today] or exps
        if not exps:
            self.chain = {}
            return
        exp = exps[0]
        legs = [i for i in ins if i["expiry"] == exp]
        strikes = sorted({float(i["strike"]) for i in legs})
        step = min(b - a for a, b in zip(strikes, strikes[1:]) if b > a) if len(strikes) > 1 else 100
        self.chain = {"expiry": exp.isoformat(), "strikes": strikes, "step": step,
                      "lot": int(legs[0]["lot_size"]),
                      "ts": {(float(i["strike"]), i["instrument_type"]): i["tradingsymbol"] for i in legs},
                      "by_ts": {i["tradingsymbol"]: (float(i["strike"]), i["instrument_type"]) for i in legs}}

    # ------------------------------------------------------------------ polling
    def _window(self, spot, n=12):
        if not self.chain:
            return []
        st = self.chain["step"]
        atm = round(spot / st) * st
        out = []
        for k in range(-n, n + 1):
            K = atm + k * st
            for typ in ("CE", "PE"):
                ts = self.chain["ts"].get((K, typ))
                if ts:
                    out.append(ts)
        return out

    def _poll(self, kite, now):
        last_spot = (self.quotes.get(SPOT) or {}).get("last_price") or self.prev_close.get(SPOT)
        opts = self._window(last_spot) if last_spot else []
        open_ts = [r["plan"]["tradingsymbol"] for r in self.manager.open_signals() if r["symbol"] == "SENSEX"]
        keys = [SPOT, NIFTY, VIX] + [f"BFO:{t}" for t in dict.fromkeys(opts + open_ts)]
        q = kite.quote(keys)
        self.quotes = q
        sp = q.get(SPOT) or {}
        ltp = sp.get("last_price")
        self.status.update(stage="live", updated=now.isoformat(timespec="seconds"), error=None)
        if not ltp:
            return
        hhmm = now.strftime("%H:%M")
        # minute boundary: finalise the previous minute with Kite's exact 1-min candle
        if self.cur and self.cur["t"] != hhmm and hhmm >= "09:16":
            self._finish_minute(kite, self.cur["t"], now)
        if not self.cur or self.cur["t"] != hhmm:
            self.cur = {"t": hhmm, "o": ltp, "h": ltp, "l": ltp, "c": ltp}
        else:
            c = self.cur
            c["h"], c["l"], c["c"] = max(c["h"], ltp), min(c["l"], ltp), ltp
        self._paper_update(now)

    def _chg(self, key):
        q = self.quotes.get(key) or {}
        pc = self.prev_close.get(key)
        lp = q.get("last_price")
        return round((lp - pc) / pc * 100, 2) if lp and pc else None

    def _oi_levels(self, spot):
        ce, pe = {}, {}
        for k, v in self.quotes.items():
            if not k.startswith("BFO:"):
                continue
            key = self.chain.get("by_ts", {}).get(k[4:])
            if key:
                (ce if key[1] == "CE" else pe)[key[0]] = v.get("oi") or 0
        res = max(((K, o) for K, o in ce.items() if K >= spot), key=lambda x: x[1], default=(None, 0))[0]
        sup = max(((K, o) for K, o in pe.items() if K <= spot), key=lambda x: x[1], default=(None, 0))[0]
        return {"res": res, "sup": sup}

    def _finish_minute(self, kite, t, now):
        bar = dict(self.cur)
        try:   # exact OHLC from Kite (polling every 5 s can miss a wick)
            start = now.replace(hour=int(t[:2]), minute=int(t[3:]), second=0, microsecond=0).replace(tzinfo=None)
            cs = kite.historical_data(self.tokens[SPOT], start, start + dt.timedelta(minutes=1), "minute")
            m = [c for c in cs if c["date"].strftime("%H:%M") == t]
            if m:
                bar = {"t": t, "o": m[0]["open"], "h": m[0]["high"], "l": m[0]["low"], "c": m[0]["close"]}
        except Exception as e:
            self.status["error"] = f"1-min candle fallback to ticks: {e}"
        if t < "09:15" or t > "15:29":
            return
        spot = bar["c"]
        with self.lock:
            evs = self.brain.on_bar(bar, nifty_chg=self._chg(NIFTY), vix_chg=self._chg(VIX), oi_levels=self._oi_levels(spot))
        for e in evs:
            self._add(e)
            if e["kind"] == "SIGNAL":
                self._make_signal(e, now)
        if t == "15:29":
            b = self.brain
            r = sum(x.get("r", 0) for x in b.trades)
            self._add({"t": "15:30", "kind": "INFO", "important": True,
                       "text": f"Market close {fmt(spot)}. Ivala {len(b.trades)} trade(s), result {r:+.2f}R. "
                               f"Day range {fmt(b.lo)} - {fmt(b.hi)}. Repu malli kaluddam."})

    # ------------------------------------------------------------------ commentary store + notifications
    def _add(self, e, notify=True):
        e = dict(e)
        with self.lock:
            e["id"] = (self.comm[-1]["id"] + 1) if self.comm else 1
            self.comm.append(e)
            self.comm = self.comm[-500:]
        store.kv_set(f"sensex_comm:{self.day}", self.comm[-300:])
        if notify and e.get("important") and e["kind"] != "SIGNAL" and not e.get("replayed"):
            self.manager.emit(ist_now(), "SENSEX", f"Sensex {e['t']}: {e['kind']}", e["text"])

    # ------------------------------------------------------------------ option contract + paper signal
    def pick_contract(self, side: str, spot: float, now) -> dict:
        cfg = self.cfg()
        typ = "CE" if side == "BULL" else "PE"
        T = options.years_to_expiry(self.chain["expiry"], now)
        cands = []
        for k, v in self.quotes.items():
            if not k.startswith("BFO:"):
                continue
            key = self.chain["by_ts"].get(k[4:])
            if not key or key[1] != typ:
                continue
            d = v.get("depth") or {}
            bid = ((d.get("buy") or [{}])[0] or {}).get("price") or 0
            ask = ((d.get("sell") or [{}])[0] or {}).get("price") or 0
            if bid <= 0 or ask <= 0:
                continue
            mid = (bid + ask) / 2
            spread = (ask - bid) / mid * 100
            iv = options.implied_vol(mid, spot, key[0], T, typ)
            g = options.greeks(spot, key[0], T, iv, typ) if iv else {}
            if g.get("delta") is None:
                continue
            cands.append({"ts": k[4:], "strike": key[0], "bid": bid, "ask": ask, "ltp": v.get("last_price"),
                          "oi": v.get("oi"), "spread_pct": round(spread, 2), "iv": round(iv * 100, 1),
                          "delta": g["delta"], "theta": g["theta"],
                          "ok": spread <= cfg["sensex_max_spread_pct"]})
        ok = [c for c in cands if c["ok"]]
        if not ok:
            return {"error": "No Sensex option with a two-sided quote and spread <= "
                             f"{cfg['sensex_max_spread_pct']}%", "candidates": cands}
        best = min(ok, key=lambda c: abs(abs(c["delta"]) - cfg["sensex_target_delta"]))
        return {"selected": best, "candidates": sorted(cands, key=lambda c: c["strike"])}

    def _make_signal(self, e, now):
        cfg = self.cfg()
        side = e["side"]
        spot = e["spot_entry"]
        sel = self.pick_contract(side, spot, now)
        if sel.get("error"):
            self._add({"t": e["t"], "kind": "WAIT", "important": True, "text": f"Signal vachindi kani option skip: {sel['error']}."})
            return
        c = sel["selected"]
        dlt = abs(c["delta"])
        risk_pts = abs(spot - e["spot_sl"])
        entry = c["ask"]
        sl = round(max(entry * 0.5, entry - dlt * risk_pts * 1.15), 1)
        t1 = round(entry + dlt * abs(e["spot_t1"] - spot), 1)
        t2 = round(entry + dlt * abs(e["spot_t2"] - spot), 1)
        lot = self.chain["lot"]
        per_lot = (entry - sl) * lot
        budget = cfg["capital"] * cfg.get("sensex_risk_pct", 1.0) / 100
        lots = int(budget // per_lot) if per_lot > 0 else 0
        if lots < 1:
            self._add({"t": e["t"], "kind": "WAIT", "important": True,
                       "text": f"Signal vachindi kani 1 lot risk ₹{per_lot:,.0f} > budget ₹{budget:,.0f}. Skip."})
            return
        qty = lots * lot
        typ = "CE" if side == "BULL" else "PE"
        plan = {"symbol": "SENSEX", "tradingsymbol": c["ts"], "direction": side, "bias": side, "option_type": typ,
                "strike": c["strike"], "expiry": self.chain["expiry"], "lot_size": lot, "lots": lots, "qty": qty,
                "entry_premium": entry, "entry_range": [round(entry * 0.98, 1), round(entry * 1.02, 1)],
                "sl_premium": sl, "t1_premium": t1, "t2_premium": t2, "spot_entry": round(spot, 1),
                "spot_sl": round(e["spot_sl"], 1), "spot_t1": round(e["spot_t1"], 1), "spot_t2": round(e["spot_t2"], 1),
                "max_loss": round((entry - sl) * qty), "premium_outlay": round(entry * qty), "per_lot_risk": round(per_lot),
                "delta": c["delta"], "iv": c["iv"], "theta": c["theta"], "spread_pct": c["spread_pct"],
                "dte": options.days_to_expiry(self.chain["expiry"]),
                "rr_t1": round((t1 - entry) / (entry - sl), 2), "rr_t2": round((t2 - entry) / (entry - sl), 2),
                "signal_time": now.isoformat(timespec="seconds"), "confidence": "RULES",
                "why_entry": e["text"], "why_strike": f"{c['strike']:g}{typ}: delta {dlt:.2f} (target {cfg['sensex_target_delta']}), "
                                                     f"spread {c['spread_pct']}%, IV {c['iv']}%, OI {c['oi']:,}",
                "invalidation": f"Sensex 1-min close {'below' if side == 'BULL' else 'above'} {fmt(e['spot_sl'])}"}
        title = (f"BUY {c['ts']} | Sensex {'paiki' if side == 'BULL' else 'kindiki'} | "
                 f"Entry {plan['entry_range'][0]}-{plan['entry_range'][1]}")
        ev = {"symbol": "SENSEX", "direction": side, "trigger": {"level": spot}, "score": 0, "grade": None,
              "own_limits": True, "title": title}
        sel_rec = {"candidates": sel["candidates"], "selected": c, "type": typ, "expiry": self.chain["expiry"]}
        expl = {"why_stock": "Sensex only mode", "why_entry": e["text"], "why_strike": plan["why_strike"]}
        res = self.manager.on_trigger(ev, sel_rec, plan, expl, now)
        txt = (f"👉 {c['ts']} konu: ₹{plan['entry_range'][0]}-{plan['entry_range'][1]} lo. SL ₹{sl}, T1 ₹{t1}, T2 ₹{t2}. "
               f"{lots} lot ({qty} qty), max loss ₹{plan['max_loss']:,}.")
        if not res.get("created"):
            txt = f"Option plan {c['ts']} ready kani risk rule block chesindi: {res.get('reason')}."
        self._add({"t": e["t"], "kind": "PLAN", "important": False, "text": txt, "signal_id": res.get("id")}, notify=False)

    def _paper_update(self, now):
        quotes = {}
        for k, v in self.quotes.items():
            if k.startswith("BFO:"):
                d = v.get("depth") or {}
                quotes[k[4:]] = {"ltp": v.get("last_price"),
                                 "bid": ((d.get("buy") or [{}])[0] or {}).get("price") or None,
                                 "ask": ((d.get("sell") or [{}])[0] or {}).get("price") or None}
        last = self.brain.bars1[-1]["c"] if self.brain and self.brain.bars1 else None
        self.manager.update(now, quotes, {"SENSEX": last} if last else {})

    # ------------------------------------------------------------------ API view
    def view(self, after: int = 0) -> dict:
        with self.lock:
            snap = self.brain.snapshot() if self.brain else None
            comm = [c for c in self.comm if c["id"] > after]
        sp = self.quotes.get(SPOT) or {}
        live = {"ltp": sp.get("last_price"), "chg_pct": self._chg(SPOT), "nifty_chg": self._chg(NIFTY),
                "vix": (self.quotes.get(VIX) or {}).get("last_price"), "vix_chg": self._chg(VIX)}
        oi = self._oi_levels(sp["last_price"]) if sp.get("last_price") and self.chain else {}
        return {"status": self.status, "day": self.day, "live": live, "brain": snap, "oi": oi,
                "expiry": self.chain.get("expiry"), "lot": self.chain.get("lot"), "commentary": comm[-300:]}
