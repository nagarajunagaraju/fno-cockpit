"""Position sizing, SL/targets and the discipline guard."""
from __future__ import annotations

import datetime as dt
import math

DEFAULT_RULES = {
    "capital": 300000,          # trading capital in Rs
    "risk_per_trade_pct": 1.0,  # max loss per trade as % of capital
    "max_trades_per_day": 3,
    "max_daily_loss_pct": 2.0,  # stop for the day after this loss
    "cooldown_minutes": 30,     # no new trade this long after a losing trade
    "max_open_positions": 2,
    "atr_sl_mult": 1.5,         # default SL = entry -/+ ATR x this
    "min_score": 45,            # only trade stocks with |score| >= this
    "max_premium_pct": 10.0,    # max premium paid per trade as % of capital (can go to zero)
    "refresh_seconds": 5,
}


def plan_trade(row: dict, side: str, entry: float, sl: float | None, rules: dict) -> dict:
    side = side.upper()
    sign = 1 if side == "LONG" else -1
    a = row.get("atr") or entry * 0.02
    if not sl:
        sl = entry - sign * a * rules["atr_sl_mult"]
    r = abs(entry - sl)
    warnings = []
    if (side == "LONG" and sl >= entry) or (side == "SHORT" and sl <= entry):
        return {"error": "SL wrong side lo undi. LONG ki SL entry kinda, SHORT ki paina undali."}
    t1 = entry + sign * 1.5 * r
    t2 = entry + sign * 2.5 * r
    lot = row["lot_size"]
    budget = rules["capital"] * rules["risk_per_trade_pct"] / 100
    per_lot = r * lot
    lots = math.floor(budget / per_lot) if per_lot > 0 else 0
    if lots == 0:
        warnings.append(f"1 lot risk Rs {per_lot:,.0f} > nee per-trade budget Rs {budget:,.0f}. "
                        "Skip cheyi, leda defined-risk option spread vaadu.")
    if r < 0.5 * a:
        warnings.append(f"SL chala tight (< 0.5 ATR = {0.5 * a:.1f}). Normal noise lo hit avvachu.")
    if r > 2.5 * a:
        warnings.append("SL chala dooram (> 2.5 ATR). Entry better level lo tesko.")
    bias = row.get("bias")
    if (side == "LONG" and bias == "BEARISH") or (side == "SHORT" and bias == "BULLISH"):
        warnings.append(f"Trend ki against trade: score {row.get('score')} ({bias}).")
    elif abs(row.get("score", 0)) < rules["min_score"]:
        warnings.append(f"Score {row.get('score')} weak (min {rules['min_score']}). Clear setup kaadu.")
    qty = lots * lot
    return {
        "symbol": row["symbol"], "side": side, "entry": round(entry, 2), "sl": round(sl, 2),
        "t1": round(t1, 2), "t2": round(t2, 2), "atr": round(a, 2), "risk_per_share": round(r, 2),
        "lot_size": lot, "lots": lots, "qty": qty,
        "risk_amount": round(r * qty, 2), "risk_budget": round(budget, 2),
        "reward_t1": round(1.5 * r * qty, 2), "reward_t2": round(2.5 * r * qty, 2),
        "notional": round(qty * entry, 2),
        "min_capital_1lot": round(per_lot / (rules["risk_per_trade_pct"] / 100), 0), "score": row.get("score"), "bias": bias,
        "warnings": warnings,
    }


def discipline(rules: dict, trades_today: int, day_pnl: float, last_loss_at: str | None,
               open_count: int) -> dict:
    max_loss = rules["capital"] * rules["max_daily_loss_pct"] / 100
    msgs = []
    status = "GREEN"
    cooldown_until = None

    if day_pnl <= -max_loss:
        status = "RED"
        msgs.append(f"Daily loss limit Rs {max_loss:,.0f} cross ayyindi. Ivala trading STOP. Repu fresh ga.")
    if trades_today >= rules["max_trades_per_day"]:
        status = "RED"
        msgs.append(f"{trades_today}/{rules['max_trades_per_day']} trades ayipoyayi. Overtrading vaddu, ivala chaalu.")
    if last_loss_at:
        until = dt.datetime.fromisoformat(last_loss_at) + dt.timedelta(minutes=rules["cooldown_minutes"])
        if dt.datetime.now() < until:
            cooldown_until = until.isoformat(timespec="seconds")
            if status != "RED":
                status = "RED"
            msgs.append(f"Last trade loss. Revenge trade vaddu: {until:%H:%M} varaku cooldown.")
    if open_count >= rules["max_open_positions"] and status != "RED":
        status = "YELLOW"
        msgs.append(f"{open_count} positions already open. Kotha trade mundu okati close cheyi.")

    if status == "GREEN":
        if trades_today == rules["max_trades_per_day"] - 1 or day_pnl <= -0.6 * max_loss:
            status = "YELLOW"
            msgs.append("Careful: ivala limit daggara unnav. Next trade A+ setup aithe ne.")
        else:
            msgs.append("All clear. Plan prakaram trade cheyi.")

    return {
        "status": status, "messages": msgs, "trades_today": trades_today,
        "max_trades": rules["max_trades_per_day"], "day_pnl": round(day_pnl, 2),
        "max_daily_loss": round(max_loss, 2), "cooldown_until": cooldown_until,
        "open_count": open_count, "max_open": rules["max_open_positions"],
        "can_trade": status != "RED" and open_count < rules["max_open_positions"],
    }


