"""Backtest for the v3 pullback strategy, using the SAME evaluate_pullback() as live.

Memory-light: one symbol is replayed at a time (a 1 GB server can run 6 months x 40 stocks).
Portfolio rules (max 3 alerts/day, max 2 open, sector cap, B-grade backup, daily loss lock) are
applied afterwards in time order across all symbols.

Not testable here, reported in the notes: option premiums/IV/Greeks (Kite has no expired
option data), order-book depth, option-chain OI. Results are underlying R-multiples.
"""
from __future__ import annotations

import copy
import datetime as dt

from .candles import build_rvol_baseline
from .config import IST, hm, minute_index
from .pullback import evaluate_pullback, grade as grade_of
from .regime import detect as detect_regime
from .sectors import INDEX_NIFTY, risk_group, sector_of
from .signals import trade_stats

PRE_TESTABLE = 60    # pre-contract points that exist in history (70 minus depth 5 and option OI 5)
RR_POINTS = 10       # underlying reward:risk points used in place of premium R:R
BT_MAX = PRE_TESTABLE + RR_POINTS


def _bt_cfg(cfg):
    c = copy.deepcopy(cfg)
    c["bt_fut_optional"] = True
    return c


def _ranks_by_day(dailies: dict[str, list[dict]], days: list[str]) -> dict[str, dict[str, int]]:
    out = {}
    for d in days:
        tv = {}
        for sym, rows in dailies.items():
            prior = [r for r in rows if r["date"] < d][-20:]
            vals = [r["close"] * r.get("volume", 0) for r in prior if r.get("volume")]
            if vals:
                tv[sym] = sum(vals) / len(vals)
        out[d] = {s: i + 1 for i, s in enumerate(sorted(tv, key=lambda k: -tv[k]))}
    return out


def _simulate_trade(bars, start_m, side, sl, t1, t2, entry_ref, cfg, slip_pct, gap_miss_pct):
    """Fill at the next bar's open; stop checked before targets inside a bar."""
    up = 1 if side == "BULL" else -1
    by_m = {b["m"]: b for b in bars}
    fb = by_m.get(start_m)
    if fb is None:
        return None
    if up * (fb["o"] - entry_ref) / entry_ref * 100 > gap_miss_pct:
        return {"status": "MISSED_FILL"}
    entry = fb["o"] * (1 + up * slip_pct / 100)
    risk = up * (entry - sl)
    if risk <= 0:
        return {"status": "MISSED_FILL"}
    sq = minute_index(hm(cfg["square_off_time"]), cfg)
    remaining, sl_cur, t1_done, exits, best = 1.0, sl, False, [], 0.0
    exit_m = None
    for b in bars:
        if b["m"] < start_m:
            continue
        adverse = b["l"] if up > 0 else b["h"]
        favour = b["h"] if up > 0 else b["l"]
        best = max(best, up * (favour - entry))
        if up * (adverse - sl_cur) <= 0:
            px = b["o"] if up * (b["o"] - sl_cur) <= 0 else sl_cur
            exits.append((remaining, px * (1 - up * slip_pct / 100), "stop" if not t1_done else "stop@cost"))
            exit_m = b["m"]
            break
        if not t1_done and up * (favour - t1) >= 0:
            exits.append((0.5, t1, "T1"))
            remaining, t1_done, sl_cur = 0.5, True, entry
        if t1_done and up * (favour - t2) >= 0:
            exits.append((remaining, t2, "T2"))
            exit_m = b["m"]
            break
        if (not t1_done and b["m"] - start_m >= cfg["time_stop_minutes"]
                and best < cfg["time_stop_r"] * risk):
            exits.append((remaining, b["c"], "time stop"))
            exit_m = b["m"]
            break
        if b["m"] >= sq:
            exits.append((remaining, b["c"], "square-off"))
            exit_m = b["m"]
            break
    if not exits:
        exits.append((remaining, bars[-1]["c"], "end of data"))
        exit_m = bars[-1]["m"]
    gross = sum(f * (x - entry) * up for f, x, _ in exits)
    return {"status": "CLOSED", "entry": entry, "risk": risk, "exit_m": exit_m, "exit_reason": exits[-1][2],
            "gross": gross}


