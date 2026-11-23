"""Complete option trade plan + explanation, built only from calculated values."""
from __future__ import annotations

import datetime as dt
import math

from .. import options
from .charges import option_round_trip
from .config import IST


def _reprice(S_new, K, expiry, iv_pct, kind, minutes_ahead, now):
    T = max(options.years_to_expiry(expiry, now) - minutes_ahead / (365 * 1440), 600 / (365 * 86400))
    return options.bs_price(S_new, K, T, iv_pct / 100, kind)


def underlying_levels(ev: dict, cfg: dict):
    """Underlying stop: the tighter of VWAP and the trigger candle's extreme, clamped to a sane % band.
    Targets are multiples (t1_r, t2_r) of that risk. Shared by live planning and the backtest."""
    up = 1 if ev["direction"] == "BULL" else -1
    m = ev["metrics"]
    px = m["price"]
    cands = [x for x in (m.get("vwap"), m.get("last_bar_low") if up > 0 else m.get("last_bar_high")) if x]
    stop_ref = max(cands) if up > 0 else min(cands)
    dist = abs(px - stop_ref)
    note = f"Stop at {'higher' if up > 0 else 'lower'} of VWAP and trigger-candle {'low' if up > 0 else 'high'}"
    lo, hi = px * cfg["min_u_sl_pct"] / 100, px * cfg["max_u_sl_pct"] / 100
    if dist < lo:
        dist, note = lo, f"Stop widened to minimum {cfg['min_u_sl_pct']}% (structure stop was too tight)"
    elif dist > hi:
        dist, note = hi, f"Stop capped at {cfg['max_u_sl_pct']}% (structure stop was too wide)"
    return (px - up * dist, px + up * cfg["t1_r"] * dist, px + up * cfg["t2_r"] * dist, dist, note)


