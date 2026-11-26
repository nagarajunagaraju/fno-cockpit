"""Live Zerodha feed for the signal engine.

* Re-uses the existing KiteSource (login, instruments, daily history, option chain).
* Streams cash, futures and index ticks over KiteTicker (WebSocket, FULL mode) with
  auto-reconnect; falls back to throttled REST quotes whenever ticks go stale.
* Loads, once per day, 1-minute history for the last ~15 days: this gives the
  10-session relative-volume baseline and back-fills today's candles after a restart.
"""
from __future__ import annotations

import datetime as dt
import json
import logging
import threading
import time

from .. import analytics
from .candles import BarBuilder, build_rvol_baseline
from .config import IST, hm, minute_index
from .sectors import INDEX_BANKNIFTY, INDEX_NIFTY, sector_indices, sector_of
from .store import DATA

log = logging.getLogger("cockpit.feed")
CACHE = DATA / "cache"
CACHE.mkdir(exist_ok=True)


def ist_now():
    return dt.datetime.now(IST)


class KiteFeed:
    mode = "LIVE"

    def __init__(self, src, api_key: str, cfg_getter):
        self.src = src                      # data_source.KiteSource (kite already rate limited)
        self.api_key = api_key
        self.cfg = cfg_getter
        self.lock = threading.RLock()
        self.cash: dict[str, BarBuilder] = {}
        self.tok2: dict[int, tuple[str, str]] = {}   # token -> (kind, symbol/name)
        self.live: dict[str, dict] = {}              # symbol -> latest cash fields
        self.fut: dict[str, dict] = {}               # symbol -> latest futures fields
        self.idx: dict[str, dict] = {}               # index name -> {ltp, prev_close, ts}
        self.baseline: dict[str, dict] = {}
        self.circuits: dict[str, tuple] = {}
        self.day = None
        self.state = {"stage": "waiting for login", "ticker": "not started", "last_tick": None,
                      "rest_fallback": False, "errors": [], "baseline_loaded": 0, "universe": 0,
                      "ticks": 0, "rest_polls": 0}
        self.ticker = None
        self._ticker_token_version = None
        self._prepared = False
        self.idx_bars: dict[str, BarBuilder] = {}
        self.fut_hist: dict[str, list] = {}         # symbol -> [(minute, oi, ltp)] last ~20 min
        self.turnover_rank: dict[str, int] = {}
        threading.Thread(target=self._run, daemon=True, name="kite-feed").start()

    # ------------------------------------------------------------ helpers
    def now(self):
        return ist_now()

    def _err(self, msg):
        log.warning(msg)
        self.state["errors"] = (self.state["errors"] + [f"{ist_now():%H:%M:%S} {msg}"])[-20:]

    def ready(self):
        return self._prepared and bool(self.cash)

    def status(self):
        lt = self.state["last_tick"]
        return {**self.state, "mode": self.mode, "logged_in": self.src.logged_in(),
                "last_tick_age_s": round(time.time() - lt, 1) if lt else None,
                "source_progress": self.src.progress(), "api_calls": getattr(self.src.kite, "calls", None)}

    def market_open_now(self):
        cfg = self.cfg()
        n = ist_now()
        return n.weekday() < 5 and hm(cfg["market_open"]) <= n.time() <= hm(cfg["market_close"])

    # ------------------------------------------------------------ preparation
    def _run(self):
        while True:
            try:
                if self.src.ready() and self.src.progress().get("message") == "Ready":
                    today = ist_now().date().isoformat()
                    if self.day != today:
                        self._prepare(today)
                    elif self._ticker_token_version != getattr(self.src, "token_version", 0):
                        self._err("new Kite login detected: reconnecting WebSocket with the new token")
                        self._start_ticker()
                    if self.market_open_now():
                        self._maybe_rest_poll()
                    for b in list(self.cash.values()) + list(self.idx_bars.values()):
                        b.flush(ist_now())
            except Exception as e:  # never let the feed thread die
                self._err(f"feed loop: {e}")
                # back off after a failure so one bad call cannot hammer Kite every second all day
                self._fail = min(getattr(self, "_fail", 0) + 1, 6)
                time.sleep(10 * self._fail)
                continue
            self._fail = 0
            time.sleep(1)

    def _prepare(self, today):
        cfg = self.cfg()
        self.state["stage"] = "loading 1-min history (RVOL baseline + today's candles)"
        kite = self.src.kite
        universe = list(self.src.universe)
        self.state["universe"] = len(universe)
        idx_tokens = getattr(self.src, "index_tokens", None)
        if not idx_tokens:
            nse = kite.instruments("NSE")
            idx_tokens = {i["tradingsymbol"]: i["instrument_token"] for i in nse if i.get("segment") == "INDICES"}
        wanted = sector_indices() + ["INDIA VIX"]
        missing = [w for w in wanted if w not in idx_tokens]
        if missing:
            self._err(f"index not found on Kite, marked unavailable: {', '.join(missing)}")
        with self.lock:
            self.cash = {u["symbol"]: BarBuilder(cfg) for u in universe}
            self.tok2 = {}
            for u in universe:
                self.tok2[u["cash_token"]] = ("cash", u["symbol"])
                self.tok2[u["fut_token"]] = ("fut", u["symbol"])
            for name in wanted:
                if name in idx_tokens:
                    self.tok2[idx_tokens[name]] = ("idx", name)
            self.idx_bars = {INDEX_NIFTY: BarBuilder(cfg)}
            self._rank_turnover()

        cache_file = CACHE / f"minute_{today}.json"
        cached = json.loads(cache_file.read_text()) if cache_file.exists() else {}
        now = ist_now()
        cur_m = minute_index(now, cfg)
        frm = now.date() - dt.timedelta(days=16)
        for n, u in enumerate(universe):
            sym = u["symbol"]
            try:
                if sym in cached:
                    past = cached[sym]
                else:
                    candles = kite.historical_data(u["cash_token"], frm, now.replace(tzinfo=None), "minute")
                    past = {}
                    for c in candles:
                        d = c["date"]
                        day = d.date().isoformat()
                        m = minute_index(d.replace(tzinfo=IST) if d.tzinfo is None else d.astimezone(IST), cfg)
                        if m < 0:
                            continue
                        past.setdefault(day, []).append({"m": m, "t": d.strftime("%H:%M"), "o": c["open"], "h": c["high"],
                                                         "l": c["low"], "c": c["close"], "v": c["volume"]})
                    if today in past:  # today's bars are re-fetched after restarts, don't cache them
                        cached[sym] = {k: v for k, v in past.items() if k != today}
                    else:
                        cached[sym] = past
                sessions = [v for k, v in sorted(past.items()) if k < today][-cfg["rvol_lookback_sessions"]:]
                self.baseline[sym] = build_rvol_baseline(sessions)
                todays = [b for b in past.get(today, []) if b["m"] < cur_m]
                if todays:
                    self.cash[sym].set_backfill(todays)
                self.state["baseline_loaded"] = n + 1
                if n % 25 == 0:
                    cache_file.write_text(json.dumps(cached))
            except Exception as e:
                self._err(f"minute history {sym}: {e}")
        cache_file.write_text(json.dumps(cached))
        self._load_circuits()
        self.day = today
        self._prepared = True
        self.state["stage"] = "streaming"
        self._start_ticker()

    def _load_circuits(self):
        keys = [f"NSE:{s}" for s in self.cash]
        for i in range(0, len(keys), 450):
            try:
                qs = self.src.kite.quote(keys[i:i + 450])
                for k, v in qs.items():
                    self.circuits[k[4:]] = (v.get("upper_circuit_limit"), v.get("lower_circuit_limit"))
            except Exception as e:
                self._err(f"circuit limits: {e}")

    # ------------------------------------------------------------ websocket
    def _rank_turnover(self):
        """20-day average cash turnover (close x volume) rank: 1 = most liquid."""
        tv = {}
        for u in self.src.universe:
            c = (self.src.history.get(u["symbol"]) or {}).get("candles") or []
            last = [x for x in c[-20:] if x.get("volume")]
            if last:
                tv[u["symbol"]] = sum(x["close"] * x["volume"] for x in last) / len(last)
        self.turnover_rank = {s: i + 1 for i, s in enumerate(sorted(tv, key=lambda k: -tv[k]))}

    def _stop_old_ticker(self):
        old = self.ticker
        self.ticker = None
        if old is None:
            return
        try:
            from twisted.internet import reactor
            old.on_close = old.on_error = old.on_reconnect = old.on_noreconnect = None
            old.on_ticks = None
            reactor.callFromThread(old.stop_retry)
            reactor.callFromThread(old.close)
        except Exception as e:
            self._err(f"closing old WebSocket: {e}")

    def _start_ticker(self):
        """(Re)connects KiteTicker with the CURRENT access token. Called after preparation and
        whenever a new Kite login happens, so no server reboot is needed after re-login."""
        self._stop_old_ticker()
        self._ticker_token_version = getattr(self.src, "token_version", 0)
        try:
            from kiteconnect import KiteTicker
        except Exception as e:
            self._err(f"KiteTicker unavailable, REST only: {e}")
            return
        token = getattr(self.src.kite, "_kite", self.src.kite).access_token
        kws = KiteTicker(self.api_key, token, reconnect=True, reconnect_max_tries=300, reconnect_max_delay=30)

        def on_connect(ws, resp):
            toks = list(self.tok2)
            ws.subscribe(toks)
            ws.set_mode(ws.MODE_FULL, toks)
            self.state["ticker"] = f"connected ({len(toks)} tokens)"

        def on_close(ws, code, reason):
            self.state["ticker"] = f"closed {code} {reason}"

        def on_error(ws, code, reason):
            self._err(f"ticker error {code} {reason}")
            if "403" in str(code) or "403" in str(reason) or "TokenException" in str(reason):
                self.src.mark_logged_out()

        def on_reconnect(ws, attempts):
            self.state["ticker"] = f"reconnecting (attempt {attempts})"

        def on_noreconnect(ws):
            self.state["ticker"] = "gave up reconnecting - REST fallback"

        kws.on_ticks = lambda ws, ticks: self.on_ticks(ticks) if self.ticker is kws else None
        kws.on_connect = on_connect
        kws.on_close = on_close
        kws.on_error = on_error
        kws.on_reconnect = on_reconnect
        kws.on_noreconnect = on_noreconnect
        self.ticker = kws
        kws.connect(threaded=True)
        self.state["ticker"] = "connecting"

    def on_ticks(self, ticks):
        now = time.time()
        for t in ticks:
            k = self.tok2.get(t.get("instrument_token"))
            if not k:
                continue
            kind, name = k
            ts = t.get("exchange_timestamp") or t.get("last_trade_time") or dt.datetime.now()
            if isinstance(ts, dt.datetime) and ts.tzinfo is None:
                ts = ts.replace(tzinfo=IST)
            self._apply(kind, name, t, ts, now)
        self.state["last_tick"] = now
        self.state["ticks"] += len(ticks)

    def _apply(self, kind, name, t, ts, now):
        ohlc = t.get("ohlc") or {}
        if kind == "cash":
            self.cash[name].on_tick(ts, t.get("last_price"), t.get("volume_traded", t.get("volume")))
            self.live[name] = {"ltp": t.get("last_price"), "open": ohlc.get("open"), "prev_close": ohlc.get("close"),
                               "vwap": t.get("average_traded_price", t.get("average_price")),
                               "volume": t.get("volume_traded", t.get("volume")), "ts": now}
        elif kind == "fut":
            self.fut[name] = {"ltp": t.get("last_price"), "prev_close": ohlc.get("close"), "oi": t.get("oi"),
                              "volume": t.get("volume_traded", t.get("volume")), "ts": now,
                              "bq": t.get("total_buy_quantity", t.get("buy_quantity")),
                              "sq": t.get("total_sell_quantity", t.get("sell_quantity"))}
            m = minute_index(ts, self.cfg()) if isinstance(ts, dt.datetime) else None
            if m is not None and m >= 0 and t.get("oi"):
                h = self.fut_hist.setdefault(name, [])
                if not h or h[-1][0] != m:
                    h.append((m, t.get("oi"), t.get("last_price")))
                    del h[:-16]
                else:
                    h[-1] = (m, t.get("oi"), t.get("last_price"))
        else:
            self.idx[name] = {"ltp": t.get("last_price"), "prev_close": ohlc.get("close"), "open": ohlc.get("open"),
                              "ts": now}
            if name in self.idx_bars and isinstance(ts, dt.datetime):
                self.idx_bars[name].on_tick(ts, t.get("last_price"))

    def _maybe_rest_poll(self):
        lt = self.state["last_tick"]
        stale = lt is None or time.time() - lt > self.cfg()["stale_seconds"]
        self.state["rest_fallback"] = stale
        if not stale:
            return
        last = getattr(self, "_last_poll", 0)
        if time.time() - last < self.cfg()["scan_interval_seconds"]:
            return
        self._last_poll = time.time()
        inv = {}
        for tok, (kind, name) in self.tok2.items():
            if kind == "cash":
                inv[f"NSE:{name}"] = (kind, name)
            elif kind == "idx":
                inv[f"NSE:{name}"] = (kind, name)
        for u in self.src.universe:
            inv[f"NFO:{u['fut']}"] = ("fut", u["symbol"])
        keys = list(inv)
        now = time.time()
        for i in range(0, len(keys), 450):
            try:
                qs = self.src.kite.quote(keys[i:i + 450])
            except Exception as e:
                self._err(f"REST fallback quote: {e}")
                return
            for key, q in qs.items():
                kind, name = inv[key]
                q = dict(q)
                q["volume_traded"] = q.get("volume")
                q["average_traded_price"] = q.get("average_price")
                ts = q.get("last_trade_time") or dt.datetime.now()
                if isinstance(ts, dt.datetime) and ts.tzinfo is None:
                    ts = ts.replace(tzinfo=IST)
                self._apply(kind, name, q, ist_now() if kind == "idx" else ts, now)
        self.state["rest_polls"] += 1
        self.state["last_rest"] = now

    # ------------------------------------------------------------ engine inputs
    def stocks(self) -> list[dict]:
        out = []
        hist = self.src.history
        lots = {u["symbol"]: u["lot_size"] for u in self.src.universe}
        for sym, b in self.cash.items():
            h = hist.get(sym) or {}
            daily = h.get("candles") or []
            lv = self.live.get(sym) or {}
            f = self.fut.get(sym) or {}
            uc, lc = self.circuits.get(sym, (None, None))
            last_ts = max(lv.get("ts") or 0, self.state.get("last_rest") or 0) or None
            out.append({
                "symbol": sym, "sector": sector_of(sym), "lot_size": lots.get(sym),
                "bars": b.completed(), "ltp": lv.get("ltp"), "day_open": lv.get("open"),
                "prev_close": lv.get("prev_close") or (daily[-1]["close"] if daily else None),
                "vwap": lv.get("vwap"), "pdh": daily[-1]["high"] if daily else None,
                "pdl": daily[-1]["low"] if daily else None, "atr_daily": analytics.atr(daily, 14) if daily else None,
                "upper_circuit": uc, "lower_circuit": lc, "rvol_baseline": self.baseline.get(sym),
                "fut": {"ltp": f.get("ltp"), "prev_close": f.get("prev_close"), "oi": f.get("oi"),
                        "prev_oi": h.get("prev_oi")} if f else None,
                "data_ts": last_ts, "daily_closes": [c["close"] for c in daily[-60:]],
                "turnover_rank": self.turnover_rank.get(sym), "fut_bq": f.get("bq"), "fut_sq": f.get("sq"),
                "fut_hist": list(self.fut_hist.get(sym, [])),
            })
        return out

    def market(self, stocks=None) -> dict:
        idx = dict(self.idx)
        adv = dec = 0
        for s in stocks or []:
            if s.get("ltp") and s.get("prev_close"):
                adv += s["ltp"] > s["prev_close"]
                dec += s["ltp"] < s["prev_close"]
        nifty = dict(idx.get(INDEX_NIFTY) or {})
        nb = self.idx_bars.get(INDEX_NIFTY)
        if nb:
            bars = nb.completed()
            orb = [b for b in bars if b["m"] < self.cfg()["opening_range_minutes"]]
            if len(orb) >= self.cfg()["opening_range_minutes"] - 2:
                nifty["or_high"] = max(b["h"] for b in orb)
                nifty["or_low"] = min(b["l"] for b in orb)
        nifty["day_open"] = nifty.get("open")
        return {"nifty": nifty or None, "banknifty": idx.get(INDEX_BANKNIFTY), "vix": idx.get("INDIA VIX"),
                "sectors": idx, "breadth": {"adv": adv, "dec": dec} if stocks else {},
                "feed_degraded": bool(self.state.get("rest_fallback"))}

    # ------------------------------------------------------------ options
    def expiries(self, symbol):
        return self.src.expiries(symbol)

    def option_chain(self, symbol, expiry, width=6):
        return self.src.option_chain(symbol, expiry, width=width)

    def prev_oi(self, ts):
        return self.src.opt_prev_oi.get(ts)

    def option_quotes(self, tradingsymbols: list[str]) -> dict:
        if not tradingsymbols:
            return {}
        qs = self.src.kite.quote([f"NFO:{t}" for t in tradingsymbols])
        out = {}
        for k, q in qs.items():
            d = q.get("depth") or {}
            bid = ((d.get("buy") or [{}])[0] or {}).get("price") or None
            ask = ((d.get("sell") or [{}])[0] or {}).get("price") or None
            out[k[4:]] = {"ltp": q.get("last_price"), "bid": bid, "ask": ask, "oi": q.get("oi")}
        return out

    # ------------------------------------------------------------ legacy dashboard rows (no extra API calls)
    def legacy_snapshot(self):
        rows = []
        lots = {u["symbol"]: u for u in self.src.universe}
        for s in self.stocks():
            f = s.get("fut") or {}
            h = self.src.history.get(s["symbol"]) or {}
            if not s.get("ltp") or not f.get("ltp") or not h.get("candles"):
                continue
            rows.append({"symbol": s["symbol"], "lot_size": lots[s["symbol"]]["lot_size"], "ltp": f["ltp"],
                         "fut_ltp": f["ltp"], "fut_prev_close": f.get("prev_close") or h["candles"][-1]["close"],
                         "prev_close": s["prev_close"], "spot_ltp": s["ltp"], "oi": f.get("oi") or 0,
                         "prev_oi": h.get("prev_oi"), "volume": (self.live.get(s["symbol"]) or {}).get("volume", 0),
                         "candles": h["candles"]})
        return rows

    # ------------------------------------------------------------ backtest data (v3, chunked, on disk)
    def backtest_source(self, days: int, progress: dict) -> dict:
        cfg = self.cfg()
        kite = self.src.kite
        end = ist_now().date() - dt.timedelta(days=1)
        start = end - dt.timedelta(days=int(days * 1.5) + 25)
        folder = CACHE / f"bt3_{end.isoformat()}_{days}"
        folder.mkdir(exist_ok=True)
        ranked = sorted(self.src.universe, key=lambda u: self.turnover_rank.get(u["symbol"], 9999))
        uni = ranked[:cfg["universe_top_n"]]
        idx_tokens = {name: tok for tok, (k, name) in self.tok2.items() if k == "idx" and name != "INDIA VIX"}
        need_idx = {INDEX_NIFTY} | {sector_of(u["symbol"]) for u in uni}
        progress.update(stage="downloading 1-min history", total=len(uni) + len(need_idx), done=0)

        def chunks():
            a = start
            while a <= end:
                b = min(end, a + dt.timedelta(days=55))
                yield a, b
                a = b + dt.timedelta(days=1)

        def minute(token, oi=False):
            out = {}
            for a, b in chunks():
                for c in kite.historical_data(token, a, b, "minute", oi=oi):
                    d = c["date"]
                    d = d.replace(tzinfo=IST) if d.tzinfo is None else d.astimezone(IST)
                    m = minute_index(d, cfg)
                    if m < 0:
                        continue
                    bar = {"m": m, "t": d.strftime("%H:%M"), "o": c["open"], "h": c["high"], "l": c["low"],
                           "c": c["close"], "v": c.get("volume", 0)}
                    if oi:
                        bar["oi"] = c.get("oi")
                    out.setdefault(d.date().isoformat(), []).append(bar)
            return out

        notes, indices = [], {}
        for name in sorted(need_idx):
            f = folder / f"idx_{name.replace(' ', '_')}.json"
            try:
                if f.exists():
                    indices[name] = json.loads(f.read_text())
                elif name in idx_tokens:
                    indices[name] = {"minute": minute(idx_tokens[name])}
                    f.write_text(json.dumps(indices[name]))
            except Exception as e:
                notes.append(f"index {name}: {e}")
            progress["done"] += 1
        syms = []
        for u in uni:
            sym = u["symbol"]
            f = folder / f"{sym}.json"
            try:
                if not f.exists():
                    try:
                        fut = minute(u["fut_token"], oi=True)
                    except Exception as e:
                        fut = {}
                        notes.append(f"{sym}: futures history unavailable ({e})")
                    daily = kite.historical_data(u["cash_token"], start - dt.timedelta(days=60), end, "day")
                    f.write_text(json.dumps({
                        "lot_size": u["lot_size"], "minute": minute(u["cash_token"]), "fut_minute": fut,
                        "daily": [{"date": c["date"].date().isoformat(), "open": c["open"], "high": c["high"],
                                   "low": c["low"], "close": c["close"], "volume": c.get("volume", 0)} for c in daily]}))
                syms.append(sym)
            except Exception as e:
                notes.append(f"{sym}: {e}")
            progress["done"] += 1

        def loader(sym, daily_only=False):
            d = json.loads((folder / f"{sym}.json").read_text())
            return {"daily": d["daily"]} if daily_only else d

        return {"symbols": syms, "indices": indices, "loader": loader, "source": "KITE", "notes": notes}

    def backtest_dataset(self, days: int, symbols: list[str] | None, progress: dict) -> dict:
        """Downloads 1-min cash + futures(with OI) + index candles. Expired option contracts are not
        available from Kite, so the option layer cannot be backtested (reported clearly)."""
        cfg = self.cfg()
        kite = self.src.kite
        end = ist_now().date() - dt.timedelta(days=1)
        start = end - dt.timedelta(days=int(days * 1.5) + 20)
        uni = [u for u in self.src.universe if not symbols or u["symbol"] in symbols]
        ds = {"source": "KITE", "symbols": {}, "indices": {}, "notes": []}
        idx_tokens = {name: tok for tok, (k, name) in self.tok2.items() if k == "idx"}
        jobs = [("idx", n, t) for n, t in idx_tokens.items()] + [("stock", u["symbol"], u) for u in uni]
        progress.update(total=len(jobs), done=0)

        def bars_by_day(candles, with_oi=False):
            out = {}
            for c in candles:
                d = c["date"]
                d = d.replace(tzinfo=IST) if d.tzinfo is None else d.astimezone(IST)
                m = minute_index(d, cfg)
                if m < 0:
                    continue
                b = {"m": m, "t": d.strftime("%H:%M"), "o": c["open"], "h": c["high"], "l": c["low"],
                     "c": c["close"], "v": c.get("volume", 0)}
                if with_oi:
                    b["oi"] = c.get("oi")
                out.setdefault(d.date().isoformat(), []).append(b)
            return out

        for kind, name, obj in jobs:
            try:
                if kind == "idx":
                    ds["indices"][name] = {"minute": bars_by_day(kite.historical_data(obj, start, end, "minute"))}
                else:
                    u = obj
                    cash = bars_by_day(kite.historical_data(u["cash_token"], start, end, "minute"))
                    try:
                        fut = bars_by_day(kite.historical_data(u["fut_token"], start, end, "minute", oi=True), True)
                    except Exception as e:
                        fut = {}
                        ds["notes"].append(f"{name}: futures minute history unavailable ({e})")
                    daily = kite.historical_data(u["cash_token"], start - dt.timedelta(days=60), end, "day")
                    ds["symbols"][name] = {
                        "lot_size": u["lot_size"], "sector": sector_of(name), "minute": cash, "fut_minute": fut,
                        "daily": [{"date": c["date"].date().isoformat(), "open": c["open"], "high": c["high"],
                                   "low": c["low"], "close": c["close"]} for c in daily]}
            except Exception as e:
                ds["notes"].append(f"{name}: {e}")
            progress["done"] += 1
        ds["notes"].append("Only the CURRENT futures contract has minute OI history on Kite; days before it was "
                           "listed have no futures OI and are evaluated with futures data unavailable.")
        ds["notes"].append("Universe = today's F&O list (survivorship bias: stocks removed from F&O are missing).")
        return ds
