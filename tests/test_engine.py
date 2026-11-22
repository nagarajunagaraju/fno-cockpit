"""Unit + integration tests for the signal engine. Run: python -m pytest -q"""
import copy
import datetime as dt
import os
import sys
import tempfile
import time
from pathlib import Path

os.environ.setdefault("COCKPIT_DATA_DIR", tempfile.mkdtemp(prefix="cockpit_test_"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest  # noqa: E402

from app.engine import store  # noqa: E402
from app.engine.backtest import run_backtest  # noqa: E402
from app.engine.candles import BarBuilder, build_rvol_baseline, rvol  # noqa: E402
from app.engine.config import DEFAULTS, IST, merged  # noqa: E402
from app.engine.contracts import select_contract  # noqa: E402
from app.engine.feed_sim import SimFeed  # noqa: E402
from app.engine.layers import evaluate  # noqa: E402
from app.engine.planner import build_plan  # noqa: E402
from app.engine.ratelimit import RateLimitedKite  # noqa: E402
from app.engine.signals import SignalManager  # noqa: E402
from app import options  # noqa: E402

CFG = merged({})
DAY = dt.date(2026, 11, 20)
T0 = dt.datetime.combine(DAY, dt.time(9, 15), IST)


def bars_trend(n, start=100.0, step=0.05, vol=1000, open_eq_low=True):
    out, px = [], start
    for m in range(n):
        o = px
        c = px + step
        out.append({"m": m, "t": (T0 + dt.timedelta(minutes=m)).strftime("%H:%M"), "o": o,
                    "h": max(o, c) + 0.01, "l": o if (m == 0 and open_eq_low) else min(o, c) - 0.01, "c": c, "v": vol})
        px = c
    return out


def bull_stock(n=30, **kw):
    bars = bars_trend(n)
    # last bar breaks the prior high (trigger)
    bars[-2]["c"] = bars[-2]["h"] - 0.02
    bars[-1]["c"] = max(b["h"] for b in bars[:-1]) + 0.2
    bars[-1]["h"] = bars[-1]["c"] + 0.01
    s = {"symbol": "TEST", "sector": "NIFTY IT", "lot_size": 100, "bars": bars, "prev_close": 99.0,
         "day_open": bars[0]["o"], "pdh": 100.5, "pdl": 98.0, "rvol_baseline": {m: 400.0 * (m + 1) for m in range(375)},
         "fut": {"ltp": 101.6, "prev_close": 99.1, "oi": 1_060_000, "prev_oi": 1_000_000},
         "upper_circuit": 120, "lower_circuit": 80, "data_ts": time.time()}
    s.update(kw)
    return s


MARKET = {"nifty": {"ltp": 25200, "prev_close": 25100}, "banknifty": {"ltp": 52100, "prev_close": 52000},
          "sectors": {"NIFTY IT": {"ltp": 1015, "prev_close": 1000}}, "breadth": {"adv": 100, "dec": 80}}
OPT_OK = {"available": True, "ce_oi_chg_pct": -4.0, "pe_oi_chg_pct": 9.0, "since": "previous close",
          "baseline_kind": "previous-day OI", "atm_iv": 30.0, "iv_ref": 28.0, "iv_ref_kind": "test"}


def now_after(s):
    return T0 + dt.timedelta(minutes=s["bars"][-1]["m"] + 1, seconds=2)


# ------------------------------------------------------------------ layers
def test_full_bull_setup_triggers():
    s = bull_stock()
    ev = evaluate(s, MARKET, CFG, now_after(s), OPT_OK)
    assert ev["direction"] == "BULL"
    assert ev["score"] >= CFG["min_score"], ev["checks"]
    assert ev["status"] == "TRIGGER", (ev["status"], ev["reject_reasons"], ev.get("missing"))
    assert ev["trigger"]["fired"]


def test_missing_option_data_never_signals():
    s = bull_stock()
    ev = evaluate(s, MARKET, CFG, now_after(s), {"available": False, "reason": "unavailable"})
    assert ev["status"] == "REJECTED"
    ev2 = evaluate(s, MARKET, CFG, now_after(s), None)  # not fetched yet
    assert ev2["status"] != "TRIGGER"


def test_missing_futures_oi_rejects():
    s = bull_stock(fut=None)
    ev = evaluate(s, MARKET, CFG, now_after(s), OPT_OK)
    assert ev["status"] == "REJECTED"
    assert any("Futures" in r for r in ev["reject_reasons"])


def test_chasing_rejected():
    s = bull_stock(prev_close=96.0)  # ~ +5.8%
    ev = evaluate(s, MARKET, CFG, now_after(s), OPT_OK)
    assert ev["status"] == "REJECTED"
    assert any("chasing" in r.lower() for r in ev["reject_reasons"])


def test_vwap_side_mandatory():
    s = bull_stock(vwap=200.0)
    ev = evaluate(s, MARKET, CFG, now_after(s), OPT_OK, live=True)
    assert ev["status"] == "REJECTED"


def test_stale_data_rejected():
    s = bull_stock(data_ts=time.time() - 600)
    ev = evaluate(s, MARKET, CFG, dt.datetime.now(IST), OPT_OK)
    assert ev["status"] == "REJECTED"


def test_index_conflict_rejects():
    mk = copy.deepcopy(MARKET)
    mk["nifty"]["ltp"] = 24800
    mk["banknifty"]["ltp"] = 51500
    s = bull_stock()
    ev = evaluate(s, mk, CFG, now_after(s), OPT_OK)
    assert ev["status"] == "REJECTED"


def test_no_trigger_inside_opening_range():
    s = bull_stock(n=8)
    ev = evaluate(s, MARKET, CFG, now_after(s), OPT_OK)
    assert ev["status"] != "TRIGGER"


def test_high_score_cannot_bypass_mandatory():
    s = bull_stock(prev_close=96.0)  # chasing, yet other checks strong
    ev = evaluate(s, MARKET, CFG, now_after(s), OPT_OK)
    assert ev["score"] >= 5 and ev["status"] == "REJECTED"


def test_evaluate_ignores_future_bars():
    s = bull_stock(n=40)
    m = 25
    cut = dict(s, bars=s["bars"][:m + 1])
    ev1 = evaluate(cut, MARKET, CFG, T0 + dt.timedelta(minutes=m + 1), OPT_OK, live=False)
    fut = copy.deepcopy(s["bars"])
    for b in fut[m + 1:]:
        b.update(h=b["h"] * 3, l=b["l"] * 0.3, c=b["c"] * 2, v=b["v"] * 50)
    ev2 = evaluate(dict(s, bars=fut[:m + 1]), MARKET, CFG, T0 + dt.timedelta(minutes=m + 1), OPT_OK, live=False)
    assert ev1["score"] == ev2["score"] and ev1["status"] == ev2["status"] and ev1["metrics"] == ev2["metrics"]


# ------------------------------------------------------------------ candles
def test_bar_builder_completes_only_after_minute_ends():
    b = BarBuilder(CFG)
    b.on_tick(T0 + dt.timedelta(seconds=5), 100, 1000)
    b.on_tick(T0 + dt.timedelta(seconds=40), 101, 1500)
    assert b.completed() == []
    b.on_tick(T0 + dt.timedelta(seconds=65), 102, 1800)
    done = b.completed()
    assert len(done) == 1 and done[0]["h"] == 101 and done[0]["c"] == 101 and done[0]["v"] == 1500
    b.flush(T0 + dt.timedelta(minutes=2, seconds=1))
    assert len(b.completed()) == 2 and b.completed()[1]["v"] == 300


def test_rvol_baseline():
    base = build_rvol_baseline([[{"m": 0, "v": 100}, {"m": 1, "v": 100}], [{"m": 0, "v": 300}, {"m": 1, "v": 100}]])
    assert base[0] == 200 and base[1] == 300
    assert rvol([{"m": 0, "v": 400}, {"m": 1, "v": 200}], base) == pytest.approx(2.0)


# ------------------------------------------------------------------ contracts + plan
def make_chain(spread_pct=0.6, oi_lots=500, vol_lots=200, spot=1000.0, lot=100):
    expiry = (DAY + dt.timedelta(days=30)).isoformat()
    rows = []
    now = dt.datetime.combine(DAY, dt.time(10, 0), IST)
    for i in range(-5, 6):
        K = 1000 + i * 20
        row = {"strike": K}
        for kind in ("CE", "PE"):
            p = options.bs_price(spot, K, options.years_to_expiry(expiry, now), 0.25, kind)
            sp = p * spread_pct / 100
            row[kind] = {"ts": f"TEST{K}{kind}", "ltp": p, "bid": p - sp / 2, "ask": p + sp / 2,
                         "oi": oi_lots * lot, "prev_oi": oi_lots * lot, "volume": vol_lots * lot}
        rows.append(row)
    ch = {"symbol": "TEST", "spot": spot, "expiry": expiry, "lot_size": lot, "strikes": rows}
    return options.enrich_chain(ch, 0.22)


def test_contract_selection_explains_and_respects_rules():
    sel = select_contract(make_chain(), "BULL", CFG)
    assert sel["selected"] and sel["type"] == "CE"
    assert CFG["min_delta"] <= abs(sel["selected"]["delta"]) <= CFG["max_delta"]
    assert sel["why"] and any("Better than" in w for w in sel["why"])
    pe = select_contract(make_chain(), "BEAR", CFG)
    assert pe["type"] == "PE" and pe["selected"]["ts"].endswith("PE")


def test_illiquid_chain_rejected():
    sel = select_contract(make_chain(spread_pct=9.0), "BULL", CFG)
    assert sel["selected"] is None and "No eligible" in sel["error"]
    sel = select_contract(make_chain(oi_lots=5), "BULL", CFG)
    assert sel["selected"] is None


def test_plan_has_all_fields_and_sane_levels():
    s = bull_stock()
    ev = evaluate(s, MARKET, CFG, now_after(s), OPT_OK)
    ch = make_chain(spot=s["bars"][-1]["c"] * 10)  # scale so strikes are around spot*10
    ev["metrics"] = dict(ev["metrics"], price=ch["spot"], vwap=ch["spot"] * 0.995, last_bar_low=ch["spot"] * 0.998)
    sel = select_contract(ch, "BULL", CFG)
    plan = build_plan(ev, sel, ch, CFG, dt.datetime.combine(DAY, dt.time(10, 0), IST))
    assert "error" not in plan, plan
    for k in ("tradingsymbol", "strike", "expiry", "entry_premium", "entry_range", "sl_premium", "t1_premium",
              "t2_premium", "rr_t1", "lots", "qty", "max_loss", "signal_time", "data_time", "confidence", "entry_trigger"):
        assert plan.get(k) is not None, k
    assert plan["sl_premium"] < plan["entry_premium"] < plan["t1_premium"] < plan["t2_premium"]
    assert plan["spot_sl"] < plan["spot_entry"] < plan["spot_t1"]
    assert plan["rr_t1"] >= CFG["min_rr"]
    assert plan["max_loss"] <= CFG["capital"] * CFG["risk_per_trade_pct"] / 100 + 1


def test_plan_rejects_when_one_lot_exceeds_risk():
    s = bull_stock()
    ev = evaluate(s, MARKET, CFG, now_after(s), OPT_OK)
    ch = make_chain(spot=1000.0, lot=5000)
    ev["metrics"] = dict(ev["metrics"], price=1000.0, vwap=995.0, last_bar_low=998.0)
    cfg = dict(CFG, max_premium_pct=100.0)
    sel = select_contract(ch, "BULL", cfg)
    plan = build_plan(ev, sel, ch, cfg, dt.datetime.combine(DAY, dt.time(10, 0), IST))
    assert "error" in plan and "0 lots" in plan["error"]


# ------------------------------------------------------------------ lifecycle / dedupe / risk
def _fake_plan(ts="TESTCE", entry=10.0):
    return {"tradingsymbol": ts, "option_type": "CE", "direction": "BUY", "strike": 100, "expiry": "2026-12-29",
            "lot_size": 100, "lots": 2, "qty": 200, "entry_premium": entry, "entry_range": [entry * 0.99, entry * 1.01],
            "sl_premium": entry * 0.85, "t1_premium": entry * 1.25, "t2_premium": entry * 1.45,
            "spot_sl": 99.0, "spot_entry": 100.0, "premium_outlay": entry * 1.01 * 200, "max_loss": 400, "rr_t1": 1.6}


def _fake_ev(sym="AAA", level=100.0):
    return {"symbol": sym, "direction": "BULL", "score": 8, "trigger": {"level": level, "text": "t", "bar_time": "10:00"},
            "checks": [], "metrics": {}}


def test_lifecycle_fill_t1_t2_and_events():
    mgr = SignalManager(lambda: CFG)
    now = dt.datetime.combine(DAY, dt.time(10, 0), IST)
    r = mgr.on_trigger(_fake_ev("LC1"), {"candidates": []}, _fake_plan("LC1CE"), {}, now)
    assert r["created"]
    assert not mgr.on_trigger(_fake_ev("LC1", 101), {"candidates": []}, _fake_plan("LC1CE"), {}, now)["created"]
    mgr.update(now + dt.timedelta(seconds=5), {"LC1CE": {"bid": 9.95, "ask": 10.0, "ltp": 10}}, {"LC1": 100.2})
    assert mgr.get(r["id"])["status"] == "ACTIVE"
    mgr.update(now + dt.timedelta(minutes=5), {"LC1CE": {"bid": 12.6, "ask": 12.7, "ltp": 12.6}}, {"LC1": 101})
    s = mgr.get(r["id"])
    assert s["t1_done"] == 1 and s["remaining_qty"] == 100 and s["sl_current"] == s["fill_price"]
    mgr.update(now + dt.timedelta(minutes=9), {"LC1CE": {"bid": 14.6, "ask": 14.7, "ltp": 14.6}}, {"LC1": 102})
    s = mgr.get(r["id"])
    assert s["status"] == "TARGET_HIT" and s["net_pnl"] > 0 and s["charges"] > 0
    kinds = [e["kind"] for e in mgr.events() if e["signal_id"] == r["id"]]
    assert kinds[0] == "TRIGGERED" and "ACTIVE" in kinds and "TARGET_HIT" in kinds


def test_missed_fill_and_invalidation():
    mgr = SignalManager(lambda: CFG)
    now = dt.datetime.combine(DAY, dt.time(11, 0), IST)
    a = mgr.on_trigger(_fake_ev("MF1"), {"candidates": []}, _fake_plan("MF1CE"), {}, now)["id"]
    mgr.update(now + dt.timedelta(minutes=4), {"MF1CE": {"bid": 11, "ask": 11.2}}, {"MF1": 100.5})
    assert mgr.get(a)["status"] == "EXPIRED"
    b = mgr.on_trigger(_fake_ev("IV1"), {"candidates": []}, _fake_plan("IV1CE"), {}, now)["id"]
    mgr.update(now + dt.timedelta(seconds=30), {"IV1CE": {"bid": 11, "ask": 11.2}}, {"IV1": 98.5})
    assert mgr.get(b)["status"] == "INVALIDATED"


def test_risk_controls_block_new_signals():
    cfg = dict(CFG, max_open_positions=1)
    mgr = SignalManager(lambda: cfg)
    now = dt.datetime.combine(DAY, dt.time(12, 0), IST)
    assert mgr.on_trigger(_fake_ev("RK1"), {"candidates": []}, _fake_plan("RK1CE"), {}, now)["created"]
    r = mgr.on_trigger(_fake_ev("RK2"), {"candidates": []}, _fake_plan("RK2CE"), {}, now)
    assert not r["created"] and "Max simultaneous" in r["reason"]


def test_daily_loss_lock():
    cfg = dict(CFG, max_daily_loss_pct=0.1, max_open_positions=10)  # Rs 300 limit
    mgr = SignalManager(lambda: cfg)
    day = dt.date(2026, 11, 18)
    now = dt.datetime.combine(day, dt.time(10, 0), IST)
    sid = mgr.on_trigger(_fake_ev("DL1"), {"candidates": []}, _fake_plan("DL1CE"), {}, now)["id"]
    mgr.update(now + dt.timedelta(seconds=5), {"DL1CE": {"bid": 9.9, "ask": 10}}, {"DL1": 100.1})
    mgr.update(now + dt.timedelta(minutes=2), {"DL1CE": {"bid": 8.0, "ask": 8.1}}, {"DL1": 99.5})
    assert mgr.get(sid)["status"] == "STOP_LOSS_HIT"
    r = mgr.on_trigger(_fake_ev("DL2"), {"candidates": []}, _fake_plan("DL2CE"), {}, now + dt.timedelta(minutes=3))
    assert not r["created"] and "Daily loss" in r["reason"]


# ------------------------------------------------------------------ backtest
def test_backtest_runs_and_has_no_lookahead():
    feed = SimFeed(lambda: CFG, manual=True, day=DAY)
    ds = feed.backtest_dataset(12)
    r1 = run_backtest(copy.deepcopy(ds), CFG, days=12)
    assert r1["stats"]["trades"] > 0 and "by_hour" in r1["stats"] and "by_side" in r1["stats"]
    assert any("EXCLUDED" in n for n in r1["notes"])
    last_day = r1["tested_days"][-1]
    cut = 200
    ds2 = copy.deepcopy(ds)
    for s in ds2["symbols"].values():
        for b in s["minute"][last_day][cut:]:
            b.update(o=b["o"] * 1.5, h=b["h"] * 1.6, l=b["l"] * 1.4, c=b["c"] * 1.5, v=b["v"] * 10)
    r2 = run_backtest(ds2, CFG, days=12)
    key = lambda t: (t["day"], t["symbol"], t["signal_time"], t.get("entry"))  # noqa: E731
    before = lambda r: sorted(key(t) for t in r["trades"]  # noqa: E731
                              if t["day"] < last_day or int(t["signal_time"][:2]) * 60 + int(t["signal_time"][3:]) - 555 < cut - 1)
    assert before(r1) == before(r2)


def test_sufficiency_flag():
    feed = SimFeed(lambda: CFG, manual=True, day=DAY)
    r = run_backtest(feed.backtest_dataset(6), CFG, days=6)
    assert r["sufficiency"].startswith("INSUFFICIENT")


# ------------------------------------------------------------------ scanner integration (simulated session)
def test_scanner_end_to_end_simulated_day():
    from app.engine.scanner import Scanner
    feed = SimFeed(lambda: CFG, manual=True, day=DAY)
    sc = Scanner(feed)
    for k in range(0, 200 * 4):
        feed.set_minute(k / 4)
        sc.cycle()
    sigs = sc.manager.list(day=DAY.isoformat())
    assert sc.snapshot["universe"] == 40
    assert sigs, sc.trigger_log[-5:]
    sigs = [x for x in sigs if x["mode"] == "SIMULATED"]
    assert sigs
    s = sc.manager.get(sigs[0]["id"])
    for k in ("why_stock", "passed", "futures_oi", "option_chain", "why_strike", "why_entry", "invalidation", "risks"):
        assert s["explanation"].get(k), k
    keys = [(x["symbol"], x["bias"]) for x in sigs if x["status"] in ("TRIGGERED", "ACTIVE")]
    assert len(keys) == len(set(keys))  # no duplicate open alerts


def test_sim_feed_data_fresh_after_day_roll():
    feed = SimFeed(lambda: CFG, manual=True, day=dt.date(2026, 1, 7))
    feed.set_minute(120)
    st = feed.stocks()[0]
    ev = evaluate(st, feed.market(), CFG, feed.now(), None)
    fresh = next(c for c in ev["checks"] if c["key"] == "data_fresh")
    assert fresh["passed"] is True


def test_rate_limiter_spaces_quote_calls():
    class K:
        def quote(self, *_):
            return {}
    k = RateLimitedKite(K())
    t = time.monotonic()
    for _ in range(3):
        k.quote(["x"])
    assert time.monotonic() - t >= 2.0


def test_config_merge_types():
    c = merged({"min_score": "8", "require_vwap_side": "false", "weights": {"rvol": 3}, "junk": 1})
    assert c["min_score"] == 8 and c["require_vwap_side"] is False and c["weights"]["rvol"] == 3 and "junk" not in c
    assert DEFAULTS["min_score"] == 7
