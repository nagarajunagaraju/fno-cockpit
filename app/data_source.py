"""Market data sources.

DemoSource  - synthetic but realistic-looking data, no login needed.
KiteSource  - your own Zerodha Kite Connect account (personal use only).

Both expose the same methods:
    ready() -> bool
    progress() -> dict
    snapshot() -> list[dict]   (symbol, lot_size, ltp, prev_close, oi, prev_oi, volume, candles)
    index() -> dict            (NIFTY 50 ltp + prev_close)
    day_pnl() -> float | None  (broker P&L for today, None in demo)
    orders_today() -> int | None
"""
from __future__ import annotations

import datetime as dt
import json
import math
import random
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CACHE = ROOT / "cache"
CACHE.mkdir(exist_ok=True)

# ---------------------------------------------------------------- DEMO
# Lot sizes and prices here are rough placeholders for demo only.
DEMO_STOCKS = [
    ("RELIANCE", 500, 1380), ("HDFCBANK", 550, 960), ("ICICIBANK", 700, 1400), ("INFY", 400, 1520),
    ("TCS", 175, 3100), ("SBIN", 750, 820), ("AXISBANK", 625, 1150), ("KOTAKBANK", 400, 2050),
    ("LT", 175, 3600), ("ITC", 1600, 410), ("BHARTIARTL", 475, 1900), ("HINDUNILVR", 300, 2450),
    ("BAJFINANCE", 750, 880), ("MARUTI", 50, 12800), ("M&M", 200, 3300), ("TATAMOTORS", 800, 700),
    ("TATASTEEL", 5500, 160), ("JSWSTEEL", 675, 1050), ("SUNPHARMA", 350, 1650), ("DRREDDY", 625, 1280),
    ("CIPLA", 375, 1520), ("WIPRO", 3000, 250), ("HCLTECH", 350, 1480), ("TECHM", 600, 1450),
    ("ADANIENT", 300, 2400), ("ADANIPORTS", 475, 1400), ("POWERGRID", 1900, 290), ("NTPC", 1500, 340),
    ("ONGC", 2250, 240), ("COALINDIA", 1350, 390), ("ULTRACEMCO", 50, 12000), ("ASIANPAINT", 250, 2400),
    ("TITAN", 175, 3500), ("HEROMOTOCO", 150, 5200), ("EICHERMOT", 175, 6800), ("DLF", 825, 780),
    ("INDUSINDBK", 700, 760), ("BEL", 1425, 400), ("HAL", 150, 4700), ("TRENT", 100, 5100),
]


