"""REST API for the signal engine (used by the Android app and the web Signals page)."""
from __future__ import annotations

import datetime as dt
import json
import threading
import time

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from . import store
from .backtest import run_backtest
from .config import DEFAULTS, IST
from .scanner import get_cfg, save_cfg

VERSION = "3.0.0"
router = APIRouter(prefix="/api/engine")
CTX: dict = {"scanner": None, "feed": None, "started": time.time(), "singleton": None}
BT: dict = {"running": False, "progress": {}, "error": None}


def _sc():
    if CTX["scanner"] is None:
        raise HTTPException(503, "Scanner not running in this process")
    return CTX["scanner"]


def _slim_signal(r):
    p = r["plan"]
    return {"id": r["id"], "day": r["day"], "symbol": r["symbol"], "bias": r["bias"], "status": r["status"],
            "score": r["score"], "created_at": r["created_at"], "updated_at": r["updated_at"],
            "tradingsymbol": p["tradingsymbol"], "option_type": p["option_type"], "strike": p["strike"],
            "expiry": p["expiry"], "entry_range": p["entry_range"], "entry_premium": p["entry_premium"],
            "sl_premium": p["sl_premium"], "t1_premium": p["t1_premium"], "t2_premium": p["t2_premium"],
            "lots": p["lots"], "qty": p["qty"], "max_loss": p["max_loss"], "rr_t1": p["rr_t1"],
            "confidence": p["confidence"], "fill_price": r["fill_price"], "last_bid": r["last_bid"],
            "net_pnl": r["net_pnl"], "outcome": r["outcome"], "mode": r["mode"]}


@router.get("/health")
def health():
    sc, feed = CTX["scanner"], CTX["feed"]
    now = dt.datetime.now(IST)
    return {"version": VERSION, "server_time": now.isoformat(timespec="seconds"),
            "uptime_s": round(time.time() - CTX["started"]), "execution": "PAPER (no live orders)",
            "singleton": CTX["singleton"], "feed": feed.status() if feed else None,
            "scanner": {"status": sc.snapshot.get("status") if sc else "not running",
                        "updated": sc.snapshot.get("updated") if sc else None,
                        "cycle_ms": sc.cycle_ms if sc else None,
                        "error": sc.snapshot.get("error") if sc else None}}


@router.get("/scanner")
def scanner_view():
    snap = dict(_sc().snapshot)
    snap.pop("full", None)
    return snap


@router.get("/stock/{symbol}")
def stock_eval(symbol: str):
    ev = (_sc().snapshot.get("full") or {}).get(symbol.upper())
    if not ev:
        raise HTTPException(404, "No active evaluation for this symbol (NO_SETUP or not in universe)")
    return ev


@router.get("/signals")
def signals(day: str | None = None, status: str | None = None, limit: int = 100):
    st = [s.strip().upper() for s in status.split(",")] if status else None
    return [_slim_signal(r) for r in _sc().manager.list(day=day, status=st, limit=limit)]


@router.get("/signals/{sid}")
def signal(sid: int):
    r = _sc().manager.get(sid)
    if not r:
        raise HTTPException(404, "Signal not found")
    return r


@router.get("/events/last")
def events_last():
    r = store.q("SELECT MAX(id) AS m FROM events")
    return {"last_id": (r[0]["m"] or 0) if r else 0}


@router.get("/events")
def events(after: int = 0, limit: int = 100):
    return _sc().manager.events(after, min(limit, 500))


@router.get("/risk")
def risk():
    sc = _sc()
    cfg = get_cfg()
    now = sc.feed.now()
    return {"state": sc.manager.risk_state(now),
            "limits": {k: cfg[k] for k in ("capital", "risk_per_trade_pct", "max_premium_pct", "max_daily_loss_pct",
                                           "max_open_positions", "max_exposure_pct", "min_rr", "min_score",
                                           "max_spread_pct", "max_signals_per_symbol_day")}}


@router.get("/paper")
def paper():
    sc = _sc()
    day = sc.feed.now().astimezone(IST).date().isoformat()
    rows = sc.manager.list(day=day, limit=500)
    return {"day": day, "open": [_slim_signal(r) for r in rows if r["status"] in ("TRIGGERED", "ACTIVE")],
            "closed_today": [_slim_signal(r) for r in rows if r["status"] not in ("TRIGGERED", "ACTIVE")],
            "stats_all_time": sc.manager.stats()}


@router.get("/config")
def config_get():
    return {"config": get_cfg(), "defaults": DEFAULTS}


@router.post("/config")
def config_set(new: dict):
    bad = [k for k in new if k not in DEFAULTS]
    if bad:
        raise HTTPException(400, f"Unknown settings: {', '.join(bad)}")
    try:
        cfg = save_cfg(new)
        from .config import merged
        merged(cfg)
    except Exception as e:
        raise HTTPException(400, f"Invalid value: {e}")
    return cfg


class BacktestIn(BaseModel):
    days: int = 120
    symbols: list[str] | None = None
    cost_pct: float = 0.20


def _bt_job(p: BacktestIn):
    try:
        BT.update(running=True, error=None, progress={"stage": "downloading data", "done": 0, "total": 0})
        feed = CTX["feed"]
        cfg = get_cfg()
        if cfg.get("strategy") == "pullback":
            from .backtest_pullback import run as run_pb
            src = feed.backtest_source(p.days, BT["progress"])
            if not src["symbols"]:
                raise RuntimeError("no historical data downloaded: " + "; ".join(src["notes"][:3]))
            res = run_pb(src["symbols"], src["loader"], src["indices"], cfg, days=p.days, cost_pct=p.cost_pct,
                         progress=BT["progress"], source=src["source"], notes=src["notes"])
            res["created_at"] = dt.datetime.now(IST).isoformat(timespec="seconds")
            store.x("INSERT INTO backtests(created_at, params, result) VALUES(?,?,?)",
                    (res["created_at"], p.model_dump_json(), json.dumps(res)))
            return
        cache = store.DATA / "cache" / f"bt_{dt.date.today()}_{p.days}_{'all' if not p.symbols else len(p.symbols)}_{feed.mode}.json"
        if cache.exists():
            ds = json.loads(cache.read_text())
        else:
            ds = feed.backtest_dataset(p.days, p.symbols, BT["progress"])
            cache.parent.mkdir(exist_ok=True)
            cache.write_text(json.dumps(ds))
        res = run_backtest(ds, get_cfg(), days=p.days, cost_pct=p.cost_pct, progress=BT["progress"])
        res["created_at"] = dt.datetime.now(IST).isoformat(timespec="seconds")
        store.x("INSERT INTO backtests(created_at, params, result) VALUES(?,?,?)",
                (res["created_at"], p.model_dump_json(), json.dumps(res)))
    except Exception as e:
        BT["error"] = str(e)
    finally:
        BT["running"] = False


@router.post("/backtest")
def backtest_start(p: BacktestIn):
    if BT["running"]:
        raise HTTPException(409, "A backtest is already running")
    if not 5 <= p.days <= 180:
        raise HTTPException(400, "days must be 5..180")
    if CTX["feed"] is None or not CTX["feed"].ready():
        raise HTTPException(503, "Market data feed not ready (login first)")
    threading.Thread(target=_bt_job, args=(p,), daemon=True).start()
    return {"started": True}


@router.get("/backtest")
def backtest_get():
    last = store.q("SELECT id, created_at, params, result FROM backtests ORDER BY id DESC LIMIT 1")
    out = {"running": BT["running"], "progress": BT["progress"], "error": BT["error"], "last": None}
    if last:
        out["last"] = json.loads(last[0]["result"])
    return out