def trade_alert(trade: dict, ltp: float | None) -> str | None:
    if ltp is None:
        return None
    sign = -1 if trade["side"] == "SHORT" else 1
    if sign * (ltp - trade["sl"]) <= 0:
        return "SL HIT - exit now, hope pettukoku"
    if sign * (ltp - trade["t2"]) >= 0:
        return "T2 HIT - book profits"
    if sign * (ltp - trade["t1"]) >= 0:
        return "T1 HIT - book partial, SL ni cost ki move cheyi"
    return None


def plan_option_trade(row: dict, chain: dict, strike: float, kind: str, rules: dict,
                      entry: float | None = None, sl: float | None = None) -> dict:
    """Option BUY plan. Premium SL comes from the stock's ATR stop translated with delta."""
    srow = next((r for r in chain["strikes"] if r["strike"] == strike), None)
    leg = srow.get(kind) if srow else None
    if not leg:
        return {"error": f"{strike} {kind} chain lo ledu"}
    premium = entry or leg.get("ask") or leg["ltp"]
    if not premium or premium <= 0:
        return {"error": "Premium price ledu"}
    S = chain["spot"]
    delta = abs(leg.get("delta") or 0.5)
    a = row.get("atr") or S * 0.02
    u_dist = a * rules["atr_sl_mult"]
    up = 1 if kind == "CE" else -1
    u_sl = S - up * u_dist
    u_t1 = S + up * 1.5 * u_dist

    if sl:
        if not 0 < sl < premium:
            return {"error": "Option SL premium kante takkuva, zero kante ekkuva undali."}
        prem_risk = premium - sl
    else:
        prem_risk = min(max(delta * u_dist, 0.15 * premium), 0.40 * premium)
        sl = premium - prem_risk
    t1 = premium + 1.5 * prem_risk
    t2 = premium + 2.5 * prem_risk

    lot = chain["lot_size"]
    budget = rules["capital"] * rules["risk_per_trade_pct"] / 100
    outlay_cap = rules["capital"] * rules["max_premium_pct"] / 100
    per_lot_risk = prem_risk * lot
    per_lot_outlay = premium * lot
    lots_risk = math.floor(budget / per_lot_risk) if per_lot_risk else 0
    lots_cap = math.floor(outlay_cap / per_lot_outlay) if per_lot_outlay else 0
    lots = min(lots_risk, lots_cap)
    qty = lots * lot
    theta = abs(leg.get("theta") or 0)
    theta_day = theta * max(qty, lot)

    w = []
    if lots == 0:
        if lots_risk == 0:
            w.append(f"1 lot SL risk Rs {per_lot_risk:,.0f} > nee budget Rs {budget:,.0f}. Skip cheyi, leda SL tight cheyi.")
        else:
            w.append(f"1 lot premium Rs {per_lot_outlay:,.0f} > max premium cap Rs {outlay_cap:,.0f}.")
    dte = chain.get("dte", 30)
    if dte < 5:
        w.append(f"Expiry {dte} days lo undi. Theta fast ga tintundi. Next expiry select cheyi.")
    iv, hv = leg.get("iv"), chain.get("hv20")
    if iv and hv and iv > 1.4 * hv:
        w.append(f"IV {iv}% vs stock HV20 {hv}%: premium costly. Move vachina IV crush avvachu.")
    if leg.get("spread_pct") and leg["spread_pct"] > 2:
        w.append(f"Bid-ask spread {leg['spread_pct']}%: illiquid. Limit order vaadu.")
    if leg.get("oi", 0) < 50 * lot:
        w.append(f"OI only {leg.get('oi', 0) // lot} lots: exit kastam avvachu.")
    if delta < 0.3:
        w.append(f"Far OTM (delta {delta:.2f}): lottery ticket. ATM/ITM better.")
    if theta_day and theta_day > 0.25 * prem_risk * max(qty, lot):
        w.append(f"Theta roju Rs {theta_day:,.0f}: SL risk lo {theta_day / (prem_risk * max(qty, lot)) * 100:.0f}%. "
                 "Stock 1-2 days lo move avvakapothe exit.")
    bias = row.get("bias")
    if (kind == "CE" and bias == "BEARISH") or (kind == "PE" and bias == "BULLISH"):
        w.append(f"Trend ki against: stock score {row.get('score')} ({bias}).")
    elif abs(row.get("score", 0)) < rules["min_score"]:
        w.append(f"Score {row.get('score')} weak (min {rules['min_score']}). Clear setup kaadu.")

    return {
        "symbol": leg["ts"], "underlying": row["symbol"], "opt_type": kind, "strike": strike,
        "expiry": chain["expiry"], "dte": dte, "side": "BUY",
        "entry": round(premium, 2), "sl": round(sl, 2), "t1": round(t1, 2), "t2": round(t2, 2),
        "prem_risk": round(prem_risk, 2), "prem_risk_pct": round(prem_risk / premium * 100, 1),
        "delta": leg.get("delta"), "theta": leg.get("theta"), "iv": iv, "hv20": hv,
        "spot": round(S, 2), "u_sl": round(u_sl, 2), "u_t1": round(u_t1, 2), "atr": round(a, 2),
        "lot_size": lot, "lots": lots, "qty": qty,
        "risk_amount": round(prem_risk * qty, 2), "risk_budget": round(budget, 2),
        "outlay": round(premium * qty, 2), "outlay_cap": round(outlay_cap, 2),
        "reward_t1": round(1.5 * prem_risk * qty, 2), "reward_t2": round(2.5 * prem_risk * qty, 2),
        "theta_day": round(theta_day, 0), "per_lot_risk": round(per_lot_risk, 0),
        "per_lot_outlay": round(per_lot_outlay, 0),
        "score": row.get("score"), "bias": bias, "warnings": w,
    }
