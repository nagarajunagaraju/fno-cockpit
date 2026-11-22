"""The four-layer signal engine.

`evaluate()` is a pure function of the data it is given. Live scanning, paper
trading and the historical backtest all call this same function, so the logic
cannot drift between them. It only looks at *completed* 1-minute bars; the
decision price is the close of the last completed bar.

Every check records: observed value, threshold, points, and whether it passed,
failed, or was unavailable (None). Unavailable data is never filled in.
"""
from __future__ import annotations

import datetime as dt

from .candles import opening_range, rvol as calc_rvol, vwap as calc_vwap
from .config import IST, hm, minute_index


def pct(a, b):
    return (a - b) / b * 100 if (a is not None and b) else None


def buildup(price_chg, oi_chg, cfg):
    if price_chg is None or oi_chg is None:
        return None
    if abs(price_chg) < cfg["min_price_move_pct"] or abs(oi_chg) < cfg["min_oi_move_pct"]:
        return "Neutral"
    if price_chg > 0 and oi_chg > 0:
        return "Long Buildup"
    if price_chg < 0 and oi_chg > 0:
        return "Short Buildup"
    if price_chg > 0 and oi_chg < 0:
        return "Short Covering"
    return "Long Unwinding"


def _check(checks, key, layer, label, passed, value, threshold, points=0, max_points=0, mandatory=False, note=""):
    checks.append({"key": key, "layer": layer, "label": label, "passed": passed, "value": value,
                   "threshold": threshold, "points": points, "max_points": max_points,
                   "mandatory": mandatory, "note": note})


def _r(x, d=2):
    return None if x is None else round(x, d)


