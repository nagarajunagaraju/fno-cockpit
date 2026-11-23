"""Continuous scanner: evaluates the whole F&O universe on every cycle, fetches option-chain data
only for stocks that pass the cheap layers, and turns genuine entry triggers into signals.

There is no fixed signal time and no quota: zero signals is a valid outcome.
"""
from __future__ import annotations

import datetime as dt
import logging
import os
import threading
import time
import traceback

from .. import options
from . import store
from .config import IST, merged
from .contracts import pick_expiry, select_contract
from .layers import evaluate, prescore
from .planner import build_plan, explain
from .pullback import contract_points, evaluate_pullback, grade as grade_of
from .regime import detect as detect_regime
from .signals import SignalManager

log = logging.getLogger("cockpit.scanner")


def get_cfg():
    return merged(store.kv_get("engine_cfg", {}))


def save_cfg(new: dict):
    cur = store.kv_get("engine_cfg", {}) or {}
    cur.update(new)
    store.kv_set("engine_cfg", cur)
    return get_cfg()


class OptionTracker:
    """Near-ATM option OI change + IV context per stock, refreshed at most once per `option_refresh_seconds`."""

    def __init__(self, feed, cfg_getter):
        self.feed = feed
        self.cfg = cfg_getter
        self.cache: dict[str, dict] = {}

    def chain(self, stock, now, max_age=None):
        cfg = self.cfg()
        sym = stock["symbol"]
        c = self.cache.get(sym)
        age = max_age if max_age is not None else cfg["option_refresh_seconds"]
        if c and now.timestamp() - c["at"] < age:
            return c
        exps = self.feed.expiries(sym)
        expiry = pick_expiry(exps, cfg) if exps else None
        if not expiry:
            res = {"at": now.timestamp(), "info": {"available": False, "reason": "no option expiry >= min DTE"}, "chain": None}
            self.cache[sym] = res
            return res
        try:
            width = max(cfg["strikes_each_side"], cfg["option_oi_strikes_each_side"]) + 1
            ch = self.feed.option_chain(sym, expiry, width=width)
            ch = options.enrich_chain(ch, stock.get("hv20"))
        except Exception as e:
            res = {"at": now.timestamp(), "info": {"available": False, "reason": f"option chain error: {e}"}, "chain": None}
            self.cache[sym] = res
            return res
        info = self._oi_info(sym, ch, now)
        res = {"at": now.timestamp(), "info": info, "chain": ch}
        self.cache[sym] = res
        return res

    def _oi_info(self, sym, ch, now):
        cfg = self.cfg()
        day = now.astimezone(IST).date().isoformat()
        S = ch["spot"]
        rows = sorted(ch["strikes"], key=lambda r: abs(r["strike"] - S))[: 2 * cfg["option_oi_strikes_each_side"] + 1]
        tot = {"CE": [0, 0], "PE": [0, 0]}
        kinds, firsts = set(), []
        for r in rows:
            for k in ("CE", "PE"):
                leg = r.get(k)
                if not leg or leg.get("oi") is None:
                    continue
                base = leg.get("prev_oi") or self.feed.prev_oi(leg["ts"])
                if base:
                    kinds.add("previous-day OI")
                else:
                    b = store.q("SELECT oi, at FROM option_oi_baseline WHERE day=? AND ts=?", (day, leg["ts"]))
                    if b:
                        base, at = b[0]["oi"], b[0]["at"]
                    else:
                        base, at = leg["oi"], now.astimezone(IST).strftime("%H:%M")
                        store.x("INSERT OR IGNORE INTO option_oi_baseline(day, ts, oi, kind, at) VALUES(?,?,?,?,?)",
                                (day, leg["ts"], base, "first_seen", at))
                    kinds.add("first seen today")
                    firsts.append(at)
                tot[k][0] += leg["oi"]
                tot[k][1] += base
        if not tot["CE"][1] or not tot["PE"][1]:
            return {"available": False, "reason": "near-ATM option OI not available"}
        info = {"available": True,
                "ce_oi_chg_pct": round((tot["CE"][0] - tot["CE"][1]) / tot["CE"][1] * 100, 2),
                "pe_oi_chg_pct": round((tot["PE"][0] - tot["PE"][1]) / tot["PE"][1] * 100, 2),
                "baseline_kind": " + ".join(sorted(kinds)),
                "since": "previous close" if kinds == {"previous-day OI"} else (min(firsts) if firsts else "?"),
                "atm_iv": ch.get("atm_iv"), "pcr": ch.get("pcr")}
        if "first seen today" in kinds and info["since"] == now.astimezone(IST).strftime("%H:%M"):
            # baseline taken this very minute -> no change measurable yet; do not pretend there is one
            return {"available": False, "reason": "option OI baseline just recorded; change measurable from next refresh"}
        if ch.get("atm_iv"):
            store.x("INSERT OR REPLACE INTO iv_history(symbol, day, atm_iv) VALUES(?,?,?)", (sym, day, ch["atm_iv"]))
            hist = store.q("SELECT atm_iv FROM iv_history WHERE symbol=? AND day<? ORDER BY day DESC LIMIT 20", (sym, day))
            if len(hist) >= 5:
                info["iv_ref"], info["iv_ref_kind"] = sum(h["atm_iv"] for h in hist) / len(hist), f"{len(hist)}-session avg ATM IV"
            elif ch.get("hv20"):
                info["iv_ref"], info["iv_ref_kind"] = ch["hv20"], "HV20 (no IV history stored yet)"
        return info


