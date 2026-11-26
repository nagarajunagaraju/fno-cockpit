"""Sensex live commentary + trade-signal brain (pure logic, used by both live and backtest).

Input: completed 1-minute Sensex bars (time, o, h, l, c) + optional NIFTY % change, VIX % change and
option-OI levels. Output: commentary events. Every statement is derived from those numbers only.

Strategy (index has no volume, so no VWAP):
  1. 09:15-09:30 opening range (OR).
  2. Breakout: a 5-min close beyond the OR, on the right side of EMA20 (5-min), with NIFTY agreeing.
  3. Pullback: price comes back near the breakout level / EMA20 but does not close through EMA20.
  4. Trigger: a 5-min close beyond the previous 5-min bar's high (low for bear).
  SL = pullback extreme + buffer (0.12%-0.45% of price), T1 = 1.5R (half), T2 = 2.5R, stop to cost
  after T1, time stop 30 min, square-off 15:15. Max 2 trades a day; stop for the day after a loss.
"""
from __future__ import annotations

DEFAULTS = {
    "or_end": "09:30", "first_entry": "09:45", "last_entry": "13:30", "square_off": "15:15",
    "ema_len": 20, "touch_pct": 0.10, "ema_break_pct": 0.05, "sl_buffer_pct": 0.03,
    "min_sl_pct": 0.12, "max_sl_pct": 0.45, "t1_r": 1.5, "t2_r": 2.5, "min_room_r": 1.0,
    "max_trades": 2, "stop_after_loss": True, "time_stop_min": 30, "time_stop_r": 0.5,
    "vix_spike_pct": 8.0, "level_near_pct": 0.08,
}


def hm(t: str) -> int:
    h, m = t.split(":")
    return int(h) * 60 + int(m)


def fmt(x: float) -> str:
    return f"{x:,.0f}"