def evaluate(stock: dict, market: dict, cfg: dict, now: dt.datetime,
             options_info: dict | None = None, live: bool = True) -> dict:
    W = cfg["weights"]
    sym = stock["symbol"]
    bars = stock.get("bars") or []
    checks: list[dict] = []
    out = {"symbol": sym, "sector": stock.get("sector"), "lot_size": stock.get("lot_size"),
           "eval_time": now.astimezone(IST).isoformat(timespec="seconds"), "status": "NO_SETUP",
           "direction": None, "score": 0, "max_score": int(sum(W.values())), "checks": checks,
           "trigger": None, "metrics": {}, "reject_reasons": []}
    if not bars:
        out["reject_reasons"].append("No completed 1-min candles yet")
        return out

    last = bars[-1]
    px = last["c"]
    prev_close = stock.get("prev_close")
    chg = pct(px, prev_close)
    day_open = stock.get("day_open") or bars[0]["o"]
    day_high = max(b["h"] for b in bars)
    day_low = min(b["l"] for b in bars)
    vw = stock.get("vwap") if (live and stock.get("vwap")) else calc_vwap(bars)
    orng = opening_range(bars, cfg["opening_range_minutes"])
    rv = calc_rvol(bars, stock.get("rvol_baseline"))
    pdh, pdl = stock.get("pdh"), stock.get("pdl")

    direction = None
    if chg is not None and abs(chg) >= 0.2:
        direction = "BULL" if chg > 0 else "BEAR"
    up = 1 if direction == "BULL" else -1
    out["direction"] = direction

    # ---------------- data freshness ----------------
    if live:
        age = max(0.0, now.timestamp() - stock["data_ts"]) if stock.get("data_ts") else None
        _check(checks, "data_fresh", 0, "Live data fresh", None if age is None else age <= cfg["stale_seconds"],
               None if age is None else f"{age:.0f}s old", f"<= {cfg['stale_seconds']}s", mandatory=True)
    else:
        _check(checks, "data_fresh", 0, "Historical bar data", True, last["t"], "completed bar", mandatory=True)

    _check(checks, "direction", 0, "Clear direction (|change| >= 0.2%)", direction is not None,
           _r(chg), ">= 0.2%", mandatory=True)

    # ---------------- layer 1: market & sector ----------------
    nifty = market.get("nifty") or {}
    bank = market.get("banknifty") or {}
    n_chg = pct(nifty.get("ltp"), nifty.get("prev_close"))
    b_chg = pct(bank.get("ltp"), bank.get("prev_close"))
    if direction and n_chg is not None and b_chg is not None:
        lim = cfg["index_conflict_pct"]
        n_against = up * n_chg < -lim
        b_against = up * b_chg < -lim
        both = n_against and b_against
        passed = not (both and cfg["reject_on_index_conflict"])
        note = ("NIFTY and BANKNIFTY both against the trade" if both else
                "One index against the trade (partial conflict)" if (n_against or b_against) else
                "Indices not against the trade")
        _check(checks, "index_conflict", 1, "Market structure (NIFTY / BANKNIFTY)", passed,
               f"NIFTY {n_chg:+.2f}%, BANKNIFTY {b_chg:+.2f}%", f"not both against by > {lim}%",
               mandatory=True, note=note)
    else:
        _check(checks, "index_conflict", 1, "Market structure (NIFTY / BANKNIFTY)", None,
               "index data unavailable", "", mandatory=True)

    sec_name = stock.get("sector") or "UNMAPPED"
    sec = (market.get("sectors") or {}).get(sec_name) or {}
    s_chg = pct(sec.get("ltp"), sec.get("prev_close"))
    if direction and s_chg is not None and n_chg is not None:
        rs = s_chg - n_chg
        ok = up * s_chg > 0 and up * rs >= cfg["sector_min_rs_pct"]
        _check(checks, "sector", 1, f"Sector alignment ({sec_name})", ok,
               f"sector {s_chg:+.2f}%, vs NIFTY {rs:+.2f}%",
               "sector moving with trade and outperforming NIFTY" if up > 0 else "sector falling and underperforming NIFTY",
               W["sector"] if ok else 0, W["sector"])
    else:
        _check(checks, "sector", 1, f"Sector alignment ({sec_name})", None,
               "sector index data unavailable" if sec_name != "UNMAPPED" else "stock not in sector map",
               "", 0, W["sector"])
    br = market.get("breadth") or {}
    if br.get("adv") is not None:
        _check(checks, "breadth", 1, "F&O universe breadth (info)", None,
               f"{br['adv']} up / {br['dec']} down", "info only")

    # ---------------- layer 2: price action & volume ----------------
    if rv is not None:
        pts = W["rvol"] if rv >= cfg["rvol_strong"] else (W["rvol"] / 2 if rv >= cfg["rvol_ok"] else 0)
        _check(checks, "rvol", 2, "Relative volume (vs same time, last 10 sessions)", pts > 0, f"{rv:.2f}x",
               f">= {cfg['rvol_ok']}x (full points >= {cfg['rvol_strong']}x)", pts, W["rvol"])
    else:
        _check(checks, "rvol", 2, "Relative volume", None, "10-session baseline unavailable", "", 0, W["rvol"])

    tol = cfg["open_eq_tolerance_pct"]
    if direction:
        ref = day_low if up > 0 else day_high
        gap = abs(day_open - ref) / day_open * 100
        ok = gap <= tol
        _check(checks, "open_eq", 2, "Open = Low" if up > 0 else "Open = High", ok,
               f"open {day_open:.2f}, {'low' if up > 0 else 'high'} {ref:.2f} ({gap:.2f}%)", f"<= {tol}%",
               W["open_eq"] if ok else 0, W["open_eq"])

        a = abs(chg)
        ok = cfg["chg_min_pct"] <= a <= cfg["chg_max_pct"]
        _check(checks, "momentum", 2, "Price change in preferred range", ok, f"{chg:+.2f}%",
               f"{cfg['chg_min_pct']}% to {cfg['chg_max_pct']}%", W["momentum"] if ok else 0, W["momentum"])
        _check(checks, "not_chasing", 2, "Not chasing an extended move", a <= cfg["chase_limit_pct"],
               f"{chg:+.2f}%", f"<= {cfg['chase_limit_pct']}%", mandatory=True)

        if vw is not None:
            side_ok = up * (px - vw) > 0
            _check(checks, "vwap_side", 2, "Price above VWAP" if up > 0 else "Price below VWAP",
                   side_ok if cfg["require_vwap_side"] else None, f"price {px:.2f}, VWAP {vw:.2f}",
                   "above" if up > 0 else "below", mandatory=cfg["require_vwap_side"])
            lvl = pdh if up > 0 else pdl
            if lvl:
                ok = side_ok and up * (px - lvl) > 0
                _check(checks, "vwap_pdh", 2, "VWAP + PDH breakout" if up > 0 else "VWAP + PDL breakdown", ok,
                       f"price {px:.2f}, {'PDH' if up > 0 else 'PDL'} {lvl:.2f}",
                       "beyond previous-day level and VWAP", W["vwap_pdh"] if ok else 0, W["vwap_pdh"])
            else:
                _check(checks, "vwap_pdh", 2, "VWAP + PDH/PDL", None, "previous-day level unavailable", "",
                       0, W["vwap_pdh"])
        else:
            _check(checks, "vwap_side", 2, "VWAP side", None, "VWAP unavailable", "", mandatory=cfg["require_vwap_side"])
            _check(checks, "vwap_pdh", 2, "VWAP + PDH/PDL", None, "VWAP unavailable", "", 0, W["vwap_pdh"])

        uc, lc = stock.get("upper_circuit"), stock.get("lower_circuit")
        if uc and lc:
            buf = cfg["circuit_buffer_pct"]
            near = (uc - px) / px * 100 <= buf or (px - lc) / px * 100 <= buf
            _check(checks, "circuit", 2, "Not near circuit limit", not near, f"price {px:.2f}, band {lc}-{uc}",
                   f"> {buf}% away", mandatory=True)
        else:
            _check(checks, "circuit", 2, "Circuit limit check", True, "circuit band not provided", "skipped",
                   mandatory=True, note="no band data; check not applied")

    # ---------------- layer 3: derivatives ----------------
    fut = stock.get("fut") or {}
    f_chg = pct(fut.get("ltp"), fut.get("prev_close"))
    oi_chg = pct(fut.get("oi"), fut.get("prev_oi")) if fut.get("prev_oi") else None
    bu = buildup(f_chg, oi_chg, cfg)
    _check(checks, "futures_oi_data", 3, "Futures price + OI data available", bu is not None if cfg["require_futures_oi"] else True,
           "available" if bu is not None else "futures OI unavailable", "required" if cfg["require_futures_oi"] else "optional",
           mandatory=True)
    if direction and bu is not None:
        full = {"BULL": "Long Buildup", "BEAR": "Short Buildup"}[direction]
        half = {"BULL": "Short Covering", "BEAR": "Long Unwinding"}[direction]
        pts = W["fut_oi"] if bu == full else (W["fut_oi"] / 2 if bu == half else 0)
        _check(checks, "fut_oi", 3, "Futures price + OI build-up", pts > 0,
               f"{bu} (fut {f_chg:+.2f}%, OI {oi_chg:+.2f}%)", f"{full} (full) / {half} (half)", pts, W["fut_oi"])
    elif direction:
        _check(checks, "fut_oi", 3, "Futures price + OI build-up", None, "unavailable", "", 0, W["fut_oi"])

    oinfo = options_info or {}
    if options_info is None:
        _check(checks, "option_chain_data", 3, "Option chain OI data", None, "not fetched yet (pre-screen)", "",
               mandatory=True)
    elif not oinfo.get("available"):
        _check(checks, "option_chain_data", 3, "Option chain OI data", False if cfg["require_option_chain"] else True,
               oinfo.get("reason", "unavailable"), "required" if cfg["require_option_chain"] else "optional",
               mandatory=True)
    else:
        _check(checks, "option_chain_data", 3, "Option chain OI data", True,
               f"OI change since {oinfo.get('since', '?')} ({oinfo.get('baseline_kind', '')})", "available",
               mandatory=True)
        if direction:
            ce, pe = oinfo.get("ce_oi_chg_pct"), oinfo.get("pe_oi_chg_pct")
            if up > 0:
                a_ok, b_ok = (pe or 0) > 0, (ce or 0) < 0
                lbl = "Put OI addition + Call OI unwinding (near ATM)"
            else:
                a_ok, b_ok = (ce or 0) > 0, (pe or 0) < 0
                lbl = "Call OI addition + Put OI unwinding (near ATM)"
            pts = W["option_chain"] if (a_ok and b_ok) else (W["option_chain"] / 2 if (a_ok or b_ok) else 0)
            _check(checks, "option_chain", 3, lbl, pts > 0, f"CE OI {ce:+.1f}%, PE OI {pe:+.1f}%",
                   "both (full) / one (half)", pts, W["option_chain"])
        iv, ref = oinfo.get("atm_iv"), oinfo.get("iv_ref")
        if iv and ref:
            ratio = iv / ref
            _check(checks, "iv_normal", 3, "IV not abnormal", ratio <= cfg["max_iv_vs_ref"],
                   f"ATM IV {iv:.1f}% vs {oinfo.get('iv_ref_kind')} {ref:.1f}% ({ratio:.2f}x)",
                   f"<= {cfg['max_iv_vs_ref']}x", mandatory=True)
        else:
            _check(checks, "iv_normal", 3, "IV context", None, "IV reference unavailable", "info only")

    # ---------------- layer 4: score, mandatory filters, trigger ----------------
    score = sum(c["points"] for c in checks)
    out["score"] = round(score, 1)
    _check(checks, "min_score", 4, "Minimum score", score >= cfg["min_score"], f"{score:g}/{out['max_score']}",
           f">= {cfg['min_score']}", mandatory=True)

    hard_fail = [c for c in checks if c["mandatory"] and c["passed"] is False and c["key"] != "min_score"]
    missing = [c for c in checks if c["mandatory"] and c["passed"] is None]
    out["reject_reasons"] = [f"{c['label']}: {c['value']} (needs {c['threshold']})" for c in hard_fail]
    out["missing"] = [f"{c['label']}: {c['value']}" for c in missing]

    m_now = minute_index(now, cfg)
    or_end = cfg["opening_range_minutes"]
    entry_end = minute_index(hm(cfg["last_entry_time"]), cfg)
    trig = None
    if direction and len(bars) >= 2 and last["m"] >= or_end:
        prior = bars[:-1]
        if up > 0:
            lvl = max(b["h"] for b in prior)
            fired = last["c"] > lvl and prior[-1]["c"] <= lvl
        else:
            lvl = min(b["l"] for b in prior)
            fired = last["c"] < lvl and prior[-1]["c"] >= lvl
        trig = {"type": "day_extreme_breakout" if up > 0 else "day_extreme_breakdown", "level": round(lvl, 2),
                "bar_time": last["t"], "close": px, "fired": bool(fired),
                "text": (f"1-min candle {last['t']} closed {px:.2f} {'above' if up > 0 else 'below'} "
                         f"the day's {'high' if up > 0 else 'low'} {lvl:.2f}"
                         + (f" (opening range {orng[1]:.2f}-{orng[0]:.2f})" if orng else ""))}
    out["trigger"] = trig

    if not direction:
        out["status"] = "NO_SETUP"
    elif hard_fail:
        out["status"] = "REJECTED"
    elif score < cfg["watch_min_score"]:
        out["status"] = "NO_SETUP"
    elif missing or score < cfg["min_score"]:
        out["status"] = "WATCHING"
    elif m_now < or_end or last["m"] < or_end:
        out["status"] = "WATCHING"
        out["wait_reason"] = f"Opening range forming (first {or_end} min)"
    elif m_now > entry_end:
        out["status"] = "WATCHING"
        out["wait_reason"] = f"After last entry time {cfg['last_entry_time']}"
    elif trig and trig["fired"]:
        out["status"] = "TRIGGER"
    else:
        out["status"] = "READY"
        out["wait_reason"] = "All filters pass; waiting for breakout candle" if up > 0 else \
            "All filters pass; waiting for breakdown candle"

    out["metrics"] = {
        "price": _r(px), "chg_pct": _r(chg), "prev_close": _r(prev_close), "day_open": _r(day_open),
        "day_high": _r(day_high), "day_low": _r(day_low), "vwap": _r(vw), "rvol": _r(rv),
        "pdh": _r(pdh), "pdl": _r(pdl), "or_high": _r(orng[0]) if orng else None,
        "or_low": _r(orng[1]) if orng else None, "fut_chg_pct": _r(f_chg), "fut_oi_chg_pct": _r(oi_chg),
        "buildup": bu, "nifty_chg_pct": _r(n_chg), "banknifty_chg_pct": _r(b_chg),
        "sector_chg_pct": _r(s_chg), "atr_daily": _r(stock.get("atr_daily")),
        "last_bar_time": last["t"], "bars": len(bars),
        "last_bar_low": _r(last["l"]), "last_bar_high": _r(last["h"]),
    }
    return out


def prescore(ev: dict) -> float:
    """Score before option-chain data is fetched (option points excluded)."""
    return sum(c["points"] for c in ev["checks"] if c["key"] != "option_chain")