class DemoSource:
    mode = "DEMO"

    def __init__(self, seed: int = 7):
        self.rnd = random.Random(seed)
        self.state: dict[str, dict] = {}
        today = dt.date.today()
        for sym, lot, px in DEMO_STOCKS:
            drift = self.rnd.uniform(-0.004, 0.004)
            price = px * self.rnd.uniform(0.85, 1.1)
            candles = []
            d = today - dt.timedelta(days=170)
            while len(candles) < 120:
                d += dt.timedelta(days=1)
                if d.weekday() >= 5:
                    continue
                o = price
                c = o * (1 + drift + self.rnd.gauss(0, 0.014))
                h = max(o, c) * (1 + abs(self.rnd.gauss(0, 0.006)))
                l = min(o, c) * (1 - abs(self.rnd.gauss(0, 0.006)))
                candles.append({"date": d.isoformat(), "open": o, "high": h, "low": l, "close": c})
                price = c
            prev_close = candles[-1]["close"]
            prev_oi = int(self.rnd.uniform(3e6, 4e7) / lot) * lot
            self.state[sym] = {
                "symbol": sym, "lot_size": lot, "candles": candles,
                "prev_close": prev_close, "ltp": prev_close * (1 + self.rnd.gauss(0, 0.012)),
                "spot_ltp": prev_close * (1 + self.rnd.gauss(0, 0.012)),
                "fut_ltp": prev_close * (1 + self.rnd.gauss(0, 0.012)),
                "fut_prev_close": prev_close,
                "prev_oi": prev_oi, "oi": int(prev_oi * (1 + self.rnd.gauss(0, 0.04))),
                "p_drift": self.rnd.gauss(0, 0.0006), "oi_drift": self.rnd.gauss(0, 0.003),
                "volume": 0,
            }
        self.nifty_prev = 25100.0
        self.nifty = self.nifty_prev
        self.lock = threading.Lock()

    def ready(self):
        return True

    def progress(self):
        return {"loaded": len(self.state), "total": len(self.state), "message": "Demo data"}

    def _step(self):
        for s in self.state.values():
            s["ltp"] *= 1 + s["p_drift"] + self.rnd.gauss(0, 0.0012)
            s["spot_ltp"] = s["ltp"]
            s["fut_ltp"] = s["ltp"]
            s["oi"] = max(1, int(s["oi"] * (1 + s["oi_drift"] + self.rnd.gauss(0, 0.002))))
            s["volume"] += int(self.rnd.uniform(0.5, 3) * s["lot_size"] * 20)
            if self.rnd.random() < 0.02:  # regime shifts so the scanner changes over time
                s["p_drift"] = self.rnd.gauss(0, 0.0006)
                s["oi_drift"] = self.rnd.gauss(0, 0.002)
        self.nifty *= 1 + self.rnd.gauss(0.00005, 0.0006)

    def snapshot(self):
        with self.lock:
            self._step()
            return [dict(s) for s in self.state.values()]

    def ltp(self, symbol):
        s = self.state.get(symbol)
        return s["ltp"] if s else None

    def index(self):
        return {"name": "NIFTY 50", "ltp": self.nifty, "prev_close": self.nifty_prev}

    def day_pnl(self):
        return None

    def orders_today(self):
        return None



    # ---- options (demo) ---------------------------------------------
    @staticmethod
    def _last_tuesday(y, m):
        d = dt.date(y + (m == 12), m % 12 + 1, 1) - dt.timedelta(days=1)
        while d.weekday() != 1:
            d -= dt.timedelta(days=1)
        return d

    def expiries(self, symbol):
        t = dt.date.today()
        e1 = self._last_tuesday(t.year, t.month)
        if e1 <= t:
            e1 = self._last_tuesday(t.year + (t.month == 12), t.month % 12 + 1)
        e2 = self._last_tuesday(e1.year + (e1.month == 12), e1.month % 12 + 1)
        return [e1.isoformat(), e2.isoformat()]

    @staticmethod
    def _strike_step(px):
        return 5 if px < 500 else 10 if px < 1000 else 20 if px < 2500 else 50 if px < 6000 else 100

    def _leg(self, sym, K, kind, expiry, S):
        from . import options
        s = self.state[sym]
        r = random.Random(f"{sym}{K}{kind}{expiry}")
        base_iv = 0.17 + (sum(map(ord, sym)) % 13) / 100
        m = (K - S) / S
        iv = base_iv + 0.9 * m * m + (0.02 if kind == "PE" else 0)
        T = options.years_to_expiry(expiry)
        price = max(0.05, options.bs_price(S, K, T, iv, kind))
        step = self._strike_step(s["prev_close"])
        spread = max(0.05, price * (0.004 + 0.002 * abs(K - S) / step))
        center = S * (1.04 if kind == "CE" else 0.96)
        oi_base = math.exp(-((K - center) / (6 * step)) ** 2) * r.uniform(0.6, 1.4)
        if K % (step * 5) == 0:
            oi_base *= 1.8
        prev_oi = int(oi_base * 800 + 10) * s["lot_size"]
        oi = int(prev_oi * (1 + r.gauss(0, 0.15) + (s["oi"] - s["prev_oi"]) / s["prev_oi"]))
        return {"ts": f"{sym}{dt.date.fromisoformat(expiry):%y%b}{K:g}{kind}".upper(),
                "ltp": round(price, 2), "bid": round(price - spread / 2, 2), "ask": round(price + spread / 2, 2),
                "oi": max(oi, 0), "prev_oi": prev_oi, "volume": int(oi_base * 5000) * s["lot_size"]}

    def option_chain(self, symbol, expiry, width=10):
        s = self.state[symbol]
        S = s["ltp"]
        step = self._strike_step(s["prev_close"])
        atm = round(S / step) * step
        strikes = []
        for i in range(-width, width + 1):
            K = atm + i * step
            if K <= 0:
                continue
            strikes.append({"strike": K, "CE": self._leg(symbol, K, "CE", expiry, S),
                            "PE": self._leg(symbol, K, "PE", expiry, S)})
        return {"symbol": symbol, "spot": S, "expiry": expiry, "lot_size": s["lot_size"], "strikes": strikes}

    def option_ltps(self, trades):
        out = {}
        for t in trades:
            if t.get("underlying") in self.state:
                out[t["symbol"]] = self._leg(t["underlying"], t["strike"], t["opt_type"], t["expiry"],
                                             self.state[t["underlying"]]["ltp"])["ltp"]
        return out