def build_plan(ev: dict, sel: dict, chain: dict, cfg: dict, now: dt.datetime, levels: dict | None = None) -> dict:
    up = 1 if ev["direction"] == "BULL" else -1
    m = ev["metrics"]
    leg = sel["selected"]
    kind = sel["type"]
    K = leg["strike"]
    lot = chain["lot_size"]
    S = chain["spot"]
    px = m["price"]

    if levels:   # strategy-defined structure levels (v3 pullback)
        u_sl, u_t1, u_t2, dist, note_sl = levels["u_sl"], levels["u_t1"], levels["u_t2"], levels["risk"], levels["note"]
        px = levels["entry"]
    else:
        u_sl, u_t1, u_t2, dist, note_sl = underlying_levels(ev, cfg)
    # ---- premium levels from the actual contract (IV, strike, expiry) ----
    ask, bid = leg["ask"], leg["bid"]
    entry = ask
    iv = leg["iv"]
    model_now = _reprice(S, K, chain["expiry"], iv, kind, 0, now)

    def premiums(usl, ut1, ut2):
        return (entry + _reprice(usl, K, chain["expiry"], iv, kind, cfg["sl_reprice_minutes"], now) - model_now,
                entry + _reprice(ut1, K, chain["expiry"], iv, kind, cfg["target_reprice_minutes"], now) - model_now,
                entry + _reprice(ut2, K, chain["expiry"], iv, kind, cfg["target_reprice_minutes"], now) - model_now)

    p_sl, p_t1, p_t2 = premiums(u_sl, u_t1, u_t2)
    notes = [note_sl]
    sl_pct = (entry - p_sl) / entry * 100
    if 0 < sl_pct < cfg["min_premium_sl_pct"] and not levels:
        # premium stop would sit inside normal option noise: widen the UNDERLYING stop (and targets with it)
        scale = cfg["min_premium_sl_pct"] / sl_pct
        new_dist = min(dist * scale, px * cfg["max_u_sl_pct"] / 100)
        if new_dist > dist:
            dist = new_dist
            u_sl, u_t1, u_t2 = px - up * dist, px + up * cfg["t1_r"] * dist, px + up * cfg["t2_r"] * dist
            p_sl, p_t1, p_t2 = premiums(u_sl, u_t1, u_t2)
            notes.append(f"Underlying stop widened to {dist / px * 100:.2f}% so the premium stop is outside option noise")
        sl_pct = (entry - p_sl) / entry * 100
        if sl_pct < cfg["min_premium_sl_pct"]:
            p_sl = entry * (1 - cfg["min_premium_sl_pct"] / 100)
            notes.append(f"Premium SL set to the {cfg['min_premium_sl_pct']}% floor")
    if levels and 0 < sl_pct < cfg["min_premium_sl_pct"]:
        p_sl = entry * (1 - cfg["min_premium_sl_pct"] / 100)
        notes.append(f"Premium SL set to the {cfg['min_premium_sl_pct']}% floor")
        sl_pct = cfg["min_premium_sl_pct"]
    if sl_pct > cfg["max_premium_sl_pct"]:
        return {"error": f"Premium stop {sl_pct:.0f}% of entry > max {cfg['max_premium_sl_pct']}% - option too risky for this stop"}
    p_sl = max(0.05, p_sl)
    risk_ps = entry - p_sl
    rr = (p_t1 - entry) / risk_ps if risk_ps > 0 else 0
    if rr < cfg["min_rr"]:
        return {"error": f"Reward:risk to T1 is {rr:.2f} < minimum {cfg['min_rr']} (theta/IV eat the move)"}

    entry_hi = round(ask * (1 + cfg["entry_slippage_pct"] / 100), 2)
    entry_lo = round((ask + bid) / 2, 2)

    # ---- sizing ----
    budget = cfg["capital"] * cfg["risk_per_trade_pct"] / 100
    cap = cfg["capital"] * cfg["max_premium_pct"] / 100
    ch1 = option_round_trip(entry_hi, p_sl, lot, cfg)["total"]
    per_lot_risk = (entry_hi - p_sl) * lot + ch1
    lots_risk = math.floor(budget / per_lot_risk) if per_lot_risk > 0 else 0
    lots_cap = math.floor(cap / (entry_hi * lot)) if entry_hi > 0 else 0
    lots = min(lots_risk, lots_cap)
    if lots < 1:
        why = (f"1 lot risk Rs {per_lot_risk:,.0f} > risk budget Rs {budget:,.0f}" if lots_risk < 1
               else f"1 lot premium Rs {entry_hi * lot:,.0f} > premium cap Rs {cap:,.0f}")
        return {"error": f"Position size 0 lots: {why}"}
    qty = lots * lot
    charges = option_round_trip(entry_hi, p_sl, qty, cfg)
    max_loss = (entry_hi - p_sl) * qty + charges["total"]
    sc = ev["score"]
    conf = ev.get("grade") or ("HIGH" if sc >= 9 else "GOOD" if sc >= 8 else "MODERATE")
    return {
        "symbol": ev["symbol"], "direction": "BUY" if up > 0 else "SELL", "bias": ev["direction"],
        "option_type": kind, "tradingsymbol": leg["ts"], "strike": K, "expiry": chain["expiry"],
        "dte": chain.get("dte"), "lot_size": lot, "lots": lots, "qty": qty,
        "entry_premium": round(entry, 2), "entry_range": [entry_lo, entry_hi],
        "entry_trigger": ev["trigger"]["text"] if ev.get("trigger") else None,
        "sl_premium": round(p_sl, 2), "t1_premium": round(p_t1, 2), "t2_premium": round(p_t2, 2),
        "rr_t1": round(rr, 2), "rr_t2": round((p_t2 - entry) / risk_ps, 2),
        "spot_entry": round(px, 2), "spot_sl": round(u_sl, 2), "spot_t1": round(u_t1, 2), "spot_t2": round(u_t2, 2),
        "invalidation": f"1-min close {'below' if up > 0 else 'above'} {u_sl:.2f} on {ev['symbol']} spot, "
                        f"or option premium <= {p_sl:.2f}",
        "risk_budget": round(budget, 2), "per_lot_risk": round(per_lot_risk, 2),
        "max_loss": round(max_loss, 2), "est_charges": charges, "premium_outlay": round(entry_hi * qty, 2),
        "iv": iv, "delta": leg["delta"], "gamma": leg["gamma"], "theta": leg["theta"], "vega": leg["vega"],
        "spread_pct": leg["spread_pct"], "score": sc, "max_score": ev["max_score"], "confidence": conf,
        "signal_time": now.astimezone(IST).isoformat(timespec="seconds"),
        "data_time": ev["eval_time"], "plan_notes": notes,
    }


