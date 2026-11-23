"""NIFTY market regime. Signals against the regime are blocked.

Index has no volume, so "above VWAP" is approximated by "above the day's open".
"""
from __future__ import annotations


def _pct(a, b):
    return (a - b) / b * 100 if (a is not None and b) else None


def detect(market: dict, cfg: dict) -> dict:
    n = market.get("nifty") or {}
    ltp, pc = n.get("ltp"), n.get("prev_close")
    chg = _pct(ltp, pc)
    out = {"regime": "UNKNOWN", "allows": None, "points": 0, "min_score_add": 0, "chg": chg,
           "or_high": n.get("or_high"), "or_low": n.get("or_low"), "note": ""}
    if chg is None:
        out["note"] = "NIFTY data unavailable"
        return out
    vix = market.get("vix") or {}
    vchg = _pct(vix.get("ltp"), vix.get("prev_close"))
    out["vix_chg"] = vchg
    if vchg is not None and vchg >= cfg["vix_spike_pct"]:
        out.update(regime="HIGH_VOLATILITY", note=f"India VIX {vchg:+.1f}% today")
        return out
    orh, orl, day_open = n.get("or_high"), n.get("or_low"), n.get("day_open")
    inside_or = orh is not None and orl is not None and orl <= ltp <= orh
    br = market.get("breadth") or {}
    adv, dec = br.get("adv") or 0, br.get("dec") or 0
    if not br:   # breadth unknown (e.g. backtest): do not let it block
        ratio_up = ratio_dn = 9.9
    else:
        ratio_up = adv / dec if dec else (9.9 if adv else 1.0)
        ratio_dn = dec / adv if adv else (9.9 if dec else 1.0)
    side = cfg["regime_sideways_pct"]
    strong = cfg["regime_strong_pct"]
    if abs(chg) < side and (inside_or or orh is None):
        out.update(regime="SIDEWAYS", note=f"NIFTY {chg:+.2f}% inside opening range")
    elif chg >= strong and (orh is None or ltp > orh) and ratio_up >= 1.5:
        out.update(regime="STRONG_BULLISH", allows="CE", points=10)
    elif chg <= -strong and (orl is None or ltp < orl) and ratio_dn >= 1.5:
        out.update(regime="STRONG_BEARISH", allows="PE", points=10)
    elif chg > 0 and (day_open is None or ltp > day_open):
        out.update(regime="WEAK_BULLISH", allows="CE", points=5, min_score_add=5)
    elif chg < 0 and (day_open is None or ltp < day_open):
        out.update(regime="WEAK_BEARISH", allows="PE", points=5, min_score_add=5)
    else:
        out.update(regime="SIDEWAYS", note="NIFTY direction unclear")
    out["note"] = out["note"] or f"NIFTY {chg:+.2f}%, breadth {adv}:{dec}"
    return out
