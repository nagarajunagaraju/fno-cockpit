"""v3 strategy: "strong stock, first pullback" (3 stages).

Stage 1  strength : 1-3% move, RVOL >= 1.5x, futures build-up aligned, NIFTY regime allows the side,
                    liquid top-40 stock, not near circuit.
Stage 2  pullback : after the day's impulse extreme, price comes back to VWAP on lighter volume
                    and no 5-minute candle closes through VWAP.
Stage 3  trigger  : a completed 5-minute candle closes beyond the previous 5-minute candle's
                    high (bull) / low (bear), back on the trend side of VWAP, with volume.

Pure function of the data it is given (only completed candles): live scanning and the
backtest call the same code. Missing data is reported, never invented.
"""
from __future__ import annotations

import datetime as dt

from .candles import rvol as calc_rvol
from .config import IST, hm, minute_index
from .layers import _check, buildup, pct
from .sectors import sector_of

PRE_MAX = 70     # points available before the option contract is known


def five_min(bars: list[dict]) -> list[dict]:
    """Aggregate completed 1-min bars into completed 5-min bars, each with VWAP at its close."""
    if not bars:
        return []
    last_m = bars[-1]["m"]
    complete_upto = (last_m + 1) // 5          # groups < this are complete
    out, cur, pv, vv = [], None, 0.0, 0.0
    for b in bars:
        k = b["m"] // 5
        if k >= complete_upto:
            break
        pv += (b["h"] + b["l"] + b["c"]) / 3 * b["v"]
        vv += b["v"]
        if cur is None or cur["k"] != k:
            if cur is not None:
                out.append(cur)
            cur = {"k": k, "t": b["t"], "o": b["o"], "h": b["h"], "l": b["l"], "c": b["c"], "v": b["v"], "n": 1}
        else:
            cur["h"] = max(cur["h"], b["h"]); cur["l"] = min(cur["l"], b["l"])
            cur["c"] = b["c"]; cur["v"] += b["v"]; cur["n"] += 1
        cur["vwap"] = pv / vv if vv else None
    if cur is not None:
        out.append(cur)
    return out


