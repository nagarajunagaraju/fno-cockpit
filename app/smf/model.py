"""Phases 2-3: indicators and the transparent SMF score.

All rolling values use only rows up to and including that day (no look-ahead). Forward returns for the
backtest are computed separately in backtest.py.
"""
from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd
import yaml

from . import db

CFG_FILE = Path(__file__).with_name("config.yaml")
USER_CFG = db.OUT / "config_override.yaml"


def load_cfg() -> dict:
    cfg = yaml.safe_load(CFG_FILE.read_text())
    if USER_CFG.exists():
        over = yaml.safe_load(USER_CFG.read_text()) or {}
        for k, v in over.items():
            if isinstance(v, dict) and isinstance(cfg.get(k), dict):
                cfg[k].update(v)
            else:
                cfg[k] = v
    return cfg


# ------------------------------------------------------------------ load
def load_frame(c=None, since: str | None = None) -> pd.DataFrame:
    own = c is None
    c = c or db.conn()
    try:
        q = "SELECT * FROM equity" + (" WHERE d >= ?" if since else "")
        eq = pd.read_sql_query(q, c, params=(since,) if since else None)
        fo = pd.read_sql_query("SELECT * FROM fo" + (" WHERE d >= ?" if since else ""), c,
                               params=(since,) if since else None)
        ix = pd.read_sql_query("SELECT * FROM idx", c)
        n500 = db.meta_get(c, "n500", [])
    finally:
        if own:
            c.close()
    df = eq.merge(fo, on=["d", "symbol"], how="left").merge(ix, on="d", how="left")
    df["is_fno"] = df["fut_oi"].notna()
    ind = {r["symbol"]: r["industry"] for r in n500}
    df["in_n500"] = df["symbol"].isin(ind)
    df["sector"] = df["symbol"].map(ind).fillna("Other")
    return df.sort_values(["symbol", "d"]).reset_index(drop=True)


# ------------------------------------------------------------------ indicators
def _ema(s, n):
    return s.ewm(span=n, adjust=False, min_periods=n).mean()


def indicators(df: pd.DataFrame, cfg: dict) -> pd.DataFrame:
    r = cfg["rules"]
    df = df.copy()
    g = df.groupby("symbol", group_keys=False)
    sma = lambda col, n=20: g[col].transform(lambda s: s.rolling(n, min_periods=n).mean())  # noqa: E731

    df["vol_ratio"] = df["volume"] / sma("volume")
    df["deliv_ratio"] = df["deliv_qty"] / sma("deliv_qty")
    df["deliv_pct_vs_avg"] = df["deliv_per"] - sma("deliv_per")
    rng = (df["high"] - df["low"]).replace(0, np.nan)
    df["clv"] = (((df["close"] - df["low"]) - (df["high"] - df["close"])) / rng).fillna(0.0)
    prev_c = g["close"].shift(1)
    tr = pd.concat([df["high"] - df["low"], (df["high"] - prev_c).abs(), (df["low"] - prev_c).abs()], axis=1).max(axis=1)
    df["tr"] = tr
    df["atr14"] = df.groupby("symbol")["tr"].transform(lambda s: s.ewm(alpha=1 / 14, adjust=False, min_periods=14).mean())
    df["range_atr"] = (df["high"] - df["low"]) / df["atr14"]
    df["h20"] = g["high"].transform(lambda s: s.rolling(20, min_periods=20).max())
    df["l20"] = g["low"].transform(lambda s: s.rolling(20, min_periods=20).min())
    df["prev_h20"] = df.groupby("symbol")["h20"].shift(1)
    df["prev_l20"] = df.groupby("symbol")["l20"].shift(1)
    df["base_width"] = (df["h20"] - df["l20"]) / df["close"] * 100
    df["dist_from_20dh"] = (df["h20"] - df["close"]) / df["close"] * 100
    df["dist_from_20dl"] = (df["close"] - df["l20"]) / df["close"] * 100
    df["ema20"] = g["close"].transform(lambda s: _ema(s, 20))
    df["ema50"] = g["close"].transform(lambda s: _ema(s, 50))
    df["trend_up"] = (df["close"] > df["ema20"]) & (df["ema20"] > df["ema50"])
    df["trend_dn"] = (df["close"] < df["ema20"]) & (df["ema20"] < df["ema50"])
    ret20 = g["close"].transform(lambda s: s.pct_change(20)) * 100
    nifty = df.drop_duplicates("d").set_index("d")["nifty"].sort_index()
    n20 = (nifty.pct_change(20) * 100)
    df["rs"] = ret20 - df["d"].map(n20)
    df["price_chg_pct"] = g["close"].transform(lambda s: s.pct_change()) * 100
    df["oi_chg_pct"] = g["fut_oi"].transform(lambda s: s.pct_change(fill_method=None)) * 100
    df["oi_chg5_pct"] = g["fut_oi"].transform(lambda s: s.pct_change(5, fill_method=None)) * 100
    up, oiu = df["price_chg_pct"] > 0, df["oi_chg_pct"] > 0
    df["oi_buildup"] = np.select([up & oiu, ~up & oiu, up & ~oiu, ~up & ~oiu],
                                 ["LONG", "SHORT", "SHORT_COVER", "LONG_UNWIND"], default=None)
    df.loc[df["oi_chg_pct"].isna(), "oi_buildup"] = None
    big = df["deliv_ratio"] > r["deliv_ratio_day"]
    df["accum_day"] = (big & (df["clv"] > 0)).astype(int)
    df["distrib_day"] = (big & (df["clv"] < 0)).astype(int)
    g2 = df.groupby("symbol")
    df["accum_days_10"] = g2["accum_day"].transform(lambda s: s.rolling(10, min_periods=10).sum())
    df["distrib_days_10"] = g2["distrib_day"].transform(lambda s: s.rolling(10, min_periods=10).sum())
    absorb = (df["vol_ratio"] > r["absorption_vol"]) & (df["range_atr"] < r["absorption_range_atr"])
    df["absorption_day"] = (absorb & (df["clv"] > 0)).astype(int)
    df["supply_day"] = (absorb & (df["clv"] < 0)).astype(int)
    df["absorb_5"] = g2["absorption_day"].transform(lambda s: s.rolling(5, min_periods=1).sum())
    df["supply_5"] = g2["supply_day"].transform(lambda s: s.rolling(5, min_periods=1).sum())
    df["turnover20"] = g2["turnover_cr"].transform(lambda s: s.rolling(20, min_periods=10).mean())
    df = df.drop(columns=["tr", "trades", "prev_close", "fut_close", "near_exp", "fut_oi_chg"], errors="ignore")
    return df


