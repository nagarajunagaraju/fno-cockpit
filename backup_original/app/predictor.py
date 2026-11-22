"""Next-session research ranking and simple chronological historical evaluation.

Important: the score-derived confidence is NOT a calibrated probability. A true
probability requires out-of-sample training/calibration on licensed historical data.
"""
from __future__ import annotations

import math
from .analytics import ema, rsi, atr, pct


def _clamp(v: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, v))


def forecast_one(row: dict, index_chg: float = 0.0) -> dict:
    candles = row.get("candles") or []
    if len(candles) < 55:
        return {"symbol": row.get("symbol"), "signal": "NO TRADE", "score": 0,
                "confidence_estimate_pct": None, "reason": "Insufficient historical candles"}
    closes = [float(c["close"]) for c in candles]
    live = float(row.get("spot_ltp") or row.get("ltp") or closes[-1])
    series = closes + [live]
    e20, e50 = ema(series, 20), ema(series, 50)
    momentum5 = pct(live, closes[-6]) if len(closes) >= 6 else 0.0
    momentum20 = pct(live, closes[-21]) if len(closes) >= 21 else 0.0
    rel_strength = momentum5 - float(index_chg)
    r = rsi(series, 14)
    a = atr(candles, 14)
    atr_pct = (a / live * 100) if a and live else 0.0

    score = 0.0
    reasons = []
    if e20 and e50:
        if live > e20 > e50:
            score += 35; reasons.append("Spot above rising EMA structure")
        elif live < e20 < e50:
            score -= 35; reasons.append("Spot below falling EMA structure")
        elif live > e20:
            score += 12; reasons.append("Spot above EMA20")
        else:
            score -= 12; reasons.append("Spot below EMA20")
    score += _clamp(momentum5 * 7, -20, 20)
    score += _clamp(momentum20 * 2.5, -15, 15)
    score += _clamp(rel_strength * 5, -10, 10)
    if r is not None:
        if 55 <= r <= 72:
            score += 12; reasons.append(f"RSI {r:.1f}: positive momentum")
        elif 28 <= r <= 45:
            score -= 12; reasons.append(f"RSI {r:.1f}: negative momentum")
        elif r > 78 or r < 22:
            score *= 0.65; reasons.append(f"RSI {r:.1f}: extreme momentum; confidence reduced")
    # High ATR does not pick a side; it reduces confidence / increases risk.
    if atr_pct > 5:
        score *= 0.75; reasons.append("High ATR: volatility risk adjustment")
    score = int(round(_clamp(score, -100, 100)))
    if score >= 45:
        signal = "BULLISH"
    elif score <= -45:
        signal = "BEARISH"
    else:
        signal = "NO TRADE"
    # This is a transparent score-strength indicator, not a probability.
    confidence = int(round(_clamp(abs(score), 0, 100))) if signal != "NO TRADE" else int(round(100 - abs(score)))
    entry = round(live, 2)
    sl_dist = (a or live * 0.015) * 1.5
    if signal == "BULLISH":
        stop, target1, target2 = entry - sl_dist, entry + sl_dist * 1.5, entry + sl_dist * 2.5
    elif signal == "BEARISH":
        stop, target1, target2 = entry + sl_dist, entry - sl_dist * 1.5, entry - sl_dist * 2.5
    else:
        stop = target1 = target2 = None
    return {
        "symbol": row["symbol"], "signal": signal, "score": score,
        "confidence_estimate_pct": confidence,
        "confidence_label": "score strength only; NOT calibrated probability",
        "spot_ltp": round(live, 2), "momentum_5d_pct": round(momentum5, 2),
        "momentum_20d_pct": round(momentum20, 2), "rsi": round(r, 1) if r is not None else None,
        "atr": round(a, 2) if a else None, "atr_pct": round(atr_pct, 2),
        "entry_reference": entry, "stop_reference": round(stop, 2) if stop else None,
        "target1_reference": round(target1, 2) if target1 else None,
        "target2_reference": round(target2, 2) if target2 else None,
        "reasons": reasons or ["No sufficiently strong directional alignment"],
        "horizon": "next trading session; research signal only",
    }


def build_report(rows: list[dict], index_chg: float = 0.0) -> dict:
    results = [forecast_one(r, index_chg) for r in rows]
    bulls = sorted((x for x in results if x["signal"] == "BULLISH"), key=lambda x: x["score"], reverse=True)[:5]
    bears = sorted((x for x in results if x["signal"] == "BEARISH"), key=lambda x: x["score"])
    bears = bears[:5]
    return {
        "generated_at": __import__("datetime").datetime.now().astimezone().isoformat(),
        "universe_count": len(results), "bullish_count": sum(x["signal"] == "BULLISH" for x in results),
        "bearish_count": sum(x["signal"] == "BEARISH" for x in results),
        "no_trade_count": sum(x["signal"] == "NO TRADE" for x in results),
        "top_bullish": bulls, "top_bearish": bears,
        "all": sorted(results, key=lambda x: abs(x["score"]), reverse=True),
        "disclaimer": "Research ranking only. Score strength is not a calibrated probability or a guarantee. Confirm live price, option liquidity, spread, IV and risk before any manual trade.",
    }


def walk_forward_backtest(rows: list[dict], min_history: int = 60) -> dict:
    """Simple one-day direction test using only candles available before each label."""
    observations = []
    for row in rows:
        cs = row.get("candles") or []
        closes = [float(c["close"]) for c in cs]
        if len(closes) < min_history + 2:
            continue
        for i in range(min_history, len(closes) - 1):
            hist = [{"close": v, "high": float(cs[j]["high"]), "low": float(cs[j]["low"]), "open": float(cs[j].get("open", v))}
                    for j, v in enumerate(closes[:i])]
            # Reuse same rule with the current historical close as the pseudo-live price.
            sample = {"symbol": row["symbol"], "candles": hist,
                      "spot_ltp": closes[i], "ltp": closes[i]}
            pred = forecast_one(sample, 0.0)
            next_ret = pct(closes[i + 1], closes[i])
            if pred["signal"] == "NO TRADE":
                continue
            correct = (pred["signal"] == "BULLISH" and next_ret > 0) or (pred["signal"] == "BEARISH" and next_ret < 0)
            observations.append({"correct": correct, "return_pct": next_ret,
                                 "signal": pred["signal"], "symbol": row["symbol"]})
    n = len(observations)
    if not n:
        return {"samples": 0, "message": "Not enough historical candles for the selected minimum history."}
    wins = sum(x["correct"] for x in observations)
    avg_signed = sum((x["return_pct"] if x["signal"] == "BULLISH" else -x["return_pct"]) for x in observations) / n
    return {"samples": n, "directional_hit_rate_pct": round(wins / n * 100, 2),
            "mean_next_day_signed_return_pct": round(avg_signed, 4),
            "method": "Chronological one-step walk-forward on available daily candles; no fees/slippage; not a calibrated live probability.",
            "warning": "This diagnostic is not a substitute for a full multi-year, corporate-action-aware, transaction-cost-inclusive validation."}
