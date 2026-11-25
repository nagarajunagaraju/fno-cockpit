"""CLI / subprocess entry: python -m app.smf.run {update|daily|backtest|all}

Heavy work runs in a separate process so pandas memory is released after each job (1 GB VPS).
Progress and results are written to data/smf/job_<name>.json.
"""
from __future__ import annotations

import datetime as dt
import json
import sys
import traceback

from . import backtest, collect, daily, db, model

IST = dt.timezone(dt.timedelta(hours=5, minutes=30))


def _write(name, **kv):
    f = db.OUT / f"job_{name}.json"
    cur = json.loads(f.read_text()) if f.exists() else {}
    cur.update(kv)
    f.write_text(json.dumps(cur, default=str))


def main(job: str):
    now = lambda: dt.datetime.now(IST).isoformat(timespec="seconds")  # noqa: E731
    _write(job, job=job, running=True, started=now(), finished=None, error=None, stage="starting")
    prog = lambda s: _write(job, stage=s)  # noqa: E731
    try:
        cfg = model.load_cfg()
        out = {}
        if job in ("update", "daily", "all"):
            out["update"] = collect.update(cfg["history_days"], prog)
        if job in ("daily", "all"):
            prog("building evening lists")
            rep = daily.run_daily(prog)
            out["daily"] = {"ok": rep.get("ok"), "date": rep.get("date"), "error": rep.get("error"),
                            "alerts": rep.get("alerts"), "telegram": rep.get("telegram"),
                            "counts": {k: len(v) for k, v in (rep.get("lists") or {}).items()}}
        if job in ("backtest", "all"):
            res = backtest.run(cfg, progress=prog)
            c = db.conn()
            try:
                db.meta_set(c, "backtest", {**res, "created_at": now()})
            finally:
                c.close()
            out["backtest"] = {"ok": res.get("ok"), "error": res.get("error"),
                               "verdicts": {k: v["verdict"].get("label") for k, v in (res.get("lists") or {}).items()}}
        _write(job, running=False, finished=now(), stage="done", result=out)
    except Exception as e:
        _write(job, running=False, finished=now(), stage="failed", error=f"{type(e).__name__}: {e}",
               trace=traceback.format_exc()[-2000:])
        raise


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "daily")