def evaluate_pullback(stock: dict, market: dict, regime: dict, cfg: dict, now: dt.datetime,
                      options_info: dict | None = None, live: bool = True) -> dict:
    sym = stock["symbol"]
    bars = stock.get("bars") or []
    checks: list[dict] = []
    ev = {"symbol": sym, "sector": stock.get("sector") or sector_of(sym), "lot_size": stock.get("lot_size"),
          "strategy": "pullback", "eval_time": now.astimezone(IST).isoformat(timespec="seconds"),
          "status": "NO_SETUP", "stage": None, "direction": None, "score": 0, "pre_score": 0,
          "max_score": 100, "checks": checks, "trigger": None, "levels": None, "metrics": {},
          "reject_reasons": [], "missing": [], "regime": regime.get("regime")}
    if len(bars) < 15:
        ev["wait_reason"] = "Opening range forming"
        return ev

    px = bars[-1]["c"]
    chg = pct(px, stock.get("prev_close"))
    day_open = stock.get("day_open") or bars[0]["o"]
    if chg is None or abs(chg) < 0.2:
        return ev
    up = 1 if chg > 0 else -1
    ev["direction"] = "BULL" if up > 0 else "BEAR"
    side = "CE" if up > 0 else "PE"

    # ---------------- hard filters ----------------
    if live:
        age = max(0.0, now.timestamp() - stock["data_ts"]) if stock.get("data_ts") else None
        degraded = cfg["require_websocket"] and market.get("feed_degraded")
        ok = (age is not None and age <= cfg["stale_seconds"]) and not degraded
        _check(checks, "data_fresh", 0, "Live data (WebSocket) healthy", ok,
               "REST fallback (degraded)" if degraded else (f"{age:.0f}s old" if age is not None else "no data"),
               f"<= {cfg['stale_seconds']}s, WebSocket", mandatory=True)
    rank = stock.get("turnover_rank")
    _check(checks, "universe", 0, f"Top {cfg['universe_top_n']} liquid stock", None if rank is None else rank <= cfg["universe_top_n"],
           "rank unavailable" if rank is None else f"turnover rank {rank}", f"<= {cfg['universe_top_n']}", mandatory=rank is not None)
    a = abs(chg)
    _check(checks, "move_range", 1, "Stage 1: move in 1-3% range", cfg["pb_chg_min"] <= a <= cfg["pb_chg_max"],
           f"{chg:+.2f}%", f"{cfg['pb_chg_min']}% to {cfg['pb_chg_max']}%", mandatory=True)
    allows = regime.get("allows")
    _check(checks, "regime", 1, "NIFTY regime allows this side", allows == side,
           f"{regime.get('regime')} ({regime.get('note', '')})", f"{side} allowed", regime.get("points", 0) if allows == side else 0,
           10, mandatory=True)
    rv = calc_rvol(bars, stock.get("rvol_baseline"))
    if rv is not None:
        pts = 6 if rv >= 2 else (3 if rv >= cfg["pb_rvol_min"] else 0)
        _check(checks, "rvol", 1, "Stage 1: relative volume", rv >= cfg["pb_rvol_min"], f"{rv:.2f}x",
               f">= {cfg['pb_rvol_min']}x (full at 2x)", pts, 6, mandatory=True)
    else:
        _check(checks, "rvol", 1, "Stage 1: relative volume", None, "baseline unavailable", "", 0, 6, mandatory=True)
    fut = stock.get("fut") or {}
    f_chg = pct(fut.get("ltp"), fut.get("prev_close"))
    oi_chg = pct(fut.get("oi"), fut.get("prev_oi")) if fut.get("prev_oi") else None
    bu = buildup(f_chg, oi_chg, cfg)
    good = {"BULL": ("Long Buildup", "Short Covering"), "BEAR": ("Short Buildup", "Long Unwinding")}[ev["direction"]]
    if bu is None:
        _check(checks, "fut_buildup", 1, "Stage 1: futures build-up", None, "futures OI unavailable", "", 0, 10,
               mandatory=not (not live and cfg.get("bt_fut_optional")))
    else:
        pts = 10 if bu == good[0] else (5 if bu == good[1] else 0)
        _check(checks, "fut_buildup", 1, "Stage 1: futures build-up aligned", bu in good,
               f"{bu} (fut {f_chg:+.2f}%, OI {oi_chg:+.2f}%)", f"{good[0]} (full) / {good[1]} (half)", pts, 10, mandatory=True)
    uc, lc = stock.get("upper_circuit"), stock.get("lower_circuit")
    if uc and lc:
        near = (uc - px) / px * 100 <= 1.0 or (px - lc) / px * 100 <= 1.0
        _check(checks, "circuit", 0, "Not near circuit limit", not near, f"{px:.2f} in {lc}-{uc}", "> 1% away", mandatory=True)

    # ---------------- optional confirmations ----------------
    if f_chg is not None:
        basis_ok = up * (f_chg - chg) >= -0.05
        _check(checks, "basis", 1, "Futures basis aligned", basis_ok, f"fut {f_chg:+.2f}% vs spot {chg:+.2f}%",
               "futures not lagging spot", 5 if basis_ok else 0, 5)
    bq, sq = stock.get("fut_bq"), stock.get("fut_sq")
    if bq and sq:
        r = (bq / sq) if up > 0 else (sq / bq)
        _check(checks, "depth", 1, "Order-book imbalance (futures depth)", r >= 1.2, f"{r:.2f}x", ">= 1.2x", 5 if r >= 1.2 else 0, 5)
    else:
        _check(checks, "depth", 1, "Order-book imbalance", None, "depth unavailable", "", 0, 5)
    fh = stock.get("fut_hist") or []
    if len(fh) >= 2 and fh[0][1] and fh[-1][1]:
        d_oi = (fh[-1][1] - fh[0][1]) / fh[0][1] * 100
        _check(checks, "oi_15m", 1, "Futures OI rising last 15 min", d_oi >= 0.3, f"{d_oi:+.2f}%", ">= +0.3%",
               5 if d_oi >= 0.3 else 0, 5)
    else:
        _check(checks, "oi_15m", 1, "Futures OI last 15 min", None, "history unavailable", "", 0, 5)
    sec = (market.get("sectors") or {}).get(ev["sector"]) or {}
    s_chg = pct(sec.get("ltp"), sec.get("prev_close"))
    n_chg = regime.get("chg")
    if s_chg is not None and n_chg is not None:
        ok = up * s_chg > 0 and up * (s_chg - n_chg) >= 0
        _check(checks, "sector", 1, f"Sector strength ({ev['sector']})", ok, f"sector {s_chg:+.2f}% vs NIFTY {n_chg:+.2f}%",
               "same side, stronger than NIFTY", 5 if ok else 0, 5)
    else:
        _check(checks, "sector", 1, f"Sector strength ({ev['sector']})", None, "unavailable", "", 0, 5)
    if options_info and options_info.get("available"):
        ce, pe = options_info.get("ce_oi_chg_pct") or 0, options_info.get("pe_oi_chg_pct") or 0
        a_ok, b_ok = (pe > 0, ce < 0) if up > 0 else (ce > 0, pe < 0)
        pts = 5 if (a_ok and b_ok) else (2 if (a_ok or b_ok) else 0)
        _check(checks, "option_oi", 1, "Option chain near-ATM OI", pts > 0, f"CE {ce:+.1f}%, PE {pe:+.1f}%",
               "writers on the right side", pts, 5)

    # ---------------- stage 2 / 3: structure on 5-min candles ----------------
    b5 = five_min(bars)
    first_entry = minute_index(hm(cfg["pb_first_entry"]), cfg)
    last_entry = minute_index(hm(cfg["last_entry_time"]), cfg)
    ext = (lambda b: b["h"]) if up > 0 else (lambda b: b["l"])
    opp = (lambda b: b["l"]) if up > 0 else (lambda b: b["h"])
    trig = None
    levels = None
    stage = "STAGE1"
    wait = "Strong stock; waiting for a pullback to VWAP"
    m_last = bars[-1]["m"]
    metrics_extra = {}
    if len(b5) >= 4:
        hist, last = b5[:-1], b5[-1]
        h_idx = max(range(len(hist)), key=lambda i: up * ext(hist[i]))
        H = ext(hist[h_idx])
        impulse = up * (H - day_open) / day_open * 100
        pb = hist[h_idx + 1:]
        metrics_extra.update(impulse_pct=round(impulse, 2), extreme=round(H, 2))
        if impulse < cfg["pb_min_impulse_pct"]:
            wait = f"Impulse {impulse:.2f}% < {cfg['pb_min_impulse_pct']}%"
        elif len(pb) < cfg["pb_min_pullback_bars"]:
            wait = "Waiting for pullback (needs 2+ five-minute candles)"
        else:
            L = min((opp(b) for b in pb + [last]), key=lambda x: up * x)
            touch = any(b.get("vwap") and up * (opp(b) - b["vwap"] * (1 + up * cfg["pb_vwap_touch_pct"] / 100)) <= 0 for b in pb)
            hold = all(b.get("vwap") and up * (b["c"] - b["vwap"] * (1 - up * cfg["pb_vwap_break_pct"] / 100)) >= 0 for b in pb)
            imp_bars = hist[:h_idx + 1]
            imp_v = sum(b["v"] for b in imp_bars) / max(1, sum(b["n"] for b in imp_bars))
            pb_v = sum(b["v"] for b in pb) / max(1, sum(b["n"] for b in pb))
            depth = up * (H - L) / (up * (H - day_open)) * 100 if up * (H - day_open) > 0 else None
            metrics_extra.update(pullback_low=round(L, 2), pullback_depth_pct=round(depth, 1) if depth else None,
                                 vwap_touched=touch, vwap_held=hold)
            stage = "STAGE2"
            wait = "In pullback; waiting for a 5-min candle to turn back up" if up > 0 else \
                "In pullback; waiting for a 5-min candle to turn back down"
            _check(checks, "pb_hold", 2, "Stage 2: pullback held VWAP", hold, "held" if hold else "closed through VWAP",
                   f"no 5-min close beyond VWAP by > {cfg['pb_vwap_break_pct']}%", mandatory=True)
            _check(checks, "pb_touch", 2, "Stage 2: pullback reached VWAP", touch, "touched" if touch else "not yet",
                   f"within {cfg['pb_vwap_touch_pct']}% of VWAP", mandatory=True)
            _check(checks, "pb_volume", 2, "Stage 2: pullback on lighter volume", pb_v < imp_v,
                   f"{pb_v:,.0f} vs impulse {imp_v:,.0f} per min", "lower", 4 if pb_v < imp_v else 0, 4)
            if depth is not None:
                pts = 5 if 30 <= depth <= 70 else (2 if 20 <= depth <= 80 else 0)
                _check(checks, "pb_depth", 2, "Pullback depth vs impulse", pts > 0, f"{depth:.0f}%", "30-70% ideal", pts, 5)
            _check(checks, "impulse", 2, "Impulse strength (open to extreme)", impulse >= 1.2, f"{impulse:.2f}%",
                   ">= 1.2% (2 pts from 0.8%)", 5 if impulse >= 1.2 else 2, 5)
            prev = hist[-1]
            pb5 = [b["v"] for b in pb] or [1]
            vol_ok = last["v"] >= cfg["pb_trigger_vol_mult"] * (sum(pb5) / len(pb5))
            turn = up * (last["c"] - ext(prev)) > 0 and up * (H - last["c"]) > 0 and \
                last.get("vwap") is not None and up * (last["c"] - last["vwap"]) > 0
            just_closed = m_last % 5 == 4
            rng = last["h"] - last["l"]
            body_ok = rng > 0 and abs(last["c"] - last["o"]) >= 0.5 * rng and \
                ((last["c"] - last["l"]) / rng >= 0.7 if up > 0 else (last["h"] - last["c"]) / rng >= 0.7)
            _check(checks, "trigger_candle", 3, "Trigger candle strong (body >= 50%, close near extreme)", body_ok,
                   f"{last['t']} O{last['o']:.2f} H{last['h']:.2f} L{last['l']:.2f} C{last['c']:.2f}", "strong close",
                   5 if body_ok else 0, 5)
            if turn and vol_ok and touch and hold and just_closed:
                entry = last["c"]
                sl = L * (1 - up * cfg["pb_sl_buffer_pct"] / 100)
                dist = up * (entry - sl)
                note = "Stop below pullback low" if up > 0 else "Stop above pullback high"
                if dist / entry * 100 < cfg["pb_min_sl_pct"]:
                    dist = entry * cfg["pb_min_sl_pct"] / 100
                    sl = entry - up * dist
                    note = f"Stop widened to minimum {cfg['pb_min_sl_pct']}%"
                if dist / entry * 100 > cfg["pb_max_sl_pct"]:
                    _check(checks, "sl_width", 3, "Stop distance", False, f"{dist / entry * 100:.2f}%",
                           f"<= {cfg['pb_max_sl_pct']}%", mandatory=True)
                else:
                    t1 = H if up * (H - entry) >= 1.5 * dist else entry + up * 1.5 * dist
                    t2 = entry + up * cfg["pb_t2_r"] * dist
                    if up * (t2 - t1) <= 0:
                        t2 = t1 + up * 0.5 * dist
                    levels = {"entry": round(entry, 2), "u_sl": round(sl, 2), "u_t1": round(t1, 2), "u_t2": round(t2, 2),
                              "risk": round(dist, 2), "note": note}
                    trig = {"type": "pullback_reversal", "level": round(entry, 2), "bar_time": last["t"], "close": entry,
                            "fired": True,
                            "text": (f"5-min candle {last['t']} closed {entry:.2f} {'above' if up > 0 else 'below'} the "
                                     f"previous candle {'high' if up > 0 else 'low'} {ext(prev):.2f} after a pullback to "
                                     f"VWAP {last['vwap']:.2f} (pullback {'low' if up > 0 else 'high'} {L:.2f}, "
                                     f"day {'high' if up > 0 else 'low'} {H:.2f})")}
            elif turn and not vol_ok:
                wait = "Turn candle without volume; waiting"

    ev["trigger"] = trig
    ev["levels"] = levels
    pre = sum(c["points"] for c in checks)
    ev["pre_score"] = round(pre, 1)
    hard_fail = [c for c in checks if c["mandatory"] and c["passed"] is False]
    missing = [c for c in checks if c["mandatory"] and c["passed"] is None]
    ev["reject_reasons"] = [f"{c['label']}: {c['value']} (needs {c['threshold']})" for c in hard_fail
                            if c["key"] not in ("pb_touch",)]
    ev["missing"] = [f"{c['label']}: {c['value']}" for c in missing]
    touch_fail = any(c["key"] == "pb_touch" and c["passed"] is False for c in checks)
    real_fail = [c for c in hard_fail if c["key"] != "pb_touch"]
    if real_fail:
        ev["status"] = "REJECTED"
    elif missing:
        ev["status"] = "WATCHING"
        ev["wait_reason"] = "Data missing: " + ev["missing"][0]
    elif m_last < first_entry:
        ev["status"], ev["stage"] = "WATCHING", stage
        ev["wait_reason"] = f"Entries start {cfg['pb_first_entry']}"
    elif m_last > last_entry:
        ev["status"], ev["stage"] = "WATCHING", stage
        ev["wait_reason"] = f"After last entry time {cfg['last_entry_time']}"
    elif trig:
        ev["status"], ev["stage"] = "TRIGGER", "STAGE3"
    else:
        ev["status"], ev["stage"] = "WATCHING", stage
        ev["wait_reason"] = "Pullback has not reached VWAP yet" if (touch_fail and stage == "STAGE2") else wait
    ev["score"] = ev["pre_score"]
    ev["metrics"] = {
        "price": round(px, 2), "chg_pct": round(chg, 2), "prev_close": stock.get("prev_close"),
        "day_open": round(day_open, 2), "vwap": round(b5[-1]["vwap"], 2) if b5 and b5[-1].get("vwap") else None,
        "rvol": round(rv, 2) if rv is not None else None, "buildup": bu,
        "fut_chg_pct": round(f_chg, 2) if f_chg is not None else None,
        "fut_oi_chg_pct": round(oi_chg, 2) if oi_chg is not None else None,
        "nifty_chg_pct": round(n_chg, 2) if n_chg is not None else None,
        "sector_chg_pct": round(s_chg, 2) if s_chg is not None else None,
        "last_bar_time": bars[-1]["t"], "bars": len(bars), "regime": regime.get("regime"), **metrics_extra,
    }
    return ev


