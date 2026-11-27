"""Replay the Sensex brain on Kite 1-minute history (same code as live, no look-ahead).

Limits (reported in the result): results are in Sensex points / R on the underlying - option P&L
differs (premium, theta, IV). Option-OI levels and VIX are not available historically, so they are
not used in the backtest.
"""
from __future__ import annotations

import datetime as dt
import json
from collections import defaultdict
from pathlib import Path

from .brain import Brain

IST = dt.timezone(dt.timedelta(hours=5, minutes=30))


def fetch_minutes(kite, token: int, days: int, cache: Path, progress=None) -> dict[str, list[dict]]:
    """{date: [{"t","o","h","l","c"}...]} for the last `days` trading days (+ warm-up), cached per chunk."""
    cache.mkdir(parents=True, exist_ok=True)
    end = dt.datetime.now(IST).date() - dt.timedelta(days=1)
    start = end - dt.timedelta(days=int(days * 1.5) + 20)
    out: dict[str, list] = defaultdict(list)
    d = start
    while d <= end:
        e = min(d + dt.timedelta(days=55), end)
        f = cache / f"{token}_{d}_{e}.json"
        if f.exists() and e < end:
            rows = json.loads(f.read_text())
        else:
            if progress:
                progress(f"downloading {token} {d}..{e}")
            rows = [{"d": c["date"].strftime("%Y-%m-%d"), "t": c["date"].strftime("%H:%M"), "o": c["open"],
                     "h": c["high"], "l": c["low"], "c": c["close"]}
                    for c in kite.historical_data(token, d, e + dt.timedelta(days=1), "minute")]
            f.write_text(json.dumps(rows))
        for r in rows:
            out[r["d"]].append({k: r[k] for k in ("t", "o", "h", "l", "c")})
        d = e + dt.timedelta(days=1)
    return {k: sorted(v, key=lambda b: b["t"]) for k, v in sorted(out.items()) if len(v) >= 300}


def five_min_closes(bars):
    return [b["c"] for b in bars if int(b["t"][3:]) % 5 == 4]


def run(sensex: dict, nifty: dict | None, days: int, cfg: dict | None = None, keep_all: bool = False) -> dict:
    dates = sorted(sensex)
    if len(dates) < 25:
        return {"ok": False, "error": f"only {len(dates)} days of Sensex minute data"}
    test = dates[-days:] if len(dates) > days + 5 else dates[5:]
    trades, day_rows = [], []
    for d in test:
        i = dates.index(d)
        prev = sensex[dates[i - 1]]
        levels = {"pdh": max(b["h"] for b in prev), "pdl": min(b["l"] for b in prev), "pdc": prev[-1]["c"]}
        seed = []
        for pd_ in dates[max(0, i - 5):i]:
            seed += five_min_closes(sensex[pd_])
        br = Brain(levels, seed, cfg)
        nb = {b["t"]: b["c"] for b in (nifty or {}).get(d, [])}
        nprev = (nifty or {}).get(dates[i - 1])
        npc = nprev[-1]["c"] if nprev else None
        for b in sensex[d]:
            if b["t"] < "09:15" or b["t"] > "15:29":
                continue
            nc = nb.get(b["t"])
            br.on_bar(b, nifty_chg=((nc - npc) / npc * 100) if (nc and npc) else None)
        for tr in br.trades:
            if "exit" not in tr:   # still open at data end: close at last price
                last = sensex[d][-1]["c"]
                tr["r"] = (last - tr["entry"]) / tr["risk"] * (1 if tr["side"] == "BULL" else -1)
                tr["exit"] = "EOD"
            trades.append({"day": d, "side": tr["side"], "time": tr["t"], "entry": round(tr["entry"], 1),
                           "sl": round(tr["sl"], 1), "r": round(tr["r"], 2), "exit": tr["exit"],
                           "risk_pts": round(tr["risk"], 1)})
        day_rows.append({"day": d, "trades": len(br.trades), "r": round(sum(t["r"] for t in br.trades), 2)})
    if keep_all:
        return {"ok": True, "all_days": test, "all_trades": trades}
    return {"ok": True, "days": len(test), "from": test[0], "to": test[-1], "stats": stats(trades),
            "by_month": by_month(trades), "by_side": {s: stats([t for t in trades if t["side"] == s]) for s in ("BULL", "BEAR")},
            "trades": trades[-60:], "no_trade_days": sum(1 for r in day_rows if r["trades"] == 0),
            "notes": ["Result Sensex points lo R ga (1R = SL distance). Option P&L veru untundi (premium, theta, IV).",
                      "Option OI levels, VIX history ledu kabatti backtest lo vaadaledu (live lo vaadutundi).",
                      "Entry = trigger 5-min candle close; SL/target 1-min high/low tho check. Same minute lo rendu aithe SL first.",
                      "Gata performance future guarantee kaadu."]}