def explain(ev: dict, sel: dict, plan: dict, oinfo: dict | None) -> dict:
    up = ev["direction"] == "BULL"
    m = ev["metrics"]
    passed = [c for c in ev["checks"] if c["passed"] is True and (c["max_points"] or c["mandatory"])]
    failed = [c for c in ev["checks"] if c["passed"] is False]
    unavailable = [c for c in ev["checks"] if c["passed"] is None and c["threshold"] != "info only"]
    fmt = lambda c: f"{c['label']}: {c['value']} (threshold {c['threshold']})" + (f" +{c['points']:g}" if c["points"] else "")
    bu = m.get("buildup")
    fut_txt = {
        "Long Buildup": "Futures price up with OI up: fresh longs being added.",
        "Short Buildup": "Futures price down with OI up: fresh shorts being added.",
        "Short Covering": "Futures price up with OI down: shorts exiting (weaker than long buildup).",
        "Long Unwinding": "Futures price down with OI down: longs exiting (weaker than short buildup).",
        "Neutral": "Futures move or OI change too small to classify.",
    }.get(bu, "Futures OI unavailable.")
    if oinfo and oinfo.get("available"):
        opt_txt = (f"Near-ATM OI since {oinfo.get('since')}: CE {oinfo['ce_oi_chg_pct']:+.1f}%, "
                   f"PE {oinfo['pe_oi_chg_pct']:+.1f}%. "
                   + ("Put writers adding / call writers covering supports upside."
                      if up else "Call writers adding / put writers covering supports downside.")
                   + f" Baseline: {oinfo.get('baseline_kind')}.")
    else:
        opt_txt = "Option chain OI unavailable."
    risks = []
    if plan["dte"] is not None and plan["dte"] < 10:
        risks.append(f"Only {plan['dte']} days to expiry: theta decay is fast.")
    if plan["theta"]:
        risks.append(f"Theta Rs {abs(plan['theta']):.2f}/share/day on a Rs {plan['entry_premium']} premium.")
    if (m.get("rvol") or 0) < 1.5:
        risks.append("Volume participation is not strong.")
    if abs(m.get("chg_pct") or 0) > 2.8:
        risks.append("Move already extended beyond the preferred range.")
    if plan["spread_pct"] and plan["spread_pct"] > 1.5:
        risks.append(f"Spread {plan['spread_pct']}%: use limit orders.")
    for c in ev["checks"]:
        if c["key"] == "index_conflict" and "partial" in (c.get("note") or ""):
            risks.append("One index is moving against the trade.")
    risks.append("IV can drop after the move (IV crush) even if direction is right.")
    return {
        "why_stock": (f"{ev['symbol']} {'up' if up else 'down'} {m['chg_pct']:+.2f}% with score "
                      f"{ev['score']:g}/{ev['max_score']}; all mandatory filters passed and the entry trigger fired."),
        "passed": [fmt(c) for c in passed], "failed": [fmt(c) for c in failed],
        "unavailable": [f"{c['label']}: {c['value']}" for c in unavailable],
        "futures_oi": fut_txt, "option_chain": opt_txt,
        "sector": next((f"{c['label']}: {c['value']}" for c in ev["checks"] if c["key"] == "sector"), "n/a"),
        "why_strike": sel["why"],
        "why_entry": (f"{plan['entry_trigger']}. Entry premium {plan['entry_premium']} is the live ask; "
                      f"valid while premium stays within {plan['entry_range'][0]}-{plan['entry_range'][1]}."),
        "invalidation": plan["invalidation"], "risks": risks,
    }
