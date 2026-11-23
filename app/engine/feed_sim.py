"""SIMULATED market feed - used for demo mode and automated tests only.

Generates synthetic intraday sessions (some stocks trend strongly up / down with
volume and OI build-up, most chop) so the whole pipeline - scanner, contract
selection, notifications, paper trading, backtest - can be exercised without a
Kite login. Everything produced here is labelled SIMULATED and must never be
used to judge real-market accuracy.
"""
from __future__ import annotations

import datetime as dt
import math
import random
import threading
import time

from .. import options
from ..data_source import DEMO_STOCKS
from .config import IST, hm
from .sectors import INDEX_BANKNIFTY, INDEX_NIFTY, sector_of

N_MIN = 375  # 09:15 .. 15:29


def _strike_step(px):
    return 5 if px < 500 else 10 if px < 1000 else 20 if px < 2500 else 50 if px < 6000 else 100


def _vol_weight(m):
    return 1 + 2.2 * math.exp(-m / 18) + 1.4 * math.exp(-(N_MIN - 1 - m) / 20)


def _trading_days_before(d: dt.date, n: int) -> list[dt.date]:
    out, x = [], d
    while len(out) < n:
        x -= dt.timedelta(days=1)
        if x.weekday() < 5:
            out.append(x)
    return sorted(out)