def contract_points(ev: dict, sel: dict, plan: dict, chain: dict, stock: dict, cfg: dict) -> int:
    """The remaining 30 points: IV+Greeks (10), liquidity (10), reward:risk (10). Adds checks to ev."""
    checks = ev["checks"]
    leg = sel["selected"]
    lot = chain["lot_size"]
    hv = chain.get("hv20")
    iv = leg.get("iv")
    if iv and hv:
        r = iv / hv
        pts = 5 if r <= 1.2 else (2 if r <= cfg["max_ivhv_ratio"] else 0)
        _check(checks, "iv_hv", 4, "IV vs HV20 (IVP proxy)", r <= cfg["max_ivhv_ratio"], f"{iv:.1f}% / {hv:.1f}% = {r:.2f}x",
               f"<= {cfg['max_ivhv_ratio']}x (5 pts <= 1.2x)", pts, 5, mandatory=True)
    else:
        _check(checks, "iv_hv", 4, "IV vs HV20", None, "unavailable", "", 0, 5)
    th = leg.get("theta_pct_day")
    _check(checks, "theta", 4, "Theta per day", th is not None and th <= 3, f"{th}% of premium", "<= 3%",
           3 if th is not None and th <= 3 else 0, 3)
    d = abs(leg.get("delta") or 0)
    _check(checks, "delta", 4, "Delta in 0.45-0.65", 0.45 <= d <= 0.65, f"{d:.2f}", "0.45-0.65", 2 if 0.45 <= d <= 0.65 else 0, 2)
    sp = leg.get("spread_pct") or 99
    _check(checks, "spread", 4, "Bid-ask spread", sp <= 2, f"{sp}%", "<= 1% (5 pts), <= 2% (2 pts)",
           5 if sp <= 1 else (2 if sp <= 2 else 0), 5)
    oil = (leg.get("oi") or 0) / lot
    _check(checks, "oi_lots", 4, "Contract OI", oil >= 200, f"{oil:,.0f} lots", ">= 200 lots", 3 if oil >= 200 else 0, 3)
    rank = stock.get("turnover_rank")
    _check(checks, "turnover", 4, "Turnover rank top 20", rank is not None and rank <= 20, str(rank), "<= 20",
           2 if rank is not None and rank <= 20 else 0, 2)
    rr = plan.get("rr_t1") or 0
    _check(checks, "rr", 4, "Premium reward:risk to T1", rr >= cfg["min_rr"], f"{rr:.2f}", ">= 2.0 (10), >= 1.5 (6)",
           10 if rr >= 2 else (6 if rr >= 1.5 else 0), 10)
    return sum(c["points"] for c in checks if c["layer"] == 4)


def grade(score: float, regime: dict, cfg: dict) -> str | None:
    add = regime.get("min_score_add", 0)
    if score >= cfg["grade_aplus_min"] + add:
        return "A+"
    if score >= cfg["grade_a_min"] + add:
        return "A"
    if score >= cfg["grade_b_min"]:
        return "B"
    return None
