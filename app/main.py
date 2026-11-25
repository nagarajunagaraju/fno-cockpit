"""F&O Cockpit - personal stock F&O trading dashboard."""
from __future__ import annotations

import datetime as dt
import json
import os
import threading
import time
from pathlib import Path

import hmac
import logging
import secrets

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import analytics, journal, options, risk, predictor
from .data_source import DemoSource, KiteSource
from .logging_setup import setup_logging

ROOT = Path(__file__).resolve().parent.parent
CONFIG = json.loads((ROOT / "config.json").read_text()) if (ROOT / "config.json").exists() else {}

from .engine import store as engine_store  # noqa: E402  (creates data dir + engine db)
from .engine.api import CTX, router as engine_router  # noqa: E402
from .engine.feed_kite import KiteFeed  # noqa: E402
from .engine.feed_sim import SimFeed  # noqa: E402
from .engine.ratelimit import RateLimitedKite  # noqa: E402
from .engine.scanner import Scanner, acquire_singleton, get_cfg  # noqa: E402

log = setup_logging(engine_store.DATA / "logs")

journal.init()
DEMO = os.environ.get("COCKPIT_DEMO") == "1" or not CONFIG.get("api_key")
source = DemoSource() if DEMO else KiteSource(CONFIG["api_key"], CONFIG["api_secret"])
if not DEMO:
    source.kite = RateLimitedKite(source.kite)   # every Kite REST call goes through the rate limiter

# ------------------------------------------------------------------ app token (phone / remote access)
TOKEN_FILE = engine_store.DATA / "app_token.txt"
APP_TOKEN = CONFIG.get("app_token") or os.environ.get("COCKPIT_APP_TOKEN")
if not APP_TOKEN:
    if not TOKEN_FILE.exists():
        TOKEN_FILE.write_text(secrets.token_urlsafe(24))
    APP_TOKEN = TOKEN_FILE.read_text().strip()
LOCAL_NO_AUTH = os.environ.get("COCKPIT_LOCAL") == "1"   # set by run.bat on your own PC only

app = FastAPI(title="F&O Cockpit")
app.mount("/static", StaticFiles(directory=ROOT / "static"), name="static")


@app.middleware("http")
async def require_token(request: Request, call_next):
    path = request.url.path
    if path.startswith("/api/"):
        local = request.client and request.client.host in ("127.0.0.1", "::1")
        if not (LOCAL_NO_AUTH and local):
            got = request.headers.get("x-app-token") or request.query_params.get("token") or ""
            auth = request.headers.get("authorization", "")
            if auth.lower().startswith("bearer "):
                got = auth[7:]
            if not hmac.compare_digest(got.encode(), APP_TOKEN.encode()):
                return JSONResponse({"detail": "Invalid or missing app token"}, status_code=401)
    return await call_next(request)


app.include_router(engine_router)
from .smf.api import router as smf_router, Scheduler as SmfScheduler  # noqa: E402
app.include_router(smf_router)

# ------------------------------------------------------------------ signal engine
feed = SimFeed(get_cfg, speed=float(os.environ.get("COCKPIT_SIM_SPEED", "20"))) if DEMO else \
    KiteFeed(source, CONFIG["api_key"], get_cfg)
scanner = Scanner(feed)
CTX["feed"] = feed
if acquire_singleton(engine_store.DATA / "scanner.lock"):
    CTX["scanner"] = scanner
    CTX["singleton"] = "this process owns the scanner"
    scanner.start()
    if not DEMO:
        from .engine.eod import EodJob
        from .engine.config import IST as _IST
        CTX["eod"] = EodJob(feed, get_cfg,
                            lambda k, t, b, p: scanner.manager.emit(dt.datetime.now(_IST), k, t, b, None, p))
        CTX["eod"].start()
        CTX["smf"] = SmfScheduler(lambda k, t, b, p: scanner.manager.emit(dt.datetime.now(_IST), k, t, b, None, p))
        CTX["smf"].start()
else:
    CTX["singleton"] = "another process already runs the scanner - not started here"
    log.warning(CTX["singleton"])

STATE = {"rows": [], "raw_rows": [], "by_symbol": {}, "index": None, "updated": None, "updated_epoch": 0.0, "error": None,
         "broker_pnl": None, "broker_orders": None, "hv": {}, "opt_ltp": {},
         "last_report_date": None, "latest_report": None}
REPORTS = ROOT / "reports"
REPORTS.mkdir(exist_ok=True)


def rules():
    return journal.get_rules(risk.DEFAULT_RULES)


