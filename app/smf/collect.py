"""Phase 1: incremental download of the last N trading days into data/smf.db."""
from __future__ import annotations

import datetime as dt
import json
import logging
import time

from . import db, nse

log = logging.getLogger("smf.collect")
IST = dt.timezone(dt.timedelta(hours=5, minutes=30))


def today_ist() -> dt.date:
    return dt.datetime.now(IST).date()


def weekdays_back(end: dt.date, n_trading: int, holidays_guess: float = 1.06):
    """Weekdays from `end` backwards, enough to cover n_trading sessions (NSE has ~15 holidays/yr)."""
    out, d = [], end
    need = int(n_trading * holidays_guess) + 10
    while len(out) < need:
        if d.weekday() < 5:
            out.append(d)
        d -= dt.timedelta(days=1)
    return out


def refresh_lists(c, client: nse.Client) -> dict:
    res = {}
    try:
        raw = client.get(nse.N500_URL)
        if raw:
            lst = nse.parse_n500(raw)
            db.meta_set(c, "n500", lst)
            res["n500"] = len(lst)
    except Exception as e:
        res["n500_error"] = str(e)
    try:
        raw = client.get(nse.BAN_URL)
        if raw is not None:
            ban = nse.parse_ban(raw)
            db.meta_set(c, "ban", {"list": ban, "at": dt.datetime.now(IST).isoformat(timespec="minutes")})
            res["ban"] = len(ban)
    except Exception as e:
        res["ban_error"] = str(e)
    return res


def fetch_day(c, client: nse.Client, d: dt.date, keep: set[str]) -> dict:
    """Download one date. Returns status dict. Raises nse.NseError if NSE is unreachable."""
    ds = d.isoformat()
    raw = client.first("equity", d)
    if raw is None:
        st = "HOLIDAY" if d < today_ist() else "NOT_PUBLISHED"
        if st == "HOLIDAY":
            c.execute("INSERT OR REPLACE INTO days VALUES (?,?,?,?,?,?)", (ds, st, 0, 0, "no equity bhavcopy", _now()))
            c.commit()
        return {"d": ds, "status": st}
    eq = nse.parse_equity(raw)
    fo_raw = client.first("fo", d)
    fo = nse.parse_fo(fo_raw) if fo_raw else []
    fo_syms = {r["symbol"] for r in fo}
    universe = keep | fo_syms
    eq = [r for r in eq if r["symbol"] in universe] if universe else eq
    idx_raw = client.first("index", d)
    nifty = nse.parse_index(idx_raw) if idx_raw else None
    c.executemany("INSERT OR REPLACE INTO equity VALUES (:d,:symbol,:open,:high,:low,:close,:prev_close,:volume,"
                  ":turnover_cr,:trades,:deliv_qty,:deliv_per)", [dict(r, d=ds) for r in eq])
    c.executemany("INSERT OR REPLACE INTO fo VALUES (:d,:symbol,:fut_oi,:fut_oi_chg,:near_exp,:fut_close,:lot)",
                  [dict(r, d=ds) for r in fo])
    if nifty:
        c.execute("INSERT OR REPLACE INTO idx VALUES (?,?)", (ds, nifty))
    notes = [n for n, ok in (("no F&O file", fo_raw), ("no Nifty close", nifty)) if not ok]
    st = "OK" if not notes else "PARTIAL"
    c.execute("INSERT OR REPLACE INTO days VALUES (?,?,?,?,?,?)", (ds, st, len(eq), len(fo), ", ".join(notes), _now()))
    c.commit()
    return {"d": ds, "status": st, "equity": len(eq), "fo": len(fo)}


def _now():
    return dt.datetime.now(IST).isoformat(timespec="seconds")


def update(days: int = 400, progress=None, stop_after_errors: int = 3) -> dict:
    """Download every missing date of the last `days` trading sessions (newest first)."""
    c = db.conn()
    client = nse.Client()
    rep = {"started": _now(), "fetched": [], "errors": [], "lists": {}}
    try:
        rep["lists"] = refresh_lists(c, client)
        keep = {r["symbol"] for r in db.meta_get(c, "n500", [])}
        done = {r["d"]: r["status"] for r in c.execute("SELECT d, status FROM days")}
        dates = [d for d in weekdays_back(today_ist(), days) if done.get(d.isoformat()) not in ("OK", "HOLIDAY", "PARTIAL")]
        errs = 0
        for i, d in enumerate(dates):
            if progress:
                progress(f"downloading {d} ({i + 1}/{len(dates)})")
            try:
                r = fetch_day(c, client, d, keep)
                rep["fetched"].append(r)
                errs = 0
            except nse.NseError as e:
                errs += 1
                rep["errors"].append(f"{d}: {e}")
                if errs >= stop_after_errors:
                    rep["errors"].append("NSE not reachable from this server: stopped (no data invented)")
                    break
            except Exception as e:  # parse error on one day: log and continue
                rep["errors"].append(f"{d}: {type(e).__name__}: {e}")
        trading = [r["d"] for r in c.execute("SELECT d FROM days WHERE status IN ('OK','PARTIAL') ORDER BY d DESC")]
        rep["trading_days_stored"] = len(trading)
        rep["latest"] = trading[0] if trading else None
        rep["requests"] = client.requests
        rep["finished"] = _now()
        db.meta_set(c, "last_update", rep)
        return rep
    finally:
        client.close()
        c.close()


def counts(limit: int = 15) -> dict:
    c = db.conn()
    try:
        rows = [dict(r) for r in c.execute("SELECT d, status, equity_rows, fo_rows, note FROM days ORDER BY d DESC LIMIT ?", (limit,))]
        tot = c.execute("SELECT COUNT(*) n, MIN(d) a, MAX(d) b FROM days WHERE status IN ('OK','PARTIAL')").fetchone()
        hol = c.execute("SELECT COUNT(*) FROM days WHERE status='HOLIDAY'").fetchone()[0]
        return {"trading_days": tot["n"], "from": tot["a"], "to": tot["b"], "holidays_skipped": hol,
                "equity_rows": c.execute("SELECT COUNT(*) FROM equity").fetchone()[0],
                "fo_rows": c.execute("SELECT COUNT(*) FROM fo").fetchone()[0],
                "nifty_days": c.execute("SELECT COUNT(*) FROM idx").fetchone()[0],
                "n500": len(db.meta_get(c, "n500", [])), "ban": db.meta_get(c, "ban"),
                "recent": rows, "last_update": db.meta_get(c, "last_update")}
    finally:
        c.close()
