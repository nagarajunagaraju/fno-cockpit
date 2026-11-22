"""Historical backtest that replays 1-minute data through the SAME `evaluate()` used live.

No look-ahead: at minute m the engine sees only bars 0..m of the day, previous days' daily
candles, index bars up to m and futures OI up to m. Entries fill at the NEXT bar's open
(with slippage, or a missed fill if it gaps away). Stops are checked before targets inside a bar.

Honest limits (always printed in the report):
* Kite does not serve data for expired option contracts, so the option-chain layer cannot be
  replayed. It is excluded and the minimum score is scaled to the remaining points.
* Results are in underlying R-multiples / % (not option premium P&L). Real option P&L is
  recorded going forward by paper trading.
* The universe is today's F&O list (survivorship bias).
"""
from __future__ import annotations

import copy
import datetime as dt
import math

from .candles import build_rvol_baseline
from .config import IST, hm, minute_index
from .layers import evaluate
from .planner import underlying_levels
from .signals import trade_stats


def _bt_cfg(cfg):
    c = copy.deepcopy(cfg)
    total = sum(c["weights"].values())
    opt = c["weights"]["option_chain"]
    c["weights"]["option_chain"] = 0
    c["require_option_chain"] = False
    c["min_score"] = int(round(cfg["min_score"] * (total - opt) / total))
    c["watch_min_score"] = int(round(cfg["watch_min_score"] * (total - opt) / total))
    return c