def refresher():
    """Pulls quotes in the background so the page never waits on the broker."""
    last_broker = 0.0
    while True:
        r = rules()
        try:
            if source.ready():
                fi = None if DEMO else feed.idx.get("NIFTY 50")
                idx = ({"name": "NIFTY 50", "ltp": fi["ltp"], "prev_close": fi["prev_close"]}
                       if fi and fi.get("prev_close") else source.index())
                idx_chg = analytics.pct(idx["ltp"], idx["prev_close"])
                snap = feed.legacy_snapshot() if (not DEMO and feed.ready()) else source.snapshot()
                rows = [analytics.analyse(x, idx_chg) for x in snap]
                for x in snap:
                    if x["symbol"] not in STATE["hv"]:
                        STATE["hv"][x["symbol"]] = options.hist_vol([c["close"] for c in x["candles"]])
                opens = journal.open_trades()
                if opens:
                    STATE["opt_ltp"] = source.option_ltps(opens)
                rows.sort(key=lambda x: -abs(x["score"]))
                STATE.update(rows=rows, raw_rows=snap, by_symbol={x["symbol"]: x for x in rows},
                             index={**idx, "chg": round(idx_chg, 2)},
                             updated=dt.datetime.now().strftime("%H:%M:%S"), updated_epoch=time.time(), error=None)
                if time.time() - last_broker > 15:  # positions/orders less often
                    STATE["broker_pnl"] = source.day_pnl()
                    STATE["broker_orders"] = source.orders_today()
                    last_broker = time.time()
        except Exception as e:
            STATE["error"] = str(e)
        time.sleep(max(2, r["refresh_seconds"]))


