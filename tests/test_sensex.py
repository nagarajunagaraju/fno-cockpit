"""Sensex brain / backtest / live-engine tests with synthetic and fake-Kite data."""
import datetime as dt
import os
import random
import tempfile

os.environ.setdefault("COCKPIT_DATA_DIR", tempfile.mkdtemp())

from app.sensex import backtest as bt  # noqa: E402
from app.sensex.brain import Brain  # noqa: E402

LV = {"pdh": 80600.0, "pdl": 79800.0, "pdc": 80000.0}


def mins(start="09:15", n=375):
    h, m = map(int, start.split(":"))
    out = []
    for i in range(n):
        t = h * 60 + m + i
        out.append(f"{t // 60:02d}:{t % 60:02d}")
    return out


def path_bars(points):
    """points: list of (time, price) anchors -> 1-min bars moving linearly between anchors."""
    bars = []
    for (t0, p0), (t1, p1) in zip(points, points[1:]):
        a = int(t0[:2]) * 60 + int(t0[3:])
        b = int(t1[:2]) * 60 + int(t1[3:])
        for k in range(a, b):
            x0 = p0 + (p1 - p0) * (k - a) / (b - a)
            x1 = p0 + (p1 - p0) * (k + 1 - a) / (b - a)
            bars.append({"t": f"{k // 60:02d}:{k % 60:02d}", "o": x0, "h": max(x0, x1) + 3, "l": min(x0, x1) - 3, "c": x1})
    return bars


def run_day(points, nifty=0.4, seed=None):
    br = Brain(LV, seed or [79900.0] * 60)
    evs = []
    for b in path_bars(points):
        evs += br.on_bar(b, nifty_chg=nifty)
    return br, evs


BULL_DAY = [("09:15", 80050), ("09:22", 80150), ("09:29", 80060), ("09:30", 80100), ("09:40", 80230),
            ("09:44", 80250), ("09:50", 80165), ("09:55", 80175), ("10:00", 80240), ("10:30", 80420),
            ("11:00", 80560), ("15:30", 80500)]


def test_bull_day_signal_targets():
    br, evs = run_day(BULL_DAY)
    kinds = [e["kind"] for e in evs]
    assert "SETUP" in kinds and "SIGNAL" in kinds
    sig = next(e for e in evs if e["kind"] == "SIGNAL")
    assert sig["side"] == "BULL" and sig["spot_sl"] < sig["spot_entry"] < sig["spot_t1"] < sig["spot_t2"]
    assert "09:45" <= sig["t"] <= "13:30"
    assert any(e["kind"] == "T1" for e in evs)
    ex = [e for e in evs if e["kind"] == "EXIT"]
    assert ex and ex[0]["result_r"] > 0
    # never a signal before the opening range is complete
    assert all(e["t"] >= "09:30" for e in evs if e["kind"] in ("SETUP", "SIGNAL"))


def test_sideways_day_says_wait_no_trade():
    pts = [("09:15", 80000), ("09:29", 80080), ("10:30", 79990), ("12:00", 80060), ("15:30", 80010)]
    br, evs = run_day(pts)
    assert not any(e["kind"] == "SIGNAL" for e in evs)
    st = [e for e in evs if e["kind"] == "STATUS"]
    assert st and any("wait" in e["text"].lower() or "vaddu" in e["text"] for e in st)


def test_nifty_disagree_blocks_bull():
    br, evs = run_day(BULL_DAY, nifty=-0.3)
    assert not any(e["kind"] == "SIGNAL" for e in evs)


def test_stop_after_loss_and_max_trades():
    # breakout, pullback, trigger, then collapse through SL -> no more trades that day
    pts = [("09:15", 80050), ("09:22", 80150), ("09:29", 80060), ("09:30", 80100), ("09:40", 80230),
           ("09:44", 80250), ("09:50", 80165), ("09:55", 80175), ("10:00", 80240), ("10:10", 80050),
           ("10:40", 80300), ("10:50", 80240), ("11:00", 80320), ("15:30", 80300)]
    br, evs = run_day(pts)
    exits = [e for e in evs if e["kind"] == "EXIT"]
    assert exits and exits[0]["why"] == "SL" and exits[0]["result_r"] == -1.0
    assert sum(1 for e in evs if e["kind"] == "SIGNAL") == 1
    assert br.losses == 1


def test_levels_and_snapshot():
    br, _ = run_day(BULL_DAY[:6])
    s = br.snapshot()
    names = [l["name"] for l in s["levels"]]
    assert "Ninnati high" in names and "Opening range high" in names and "R1" in names
    assert s["levels"] == sorted(s["levels"], key=lambda l: -l["value"])
    assert abs(br.lv["pivot"] - (80600 + 79800 + 80000) / 3) < 1e-6