class Scanner:
    def __init__(self, feed):
        self.feed = feed
        self.manager = SignalManager(get_cfg)
        self.tracker = OptionTracker(feed, get_cfg)
        self.snapshot = {"status": "starting", "updated": None}
        self.processed: set = set()
        self.trigger_log: list[dict] = []
        self.running = False
        self.cycle_ms = None

    def start(self):
        if self.running:
            return
        self.running = True
        threading.Thread(target=self._loop, daemon=True, name="scanner").start()

    def _loop(self):
        while self.running:
            t0 = time.time()
            try:
                self.cycle()
            except Exception as e:
                log.error("scanner cycle failed: %s\n%s", e, traceback.format_exc())
                self.snapshot["error"] = str(e)
            self.cycle_ms = round((time.time() - t0) * 1000)
            time.sleep(max(1.0, get_cfg()["scan_interval_seconds"] - (time.time() - t0)))

    def cycle(self, now: dt.datetime | None = None):
        cfg = get_cfg()
        if not self.feed.ready():
            self.snapshot = {"status": "waiting for data", "feed": self.feed.status(), "updated": None}
            return
        now = now or self.feed.now()
        stocks = self.feed.stocks()
        for s in stocks:
            if s.get("hv20") is None and s.get("daily_closes"):
                s["hv20"] = options.hist_vol(s["daily_closes"])
        market = self.feed.market(stocks)
        if cfg.get("strategy") == "pullback":
            return self._cycle_pullback(cfg, now, stocks, market)
        evals = []
        by_sym = {s["symbol"]: s for s in stocks}
        for s in stocks:
            ev = evaluate(s, market, cfg, now, None)
            evals.append(ev)
        cands = [e for e in evals if e["direction"] and e["status"] != "REJECTED"
                 and prescore(e) >= cfg["option_check_min_prescore"]]
        cands.sort(key=lambda e: -prescore(e))
        final = {e["symbol"]: e for e in evals}
        oinfos = {}
        for e in cands[:25]:
            s = by_sym[e["symbol"]]
            oc = self.tracker.chain(s, now)
            oinfos[e["symbol"]] = oc["info"]
            final[e["symbol"]] = evaluate(s, market, cfg, now, oc["info"])

        for ev in final.values():
            if ev["status"] == "TRIGGER":
                key = (ev["symbol"], ev["direction"], ev["trigger"]["bar_time"], now.date())
                if key in self.processed:
                    continue
                self.processed.add(key)
                self._on_trigger(ev, by_sym[ev["symbol"]], oinfos.get(ev["symbol"]), now)

        # lifecycle / paper updates
        open_sigs = self.manager.open_signals()
        if open_sigs:
            q = self.feed.option_quotes([r["plan"]["tradingsymbol"] for r in open_sigs])
            spot = {s["symbol"]: s["bars"][-1]["c"] for s in stocks if s.get("bars")}
            self.manager.update(now, q, spot)

        ordered = sorted(final.values(), key=lambda e: (-e["score"], e["symbol"]))
        slim = [self._slim(e) for e in ordered]
        self.snapshot = {
            "status": "running", "mode": self.feed.mode, "updated": now.isoformat(timespec="seconds"),
            "market": {k: market.get(k) for k in ("nifty", "banknifty", "breadth")},
            "universe": len(stocks), "evaluated": len(evals), "option_checked": len(oinfos),
            "bullish": [x for x in slim if x["direction"] == "BULL" and x["status"] in ("READY", "WATCHING", "TRIGGER")],
            "bearish": [x for x in slim if x["direction"] == "BEAR" and x["status"] in ("READY", "WATCHING", "TRIGGER")],
            "rejected": [x for x in slim if x["status"] == "REJECTED"][:60],
            "counts": {st: sum(1 for x in slim if x["status"] == st) for st in
                       ("TRIGGER", "READY", "WATCHING", "REJECTED", "NO_SETUP")},
            "full": {e["symbol"]: e for e in ordered if e["status"] in ("READY", "WATCHING", "TRIGGER", "REJECTED")},
            "trigger_log": self.trigger_log[-30:], "cycle_ms": self.cycle_ms,
        }

    # ================================================================ v3: pullback strategy
    def _cycle_pullback(self, cfg, now, stocks, market):
        regime = detect_regime(market, cfg)
        by_sym = {s["symbol"]: s for s in stocks}
        final = {s["symbol"]: evaluate_pullback(s, market, regime, cfg, now, None) for s in stocks}
        cands = [e for e in final.values() if e["status"] in ("TRIGGER", "WATCHING") and e.get("stage") in ("STAGE2", "STAGE3")
                 and e["pre_score"] >= 35]
        cands.sort(key=lambda e: (e["status"] != "TRIGGER", -e["pre_score"]))
        oinfos = {}
        for e in cands[:8]:
            st = by_sym[e["symbol"]]
            oc = self.tracker.chain(st, now)
            oinfos[e["symbol"]] = oc["info"]
            final[e["symbol"]] = evaluate_pullback(st, market, regime, cfg, now, oc["info"])
        for ev in final.values():
            if ev["status"] == "TRIGGER":
                key = (ev["symbol"], ev["direction"], ev["trigger"]["bar_time"], now.date())
                if key in self.processed:
                    continue
                self.processed.add(key)
                self._on_trigger_pullback(ev, by_sym[ev["symbol"]], oinfos.get(ev["symbol"]), regime, cfg, now)

        open_sigs = self.manager.open_signals()
        if open_sigs:
            q = self.feed.option_quotes([r["plan"]["tradingsymbol"] for r in open_sigs])
            spot = {s["symbol"]: s["bars"][-1]["c"] for s in stocks if s.get("bars")}
            self.manager.update(now, q, spot)

        ordered = sorted(final.values(), key=lambda e: (-e["pre_score"], e["symbol"]))
        slim = [self._slim(e) for e in ordered]
        live_states = ("TRIGGER", "WATCHING")
        self.snapshot = {
            "status": "running", "mode": self.feed.mode, "strategy": "pullback",
            "updated": now.isoformat(timespec="seconds"), "regime": regime,
            "market": {k: market.get(k) for k in ("nifty", "banknifty", "breadth", "vix")},
            "universe": len(stocks), "evaluated": len(final), "option_checked": len(oinfos),
            "alerts_today": self.manager.daily_count(now), "max_alerts": cfg["max_alerts_per_day"],
            "bullish": [x for x in slim if x["direction"] == "BULL" and x["status"] in live_states][:25],
            "bearish": [x for x in slim if x["direction"] == "BEAR" and x["status"] in live_states][:25],
            "rejected": [x for x in slim if x["status"] == "REJECTED"][:60],
            "counts": {"TRIGGER": sum(1 for x in slim if x["status"] == "TRIGGER"),
                       "STAGE2": sum(1 for x in slim if x.get("stage") == "STAGE2"),
                       "STAGE1": sum(1 for x in slim if x.get("stage") == "STAGE1" and x["status"] == "WATCHING"),
                       "REJECTED": sum(1 for x in slim if x["status"] == "REJECTED"),
                       "NO_SETUP": sum(1 for x in slim if x["status"] == "NO_SETUP"),
                       "READY": sum(1 for x in slim if x.get("stage") == "STAGE2"),
                       "WATCHING": sum(1 for x in slim if x["status"] == "WATCHING")},
            "full": {e["symbol"]: e for e in ordered if e["status"] in ("TRIGGER", "WATCHING", "REJECTED")},
            "trigger_log": self.trigger_log[-30:], "cycle_ms": self.cycle_ms,
        }

    def _on_trigger_pullback(self, ev, stock, oinfo, regime, cfg, now):
        rec = {"time": now.isoformat(timespec="seconds"), "symbol": ev["symbol"], "direction": ev["direction"],
               "score": ev["pre_score"]}
        try:
            oc = self.tracker.chain(stock, now, max_age=10)
            ch = oc.get("chain")
            if not ch:
                rec["result"] = f"rejected: {oc['info'].get('reason')}"
                return
            sel = select_contract(ch, ev["direction"], cfg)
            if not sel.get("selected"):
                rec["result"] = f"rejected: {sel.get('error')}"
                return
            plan = build_plan(ev, sel, ch, cfg, now, levels=ev["levels"])
            if "error" in plan:
                rec["result"] = f"rejected: {plan['error']}"
                return
            pts = contract_points(ev, sel, plan, ch, stock, cfg)
            bad = [c for c in ev["checks"] if c["layer"] == 4 and c["mandatory"] and c["passed"] is False]
            if bad:
                rec["result"] = f"rejected: {bad[0]['label']} {bad[0]['value']}"
                return
            score = round(ev["pre_score"] + pts, 1)
            g = grade_of(score, regime, cfg)
            rec["score"] = score
            if g is None:
                rec["result"] = f"score {score:g}/100 below {cfg['grade_b_min']}"
                return
            if g == "B":
                after = now.astimezone(IST).time() >= dt.datetime.strptime(cfg["b_grade_time"], "%H:%M").time()
                if not after or self.manager.daily_count(now) > 0:
                    rec["result"] = (f"B grade ({score:g}) held: B backup only after {cfg['b_grade_time']} "
                                     "on a day with no A alerts")
                    return
            ev["score"], ev["grade"], ev["max_score"] = score, g, 100
            plan.update(score=score, max_score=100, confidence=g, grade=g, strategy="pullback",
                        regime=regime.get("regime"))
            if g == "B":
                plan["plan_notes"] = plan.get("plan_notes", []) + ["B grade backup: consider half size"]
            exp = explain(ev, sel, plan, oinfo)
            exp["why_stock"] = (f"[{g}] {ev['symbol']} {ev['metrics']['chg_pct']:+.2f}% with RVOL "
                                f"{ev['metrics'].get('rvol')}x and {ev['metrics'].get('buildup')}; pulled back to VWAP "
                                f"and turned. NIFTY regime {regime.get('regime')}. Score {score:g}/100.")
            r = self.manager.on_trigger(ev, sel, plan, exp, now, mode=self.feed.mode)
            rec["result"] = f"signal #{r['id']} [{g}]" if r["created"] else f"not sent: {r['reason']}"
        except Exception as e:
            rec["result"] = f"error: {e}"
            log.error("pullback trigger %s failed: %s\n%s", ev["symbol"], e, traceback.format_exc())
        finally:
            log.info("trigger %s %s -> %s", ev["symbol"], ev["direction"], rec.get("result"))
            self.trigger_log.append(rec)

    @staticmethod
    def _slim(e):
        return {"symbol": e["symbol"], "direction": e["direction"], "status": e["status"], "score": e["score"],
                "stage": e.get("stage"), "grade": e.get("grade"),
                "sector": e["sector"], "metrics": e["metrics"], "wait_reason": e.get("wait_reason"),
                "reject_reasons": e["reject_reasons"][:3], "missing": (e.get("missing") or [])[:3],
                "trigger": e.get("trigger")}

    def _on_trigger(self, ev, stock, oinfo, now):
        cfg = get_cfg()
        rec = {"time": now.isoformat(timespec="seconds"), "symbol": ev["symbol"], "direction": ev["direction"],
               "score": ev["score"]}
        oc = self.tracker.chain(stock, now, max_age=10)  # fresh quotes for the actual contract choice
        ch = oc.get("chain")
        if not ch:
            rec["result"] = f"rejected: {oc['info'].get('reason')}"
        else:
            sel = select_contract(ch, ev["direction"], cfg)
            if not sel.get("selected"):
                rec["result"] = f"rejected: {sel.get('error')}"
            else:
                plan = build_plan(ev, sel, ch, cfg, now)
                if "error" in plan:
                    rec["result"] = f"rejected: {plan['error']}"
                else:
                    exp = explain(ev, sel, plan, oinfo)
                    r = self.manager.on_trigger(ev, sel, plan, exp, now, mode=self.feed.mode)
                    rec["result"] = f"signal #{r['id']}" if r["created"] else f"not sent: {r['reason']}"
        log.info("trigger %s %s -> %s", ev["symbol"], ev["direction"], rec["result"])
        self.trigger_log.append(rec)


# ------------------------------------------------------------ single-instance guard
_lock_handle = None


def acquire_singleton(path) -> bool:
    """Prevents two scanner processes (e.g. two uvicorn workers) from running at once."""
    global _lock_handle
    try:
        fh = open(path, "a+")
        if os.name == "nt":
            import msvcrt
            msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        _lock_handle = fh
        return True
    except OSError:
        return False