def run(symbols: list[str], load_symbol, indices: dict, live_cfg: dict, days: int = 120,
        cost_pct: float = 0.20, slip_pct: float = 0.03, gap_miss_pct: float = 0.3,
        progress: dict | None = None, source: str = "KITE", notes: list | None = None) -> dict:
    """load_symbol(sym) -> {"lot_size", "daily": [...], "minute": {day: bars}, "fut_minute": {day: bars}}"""
    cfg = _bt_cfg(live_cfg)
    lookback = cfg["rvol_lookback_sessions"]
    nifty = (indices.get(INDEX_NIFTY) or {}).get("minute", {})
    all_days = sorted(nifty) or []
    if not all_days:   # derive from first symbol
        first = load_symbol(symbols[0])
        all_days = sorted(first["minute"])
    test_days = all_days[lookback:][-days:]
    dailies = {}
    for sym in symbols:
        dailies[sym] = load_symbol(sym, daily_only=True)["daily"]
    ranks = _ranks_by_day(dailies, test_days)
    first_entry = minute_index(hm(cfg["pb_first_entry"]), cfg)
    last_entry = minute_index(hm(cfg["last_entry_time"]), cfg)
    if progress is not None:
        progress.update(stage="replaying", total=len(symbols), done=0)

    # ---- index state per day ----
    def idx_state(name, d):
        v = (indices.get(name) or {}).get("minute", {})
        bars = v.get(d) or []
        di = all_days.index(d)
        prev = v.get(all_days[di - 1]) if di > 0 else None
        return bars, (prev[-1]["c"] if prev else None)

    cands = []
    for si, sym in enumerate(symbols):
        data = load_symbol(sym)
        mins, futm, daily = data["minute"], data.get("fut_minute") or {}, data["daily"]
        sec = sector_of(sym)
        for d in test_days:
            bars = sorted(mins.get(d) or [], key=lambda b: b["m"])
            if len(bars) < 60:
                continue
            di = all_days.index(d)
            prior_daily = [r for r in daily if r["date"] < d]
            if not prior_daily:
                continue
            prev = prior_daily[-1]
            past = [mins[x] for x in all_days[max(0, di - lookback):di] if mins.get(x)]
            base = build_rvol_baseline(past)
            fb = {b["m"]: b for b in futm.get(d, [])}
            fprev = futm.get(all_days[di - 1]) if di > 0 else None
            fprev_last = fprev[-1] if fprev else None
            nb, npc = idx_state(INDEX_NIFTY, d)
            nmap = {b["m"]: b for b in nb}
            orb = [b for b in nb if b["m"] < cfg["opening_range_minutes"]]
            sb, spc = idx_state(sec, d)
            smap = {b["m"]: b for b in sb}
            for i, b in enumerate(bars):
                m = b["m"]
                if m < first_entry or m > last_entry or m % 5 != 4:
                    continue
                chg = (b["c"] - prev["close"]) / prev["close"] * 100
                if not cfg["pb_chg_min"] <= abs(chg) <= cfg["pb_chg_max"]:
                    continue
                if m not in nmap or not npc:
                    continue
                nifty_s = {"ltp": nmap[m]["c"], "prev_close": npc, "day_open": nb[0]["o"],
                           "or_high": max(x["h"] for x in orb) if orb else None,
                           "or_low": min(x["l"] for x in orb) if orb else None}
                market = {"nifty": nifty_s, "sectors": {}, "breadth": {}, "vix": None}
                if m in smap and spc:
                    market["sectors"][sec] = {"ltp": smap[m]["c"], "prev_close": spc}
                regime = detect_regime(market, cfg)
                stock = {"symbol": sym, "sector": sec, "lot_size": data.get("lot_size"), "bars": bars[:i + 1],
                         "prev_close": prev["close"], "day_open": bars[0]["o"], "rvol_baseline": base,
                         "turnover_rank": ranks.get(d, {}).get(sym)}
                f = fb.get(m)
                if f and f.get("oi") and fprev_last and fprev_last.get("oi"):
                    stock["fut"] = {"ltp": f["c"], "prev_close": fprev_last["c"], "oi": f["oi"], "prev_oi": fprev_last["oi"]}
                    stock["fut_hist"] = [(x["m"], x["oi"], x["c"]) for x in futm[d] if m - 15 <= x["m"] <= m and x.get("oi")]
                now = dt.datetime.combine(dt.date.fromisoformat(d), dt.time(9, 15), IST) + dt.timedelta(minutes=m + 1)
                ev = evaluate_pullback(stock, market, regime, cfg, now, None, live=False)
                if ev["status"] != "TRIGGER":
                    continue
                lv = ev["levels"]
                rr_u = abs(lv["u_t1"] - lv["entry"]) / lv["risk"] if lv["risk"] else 0
                rr_pts = 10 if rr_u >= 2 else (6 if rr_u >= 1.5 else 0)
                score = round((ev["pre_score"] + rr_pts) * 100 / BT_MAX, 1)
                g = grade_of(score, regime, cfg)
                if g is None:
                    break
                res = _simulate_trade(bars, m + 1, ev["direction"], lv["u_sl"], lv["u_t1"], lv["u_t2"],
                                      lv["entry"], cfg, slip_pct, gap_miss_pct)
                if res:
                    cands.append({"day": d, "symbol": sym, "group": risk_group(sym), "m": m, "time": b["t"],
                                  "hour": b["t"][:2], "side": "CE" if ev["direction"] == "BULL" else "PE",
                                  "grade": g, "score": score, "regime": regime.get("regime"),
                                  "fut_data": "fut" in stock, **res})
                break    # one signal per stock per day
        if progress is not None:
            progress["done"] = si + 1

    # ---- portfolio rules in time order ----
    accepted = []
    lock_r = cfg["max_daily_loss_pct"] / cfg["risk_per_trade_pct"]
    b_min = minute_index(hm(cfg["b_grade_time"]), cfg)
    for d in test_days:
        day_c = sorted([c for c in cands if c["day"] == d], key=lambda c: (c["m"], -c["score"]))
        taken, day_r = [], 0.0
        for c in day_c:
            if len(taken) >= cfg["max_alerts_per_day"] or day_r <= -lock_r:
                break
            if c["grade"] == "B" and (c["m"] < b_min or taken):
                continue
            open_now = [t for t in taken if t.get("exit_m", 999) > c["m"]]
            if len(open_now) >= cfg["max_open_positions"]:
                continue
            if sum(1 for t in open_now if t["group"] == c["group"]) >= cfg["max_per_sector"]:
                continue
            if c["status"] == "CLOSED":
                c["pnl_r"] = round((c["gross"] - cost_pct / 100 * c["entry"]) / c["risk"], 3)
                day_r += c["pnl_r"]
            taken.append(c)
        accepted += taken

    closed = [c for c in accepted if c["status"] == "CLOSED"]
    stats = trade_stats([{"pnl": c["pnl_r"], "symbol": c["symbol"], "hour": c["hour"], "score": c["grade"],
                          "side": c["side"]} for c in closed])
    stats["unit"] = "R-multiples (1R = planned underlying risk); by_score = by grade"
    stats["missed_fills"] = sum(1 for c in accepted if c["status"] == "MISSED_FILL")
    stats["days_with_trade"] = len({c["day"] for c in closed})
    stats["avg_trades_per_day"] = round(len(closed) / len(test_days), 2) if test_days else 0
    by_regime = {}
    for c in closed:
        g = by_regime.setdefault(c["regime"], {"trades": 0, "pnl": 0.0, "wins": 0})
        g["trades"] += 1; g["pnl"] = round(g["pnl"] + c["pnl_r"], 2); g["wins"] += c["pnl_r"] > 0
    stats["by_regime"] = {k: {**v, "win_rate": round(v["wins"] / v["trades"] * 100, 1)} for k, v in by_regime.items()}
    insufficient = []
    if len(test_days) < 60:
        insufficient.append(f"only {len(test_days)} test days (need 60+)")
    if len(closed) < 30:
        insufficient.append(f"only {len(closed)} trades (need 30+)")
    fut_cov = sum(1 for c in closed if c["fut_data"])
    return {
        "strategy": "pullback v3", "source": source, "symbols": len(symbols), "tested_days": test_days,
        "params": {"days": days, "cost_pct_round_trip": cost_pct, "slippage_pct_per_side": slip_pct,
                   "score_scale": f"testable points scaled to 100 (max {BT_MAX} raw)"},
        "stats": stats, "candidates_before_portfolio_rules": len(cands),
        "sufficiency": "INSUFFICIENT: " + "; ".join(insufficient) if insufficient else "OK",
        "notes": (notes or []) + [
            "Option premium, IV, Greeks, order-book depth and option-chain OI are NOT in the backtest "
            "(no historical data). Results are in underlying R-multiples; paper trading measures option P&L.",
            f"Futures OI history existed for {fut_cov} of {len(closed)} trades (Kite keeps minute OI only for the "
            "current futures contract); other trades were judged without the futures build-up rule.",
            "Universe = today's liquid F&O stocks (survivorship bias). Entry at next 1-min open; "
            "stop assumed before target inside a bar.",
        ],
        "trades": [{k: c.get(k) for k in ("day", "symbol", "time", "side", "grade", "score", "regime", "status",
                                         "exit_reason", "pnl_r")} for c in accepted][-400:],
    }