# ---------------------------------------------------------------- KITE
INDEX_NAMES = {"NIFTY", "BANKNIFTY", "FINNIFTY", "MIDCPNIFTY", "NIFTYNXT50", "SENSEX", "BANKEX"}
TOKEN_FILE = ROOT / ".kite_token.json"


class KiteSource:
    mode = "LIVE"

    def __init__(self, api_key: str, api_secret: str, history_days: int = 180):
        from kiteconnect import KiteConnect
        self.kite = KiteConnect(api_key=api_key)
        self.api_secret = api_secret
        self.history_days = history_days
        self.universe: list[dict] = []
        self.history: dict[str, dict] = {}
        self._progress = {"loaded": 0, "total": 0, "message": "Login required"}
        self._logged_in = False
        self._loader: threading.Thread | None = None
        self._last_quotes: dict[str, dict] = {}
        self.opts: dict[str, dict[str, list]] = {}   # name -> expiry -> instruments
        self.opt_prev_oi: dict[str, int] = {}
        self._load_token()

    # ---- login -----------------------------------------------------
    token_version = 0

    @staticmethod
    def _token_still_valid(data: dict) -> bool:
        """Kite access tokens expire every morning (~06:00 IST). A token is reused only if it was
        created after 06:00 IST today; older files (or files without a timestamp) need a fresh login."""
        saved = data.get("saved_at")
        if not saved:
            return False
        try:
            t = dt.datetime.fromisoformat(saved)
        except ValueError:
            return False
        ist = dt.timezone(dt.timedelta(hours=5, minutes=30))
        now = dt.datetime.now(ist)
        cutoff = now.replace(hour=6, minute=0, second=0, microsecond=0)
        if now < cutoff:
            cutoff -= dt.timedelta(days=1)
        return t.astimezone(ist) >= cutoff

    def _load_token(self):
        if TOKEN_FILE.exists():
            data = json.loads(TOKEN_FILE.read_text())
            if self._token_still_valid(data):
                self.kite.set_access_token(data["access_token"])
                self._logged_in = True
                self.token_version += 1
                self._start_loader()

    def login_url(self):
        return self.kite.login_url()

    def complete_login(self, request_token: str):
        session = self.kite.generate_session(request_token, api_secret=self.api_secret)
        self.kite.set_access_token(session["access_token"])
        ist = dt.timezone(dt.timedelta(hours=5, minutes=30))
        TOKEN_FILE.write_text(json.dumps({"date": dt.date.today().isoformat(),
                                          "saved_at": dt.datetime.now(ist).isoformat(timespec="seconds"),
                                          "access_token": session["access_token"]}))
        self._logged_in = True
        self.token_version += 1
        self._start_loader()

    def mark_logged_out(self):
        """Called when Kite rejects the token (expired / logged out elsewhere)."""
        self._logged_in = False

    def logged_in(self):
        return self._logged_in

    # ---- history loading (runs once a day, cached to disk) ------------
    def _start_loader(self):
        if self._loader and self._loader.is_alive():
            return
        self._loader = threading.Thread(target=self._load_all, daemon=True)
        self._loader.start()

    def _load_all(self):
        try:
            self._progress["message"] = "Loading instruments"
            today = dt.date.today()
            nfo = self.kite.instruments("NFO")
            nse = {i["tradingsymbol"]: i["instrument_token"] for i in self.kite.instruments("NSE")}
            futs: dict[str, dict] = {}
            for i in nfo:
                if i["instrument_type"] != "FUT" or i["name"] in INDEX_NAMES:
                    continue
                if i["expiry"] < today:
                    continue
                cur = futs.get(i["name"])
                if cur is None or i["expiry"] < cur["expiry"]:
                    futs[i["name"]] = i
            by_name: dict[str, dict[str, list]] = {}
            for i in nfo:
                if i["instrument_type"] in ("CE", "PE") and i["name"] in futs and i["expiry"] >= today:
                    by_name.setdefault(i["name"], {}).setdefault(i["expiry"].isoformat(), []).append(i)
            self.opts = {n: dict(sorted(e.items())[:2]) for n, e in by_name.items()}
            self.universe = [
                {"symbol": name, "fut": f["tradingsymbol"], "fut_token": f["instrument_token"],
                 "cash_token": nse.get(name), "lot_size": int(f["lot_size"]), "expiry": f["expiry"].isoformat()}
                for name, f in sorted(futs.items()) if nse.get(name)
            ]
            self._progress.update(total=len(self.universe), loaded=0)

            cache_file = CACHE / f"history_{today.isoformat()}.json"
            cached = json.loads(cache_file.read_text()) if cache_file.exists() else {}
            frm = today - dt.timedelta(days=self.history_days)
            for u in self.universe:
                sym = u["symbol"]
                if sym in cached:
                    self.history[sym] = cached[sym]
                else:
                    self._progress["message"] = f"Loading history: {sym}"
                    cash = self.kite.historical_data(u["cash_token"], frm, today, "day")
                    time.sleep(0.35)  # stay under 3 req/sec
                    fut = self.kite.historical_data(u["fut_token"], today - dt.timedelta(days=10), today, "day", oi=True)
                    time.sleep(0.35)
                    candles = [{"date": c["date"].date().isoformat(), "open": c["open"], "high": c["high"],
                                "low": c["low"], "close": c["close"], "volume": c.get("volume", 0)}
                               for c in cash if c["date"].date() < today]
                    fut_done = [c for c in fut if c["date"].date() < today]
                    prev_oi = fut_done[-1]["oi"] if fut_done else None
                    self.history[sym] = {"candles": candles, "prev_oi": prev_oi}
                    cached[sym] = self.history[sym]
                    if len(cached) % 20 == 0:
                        cache_file.write_text(json.dumps(cached))
                self._progress["loaded"] += 1
            cache_file.write_text(json.dumps(cached))
            self._progress["message"] = "Ready"
        except Exception as e:  # surface the error in the UI instead of dying silently
            self._progress["message"] = f"Error: {e}"

    def ready(self):
        return self._logged_in and len(self.history) > 0

    def progress(self):
        return dict(self._progress)

    # ---- live --------------------------------------------------------
    def snapshot(self):
        items = [u for u in self.universe if u["symbol"] in self.history]
        keys = [f"NFO:{u['fut']}" for u in items]
        cash_keys = [f"NSE:{u['symbol']}" for u in items]
        quotes = {}
        for keyset in (keys, cash_keys):
            for i in range(0, len(keyset), 250):
                quotes.update(self.kite.quote(keyset[i:i + 250]))
        rows = []
        for u in items:
            q = quotes.get(f"NFO:{u['fut']}")
            cash_q = quotes.get(f"NSE:{u['symbol']}")
            h = self.history[u["symbol"]]
            if not q or not cash_q or not h["candles"]:
                continue
            self._last_quotes[u["symbol"]] = q
            rows.append({
                "symbol": u["symbol"], "lot_size": u["lot_size"], "ltp": q["last_price"],
                "fut_ltp": q["last_price"],
                "fut_prev_close": q.get("ohlc", {}).get("close") or h["candles"][-1]["close"],
                "prev_close": cash_q.get("ohlc", {}).get("close") or h["candles"][-1]["close"],
                "spot_ltp": cash_q["last_price"],
                "oi": q.get("oi", 0), "prev_oi": h["prev_oi"], "volume": q.get("volume", 0),
                "candles": h["candles"],
            })
        return rows

    def ltp(self, symbol):
        q = self._last_quotes.get(symbol)
        return q["last_price"] if q else None

    def index(self):
        q = self.kite.quote(["NSE:NIFTY 50"])["NSE:NIFTY 50"]
        return {"name": "NIFTY 50", "ltp": q["last_price"], "prev_close": q["ohlc"]["close"]}

    def day_pnl(self):
        pos = self.kite.positions()
        return round(sum(p.get("m2m", 0) for p in pos.get("day", [])), 2)

    def orders_today(self):
        orders = self.kite.orders()
        return sum(1 for o in orders if o["status"] == "COMPLETE")

    # ---- options (live) ----------------------------------------------
    def expiries(self, symbol):
        return list(self.opts.get(symbol, {}).keys())

    def option_chain(self, symbol, expiry, width=10):
        insts = self.opts.get(symbol, {}).get(expiry, [])
        if not insts:
            raise ValueError(f"No options for {symbol} {expiry}")
        S = self.kite.quote([f"NSE:{symbol}"])[f"NSE:{symbol}"]["last_price"]
        strikes = sorted({i["strike"] for i in insts})
        atm_i = min(range(len(strikes)), key=lambda k: abs(strikes[k] - S))
        pick = set(strikes[max(0, atm_i - width): atm_i + width + 1])
        legs = {(i["strike"], i["instrument_type"]): i for i in insts if i["strike"] in pick}
        q = self.kite.quote([f"NFO:{i['tradingsymbol']}" for i in legs.values()])
        need_prev = [i for i in legs.values() if i["tradingsymbol"] not in self.opt_prev_oi]
        if need_prev:
            threading.Thread(target=self._load_prev_oi, args=(need_prev,), daemon=True).start()
        rows = []
        for K in sorted(pick):
            row = {"strike": K}
            for kind in ("CE", "PE"):
                i = legs.get((K, kind))
                qq = q.get(f"NFO:{i['tradingsymbol']}") if i else None
                if not qq:
                    continue
                depth = qq.get("depth", {})
                bid = (depth.get("buy") or [{}])[0].get("price") or 0
                ask = (depth.get("sell") or [{}])[0].get("price") or 0
                row[kind] = {"ts": i["tradingsymbol"], "ltp": qq["last_price"], "bid": bid, "ask": ask,
                             "oi": qq.get("oi", 0), "prev_oi": self.opt_prev_oi.get(i["tradingsymbol"]),
                             "volume": qq.get("volume", 0)}
            rows.append(row)
        lot = int(insts[0]["lot_size"])
        return {"symbol": symbol, "spot": S, "expiry": expiry, "lot_size": lot, "strikes": rows}

    def _load_prev_oi(self, insts):
        today = dt.date.today()
        for i in insts:
            if i["tradingsymbol"] in self.opt_prev_oi:
                continue
            try:
                c = self.kite.historical_data(i["instrument_token"], today - dt.timedelta(days=7), today, "day", oi=True)
                done = [x for x in c if x["date"].date() < today]
                self.opt_prev_oi[i["tradingsymbol"]] = done[-1]["oi"] if done else None
            except Exception:
                self.opt_prev_oi[i["tradingsymbol"]] = None
            time.sleep(0.35)

    def option_ltps(self, trades):
        keys = [f"NFO:{t['symbol']}" for t in trades]
        if not keys:
            return {}
        q = self.kite.ltp(keys)
        return {k[4:]: v["last_price"] for k, v in q.items()}
