"""Approximate statutory charges for an options BUY then SELL (paper trading / risk)."""
from __future__ import annotations


def option_round_trip(buy_price: float, sell_price: float, qty: int, cfg: dict, orders: int = 2) -> dict:
    buy_val = buy_price * qty
    sell_val = sell_price * qty
    brokerage = cfg["brokerage_per_order"] * orders
    stt = sell_val * cfg["stt_sell_pct"] / 100
    txn = (buy_val + sell_val) * cfg["exchange_txn_pct"] / 100
    sebi = (buy_val + sell_val) * cfg["sebi_per_crore"] / 1e7
    stamp = buy_val * cfg["stamp_buy_pct"] / 100
    gst = (brokerage + txn + sebi) * cfg["gst_pct"] / 100
    total = brokerage + stt + txn + sebi + stamp + gst
    return {"brokerage": round(brokerage, 2), "stt": round(stt, 2), "exchange": round(txn, 2),
            "sebi": round(sebi, 2), "stamp": round(stamp, 2), "gst": round(gst, 2), "total": round(total, 2)}