class Brain:
    def __init__(self, levels: dict, ema_seed: list[float], cfg: dict | None = None, oi_levels: dict | None = None):
        """levels: pdh, pdl, pdc (previous day high/low/close) - pivots are derived here.
        ema_seed: previous sessions' 5-min closes (oldest first) so EMA20 is ready at the open."""
        self.cfg = {**DEFAULTS, **(cfg or {})}
        self.lv = dict(levels)
        p = (levels["pdh"] + levels["pdl"] + levels["pdc"]) / 3
        rng = levels["pdh"] - levels["pdl"]
        self.lv.update(pivot=p, r1=2 * p - levels["pdl"], s1=2 * p - levels["pdh"], r2=p + rng, s2=p - rng)
        self.oi = oi_levels or {}
        self.k = 2 / (self.cfg["ema_len"] + 1)
        self.ema = None
        for c in ema_seed[-200:]:
            self.ema = c if self.ema is None else self.ema + self.k * (c - self.ema)
        self.bars1: list[dict] = []
        self.bars5: list[dict] = []
        self.cur5: dict | None = None
        self.open = self.orh = self.orl = None
        self.hi = self.lo = None
        self.state = {"BULL": "IDLE", "BEAR": "IDLE"}
        self.pb_ext = {"BULL": None, "BEAR": None}
        self.trades: list[dict] = []
        self.trade: dict | None = None
        self.losses = 0
        self.events: list[dict] = []
        self.broken: set[str] = set()
        self.nifty_chg = None
        self.vix_chg = None
        self.last = None

    # ------------------------------------------------------------------ helpers
    def _ev(self, t, kind, text, important=False, **data):
        e = {"t": t, "kind": kind, "text": text, "important": important, **data}
        self.events.append(e)
        return e

    def level_map(self) -> list[tuple[str, float]]:
        L = [("Ninnati high", self.lv["pdh"]), ("Ninnati low", self.lv["pdl"]), ("Ninnati close", self.lv["pdc"]),
             ("Pivot", self.lv["pivot"]), ("R1", self.lv["r1"]), ("S1", self.lv["s1"]), ("R2", self.lv["r2"]),
             ("S2", self.lv["s2"])]
        if self.orh is not None:
            L += [("Opening range high", self.orh), ("Opening range low", self.orl)]
        if self.oi.get("res"):
            L.append((f"Max CE OI {fmt(self.oi['res'])}", self.oi["res"]))
        if self.oi.get("sup"):
            L.append((f"Max PE OI {fmt(self.oi['sup'])}", self.oi["sup"]))
        return L

    def nearest(self, px: float) -> tuple[tuple | None, tuple | None]:
        above = sorted([x for x in self.level_map() if x[1] > px], key=lambda x: x[1])
        below = sorted([x for x in self.level_map() if x[1] < px], key=lambda x: -x[1])
        return (above[0] if above else None), (below[0] if below else None)

    def bias(self) -> str:
        if self.orh is None or self.ema is None or self.last is None:
            return "WAIT"
        c = self.last
        if c > self.orh and c > self.ema:
            return "BULLISH"
        if c < self.orl and c < self.ema:
            return "BEARISH"
        return "SIDEWAYS"

    # ------------------------------------------------------------------ main entry
    def on_bar(self, b: dict, nifty_chg: float | None = None, vix_chg: float | None = None,
               oi_levels: dict | None = None) -> list[dict]:
        """b = {"t": "HH:MM", "o", "h", "l", "c"} one completed 1-minute bar."""
        n0 = len(self.events)
        if oi_levels:
            self.oi = oi_levels
        self.nifty_chg, self.vix_chg = nifty_chg, vix_chg
        t = hm(b["t"])
        self.bars1.append(b)
        self.last = b["c"]
        if self.open is None:
            self.open = b["o"]
            self.hi, self.lo = b["h"], b["l"]
            gap = (b["o"] - self.lv["pdc"]) / self.lv["pdc"] * 100
            self._ev(b["t"], "INFO",
                     f"Sensex {fmt(b['o'])} lo open ({gap:+.2f}% vs ninnati close {fmt(self.lv['pdc'])}). "
                     f"Resistance: ninnati high {fmt(self.lv['pdh'])}, R1 {fmt(self.lv['r1'])}. "
                     f"Support: ninnati low {fmt(self.lv['pdl'])}, S1 {fmt(self.lv['s1'])}. "
                     f"First 15 min range form avvanivvu - ippude konaku.", True)
        self.hi, self.lo = max(self.hi, b["h"]), min(self.lo, b["l"])
        if t < hm(self.cfg["or_end"]):
            self.orh = b["h"] if self.orh is None else max(self.orh, b["h"])
            self.orl = b["l"] if self.orl is None else min(self.orl, b["l"])
            self._orw = True
        self._manage_trade(b, t)
        self._level_breaks(b)
        # 5-min aggregation (bars 09:15-09:19 -> 09:15 bucket)
        bucket = (t // 5) * 5
        if self.cur5 is None or self.cur5["m"] != bucket:
            self.cur5 = {"m": bucket, "o": b["o"], "h": b["h"], "l": b["l"], "c": b["c"], "n": 0}
        cb = self.cur5
        cb["h"], cb["l"], cb["c"], cb["n"] = max(cb["h"], b["h"]), min(cb["l"], b["l"]), b["c"], cb["n"] + 1
        if t % 5 == 4:   # last minute of the 5-min bucket -> bar complete
            end = bucket + 5   # label 5-min events with the time they are known (bar close)
            done = dict(cb, t=f"{end // 60:02d}:{end % 60:02d}")
            self.bars5.append(done)
            self.ema = done["c"] if self.ema is None else self.ema + self.k * (done["c"] - self.ema)
            self._on_5m(done, t)
        return self.events[n0:]

    # ------------------------------------------------------------------ level breaks (1-min closes)
    def _level_breaks(self, b):
        if len(self.bars1) < 2:
            return
        prev = self.bars1[-2]["c"]
        for name, lvl in self.level_map():
            key_up, key_dn = f"up:{name}", f"dn:{name}"
            if prev <= lvl < b["c"] and key_up not in self.broken:
                self.broken.add(key_up)
                self.broken.discard(key_dn)
                self._ev(b["t"], "LEVEL", f"{name} {fmt(lvl)} paina ki vachindi (1-min close {fmt(b['c'])}). "
                                          f"5-min close confirm ayye varaku wait.", name.startswith(("Ninnati", "Opening")))
            elif prev >= lvl > b["c"] and key_dn not in self.broken:
                self.broken.add(key_dn)
                self.broken.discard(key_up)
                self._ev(b["t"], "LEVEL", f"{name} {fmt(lvl)} kinda ki padindi (1-min close {fmt(b['c'])}). "
                                          f"5-min close confirm ayye varaku wait.", name.startswith(("Ninnati", "Opening")))

    # ------------------------------------------------------------------ 5-min logic
    def _on_5m(self, bar, t):
        cfg = self.cfg
        c = bar["c"]
        if t + 1 == hm(cfg["or_end"]):
            w = (self.orh - self.orl) / c * 100
            self._ev(bar["t"], "INFO", f"Opening range ready: {fmt(self.orl)} - {fmt(self.orh)} ({w:.2f}%). "
                                       f"Paina 5-min close aithe CE side, kinda close aithe PE side chustam. Madhyalo unte wait.", True)
            return
        if self.orh is None or t + 1 < hm(cfg["or_end"]):
            return
        near = self.cfg["touch_pct"] / 100
        brk = self.cfg["ema_break_pct"] / 100
        n_ok_bull = self.nifty_chg is None or self.nifty_chg > 0
        n_ok_bear = self.nifty_chg is None or self.nifty_chg < 0
        vix_bad = self.vix_chg is not None and self.vix_chg >= cfg["vix_spike_pct"]
        in_window = hm(cfg["first_entry"]) <= t + 1 <= hm(cfg["last_entry"])
        done_day = (len(self.trades) >= cfg["max_trades"]) or (cfg["stop_after_loss"] and self.losses > 0)
        prev = self.bars5[-2] if len(self.bars5) >= 2 else None
        msgs = []
        for side in ("BULL", "BEAR"):
            up = side == "BULL"
            st = self.state[side]
            lvl = max(self.orh, self.ema) if up else min(self.orl, self.ema)
            beyond_or = c > self.orh if up else c < self.orl
            right_ema = c > self.ema if up else c < self.ema
            nifty_ok = n_ok_bull if up else n_ok_bear
            fail = c < self.ema * (1 - brk) if up else c > self.ema * (1 + brk)
            if st == "IDLE":
                if beyond_or and right_ema and nifty_ok:
                    self.state[side] = "BROKEN"
                    msgs.append(("SETUP", f"{'Opening range high' if up else 'Opening range low'} "
                                 f"{fmt(self.orh if up else self.orl)} {'paina' if up else 'kinda'} 5-min close {fmt(c)} ✅ "
                                 f"EMA20 {fmt(self.ema)} {'paina' if up else 'kinda'}"
                                 + (f", NIFTY {self.nifty_chg:+.2f}%" if self.nifty_chg is not None else "")
                                 + f". Chase cheyaku - {fmt(lvl)} daggara pullback kosam wait ({'CE' if up else 'PE'} side).", True))
            elif st in ("BROKEN", "PULLBACK"):
                if fail:
                    self.state[side], self.pb_ext[side] = "IDLE", None
                    msgs.append(("WAIT", f"{'Paiki' if up else 'Kindiki'} move fail: 5-min close {fmt(c)} EMA20 {fmt(self.ema)} "
                                 f"{'kinda' if up else 'paina'}. {'CE' if up else 'PE'} setup cancel.", True))
                    continue
                touched = bar["l"] <= lvl * (1 + near) if up else bar["h"] >= lvl * (1 - near)
                if st == "BROKEN" and touched:
                    self.state[side] = "PULLBACK"
                    self.pb_ext[side] = bar["l"] if up else bar["h"]
                    msgs.append(("SETUP", f"Pullback {fmt(bar['l'] if up else bar['h'])} varaku vachindi, {fmt(lvl)} "
                                 f"{'support' if up else 'resistance'} hold avutundi. Next 5-min candle "
                                 f"{fmt(bar['h'] if up else bar['l'])} {'paina' if up else 'kinda'} close aithe "
                                 f"{'CE' if up else 'PE'} entry.", True))
                    continue
                if st == "PULLBACK":
                    self.pb_ext[side] = min(self.pb_ext[side], bar["l"]) if up else max(self.pb_ext[side], bar["h"])
                    trig = prev and ((c > prev["h"]) if up else (c < prev["l"]))
                    if trig and right_ema:
                        if not in_window or done_day or self.trade is not None or vix_bad or not nifty_ok:
                            why = ("entry time ayipoyindi (13:30 tarvata)" if not in_window else
                                   "ivala trades limit / loss stop" if done_day else
                                   "already oka trade lo unnam" if self.trade else
                                   f"VIX {self.vix_chg:+.1f}% spike - risky" if vix_bad else "NIFTY agree avvatledu")
                            msgs.append(("WAIT", f"{'CE' if up else 'PE'} trigger vachindi kani skip: {why}.", False))
                            self.state[side] = "IDLE"
                            continue
                        self._signal(bar, side)
                        self.state[side] = "IDLE"
                        continue
        for kind, text, imp in msgs:
            self._ev(bar["t"], kind, text, imp)
        if not msgs and self.trade is None:
            self._status(bar, done_day, in_window)

    def _status(self, bar, done_day, in_window):
        c = bar["c"]
        res, sup = self.nearest(c)
        b = self.bias()
        rs = f"Resistance {res[0]} {fmt(res[1])}" if res else "Resistance ledu (day high zone)"
        ss = f"support {sup[0]} {fmt(sup[1])}" if sup else "support ledu"
        tail = ("Ivala trading aapu." if done_day else
                "Entry time ayipoyindi - kotha trade vaddu." if not in_window and hm(bar["t"]) > hm(self.cfg["last_entry"]) else
                {"BULLISH": "Trend paiki - pullback vaste CE chudu, ippude chase cheyaku.",
                 "BEARISH": "Trend kindiki - pullback vaste PE chudu, ippude chase cheyaku.",
                 "SIDEWAYS": "Range lo undi - trade vaddu, wait.", "WAIT": "Wait."}[b])
        self._ev(bar["t"], "STATUS", f"{fmt(c)} | {b} | {rs}, {ss}. EMA20 {fmt(self.ema)}. {tail}")

    # ------------------------------------------------------------------ signals + trade management (spot)
    def _signal(self, bar, side):
        cfg = self.cfg
        up = side == "BULL"
        entry = bar["c"]
        buf = cfg["sl_buffer_pct"] / 100
        sl = self.pb_ext[side] * (1 - buf) if up else self.pb_ext[side] * (1 + buf)
        risk = abs(entry - sl)
        rp = risk / entry * 100
        if rp > cfg["max_sl_pct"]:
            self._ev(bar["t"], "WAIT", f"{'CE' if up else 'PE'} trigger kani SL {rp:.2f}% chala wide (max {cfg['max_sl_pct']}%). Skip.", True)
            return
        if rp < cfg["min_sl_pct"]:
            risk = entry * cfg["min_sl_pct"] / 100
            sl = entry - risk if up else entry + risk
        res, sup = self.nearest(entry)
        block = res if up else sup
        if block and abs(block[1] - entry) < cfg["min_room_r"] * risk:
            self._ev(bar["t"], "WAIT", f"{'CE' if up else 'PE'} trigger kani {block[0]} {fmt(block[1])} chala daggara "
                                       f"({fmt(abs(block[1] - entry))} pts, risk {fmt(risk)} pts). Room ledu - skip.", True)
            return
        t1 = entry + cfg["t1_r"] * risk if up else entry - cfg["t1_r"] * risk
        t2 = entry + cfg["t2_r"] * risk if up else entry - cfg["t2_r"] * risk
        self.trade = {"side": side, "entry": entry, "sl": sl, "stop": sl, "t1": t1, "t2": t2, "risk": risk,
                      "t": bar["t"], "m": hm(bar["t"]) - 1, "half": False, "r": 0.0}
        self.trades.append(self.trade)
        self._ev(bar["t"], "SIGNAL",
                 f"🔥 {'BUY CE' if up else 'BUY PE'} - pullback tarvata {'paiki' if up else 'kindiki'} turn. "
                 f"Sensex entry {fmt(entry)}, SL {fmt(sl)} ({fmt(risk)} pts), T1 {fmt(t1)}, T2 {fmt(t2)}.", True,
                 side=side, spot_entry=entry, spot_sl=sl, spot_t1=t1, spot_t2=t2)

    def _manage_trade(self, b, t):
        tr = self.trade
        if tr is None or t <= tr["m"]:
            return
        up = tr["side"] == "BULL"
        cfg = self.cfg
        hit_sl = b["l"] <= tr["stop"] if up else b["h"] >= tr["stop"]
        if hit_sl:
            tr["r"] += (0.5 * 0 if tr["half"] else -1.0)
            self._close(b["t"], "SL" if not tr["half"] else "COST",
                        f"{'SL hit' if not tr['half'] else 'Cost daggara exit (T1 tarvata)'} at {fmt(tr['stop'])}. "
                        + ("Ivala inka trades vaddu." if not tr["half"] and cfg["stop_after_loss"] else ""))
            if not tr["half"]:
                self.losses += 1
            return
        if not tr["half"] and (b["h"] >= tr["t1"] if up else b["l"] <= tr["t1"]):
            tr["half"], tr["stop"], tr["r"] = True, tr["entry"], cfg["t1_r"] * 0.5
            self._ev(b["t"], "T1", f"🎯 T1 {fmt(tr['t1'])} vachindi. Sagam book chey, migata daniki SL cost ({fmt(tr['entry'])}) ki marchu.", True)
        if tr["half"] and (b["h"] >= tr["t2"] if up else b["l"] <= tr["t2"]):
            tr["r"] += cfg["t2_r"] * 0.5
            self._close(b["t"], "T2", f"🎯🎯 T2 {fmt(tr['t2'])} vachindi. Motham book chey.")
            return
        if not tr["half"] and t - tr["m"] >= cfg["time_stop_min"]:
            move = (b["c"] - tr["entry"]) / tr["risk"] * (1 if up else -1)
            if move < cfg["time_stop_r"]:
                tr["r"] = move
                self._close(b["t"], "TIME", f"{cfg['time_stop_min']} min lo move raaledu ({move:+.2f}R). Exit at {fmt(b['c'])} - premium theta lo potundi.")
                if move < 0:
                    self.losses += 1
                return
        if t >= hm(cfg["square_off"]):
            move = (b["c"] - tr["entry"]) / tr["risk"] * (1 if up else -1)
            tr["r"] = (tr["r"] + 0.5 * move) if tr["half"] else move
            self._close(b["t"], "SQUARE_OFF", f"15:15 - position close chey at {fmt(b['c'])}.")

    def _close(self, t, why, text):
        tr = self.trade
        tr["exit"], tr["exit_t"] = why, t
        self.trade = None
        self._ev(t, "EXIT", f"{text} Trade result {tr['r']:+.2f}R.", True, result_r=round(tr["r"], 2), why=why)

    def snapshot(self) -> dict:
        c = self.last
        res, sup = self.nearest(c) if c else (None, None)
        ladder = sorted(self.level_map(), key=lambda x: -x[1])
        return {"last": c, "open": self.open, "day_high": self.hi, "day_low": self.lo, "orh": self.orh, "orl": self.orl,
                "ema20_5m": round(self.ema, 2) if self.ema else None, "bias": self.bias(),
                "resistance": res, "support": sup, "levels": [{"name": n, "value": round(v, 2)} for n, v in ladder],
                "state": self.state, "trades_today": len(self.trades), "losses": self.losses,
                "open_trade": self.trade, "nifty_chg": self.nifty_chg, "vix_chg": self.vix_chg}