def stats(trades):
    if not trades:
        return {"trades": 0}
    r = [t["r"] for t in trades]
    eq, peak, dd = 0.0, 0.0, 0.0
    for x in r:
        eq += x
        peak = max(peak, eq)
        dd = min(dd, eq - peak)
    wins = [x for x in r if x > 0]
    losses = [x for x in r if x <= 0]
    return {"trades": len(r), "win_pct": round(len(wins) / len(r) * 100, 1), "avg_r": round(sum(r) / len(r), 3),
            "total_r": round(sum(r), 2), "avg_win_r": round(sum(wins) / len(wins), 2) if wins else 0,
            "avg_loss_r": round(sum(losses) / len(losses), 2) if losses else 0,
            "profit_factor": round(sum(wins) / -sum(losses), 2) if losses and sum(losses) < 0 else None,
            "max_dd_r": round(dd, 2),
            "exits": {k: sum(1 for t in trades if t["exit"] == k) for k in sorted({t["exit"] for t in trades})}}


def by_month(trades):
    m = defaultdict(list)
    for t in trades:
        m[t["day"][:7]].append(t)
    return [{"month": k, **{x: stats(v)[x] for x in ("trades", "win_pct", "total_r")}} for k, v in sorted(m.items())]


# ------------------------------------------------------------------ strategy lab
import itertools  # noqa: E402

COST_R = 0.07   # options charges + spread per round trip, approx in R (conservative)


def _net(trades):
    return [dict(t, r=round(t["r"] - COST_R, 3)) for t in trades]


def lab(sensex: dict, nifty: dict | None, days: int = 120, test_days: int = 40, progress=None) -> dict:
    """Try rule combinations. Pick on the first part (train), then check on the last `test_days` (test)."""
    grid = {"t1_r": [0.75, 1.0, 1.5, 2.0], "t1_exit_frac": [0.5, 1.0], "sl_mult": [0.7, 1.0, 1.5],
            "max_trades": [1, 2], "time_stop_min": [0, 30], "require_pd_break": [False, True]}
    keys = list(grid)
    combos = list(itertools.product(*grid.values()))
    rows = []
    split = None
    for i, vals in enumerate(combos):
        cfg = dict(zip(keys, vals))
        cfg["t2_r"] = cfg["t1_r"] + 1.0
        if progress and i % 8 == 0:
            progress(f"testing {i + 1}/{len(combos)}")
        res = run(sensex, nifty, days, cfg, keep_all=True)
        if not res.get("ok"):
            return res
        all_days = res["all_days"]
        split = all_days[-test_days]
        tr = _net(res["all_trades"])
        a = stats([t for t in tr if t["day"] < split])
        b = stats([t for t in tr if t["day"] >= split])
        rows.append({"rules": cfg, "train": a, "test": b})

    def good(s, n_min):
        return s.get("trades", 0) >= n_min and s.get("win_pct", 0) >= 60 and s.get("avg_r", -1) > 0.05

    picked = sorted([r for r in rows if good(r["train"], 30)], key=lambda r: -r["train"]["avg_r"])[:10]
    for r in picked:
        r["passed_test"] = good(r["test"], 10) and r["test"]["avg_r"] > 0
    winner = next((r for r in picked if r["passed_test"]), None)
    hi_win = max(rows, key=lambda r: (r["train"].get("win_pct", 0), r["train"].get("avg_r", -9)))
    low_sl_hi_t = next(r for r in rows if r["rules"]["sl_mult"] == 0.7 and r["rules"]["t1_r"] == 2.0
                       and r["rules"]["t1_exit_frac"] == 1.0 and r["rules"]["max_trades"] == 2
                       and r["rules"]["time_stop_min"] == 0 and not r["rules"]["require_pd_break"])
    base = next(r for r in rows if r["rules"]["sl_mult"] == 1.0 and r["rules"]["t1_r"] == 1.5
                and r["rules"]["t1_exit_frac"] == 0.5 and r["rules"]["max_trades"] == 2
                and r["rules"]["time_stop_min"] == 30 and not r["rules"]["require_pd_break"])
    if winner:
        verdict = ("FOUND", f"Train lo select chesina rule test period (last {test_days} days) lo kuda pass ayyindi: "
                            f"win {winner['test']['win_pct']}%, avg {winner['test']['avg_r']:+.2f}R (charges taruvata). "
                            "Paper lo 2 weeks chusi, appude real money.")
    else:
        verdict = ("NOT FOUND", "Win rate 60%+ and profit rendu kalipi test period lo pass ayye rule dorakaledu. "
                                "Ee strategy family lo nijamaina edge ledu - real money pettakandi.")
    return {"ok": True, "combos": len(combos), "days": days, "test_from": split, "cost_r_per_trade": COST_R,
            "verdict": {"label": verdict[0], "text": verdict[1]}, "winner": winner, "top": picked,
            "highest_win_rate": hi_win, "low_sl_high_target": low_sl_hi_t, "current_rules": base,
            "criteria": "Train: >=30 trades, win >=60%, avg > +0.05R after costs. Test: >=10 trades, win >=60%, avg > 0."}