def run_backtest(ds: dict, live_cfg: dict, days: int = 30, cost_pct: float = 0.05, slip_pct: float = 0.02,
                 gap_miss_pct: float = 0.3, progress: dict | None = None) -> dict:
    cfg = _bt_cfg(live_cfg)
    lookback = cfg["rvol_lookback_sessions"]
    all_days = sorted({d for s in ds["symbols"].values() for d in s["minute"]})
    test_days = [d for i, d in enumerate(all_days) if i >= lookback][-days:]
    report = {"source": ds.get("source"), "params": {"days": days, "cost_pct_round_trip": cost_pct,
              "slippage_pct_per_side": slip_pct, "gap_miss_pct": gap_miss_pct,
              "min_score_used": cfg["min_score"], "max_score_used": int(sum(cfg["weights"].values()))},
              "tested_days": test_days, "symbols": len(ds["symbols"]), "notes": list(ds.get("notes", []))}
    report["notes"] += [
        "Option-chain layer EXCLUDED: historical option OI/IV for expired contracts is not available from Kite. "
        f"Minimum score scaled to {cfg['min_score']}/{report['params']['max_score_used']}.",
        "P&L is measured on the underlying (R-multiples and %), not on option premium. "
        "Option P&L with real premiums, spreads and charges is tracked by paper trading going forward.",
        "Entries fill at the next 1-min bar open; a stop and a target touched in the same bar count as a stop.",
    ]
    trades = []
    entry_end = minute_index(hm(cfg["last_entry_time"]), cfg)
    sq_m = minute_index(hm(cfg["square_off_time"]), cfg)
    lock_r = cfg["max_daily_loss_pct"] / cfg["risk_per_trade_pct"]
    if progress is not None:
        progress.update(stage="replaying", total=len(test_days), done=0)

    for D in test_days:
        di = all_days.index(D)
        prev_day = all_days[di - 1]
        # ---- index state per minute ----
        idx = {}
        for name, v in ds["indices"].items():
            bars = {b["m"]: b for b in v["minute"].get(D, [])}
            pb = v["minute"].get(prev_day) or []
            idx[name] = (bars, pb[-1]["c"] if pb else None)
        # ---- per-symbol prep (only data before D for baselines) ----
        prep = {}
        for sym, s in ds["symbols"].items():
            bars = sorted(s["minute"].get(D, []), key=lambda b: b["m"])
            if len(bars) < 30:
                continue
            daily_prior = [c for c in s["daily"] if c["date"] < D]
            if not daily_prior:
                continue
            past = [s["minute"][d] for d in all_days[max(0, di - lookback):di] if s["minute"].get(d)]
            fut_today = {b["m"]: b for b in (s.get("fut_minute") or {}).get(D, [])}
            fprev = (s.get("fut_minute") or {}).get(prev_day) or []
            maxh, minl, h, l = [], [], -1e18, 1e18
            for b in bars:
                h, l = max(h, b["h"]), min(l, b["l"])
                maxh.append(h); minl.append(l)
            prep[sym] = {"bars": bars, "prev": daily_prior[-1], "baseline": build_rvol_baseline(past),
                         "fut": fut_today, "fprev": fprev[-1] if fprev else None, "maxh": maxh, "minl": minl,
                         "lot": s["lot_size"], "sector": s["sector"], "count": 0, "last": None}
        open_pos, pending, day_r = [], [], 0.0
        base = dt.datetime.combine(dt.date.fromisoformat(D), dt.time(9, 15), IST)
        for m in range(375):
            # ---- fills for pending entries at this bar's open ----
            for p in list(pending):
                P = prep[p["symbol"]]
                bar = P["bars"][m] if m < len(P["bars"]) else None
                pending.remove(p)
                if bar is None:
                    continue
                gap = (bar["o"] - p["ref"]) / p["ref"] * 100 * p["up"]
                if gap > gap_miss_pct:
                    trades.append({**p, "status": "MISSED_FILL", "pnl_r": 0.0, "pnl_pct": 0.0})
                    continue
                p["entry"] = bar["o"] * (1 + p["up"] * slip_pct / 100)
                p["risk"] = abs(p["entry"] - p["sl"])
                if p["risk"] <= 0 or p["up"] * (p["entry"] - p["sl"]) <= 0:
                    trades.append({**p, "status": "MISSED_FILL", "pnl_r": 0.0, "pnl_pct": 0.0})
                    continue
                p.update(fill_m=m, remaining=1.0, exits=[], sl_cur=p["sl"], t1_done=False)
                open_pos.append(p)
            # ---- manage open positions on bar m ----
            for p in list(open_pos):
                P = prep[p["symbol"]]
                if m >= len(P["bars"]):
                    continue
                b = P["bars"][m]
                up = p["up"]
                adverse = b["l"] if up > 0 else b["h"]
                favour = b["h"] if up > 0 else b["l"]
                done = False
                if up * (adverse - p["sl_cur"]) <= 0:
                    px = b["o"] if up * (b["o"] - p["sl_cur"]) <= 0 else p["sl_cur"]
                    p["exits"].append((p["remaining"], px * (1 - up * slip_pct / 100), "stop"))
                    done = True
                else:
                    if not p["t1_done"] and up * (favour - p["t1"]) >= 0:
                        p["exits"].append((0.5, p["t1"], "T1")); p["remaining"] = 0.5
                        p["t1_done"] = True; p["sl_cur"] = p["entry"]
                    if p["t1_done"] and up * (favour - p["t2"]) >= 0:
                        p["exits"].append((p["remaining"], p["t2"], "T2")); done = True
                    elif m >= sq_m:
                        p["exits"].append((p["remaining"], b["c"], "time")); done = True
                if done:
                    open_pos.remove(p)
                    gross = sum(f * (x - p["entry"]) * up for f, x, _ in p["exits"])
                    p["pnl_pct"] = round(gross / p["entry"] * 100 - cost_pct, 4)
                    p["pnl_r"] = round((gross - cost_pct / 100 * p["entry"]) / p["risk"], 3)
                    p["status"] = "CLOSED"
                    p["exit_reason"] = p["exits"][-1][2]
                    day_r += p["pnl_r"]
                    P["last"] = (m, p["level"], p["up"])
                    trades.append({k: v for k, v in p.items() if k != "exits"})
            # ---- new signals at the close of bar m ----
            if m < cfg["opening_range_minutes"] or m + 1 > entry_end or day_r <= -lock_r:
                continue
            for sym, P in prep.items():
                bars = P["bars"]
                if m >= len(bars) or m < 1:
                    continue
                c, pc = bars[m]["c"], bars[m - 1]["c"]
                bull = c > P["maxh"][m - 1] and pc <= P["maxh"][m - 1]
                bear = c < P["minl"][m - 1] and pc >= P["minl"][m - 1]
                if not (bull or bear):
                    continue
                if any(p["symbol"] == sym for p in open_pos + pending):
                    continue
                if len(open_pos) + len(pending) >= cfg["max_open_positions"]:
                    continue
                now = base + dt.timedelta(minutes=m + 1)
                stock = {"symbol": sym, "sector": P["sector"], "lot_size": P["lot"], "bars": bars[:m + 1],
                         "prev_close": P["prev"]["close"], "day_open": bars[0]["o"], "pdh": P["prev"]["high"],
                         "pdl": P["prev"]["low"], "rvol_baseline": P["baseline"]}
                fb = P["fut"].get(m)
                if fb and P["fprev"] and fb.get("oi") and P["fprev"].get("oi"):
                    stock["fut"] = {"ltp": fb["c"], "prev_close": P["fprev"]["c"], "oi": fb["oi"], "prev_oi": P["fprev"]["oi"]}
                mk = {"sectors": {}}
                for name, (ib, ipc) in idx.items():
                    if m in ib and ipc:
                        mk["sectors"][name] = {"ltp": ib[m]["c"], "prev_close": ipc}
                mk["nifty"] = mk["sectors"].get("NIFTY 50")
                mk["banknifty"] = mk["sectors"].get("NIFTY BANK")
                ev = evaluate(stock, mk, cfg, now, {"available": False, "reason": "no historical option data"}, live=False)
                if ev["status"] != "TRIGGER":
                    continue
                up = 1 if ev["direction"] == "BULL" else -1
                if P["count"] >= cfg["max_signals_per_symbol_day"]:
                    continue
                if P["last"] and P["last"][2] == up:
                    if m - P["last"][0] < cfg["resignal_cooldown_min"] or up * (ev["trigger"]["level"] - P["last"][1]) <= 0:
                        continue
                u_sl, u_t1, u_t2, dist, _ = underlying_levels(ev, cfg)
                P["count"] += 1
                pending.append({"day": D, "symbol": sym, "up": up, "side": "CE" if up > 0 else "PE",
                                "score": ev["score"], "signal_time": now.strftime("%H:%M"), "hour": now.strftime("%H"),
                                "ref": ev["metrics"]["price"], "sl": u_sl, "t1": u_t1, "t2": u_t2,
                                "level": ev["trigger"]["level"]})
        if progress is not None:
            progress["done"] += 1

    closed = [t for t in trades if t["status"] == "CLOSED"]
    st = trade_stats([{"pnl": t["pnl_r"], "symbol": t["symbol"], "hour": t["hour"], "score": t["score"],
                       "side": t["side"]} for t in closed])
    st["unit"] = "R-multiples (1R = planned underlying risk)"
    st["missed_fills"] = sum(1 for t in trades if t["status"] == "MISSED_FILL")
    st["sum_return_pct"] = round(sum(t["pnl_pct"] for t in closed), 2)
    report["stats"] = st
    report["trades"] = [{k: (round(v, 2) if isinstance(v, float) else v) for k, v in t.items()
                         if k in ("day", "symbol", "side", "score", "signal_time", "entry", "sl", "t1", "t2",
                                  "exit_reason", "pnl_r", "pnl_pct", "status")} for t in trades][-500:]
    insufficient = []
    if len(test_days) < 20:
        insufficient.append(f"only {len(test_days)} test days (need 20+)")
    if len(closed) < 30:
        insufficient.append(f"only {len(closed)} closed trades (need 30+)")
    report["sufficiency"] = "INSUFFICIENT: " + "; ".join(insufficient) if insufficient else "OK"
    return report
