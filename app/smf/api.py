"""FastAPI routes /api/smf/* and the evening scheduler (runs jobs as subprocesses)."""
from __future__ import annotations

import datetime as dt
import json
import subprocess
import sys
import threading
import time
from pathlib import Path

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel

from . import collect, daily, db, model

router = APIRouter(prefix="/api/smf", tags=["smf"])
IST = dt.timezone(dt.timedelta(hours=5, minutes=30))
ROOT = Path(__file__).resolve().parents[2]
_procs: dict[str, subprocess.Popen] = {}
NOTIFY = {"fn": None}


def _job(name):
    f = db.OUT / f"job_{name}.json"
    return json.loads(f.read_text()) if f.exists() else None


def start_job(name: str) -> dict:
    p = _procs.get(name)
    if p and p.poll() is None:
        raise HTTPException(409, f"{name} already running")
    if any(q.poll() is None for q in _procs.values()):
        raise HTTPException(409, "another SMF job is running; wait for it")
    _procs[name] = subprocess.Popen(["nice", "-n", "10", sys.executable, "-m", "app.smf.run", name], cwd=str(ROOT),
                                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return {"started": name}


def _meta(k, default=None):
    c = db.conn()
    try:
        return db.meta_get(c, k, default)
    finally:
        c.close()


@router.get("/today")
def today():
    return {"report": _meta("today"), "job": _job("daily")}


@router.get("/status")
def status():
    return {"data": collect.counts(), "jobs": {n: _job(n) for n in ("update", "daily", "backtest", "all")},
            "telegram_configured": daily.SECRETS.exists(), "config": model.load_cfg()}


@router.post("/run/{job}")
def run(job: str):
    if job not in ("update", "daily", "backtest", "all"):
        raise HTTPException(400, "job must be update, daily, backtest or all")
    return start_job(job)


@router.get("/backtest")
def backtest_get():
    return {"result": _meta("backtest"), "job": _job("backtest")}


@router.get("/watch")
def watch():
    c = db.conn()
    try:
        rows = [dict(r) for r in c.execute("SELECT * FROM watch ORDER BY (status='WATCHING') DESC, last_d DESC LIMIT 100")]
    finally:
        c.close()
    return {"watch": rows}


@router.get("/paper")
def paper():
    return daily.paper_report()


@router.get("/excel")
def excel_file():
    rep = _meta("today")
    if not rep or not rep.get("excel") or not Path(rep["excel"]).exists():
        raise HTTPException(404, "no Excel yet")
    return FileResponse(rep["excel"], filename=Path(rep["excel"]).name,
                        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")


class Telegram(BaseModel):
    bot_token: str
    chat_id: str


@router.post("/telegram")
def telegram_set(t: Telegram):
    if ":" not in t.bot_token or not t.chat_id.strip():
        raise HTTPException(400, "bot token looks wrong")
    daily.SECRETS.write_text(json.dumps({"bot_token": t.bot_token.strip(), "chat_id": t.chat_id.strip()}))
    daily.SECRETS.chmod(0o600)
    err = daily.telegram_send("F&O Cockpit SMF: Telegram connected.")
    if err:
        raise HTTPException(400, err)
    return {"ok": True}


class Settings(BaseModel):
    min_score: float | None = None
    capital: float | None = None
    risk_pct: float | None = None


@router.post("/settings")
def settings(s: Settings):
    import yaml
    f = model.USER_CFG
    cur = yaml.safe_load(f.read_text()) if f.exists() else {}
    cur = cur or {}
    if s.min_score is not None:
        if not 0 <= s.min_score <= 10:
            raise HTTPException(400, "min_score 0-10")
        cur.setdefault("lists", {})["min_score"] = s.min_score
    if s.capital is not None:
        cur.setdefault("trade", {})["capital"] = s.capital
    if s.risk_pct is not None:
        if not 0.1 <= s.risk_pct <= 5:
            raise HTTPException(400, "risk_pct 0.1-5")
        cur.setdefault("trade", {})["risk_pct"] = s.risk_pct
    f.write_text(yaml.safe_dump(cur))
    return {"ok": True, "override": cur}


# ------------------------------------------------------------------ evening scheduler
class Scheduler:
    def __init__(self, notify):
        NOTIFY["fn"] = notify
        self.state = {"last_try": None, "notified_for": _meta("notified_for")}

    def start(self):
        threading.Thread(target=self._loop, daemon=True, name="smf").start()

    def _loop(self):
        while True:
            try:
                self.tick()
            except Exception:
                pass
            time.sleep(60)

    def tick(self):
        cfg = model.load_cfg()
        now = dt.datetime.now(IST)
        today = now.date().isoformat()
        st, end = (dt.time.fromisoformat(cfg["schedule"][k]) for k in ("start", "give_up"))
        rep = _meta("today") or {}
        p = _procs.get("daily")
        running = p is not None and p.poll() is None
        if not running and rep.get("date") == today and self.state["notified_for"] != today:
            self._notify(rep)
        if now.weekday() >= 5 or not (st <= now.time() <= end) or running or rep.get("date") == today:
            return
        last = self.state["last_try"]
        if last is None or time.time() - last > 900:     # NSE files late? retry every 15 min
            self.state["last_try"] = time.time()
            try:
                start_job("daily")
            except HTTPException:
                pass

    def _notify(self, rep):
        self.state["notified_for"] = rep["date"]
        c = db.conn()
        try:
            db.meta_set(c, "notified_for", rep["date"])
        finally:
            c.close()
        fn = NOTIFY["fn"]
        if not fn:
            return
        L = rep.get("lists") or {}
        parts = [f"{k.replace('_', ' ')}: " + (", ".join(r["symbol"] for r in v[:5]) or "none") for k, v in L.items()]
        fn("SMF", f"Smart Money list {rep['date']}", "\n".join(parts), {"smf_date": rep["date"]})
        for a in rep.get("alerts") or []:
            fn("SMF", a["text"][:80], a["text"], {"symbol": a["symbol"]})
