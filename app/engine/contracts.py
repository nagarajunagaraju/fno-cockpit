"""Automatic CE/PE contract selection.

Every strike around ATM is checked against liquidity, spread, delta, IV and
affordability rules. Eligible strikes are ranked with a transparent score and
the winner is explained against the runner-up. If nothing is eligible the trade
is rejected (no contract is invented).
"""
from __future__ import annotations

import math

from .. import options


def pick_expiry(expiries: list[str], cfg: dict) -> str | None:
    for e in expiries:
        if options.days_to_expiry(e) >= cfg["min_dte"]:
            return e
    return None


def _leg_eval(row, kind, chain, cfg, atm_iv, premium_cap):
    leg = row.get(kind)
    lot = chain["lot_size"]
    K = row["strike"]
    r = {"strike": K, "ts": leg.get("ts") if leg else None, "eligible": False, "rejected": [], "score": 0.0}
    if not leg:
        r["rejected"].append("contract not listed")
        return r
    r.update({"ltp": leg.get("ltp"), "bid": leg.get("bid"), "ask": leg.get("ask"), "oi": leg.get("oi"),
              "volume": leg.get("volume"), "iv": leg.get("iv"), "delta": leg.get("delta"),
              "gamma": leg.get("gamma"), "theta": leg.get("theta"), "vega": leg.get("vega"),
              "spread_pct": leg.get("spread_pct")})
    bid, ask = leg.get("bid") or 0, leg.get("ask") or 0
    if bid <= 0 or ask <= 0:
        r["rejected"].append("no two-sided quote (bid/ask missing)")
    sp = leg.get("spread_pct")
    if sp is None or sp > cfg["max_spread_pct"]:
        r["rejected"].append(f"spread {sp if sp is not None else 'n/a'}% > {cfg['max_spread_pct']}%")
    if (leg.get("oi") or 0) < cfg["min_oi_lots"] * lot:
        r["rejected"].append(f"OI {(leg.get('oi') or 0) // lot} lots < {cfg['min_oi_lots']} lots")
    if (leg.get("volume") or 0) < cfg["min_volume_lots"] * lot:
        r["rejected"].append(f"volume {(leg.get('volume') or 0) // lot} lots < {cfg['min_volume_lots']} lots")
    d = leg.get("delta")
    if d is None:
        r["rejected"].append("IV/delta could not be computed")
    elif not cfg["min_delta"] <= abs(d) <= cfg["max_delta"]:
        r["rejected"].append(f"|delta| {abs(d):.2f} outside {cfg['min_delta']}-{cfg['max_delta']}")
    iv = leg.get("iv")
    if iv and atm_iv and iv > atm_iv * (1 + cfg["max_iv_premium_pct"] / 100):
        r["rejected"].append(f"IV {iv}% too rich vs ATM IV {atm_iv}%")
    if ask > 0 and ask * lot > premium_cap:
        r["rejected"].append(f"1 lot premium Rs {ask * lot:,.0f} > cap Rs {premium_cap:,.0f}")
    if r["rejected"]:
        return r

    mid = (bid + ask) / 2
    delta_fit = max(0.0, 1 - abs(abs(d) - cfg["target_delta"]) / 0.3)
    spread_fit = max(0.0, 1 - sp / cfg["max_spread_pct"])
    liq = 0.5 * min(1.0, math.log10(max(1, leg["volume"] / lot)) / 4) + 0.5 * min(1.0, math.log10(max(1, leg["oi"] / lot)) / 4)
    iv_fit = 1.0 if not (iv and atm_iv) else max(0.0, 1 - max(0.0, (iv - atm_iv) / atm_iv) / (cfg["max_iv_premium_pct"] / 100))
    theta_pct = abs(leg.get("theta") or 0) / mid * 100 if mid else 100
    theta_fit = max(0.0, 1 - theta_pct / 5)
    comp = {"delta": 40 * delta_fit, "spread": 25 * spread_fit, "liquidity": 20 * liq,
            "iv": 10 * iv_fit, "theta": 5 * theta_fit}
    r.update(eligible=True, score=round(sum(comp.values()), 1),
             components={k: round(v, 1) for k, v in comp.items()},
             est_slippage=round((ask - bid) / 2, 2), theta_pct_day=round(theta_pct, 2), mid=round(mid, 2))
    return r


def select_contract(chain: dict, direction: str, cfg: dict) -> dict:
    kind = "CE" if direction == "BULL" else "PE"
    S = chain["spot"]
    rows = sorted(chain["strikes"], key=lambda r: abs(r["strike"] - S))[: 2 * cfg["strikes_each_side"] + 1]
    rows.sort(key=lambda r: r["strike"])
    premium_cap = cfg["capital"] * cfg["max_premium_pct"] / 100
    atm_iv = chain.get("atm_iv")
    evals = [_leg_eval(r, kind, chain, cfg, atm_iv, premium_cap) for r in rows]
    elig = sorted([e for e in evals if e["eligible"]], key=lambda e: -e["score"])
    res = {"type": kind, "expiry": chain["expiry"], "dte": chain.get("dte"), "atm": chain.get("atm"),
           "atm_iv": atm_iv, "spot": S, "candidates": evals, "selected": None, "why": []}
    if not elig:
        res["error"] = "No eligible contract: every strike failed liquidity / spread / delta / IV / premium rules"
        return res
    best = elig[0]
    res["selected"] = best
    atm = chain.get("atm")
    moneyness = "ATM" if best["strike"] == atm else (
        ("ITM" if best["strike"] < S else "OTM") if kind == "CE" else ("ITM" if best["strike"] > S else "OTM"))
    best["moneyness"] = moneyness
    why = [f"{best['strike']:g}{kind} ({moneyness}) ranked #1 of {len(elig)} eligible strikes "
           f"(score {best['score']}/100).",
           f"Delta {abs(best['delta']):.2f} vs target {cfg['target_delta']}; spread {best['spread_pct']}% "
           f"(est. slippage Rs {best['est_slippage']}/share); OI {best['oi'] // chain['lot_size']:,} lots, "
           f"volume {best['volume'] // chain['lot_size']:,} lots; IV {best['iv']}% (ATM {atm_iv}%); "
           f"theta {best['theta_pct_day']}% of premium per day."]
    if len(elig) > 1:
        ru = elig[1]
        diffs = []
        for k, lab in (("delta", "delta fit"), ("spread", "tighter spread"), ("liquidity", "better liquidity"),
                       ("iv", "cheaper IV"), ("theta", "lower theta burn")):
            if best["components"][k] - ru["components"][k] >= 1:
                diffs.append(lab)
        why.append(f"Better than {ru['strike']:g}{kind} (score {ru['score']}): "
                   + (", ".join(diffs) if diffs else "small overall edge across factors") + ".")
    rej = [e for e in evals if not e["eligible"]]
    if rej:
        why.append("Rejected: " + "; ".join(f"{e['strike']:g}{kind} - {e['rejected'][0]}" for e in rej[:4]) + ".")
    if best["strike"] != atm:
        why.append(f"ATM ({atm:g}) was not chosen automatically; it scored lower or failed a rule.")
    res["why"] = why
    return res