# ------------------------------------------------------------------ score
def score(df: pd.DataFrame, cfg: dict) -> pd.DataFrame:
    """Adds bull/bear score (0-10, investment list rescaled to 10 because it has no OI point),
    phase, and per-point flags (used for the reason text)."""
    r, bw, sw = cfg["rules"], cfg["bull_weights"], cfg["bear_weights"]
    df = df.copy()
    f = {
        "b_accum": df["accum_days_10"] >= r["accum_min"],
        "b_deliv": (df["deliv_ratio"] >= 2.0) & (df["deliv_pct_vs_avg"] > 10),
        "b_close": (df["vol_ratio"] >= 1.5) & (df["clv"] >= 0.5),
        "b_absorb": df["absorb_5"] >= 1,
        "b_base": df["base_width"] <= r["tight_base_pct"],
        "b_near": df["dist_from_20dh"] <= r["near_pct"],
        "b_trend": df["trend_up"],
        "b_oi": (df["oi_buildup"] == "LONG") & (df["oi_chg5_pct"] > r["oi5_min_pct"]),
        "b_rs": df["rs"] > 0,
        "s_distrib": df["distrib_days_10"] >= r["accum_min"],
        "s_deliv": (df["deliv_ratio"] >= 2.0) & (df["deliv_pct_vs_avg"] > 10) & (df["clv"] < 0),
        "s_close": (df["vol_ratio"] >= 1.5) & (df["clv"] <= -0.5),
        "s_absorb": df["supply_5"] >= 1,
        "s_base": df["base_width"] <= r["tight_base_pct"],
        "s_near": df["dist_from_20dl"] <= r["near_pct"],
        "s_trend": df["trend_dn"],
        "s_oi": (df["oi_buildup"] == "SHORT") & (df["oi_chg5_pct"] > r["oi5_min_pct"]),
        "s_rs": df["rs"] < 0,
    }
    for k, v in f.items():
        df[k] = v.fillna(False).astype(bool)
    bkeys = [("b_accum", "accum_days"), ("b_deliv", "delivery_spike"), ("b_close", "strong_close"),
             ("b_absorb", "absorption"), ("b_base", "tight_base"), ("b_near", "near_breakout"),
             ("b_trend", "trend"), ("b_oi", "oi_long"), ("b_rs", "rs")]
    skeys = [("s_distrib", "distrib_days"), ("s_deliv", "delivery_spike"), ("s_close", "weak_close"),
             ("s_absorb", "absorption"), ("s_base", "tight_base"), ("s_near", "near_breakdown"),
             ("s_trend", "trend"), ("s_oi", "oi_short"), ("s_rs", "rs")]
    braw = sum(df[k] * bw[w] for k, w in bkeys)
    sraw = sum(df[k] * sw[w] for k, w in skeys)
    bmax_f, smax_f = sum(bw.values()), sum(sw.values())
    bmax_i, smax_i = bmax_f - bw["oi_long"], smax_f - sw["oi_short"]
    # F&O stocks: full scale incl. OI. Non-F&O (investment): no OI point possible -> rescale to 10.
    df["bull"] = np.where(df["is_fno"], braw / bmax_f * 10, (braw - df["b_oi"] * bw["oi_long"]) / bmax_i * 10).round(1)
    df["bear"] = np.where(df["is_fno"], sraw / smax_f * 10, (sraw - df["s_oi"] * sw["oi_short"]) / smax_i * 10).round(1)
    # warm-up rows (not enough history) get no score
    warm = df["ema50"].isna() | df["accum_days_10"].isna() | df["atr14"].isna() | df["deliv_ratio"].isna()
    df.loc[warm, ["bull", "bear"]] = np.nan
    markup = (df["close"] > df["prev_h20"]) & (df["vol_ratio"] > r["markup_vol"])
    markdown = (df["close"] < df["prev_l20"]) & (df["vol_ratio"] > r["markup_vol"])
    accum = df["b_base"] & df["b_accum"] & ~(df["close"] > df["prev_h20"])
    distrib = df["b_near"] & df["s_distrib"]
    df["phase"] = np.select([markup, markdown, accum, distrib], ["MARKUP", "MARKDOWN", "ACCUMULATION", "DISTRIBUTION"],
                            default="NEUTRAL")
    df.loc[warm, "phase"] = None
    return df