def test_backtest_replay():
    rnd = random.Random(3)
    sx, nf = {}, {}
    d = dt.date(2026, 6, 1)
    px = 80000.0
    for i in range(60):
        while d.weekday() >= 5:
            d += dt.timedelta(days=1)
        drift = rnd.choice([-1, 1]) * rnd.uniform(0.0, 0.6)
        bars, nbars = [], []
        for t in mins():
            o = px
            px *= 1 + rnd.gauss(drift / 375 / 100, 0.0006)
            bars.append({"t": t, "o": o, "h": max(o, px) + 5, "l": min(o, px) - 5, "c": px})
            nbars.append({"t": t, "o": px / 3.2, "h": px / 3.2, "l": px / 3.2, "c": px / 3.2})
        sx[d.isoformat()], nf[d.isoformat()] = bars, nbars
        d += dt.timedelta(days=1)
    res = bt.run(sx, nf, 40)
    assert res["ok"] and res["days"] == 40
    st = res["stats"]
    assert st["trades"] == 0 or (0 <= st["win_pct"] <= 100 and st["max_dd_r"] <= 0)
    assert all(t["r"] >= -1.01 for t in res["trades"])   # loss never bigger than 1R (gap-free data)
    assert len(res["by_month"]) >= 2


# ------------------------------------------------------------------ live engine with a fake Kite
class FakeKite:
    def __init__(self):
        self.calls = []

    def quote(self, keys):
        self.calls.append(("quote", len(keys)))
        out = {}
        for k in keys:
            if k == "BSE:SENSEX":
                out[k] = {"instrument_token": 265, "last_price": 80120.0, "ohlc": {"close": 80000.0}}
            elif k == "NSE:NIFTY 50":
                out[k] = {"instrument_token": 256265, "last_price": 24600.0, "ohlc": {"close": 24500.0}}
            elif k == "NSE:INDIA VIX":
                out[k] = {"instrument_token": 264969, "last_price": 13.0, "ohlc": {"close": 13.2}}
            else:
                strike = float(k[-7:-2])
                typ = k[-2:]
                intr = max(0.0, 80120 - strike) if typ == "CE" else max(0.0, strike - 80120)
                p = intr + 250
                out[k] = {"last_price": p, "oi": int(100000 + (5 if strike % 500 == 0 else 0) * 10000),
                          "depth": {"buy": [{"price": p - 1}], "sell": [{"price": p + 1}]}}
        return out

    def historical_data(self, token, frm, to, interval, oi=False):
        base = dt.datetime(2026, 11, 25, 9, 15)
        if interval == "day":
            return [{"date": dt.datetime(2026, 11, 25), "open": 79900, "high": 80600, "low": 79800, "close": 80000, "volume": 0}]
        if interval == "5minute":
            return [{"date": base + dt.timedelta(minutes=5 * i), "close": 79950.0} for i in range(60)]
        return []

    def instruments(self, ex):
        out = []
        for k in range(79000, 81300, 100):
            for typ in ("CE", "PE"):
                for exp in (dt.date(2026, 11, 26), dt.date(2026, 12, 3)):
                    out.append({"name": "SENSEX", "instrument_type": typ, "expiry": exp, "strike": k,
                                "tradingsymbol": f"SENSEX{exp:%y%m%d}{k}{typ}", "lot_size": 20})
        return out


class FakeSrc:
    def __init__(self):
        self.kite = FakeKite()

    def logged_in(self):
        return True


class FakeManager:
    def __init__(self):
        self.emitted, self.triggers, self.updates = [], [], 0

    def emit(self, *a, **k):
        self.emitted.append(a)

    def open_signals(self):
        return []

    def on_trigger(self, ev, sel, plan, expl, now):
        self.triggers.append(plan)
        return {"created": True, "id": 1}

    def update(self, *a):
        self.updates += 1


def test_live_engine_init_poll_and_contract():
    from app.engine.config import merged
    from app.sensex.live import SensexLive
    cfg = merged({})
    lv = SensexLive(FakeSrc(), FakeManager(), lambda: cfg)
    now = dt.datetime(2026, 11, 26, 9, 10, tzinfo=dt.timezone(dt.timedelta(hours=5, minutes=30)))
    lv._init_day(lv.src.kite, now)
    assert lv.chain["expiry"] == "2026-12-03"            # expiry-day contract avoided
    assert lv.brain.lv["pdh"] == 80600 and lv.comm[0]["kind"] == "INFO"
    lv._poll(lv.src.kite, now)
    v = lv.view()
    assert v["live"]["ltp"] == 80120.0 and v["live"]["chg_pct"] == 0.15
    assert v["oi"]["res"] is not None and v["oi"]["sup"] is not None
    pick = lv.pick_contract("BULL", 80120.0, now)
    sel = pick["selected"]
    assert sel["ts"].endswith("CE") and 0.3 < abs(sel["delta"]) < 0.8
    e = {"t": "10:00", "side": "BULL", "spot_entry": 80120.0, "spot_sl": 80020.0, "spot_t1": 80270.0, "spot_t2": 80370.0,
         "text": "test"}
    lv._make_signal(e, now)
    plan = lv.manager.triggers[0]
    assert plan["sl_premium"] < plan["entry_premium"] < plan["t1_premium"] < plan["t2_premium"]
    assert plan["qty"] == plan["lots"] * 20 and plan["max_loss"] <= cfg["capital"] * cfg["sensex_risk_pct"] / 100 + 1
