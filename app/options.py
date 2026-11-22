"""Black-Scholes pricing, implied volatility, Greeks and option-chain analytics."""
from __future__ import annotations

import datetime as dt
import math

RISK_FREE = 0.065  # approx Indian risk-free rate
IST = dt.timezone(dt.timedelta(hours=5, minutes=30))


def _ncdf(x):
    return 0.5 * (1 + math.erf(x / math.sqrt(2)))


def _npdf(x):
    return math.exp(-x * x / 2) / math.sqrt(2 * math.pi)


def years_to_expiry(expiry: str, now: dt.datetime | None = None) -> float:
    """Expiry at 15:30 IST on the expiry date."""
    now = now or dt.datetime.now(IST)
    exp = dt.datetime.fromisoformat(expiry).replace(hour=15, minute=30, tzinfo=IST)
    return max((exp - now).total_seconds(), 600) / (365 * 86400)


def days_to_expiry(expiry: str) -> float:
    return round(years_to_expiry(expiry) * 365, 1)


def bs_price(S, K, T, sigma, kind, r=RISK_FREE):
    if sigma <= 0 or T <= 0:
        return max(0.0, S - K) if kind == "CE" else max(0.0, K - S)
    d1 = (math.log(S / K) + (r + sigma * sigma / 2) * T) / (sigma * math.sqrt(T))
    d2 = d1 - sigma * math.sqrt(T)
    if kind == "CE":
        return S * _ncdf(d1) - K * math.exp(-r * T) * _ncdf(d2)
    return K * math.exp(-r * T) * _ncdf(-d2) - S * _ncdf(-d1)


def implied_vol(price, S, K, T, kind, r=RISK_FREE):
    """Bisection IV; None if price is below intrinsic or not solvable."""
    intrinsic = max(0.0, S - K) if kind == "CE" else max(0.0, K - S)
    if price is None or price <= intrinsic + 0.01:
        return None
    lo, hi = 0.01, 3.0
    for _ in range(60):
        mid = (lo + hi) / 2
        if bs_price(S, K, T, mid, kind, r) > price:
            hi = mid
        else:
            lo = mid
    return (lo + hi) / 2


def greeks(S, K, T, sigma, kind, r=RISK_FREE):
    if not sigma or T <= 0:
        return {"delta": None, "theta": None, "vega": None, "gamma": None}
    sq = math.sqrt(T)
    d1 = (math.log(S / K) + (r + sigma * sigma / 2) * T) / (sigma * sq)
    d2 = d1 - sigma * sq
    if kind == "CE":
        delta = _ncdf(d1)
        theta = -S * _npdf(d1) * sigma / (2 * sq) - r * K * math.exp(-r * T) * _ncdf(d2)
    else:
        delta = _ncdf(d1) - 1
        theta = -S * _npdf(d1) * sigma / (2 * sq) + r * K * math.exp(-r * T) * _ncdf(-d2)
    return {
        "delta": round(delta, 3),
        "theta": round(theta / 365, 2),        # Rs per share per calendar day
        "vega": round(S * _npdf(d1) * sq / 100, 2),  # Rs per 1 IV point
        "gamma": round(_npdf(d1) / (S * sigma * sq), 5),
    }


def hist_vol(closes: list[float], n: int = 20) -> float | None:
    if len(closes) < n + 1:
        return None
    rets = [math.log(b / a) for a, b in zip(closes[-n - 1:-1], closes[-n:])]
    m = sum(rets) / n
    var = sum((x - m) ** 2 for x in rets) / (n - 1)
    return math.sqrt(var * 252)


def mid(leg):
    b, a = leg.get("bid") or 0, leg.get("ask") or 0
    if b > 0 and a > 0:
        return (a + b) / 2
    return leg.get("ltp") or 0


def enrich_chain(chain: dict, hv: float | None) -> dict:
    """Adds IV/Greeks per leg + PCR, max pain, OI walls, ATM IV."""
    S, T = chain["spot"], years_to_expiry(chain["expiry"])
    strikes = chain["strikes"]
    for row in strikes:
        K = row["strike"]
        for kind in ("CE", "PE"):
            leg = row.get(kind)
            if not leg:
                continue
            iv = implied_vol(mid(leg), S, K, T, kind)
            leg["iv"] = round(iv * 100, 1) if iv else None
            leg.update(greeks(S, K, T, iv, kind) if iv else {"delta": None, "theta": None, "vega": None, "gamma": None})
            leg["oi_chg"] = round((leg["oi"] - leg["prev_oi"]) / leg["prev_oi"] * 100, 1) if leg.get("prev_oi") else None
            leg["spread_pct"] = round((leg["ask"] - leg["bid"]) / mid(leg) * 100, 1) if leg.get("bid") and leg.get("ask") and mid(leg) else None

    ce_oi = sum(r["CE"]["oi"] for r in strikes if r.get("CE"))
    pe_oi = sum(r["PE"]["oi"] for r in strikes if r.get("PE"))
    atm = min(strikes, key=lambda r: abs(r["strike"] - S))
    atm_ivs = [atm[k]["iv"] for k in ("CE", "PE") if atm.get(k) and atm[k].get("iv")]
    atm_iv = round(sum(atm_ivs) / len(atm_ivs), 1) if atm_ivs else None

    # max pain: strike where option writers pay the least
    def pain(x):
        return sum((max(0, x - r["strike"]) * r["CE"]["oi"] if r.get("CE") else 0) +
                   (max(0, r["strike"] - x) * r["PE"]["oi"] if r.get("PE") else 0) for r in strikes)
    max_pain = min((r["strike"] for r in strikes), key=pain)
    ce_wall = max((r for r in strikes if r.get("CE")), key=lambda r: r["CE"]["oi"])["strike"]
    pe_wall = max((r for r in strikes if r.get("PE")), key=lambda r: r["PE"]["oi"])["strike"]

    chain.update({
        "atm": atm["strike"], "atm_iv": atm_iv, "hv20": round(hv * 100, 1) if hv else None,
        "pcr": round(pe_oi / ce_oi, 2) if ce_oi else None, "max_pain": max_pain,
        "resistance": ce_wall, "support": pe_wall, "dte": days_to_expiry(chain["expiry"]),
    })
    return chain


def suggest_strike(chain: dict, kind: str) -> dict | None:
    """For buying: the liquid strike with |delta| closest to 0.55 (ATM to slightly ITM)."""
    best, best_d = None, 9
    for row in chain["strikes"]:
        leg = row.get(kind)
        if not leg or leg.get("delta") is None or not leg.get("ltp"):
            continue
        if leg.get("spread_pct") is not None and leg["spread_pct"] > 5:
            continue
        d = abs(abs(leg["delta"]) - 0.55)
        if d < best_d:
            best, best_d = row, d
    return best