def reason(row, side: str) -> str:
    p = []
    if side == "BULL":
        if row["b_accum"]: p.append(f"Accum {int(row['accum_days_10'])}/10 days")
        if row["b_deliv"]: p.append(f"deliv {row['deliv_ratio']:.1f}x (+{row['deliv_pct_vs_avg']:.0f}% vs avg)")
        if row["b_close"]: p.append(f"vol {row['vol_ratio']:.1f}x strong close")
        if row["b_absorb"]: p.append("absorption day")
        if row["b_base"]: p.append(f"tight base {row['base_width']:.1f}%")
        if row["b_near"]: p.append(f"{row['dist_from_20dh']:.1f}% below 20D high")
        if row["b_trend"]: p.append("above EMA20>EMA50")
        if row["b_oi"]: p.append(f"LONG buildup OI5 {row['oi_chg5_pct']:+.0f}%")
        if row["b_rs"]: p.append(f"RS {row['rs']:+.1f}")
    else:
        if row["s_distrib"]: p.append(f"Distrib {int(row['distrib_days_10'])}/10 days")
        if row["s_deliv"]: p.append(f"deliv {row['deliv_ratio']:.1f}x on red")
        if row["s_close"]: p.append(f"vol {row['vol_ratio']:.1f}x weak close")
        if row["s_absorb"]: p.append("supply day")
        if row["s_base"]: p.append(f"tight base {row['base_width']:.1f}%")
        if row["s_near"]: p.append(f"{row['dist_from_20dl']:.1f}% above 20D low")
        if row["s_trend"]: p.append("below EMA20<EMA50")
        if row["s_oi"]: p.append(f"SHORT buildup OI5 {row['oi_chg5_pct']:+.0f}%")
        if row["s_rs"]: p.append(f"RS {row['rs']:+.1f}")
    return ", ".join(p) or "-"


def levels(row, side: str, cfg: dict) -> dict:
    """Entry above signal-day high (below low for bear); SL = base low or 1.5xATR, whichever is closer."""
    t, buf = cfg["trade"], cfg["trade"]["entry_buffer_pct"] / 100
    atr = row["atr14"]
    if side == "BULL":
        entry = row["high"] * (1 + buf)
        sl = max(row["l20"], entry - t["atr_sl_mult"] * atr)
        risk = entry - sl
        t1, t2 = entry + t["t1_r"] * risk, entry + t["t2_r"] * risk
    else:
        entry = row["low"] * (1 - buf)
        sl = min(row["h20"], entry + t["atr_sl_mult"] * atr)
        risk = sl - entry
        t1, t2 = entry - t["t1_r"] * risk, entry - t["t2_r"] * risk
    risk_amt = t["capital"] * t["risk_pct"] / 100
    qty = int(risk_amt // risk) if risk > 0 else 0
    out = {"entry": round(entry, 2), "sl": round(sl, 2), "t1": round(t1, 2), "t2": round(t2, 2),
           "risk_per_share": round(risk, 2), "risk_pct": round(risk / entry * 100, 2),
           "cash_qty": qty, "cash_value": round(qty * entry)}
    lot = row.get("lot")
    if lot and not pd.isna(lot) and risk > 0:
        out["lot"] = int(lot)
        out["fut_risk_per_lot"] = round(risk * lot)
        out["fut_lots"] = int(risk_amt // (risk * lot))
    return out
