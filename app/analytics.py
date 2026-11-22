"""Indicators, OI build-up classification and the direction score."""
from __future__ import annotations


def ema(values: list[float], n: int) -> float | None:
    if len(values) < n:
        return None
    k = 2 / (n + 1)
    e = sum(values[:n]) / n
    for v in values[n:]:
        e = v * k + e * (1 - k)
    return e


def atr(candles: list[dict], n: int = 14) -> float | None:
    """Wilder ATR on daily candles (dicts with high/low/close)."""
    if len(candles) < n + 1:
        return None
    trs = []
    for prev, c in zip(candles[:-1], candles[1:]):
        trs.append(max(c["high"] - c["low"], abs(c["high"] - prev["close"]), abs(c["low"] - prev["close"])))
    a = sum(trs[:n]) / n
    for tr in trs[n:]:
        a = (a * (n - 1) + tr) / n
    return a


def rsi(closes: list[float], n: int = 14) -> float | None:
    if len(closes) < n + 1:
        return None
    gains, losses = 0.0, 0.0
    for a, b in zip(closes[:n], closes[1:n + 1]):
        d = b - a
        gains += max(d, 0)
        losses += max(-d, 0)
    ag, al = gains / n, losses / n
    for a, b in zip(closes[n:-1], closes[n + 1:]):
        d = b - a
        ag = (ag * (n - 1) + max(d, 0)) / n
        al = (al * (n - 1) + max(-d, 0)) / n
    if al == 0:
        return 100.0
    return 100 - 100 / (1 + ag / al)


def pct(a: float, b: float) -> float:
    return (a - b) / b * 100 if b else 0.0


# Build-up needs a real move in both price and OI, otherwise it is noise.
MIN_PRICE_MOVE = 0.3   # %
MIN_OI_MOVE = 1.0      # %


def buildup(price_chg: float, oi_chg: float) -> str:
    if abs(price_chg) < MIN_PRICE_MOVE or abs(oi_chg) < MIN_OI_MOVE:
        return "Neutral"
    if price_chg > 0 and oi_chg > 0:
        return "Long Buildup"
    if price_chg < 0 and oi_chg > 0:
        return "Short Buildup"
    if price_chg > 0 and oi_chg < 0:
        return "Short Covering"
    return "Long Unwinding"


BUILDUP_POINTS = {
    "Long Buildup": 30,
    "Short Covering": 12,
    "Neutral": 0,
    "Long Unwinding": -12,
    "Short Buildup": -30,
}


def analyse(row: dict, index_chg: float) -> dict:
    """row: symbol, lot_size, ltp, prev_close, oi, prev_oi, volume, candles (completed daily)."""
    # Keep the technical-price series internally consistent: historical cash
    # candles are compared with the live cash/spot quote, never a futures LTP.
    spot_ltp = float(row.get("spot_ltp") or row["ltp"])
    fut_ltp = float(row.get("fut_ltp") or row["ltp"])
    closes = [c["close"] for c in row["candles"]] + [spot_ltp]
    ltp = spot_ltp
    spot_chg = pct(spot_ltp, row["prev_close"])
    fut_prev_close = float(row.get("fut_prev_close") or row["prev_close"])
    fut_chg = pct(fut_ltp, fut_prev_close)
    oi_chg = pct(row["oi"], row["prev_oi"]) if row.get("prev_oi") else 0.0
    bu = buildup(fut_chg, oi_chg)
    e20, e50 = ema(closes, 20), ema(closes, 50)
    r = rsi(closes, 14)
    a = atr(row["candles"], 14)

    score = 0.0
    reasons = []
    # 1. Trend (daily EMAs) : +-40
    if e20 and e50:
        if ltp > e20 and e20 > e50:
            score += 40; reasons.append("Uptrend: price > EMA20 > EMA50")
        elif ltp < e20 and e20 < e50:
            score -= 40; reasons.append("Downtrend: price < EMA20 < EMA50")
        elif ltp > e20:
            score += 15; reasons.append("Above EMA20, trend not confirmed")
        else:
            score -= 15; reasons.append("Below EMA20, trend not confirmed")
    # 2. OI build-up : +-30
    score += BUILDUP_POINTS[bu]
    if bu != "Neutral":
        reasons.append(f"{bu} (price {fut_chg:+.2f}%, OI {oi_chg:+.2f}%)")
    # 3. Relative strength vs NIFTY : +-15
    rs = max(-2.0, min(2.0, spot_chg - index_chg))
    score += rs * 7.5
    if abs(rs) >= 1:
        reasons.append(f"{'Stronger' if rs > 0 else 'Weaker'} than NIFTY by {abs(spot_chg - index_chg):.1f}%")
    # 4. Momentum (RSI) : +-15
    if r is not None:
        if r >= 60:
            score += 15; reasons.append(f"RSI {r:.0f} bullish momentum")
        elif r <= 40:
            score -= 15; reasons.append(f"RSI {r:.0f} bearish momentum")

    score = max(-100, min(100, round(score)))
    if score >= 45:
        bias = "BULLISH"
    elif score <= -45:
        bias = "BEARISH"
    else:
        bias = "NO TRADE"

    return {
        "symbol": row["symbol"],
        "lot_size": row["lot_size"],
        "ltp": round(ltp, 2),
        "fut_ltp": round(fut_ltp, 2),
        "chg": round(spot_chg, 2),
        "fut_chg": round(fut_chg, 2),
        "oi_chg": round(oi_chg, 2),
        "oi": row["oi"],
        "volume": row.get("volume", 0),
        "buildup": bu,
        "ema20": round(e20, 2) if e20 else None,
        "ema50": round(e50, 2) if e50 else None,
        "rsi": round(r, 1) if r is not None else None,
        "atr": round(a, 2) if a else None,
        "score": score,
        "bias": bias,
        "reasons": reasons,
    }