def report_scheduler():
    """Persist one snapshot at 09:28 Asia/Kolkata on each trading-calendar day.
    The scheduler does not infer exchange holidays; if the feed is unavailable,
    it skips that day's report rather than fabricating a result.
    """
    from zoneinfo import ZoneInfo
    tz = ZoneInfo("Asia/Kolkata")
    while True:
        now = dt.datetime.now(tz)
        day = now.date().isoformat()
        fresh_feed = bool(STATE["raw_rows"]) and (time.time() - STATE.get("updated_epoch", 0.0) < 30)
        if now.weekday() < 5 and now.hour == 9 and now.minute == 28 and STATE["last_report_date"] != day and fresh_feed:
            idx = STATE.get("index") or {}
            report = predictor.build_report(STATE["raw_rows"], float(idx.get("chg") or 0.0))
            report["scheduled_time_ist"] = now.isoformat()
            (REPORTS / f"{day}.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
            STATE["last_report_date"] = day
            STATE["latest_report"] = report
        time.sleep(15)


threading.Thread(target=refresher, daemon=True).start()
if os.environ.get("COCKPIT_DAILY_REPORT") == "1":  # old fixed-time research report: off by default (no fixed times)
    threading.Thread(target=report_scheduler, daemon=True).start()


# ------------------------------------------------------------------ helpers
def open_trades_live():
    out = []
    for t in journal.open_trades():
        row = STATE["by_symbol"].get(t.get("underlying") or t["symbol"])
        ltp = STATE["opt_ltp"].get(t["symbol"])
        sign = -1 if t["side"] == "SHORT" else 1
        t["ltp"] = ltp
        t["upnl"] = round(sign * (ltp - t["entry"]) * t["qty"], 2) if ltp else None
        t["r_now"] = round(sign * (ltp - t["entry"]) / abs(t["entry"] - t["sl"]), 2) if ltp and t["entry"] != t["sl"] else None
        t["alert"] = risk.trade_alert(t, ltp)
        t["score_now"] = row["score"] if row else None
        t["spot"] = row["ltp"] if row else None
        if row and t.get("u_sl") and not t["alert"]:
            up = 1 if t.get("opt_type") == "CE" else -1
            if up * (row["ltp"] - t["u_sl"]) <= 0:
                t["alert"] = f"STOCK SL HIT ({t['u_sl']}) - exit option"
        out.append(t)
    return out


def discipline_now(open_list=None):
    r = rules()
    open_list = open_list if open_list is not None else open_trades_live()
    trades_today = len(journal.today_trades())
    if STATE["broker_orders"] is not None:  # count trades you took directly in Kite too
        trades_today = max(trades_today, (STATE["broker_orders"] + 1) // 2)
    if STATE["broker_pnl"] is not None:
        day_pnl = STATE["broker_pnl"]
    else:
        day_pnl = journal.realized_today() + sum(t["upnl"] or 0 for t in open_list)
    return risk.discipline(r, trades_today, day_pnl, journal.last_loss_at(), len(open_list))


# ------------------------------------------------------------------ pages
@app.get("/", response_class=HTMLResponse)
def index():
    return FileResponse(ROOT / "static" / "index.html")


@app.get("/login")
def login():
    if DEMO:
        return RedirectResponse("/")
    return RedirectResponse(source.login_url())


@app.get("/callback")
def callback(request_token: str | None = None, status: str | None = None):
    if not request_token:
        raise HTTPException(400, "No request_token from Kite")
    try:
        source.complete_login(request_token)
    except Exception as e:
        log.error("Kite login failed: %s", type(e).__name__)
        raise HTTPException(400, "Kite login failed. Try Login again (request tokens are single-use).")
    log.info("Kite login completed")
    return RedirectResponse("/static/login_ok.html")


# ------------------------------------------------------------------ api
@app.get("/api/dashboard")
def dashboard():
    opens = open_trades_live()
    return {
        "mode": source.mode,
        "logged_in": True if DEMO else source.logged_in(),
        "ready": source.ready(),
        "progress": source.progress(),
        "updated": STATE["updated"],
        "error": STATE["error"],
        "index": STATE["index"],
        "rows": STATE["rows"],
        "open_trades": opens,
        "discipline": discipline_now(opens),
        "rules": rules(),
    }


@app.get("/api/predictions")
def predictions():
    if not STATE["raw_rows"]:
        raise HTTPException(503, "Market data is not ready yet")
    idx = STATE.get("index") or {}
    return predictor.build_report(STATE["raw_rows"], float(idx.get("chg") or 0.0))


@app.get("/api/predictions/scheduled")
def scheduled_predictions():
    files = sorted(REPORTS.glob("????-??-??.json"), reverse=True)
    if not files:
        raise HTTPException(404, "No scheduled 09:28 report saved yet")
    return json.loads(files[0].read_text(encoding="utf-8"))


@app.get("/api/backtest")
def backtest():
    if not STATE["raw_rows"]:
        raise HTTPException(503, "Market data is not ready yet")
    return predictor.walk_forward_backtest(STATE["raw_rows"])


def get_chain(symbol: str, expiry: str | None):
    row = STATE["by_symbol"].get(symbol)
    if not row:
        raise HTTPException(404, "Symbol not in scanner yet")
    exps = source.expiries(symbol)
    if not exps:
        raise HTTPException(404, f"{symbol} ki options levu")
    if expiry not in exps:  # default: skip an expiry that is under 5 days away
        expiry = exps[1] if len(exps) > 1 and options.days_to_expiry(exps[0]) < 5 else exps[0]
    try:
        chain = source.option_chain(symbol, expiry)
    except Exception as e:
        raise HTTPException(502, f"Option chain error: {e}")
    chain = options.enrich_chain(chain, STATE["hv"].get(symbol))
    chain["expiries"] = exps
    return row, chain


@app.get("/api/chain")
def chain(symbol: str, expiry: str | None = None):
    row, ch = get_chain(symbol, expiry)
    kind = "PE" if row["score"] < 0 else "CE"
    sug = options.suggest_strike(ch, kind)
    ch["suggested"] = {"type": kind, "strike": sug["strike"] if sug else None}
    ch["stock"] = row
    return ch


@app.get("/api/plan")
def plan(symbol: str, strike: float, type: str, expiry: str | None = None,
         entry: float | None = None, sl: float | None = None):
    row, ch = get_chain(symbol, expiry)
    p = risk.plan_option_trade(row, ch, strike, type.upper(), rules(), entry, sl)
    if "error" in p:
        raise HTTPException(400, p["error"])
    p["reasons"] = row["reasons"]
    return p


class TradeIn(BaseModel):
    symbol: str          # underlying stock
    expiry: str
    strike: float
    type: str            # CE / PE
    entry: float
    sl: float
    checklist: dict[str, bool] = {}
    reason: str = ""
    force: bool = False  # log a trade you took against your rules (honest journal)


@app.post("/api/trades")
def log_trade(t: TradeIn):
    row, ch = get_chain(t.symbol, t.expiry)
    p = risk.plan_option_trade(row, ch, t.strike, t.type.upper(), rules(), t.entry, t.sl)
    if "error" in p:
        raise HTTPException(400, p["error"])
    d = discipline_now()
    checklist = dict(t.checklist)
    if not t.force:
        if not d["can_trade"]:
            raise HTTPException(409, " ".join(d["messages"]))
        if p["lots"] == 0:
            raise HTTPException(409, p["warnings"][0])
        if not checklist or not all(checklist.values()):
            raise HTTPException(409, "Checklist anni tick avvaledu. Setup complete kaadu.")
    else:
        checklist["rules_broken_on_purpose"] = False
        p["lots"] = max(p["lots"], 1)
        p["qty"] = p["lots"] * p["lot_size"]
        p["risk_amount"] = round(p["prem_risk"] * p["qty"], 2)
    tid = journal.add_trade(p, checklist, t.reason)
    return {"id": tid, "plan": p}


class CloseIn(BaseModel):
    exit_price: float | None = None
    mistakes: list[str] = []
    notes: str = ""


@app.post("/api/trades/{tid}/close")
def close(tid: int, c: CloseIn):
    t = next((x for x in journal.open_trades() if x["id"] == tid), None)
    if not t:
        raise HTTPException(404, "Open trade not found")
    price = c.exit_price or STATE["opt_ltp"].get(t["symbol"])
    if not price:
        raise HTTPException(400, "Exit price needed")
    return journal.close_trade(tid, price, c.mistakes, c.notes)


@app.get("/api/journal")
def get_journal():
    trades = journal.all_trades()
    for t in trades:
        t["mistakes"] = json.loads(t["mistakes"] or "[]")
    return {"trades": trades, "stats": journal.stats(), "tags": journal.MISTAKE_TAGS}


@app.post("/api/settings")
def save_settings(new: dict):
    r = rules()
    for k, v in new.items():
        if k in risk.DEFAULT_RULES:
            r[k] = int(float(v)) if isinstance(risk.DEFAULT_RULES[k], int) else float(v)
    journal.save_rules(r)
    return r
