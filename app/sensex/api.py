"""/api/sensex/* : live commentary view and the Sensex backtest job."""
from __future__ import annotations

import datetime as dt
import threading

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from ..engine import store
from . import backtest as bt

router = APIRouter(prefix="/api/sensex", tags=["sensex"])
CTX = {"live": None, "src": None}
JOB = {"running": False, "stage": None, "error": None}
IST = dt.timezone(dt.timedelta(hours=5, minutes=30))


@router.get("/live")
def live(after: int = 0):
    lv = CTX["live"]
    if lv is None:
        raise HTTPException(503, "Sensex engine not running (demo mode or second process)")
    return lv.view(after)


@router.get("/day/{day}")
def day(day: str):
    return {"day": day, "commentary": store.kv_get(f"sensex_comm:{day}") or []}


class BT(BaseModel):
    days: int = 120


@router.get("/backtest")
def backtest_get():
    return {"job": JOB, "result": store.kv_get("sensex_backtest")}


@router.post("/backtest")
def backtest_run(p: BT):
    if JOB["running"]:
        raise HTTPException(409, "backtest already running")
    src = CTX["src"]
    if src is None or not getattr(src, "kite", None) or not src.logged_in():
        raise HTTPException(409, "Kite login kavali (history download ki)")
    days = max(20, min(p.days, 250))

    def work():
        JOB.update(running=True, error=None, stage="starting")
        try:
            kite = src.kite
            q = kite.quote(["BSE:SENSEX", "NSE:NIFTY 50"])
            cache = store.DATA / "cache" / "sensex"
            sx = bt.fetch_minutes(kite, q["BSE:SENSEX"]["instrument_token"], days, cache, lambda s: JOB.update(stage=s))
            nf = bt.fetch_minutes(kite, q["NSE:NIFTY 50"]["instrument_token"], days, cache, lambda s: JOB.update(stage=s))
            JOB["stage"] = "replaying"
            res = bt.run(sx, nf, days, store.kv_get("sensex_rules"))
            res["rules"] = store.kv_get("sensex_rules") or "default"
            res["created_at"] = dt.datetime.now(IST).isoformat(timespec="seconds")
            store.kv_set("sensex_backtest", res)
            JOB["stage"] = "done"
        except Exception as e:
            JOB.update(error=f"{type(e).__name__}: {e}", stage="failed")
        finally:
            JOB["running"] = False

    threading.Thread(target=work, daemon=True).start()
    return {"started": True, "days": days}


LAB = {"running": False, "stage": None, "error": None}


@router.get("/lab")
def lab_get():
    return {"job": LAB, "result": store.kv_get("sensex_lab"), "applied_rules": store.kv_get("sensex_rules")}


@router.post("/lab")
def lab_run():
    if LAB["running"] or JOB["running"]:
        raise HTTPException(409, "already running")
    src = CTX["src"]
    if src is None or not getattr(src, "kite", None) or not src.logged_in():
        raise HTTPException(409, "Kite login kavali (history download ki)")

    def work():
        LAB.update(running=True, error=None, stage="downloading history")
        try:
            kite = src.kite
            q = kite.quote(["BSE:SENSEX", "NSE:NIFTY 50"])
            cache = store.DATA / "cache" / "sensex"
            sx = bt.fetch_minutes(kite, q["BSE:SENSEX"]["instrument_token"], 120, cache, lambda s: LAB.update(stage=s))
            nf = bt.fetch_minutes(kite, q["NSE:NIFTY 50"]["instrument_token"], 120, cache, lambda s: LAB.update(stage=s))
            res = bt.lab(sx, nf, 120, 40, lambda s: LAB.update(stage=s))
            res["created_at"] = dt.datetime.now(IST).isoformat(timespec="seconds")
            store.kv_set("sensex_lab", res)
            LAB["stage"] = "done"
        except Exception as e:
            LAB.update(error=f"{type(e).__name__}: {e}", stage="failed")
        finally:
            LAB["running"] = False

    threading.Thread(target=work, daemon=True).start()
    return {"started": True}


class Rules(BaseModel):
    rules: dict | None = None     # None = back to the original rules


ALLOWED = {"t1_r", "t2_r", "t1_exit_frac", "sl_mult", "max_trades", "time_stop_min", "require_pd_break"}


@router.post("/rules")
def rules_set(r: Rules):
    if r.rules is None:
        store.kv_set("sensex_rules", None)
        return {"ok": True, "rules": None}
    bad = set(r.rules) - ALLOWED
    if bad:
        raise HTTPException(400, f"unknown rule keys {sorted(bad)}")
    store.kv_set("sensex_rules", r.rules)
    return {"ok": True, "rules": r.rules, "note": "Repu morning nunchi (or server restart) live lo apply avutundi"}