class SimFeed:
    mode = "SIMULATED"

    def __init__(self, cfg_getter, seed: int = 11, speed: float = 20.0, manual: bool = False,
                 day: dt.date | None = None):
        self.cfg = cfg_getter
        self.seed = seed
        self.speed = speed
        self.manual = manual
        self.lock = threading.RLock()
        self.universe = [{"symbol": s, "lot_size": lot, "base": px} for s, lot, px in DEMO_STOCKS]
        self.registry: dict[str, tuple] = {}
        self.manual_minute = 0.0
        self.day0 = day or dt.datetime.now(IST).date()
        self.real0 = time.time()
        self._build(self.day0)

    # ------------------------------------------------------------ generation
    def _gen_day(self, sym, base, prev_close, prev_oi, lot, day: dt.date):
        r = random.Random(f"{self.seed}-{sym}-{day.isoformat()}")
        u = r.random()
        regime = "up" if u < 0.10 else "down" if u < 0.20 else "mild_up" if u < 0.35 else "mild_down" if u < 0.5 else "chop"
        drift_total = {"up": r.uniform(1.6, 3.0), "down": -r.uniform(1.6, 3.0), "mild_up": r.uniform(0.3, 1.0),
                       "mild_down": -r.uniform(0.3, 1.0), "chop": r.uniform(-0.3, 0.3)}[regime] / 100
        mr = random.Random(f"{self.seed}-market-{day.isoformat()}")
        mkt = mr.choice([-1, 1]) * mr.uniform(0.3, 0.9) / 100      # market-wide drift of the day
        if regime in ("up", "down") and (mkt > 0) != (regime == "up") and r.random() < 0.6:
            regime = "up" if mkt > 0 else "down"                   # trending stocks mostly follow the market
            drift_total = (1 if mkt > 0 else -1) * r.uniform(1.6, 3.0) / 100
        drift_total += mkt * 0.8
        vol_mult = {"up": r.uniform(2.0, 3.2), "down": r.uniform(2.0, 3.2)}.get(regime, r.uniform(0.7, 1.3))
        oi_total = {"up": 0.06, "down": 0.06, "mild_up": 0.01, "mild_down": 0.01}.get(regime, r.uniform(-0.01, 0.01))
        gap = r.gauss(0, 0.002)
        day_vol = lot * r.uniform(1500, 4000)
        o = prev_close * (1 + gap)
        bars, px, oi = [], o, prev_oi
        wsum = sum(_vol_weight(m) for m in range(N_MIN))
        def path(mm):  # fraction of the day's drift reached by minute mm
            if regime in ("up", "down"):   # impulse -> pullback towards VWAP -> trend resumes
                if mm < 35:
                    return 0.7 * (mm + 1) / 35
                if mm < 60:
                    return 0.7 - 0.38 * (mm - 34) / 25
                return 0.32 + 0.68 * (mm - 59) / (N_MIN - 59)
            return 1 - math.exp(-(mm + 1) / 90)
        for m in range(N_MIN):
            shape = path(m)
            step = drift_total * (shape - (path(m - 1) if m else 0.0))
            noise = r.gauss(0, 0.0009 if regime in ("up", "down") else 0.0012)
            if regime == "up" and m < 3:
                noise = abs(noise)
            if regime == "down" and m < 3:
                noise = -abs(noise)
            c = px * (1 + step + noise)
            hi = max(px, c) * (1 + abs(r.gauss(0, 0.0004)))
            lo = min(px, c) * (1 - abs(r.gauss(0, 0.0004)))
            if m == 0:
                if regime == "up":
                    lo = px
                elif regime == "down":
                    hi = px
            v = day_vol * vol_mult * _vol_weight(m) / wsum * r.uniform(0.6, 1.4)
            if regime in ("up", "down") and 35 <= m < 60:
                v *= 0.55          # pullback on lighter volume
            if regime in ("up", "down") and 60 <= m < 70:
                v *= 1.6           # resumption with volume
            oi = prev_oi * (1 + oi_total * min(1.0, (m + 1) / 120) + r.gauss(0, 0.001))
            bars.append({"m": m, "t": (dt.datetime(2000, 1, 1, 9, 15) + dt.timedelta(minutes=m)).strftime("%H:%M"),
                         "o": round(px, 2), "h": round(hi, 2), "l": round(lo, 2), "c": round(c, 2), "v": round(v),
                         "oi": round(oi)})
            px = c
        return {"bars": bars, "regime": regime, "oi_total": oi_total}

    def _history(self, sym, base, lot, days: list[dt.date]):
        r = random.Random(f"{self.seed}-{sym}-daily")
        price = base * r.uniform(0.9, 1.1)
        daily, sessions = [], {}
        d = days[0] - dt.timedelta(days=120)
        while d < days[0]:
            if d.weekday() < 5:
                c = price * (1 + r.gauss(0, 0.012))
                daily.append({"date": d.isoformat(), "open": price, "high": max(price, c) * 1.006,
                              "low": min(price, c) * 0.994, "close": c})
                price = c
            d += dt.timedelta(days=1)
        oi = int(r.uniform(3e6, 3e7) / lot) * lot
        for day in days:
            g = self._gen_day(sym, base, price, oi, lot, day)
            sessions[day.isoformat()] = g
            bs = g["bars"]
            daily.append({"date": day.isoformat(), "open": bs[0]["o"], "high": max(b["h"] for b in bs),
                          "low": min(b["l"] for b in bs), "close": bs[-1]["c"]})
            price, oi = bs[-1]["c"], bs[-1]["oi"]
        return daily, sessions

    def _build(self, today: dt.date):
        past = _trading_days_before(today, 12)
        self.today = today
        self.data = {}
        for u in self.universe:
            daily, sessions = self._history(u["symbol"], u["base"], u["lot_size"], past + [today])
            self.data[u["symbol"]] = {"daily": daily[:-1], "today": sessions[today.isoformat()],
                                      "past": {k: v for k, v in sessions.items() if k != today.isoformat()}}
        self.rvol = {s: self._baseline(d["past"]) for s, d in self.data.items()}
        self.rank = {u["symbol"]: i + 1 for i, u in enumerate(sorted(self.universe, key=lambda u: -u["base"] * u["lot_size"]))}

    @staticmethod
    def _baseline(past):
        from .candles import build_rvol_baseline
        return build_rvol_baseline([v["bars"] for _, v in sorted(past.items())][-10:])

    # ------------------------------------------------------------ clock
    def minute_float(self) -> float:
        if self.manual:
            return self.manual_minute
        m = (time.time() - self.real0) * self.speed / 60
        day_len = N_MIN + 30
        if m >= day_len:  # roll to the next simulated session
            with self.lock:
                nxt = self.today + dt.timedelta(days=1)
                while nxt.weekday() >= 5:
                    nxt += dt.timedelta(days=1)
                self._build(nxt)
                self.real0 = time.time()
            m = 0.0
        return m

    def set_minute(self, m: float):
        self.manual_minute = m

    def now(self) -> dt.datetime:
        m = self.minute_float()
        return dt.datetime.combine(self.today, dt.time(9, 15), IST) + dt.timedelta(minutes=m)

    def ready(self):
        return True

    def market_open_now(self):
        return self.minute_float() < N_MIN

    def status(self):
        return {"mode": self.mode, "stage": "simulated session", "ticker": "simulated", "logged_in": True,
                "last_tick_age_s": 0, "rest_fallback": False, "universe": len(self.universe),
                "baseline_loaded": len(self.rvol), "errors": [], "sim_time": self.now().strftime("%Y-%m-%d %H:%M"),
                "note": "SIMULATED DATA - for testing the pipeline only"}

    # ------------------------------------------------------------ engine inputs
    def _cur(self, sym):
        mf = min(self.minute_float(), N_MIN - 0.001)
        m = int(mf)
        bars = self.data[sym]["today"]["bars"]
        b = bars[m]
        frac = mf - m
        ltp = b["o"] + (b["c"] - b["o"]) * frac
        return m, bars, ltp, b

    def stocks(self):
        out = []
        for u in self.universe:
            s = u["symbol"]
            d = self.data[s]
            m, bars, ltp, b = self._cur(s)
            done = [dict(x) for x in bars[:m]]
            prev = d["daily"][-1]
            pv = sum((x["h"] + x["l"] + x["c"]) / 3 * x["v"] for x in done)
            vv = sum(x["v"] for x in done)
            prev_oi = (list(d["past"].values())[-1]["bars"][-1]["oi"])
            out.append({
                "symbol": s, "sector": sector_of(s), "lot_size": u["lot_size"], "bars": done, "ltp": round(ltp, 2),
                "day_open": bars[0]["o"], "prev_close": prev["close"], "vwap": pv / vv if vv else None,
                "pdh": prev["high"], "pdl": prev["low"], "atr_daily": None,
                "upper_circuit": round(prev["close"] * 1.2, 2), "lower_circuit": round(prev["close"] * 0.8, 2),
                "rvol_baseline": self.rvol[s],
                "fut": {"ltp": round(ltp * 1.002, 2), "prev_close": round(prev["close"] * 1.002, 2),
                        "oi": b["oi"], "prev_oi": prev_oi},
                "data_ts": self.now().timestamp(), "daily_closes": [c["close"] for c in d["daily"][-60:]],
                "turnover_rank": self.rank.get(s),
                "fut_hist": [(x["m"], x["oi"], x["c"]) for x in bars[max(0, m - 15):m]],
                "fut_bq": 1.5 if self._regime_sign(s) > 0 else (0.7 if self._regime_sign(s) < 0 else 1.0),
                "fut_sq": 1.0,
            })
        return out

    def _index(self, members):
        chg = [(st["ltp"] / st["prev_close"] - 1) for st in members if st.get("prev_close")]
        return sum(chg) / len(chg) if chg else 0.0

    def market(self, stocks=None):
        stocks = stocks or self.stocks()
        n = self._index(stocks)
        banks = [s for s in stocks if s["sector"] in ("NIFTY BANK", "NIFTY PSU BANK", "NIFTY FIN SERVICE")]
        b = self._index(banks)
        sectors = {}
        for sec in {s["sector"] for s in stocks}:
            sectors[sec] = {"ltp": 1000 * (1 + self._index([s for s in stocks if s["sector"] == sec])), "prev_close": 1000}
        sectors[INDEX_BANKNIFTY] = {"ltp": 52000 * (1 + b), "prev_close": 52000}
        sectors[INDEX_NIFTY] = {"ltp": 25100 * (1 + n), "prev_close": 25100, "open": 25100, "day_open": 25100}
        mm = int(min(self.minute_float(), N_MIN - 1))
        if mm >= 15:
            key = (self.today, "or")
            if getattr(self, "_or_cache", (None,))[0] != key:
                levels = []
                for k in range(15):
                    rs = [st_bar_ret for st_bar_ret in
                          ((self.data[u["symbol"]]["today"]["bars"][k]["c"] / self.data[u["symbol"]]["daily"][-1]["close"] - 1)
                           for u in self.universe)]
                    levels.append(25100 * (1 + sum(rs) / len(rs)))
                self._or_cache = (key, max(levels), min(levels))
            sectors[INDEX_NIFTY]["or_high"], sectors[INDEX_NIFTY]["or_low"] = self._or_cache[1], self._or_cache[2]
        adv = sum(1 for s in stocks if s["ltp"] > s["prev_close"])
        return {"nifty": sectors[INDEX_NIFTY], "banknifty": sectors[INDEX_BANKNIFTY], "sectors": sectors,
                "breadth": {"adv": adv, "dec": len(stocks) - adv}, "vix": None, "feed_degraded": False}

    # ------------------------------------------------------------ options
    def expiries(self, symbol):
        def last_tue(y, mth):
            d = dt.date(y + (mth == 12), mth % 12 + 1, 1) - dt.timedelta(days=1)
            while d.weekday() != 1:
                d -= dt.timedelta(days=1)
            return d
        t = self.today
        e1 = last_tue(t.year, t.month)
        if e1 < t:
            e1 = last_tue(t.year + (t.month == 12), t.month % 12 + 1)
        e2 = last_tue(e1.year + (e1.month == 12), e1.month % 12 + 1)
        return [e1.isoformat(), e2.isoformat()]

    def _leg(self, sym, K, kind, expiry, S, lot, progress, regime_sign):
        r = random.Random(f"{sym}{K}{kind}{expiry}")
        base_iv = 0.20 + (sum(map(ord, sym)) % 10) / 100
        mny = (K - S) / S
        iv = base_iv + 0.8 * mny * mny + (0.01 if kind == "PE" else 0)
        T = options.years_to_expiry(expiry, self.now())
        price = max(0.05, options.bs_price(S, K, T, iv, kind))
        step = _strike_step(S)
        dist = abs(K - S) / step
        spread = max(0.05, price * (0.004 + 0.003 * dist))
        prev_oi = int((math.exp(-(dist / 5) ** 2) * r.uniform(0.6, 1.4) * 600 + 40)) * lot
        if regime_sign > 0:
            f = 0.12 if kind == "PE" else -0.08
        elif regime_sign < 0:
            f = 0.12 if kind == "CE" else -0.08
        else:
            f = r.uniform(-0.03, 0.03)
        oi = int(prev_oi * (1 + f * progress))
        ts = f"{sym}{dt.date.fromisoformat(expiry):%y%b}{K:g}{kind}".upper()
        self.registry[ts] = (sym, K, kind, expiry)
        vol = int(prev_oi * (0.3 + 1.5 * progress) * math.exp(-(dist / 4) ** 2))
        return {"ts": ts, "ltp": round(price, 2), "bid": round(price - spread / 2, 2), "ask": round(price + spread / 2, 2),
                "oi": oi, "prev_oi": prev_oi, "volume": vol}

    def _regime_sign(self, sym):
        g = self.data[sym]["today"]["regime"]
        return 1 if g == "up" else -1 if g == "down" else 0

    def option_chain(self, symbol, expiry, width=6):
        lot = next(u["lot_size"] for u in self.universe if u["symbol"] == symbol)
        m, bars, S, b = self._cur(symbol)
        step = _strike_step(S)
        atm = round(S / step) * step
        progress = min(1.0, m / 120)
        rs = self._regime_sign(symbol)
        rows = []
        for i in range(-width, width + 1):
            K = atm + i * step
            if K > 0:
                rows.append({"strike": K, "CE": self._leg(symbol, K, "CE", expiry, S, lot, progress, rs),
                             "PE": self._leg(symbol, K, "PE", expiry, S, lot, progress, rs)})
        return {"symbol": symbol, "spot": S, "expiry": expiry, "lot_size": lot, "strikes": rows}

    def prev_oi(self, ts):
        return None  # chain legs already carry prev_oi in simulation

    def option_quotes(self, tradingsymbols):
        out = {}
        for ts in tradingsymbols:
            if ts not in self.registry:
                continue
            sym, K, kind, expiry = self.registry[ts]
            lot = next(u["lot_size"] for u in self.universe if u["symbol"] == sym)
            m, bars, S, b = self._cur(sym)
            leg = self._leg(sym, K, kind, expiry, S, lot, min(1.0, m / 120), self._regime_sign(sym))
            out[ts] = {"ltp": leg["ltp"], "bid": leg["bid"], "ask": leg["ask"], "oi": leg["oi"]}
        return out

    # ------------------------------------------------------------ backtest data
    def backtest_dataset(self, days: int, symbols=None, progress=None):
        end = self.today
        dates = _trading_days_before(end, days + 11)
        ds = {"source": "SIMULATED", "symbols": {}, "indices": {}, "notes": [
            "SIMULATED data: results only prove the pipeline runs; they say nothing about real-market edge."]}
        for u in self.universe:
            if symbols and u["symbol"] not in symbols:
                continue
            daily, sessions = self._history(u["symbol"], u["base"], u["lot_size"], dates)
            ds["symbols"][u["symbol"]] = {
                "lot_size": u["lot_size"], "sector": sector_of(u["symbol"]), "daily": daily,
                "minute": {k: [{kk: vv for kk, vv in b.items() if kk != "oi"} for b in v["bars"]] for k, v in sessions.items()},
                "fut_minute": {k: [dict(b) for b in v["bars"]] for k, v in sessions.items()},
            }
        # indices: equal-weight composites of the simulated universe
        for name, pred in ((INDEX_NIFTY, lambda s: True),
                           (INDEX_BANKNIFTY, lambda s: sector_of(s) in ("NIFTY BANK", "NIFTY PSU BANK", "NIFTY FIN SERVICE"))):
            ds["indices"][name] = {"minute": self._composite(ds, pred)}
        for sec in {v["sector"] for v in ds["symbols"].values()}:
            ds["indices"].setdefault(sec, {"minute": self._composite(ds, lambda s, sec=sec: sector_of(s) == sec)})
        if progress is not None:
            progress.update(total=1, done=1)
        return ds

    def backtest_source(self, days: int, progress=None):
        ds = self.backtest_dataset(days, None, progress)
        return {"symbols": list(ds["symbols"]), "indices": ds["indices"], "source": "SIMULATED",
                "notes": ds["notes"], "loader": lambda sym, daily_only=False: ds["symbols"][sym]}

    @staticmethod
    def _composite(ds, pred):
        out = {}
        mem = [v for s, v in ds["symbols"].items() if pred(s)]
        if not mem:
            return out
        days = sorted(mem[0]["minute"])
        level = 1000.0
        for d in days:
            rows = []
            first = [v["minute"][d][0]["o"] for v in mem]
            for m in range(N_MIN):
                r = sum(v["minute"][d][m]["c"] / f for v, f in zip(mem, first)) / len(mem)
                c = level * r
                rows.append({"m": m, "t": mem[0]["minute"][d][m]["t"], "o": c, "h": c, "l": c, "c": c, "v": 0})
            out[d] = rows
            level = rows[-1]["c"]
        return out
