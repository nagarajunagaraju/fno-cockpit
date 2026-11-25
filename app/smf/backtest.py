"""Phase 4: does a high SMF score actually lead to better forward returns?

Honesty rules built in:
* Forward returns use only later closes; scores use only data up to the signal day.
* Raw stock-days overlap (the same move is counted on many days), so the verdict uses DE-DUPLICATED
  events: the first day a stock reaches the threshold, then a 10-day cooldown.
* Returns are compared with the average of all stocks on the same day (removes market direction).
* Train = first part of history, Test = last 120 days. Verdict is from the TEST period only.
"""
from __future__ import annotations

import copy

import numpy as np
import pandas as pd

from . import model

HORIZONS = (1, 5, 10, 20)
BUCKETS = [("0-3", 0, 4), ("4-5", 4, 6), ("6-7", 6, 8), ("8-10", 8, 10.01)]


def forward(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy().sort_values(["list", "symbol", "d"])
    g = df.groupby(["list", "symbol"])
    for k in HORIZONS:
        df[f"f{k}"] = (g["close"].shift(-k) / df["close"] - 1) * 100
    hi = g["high"].transform(lambda s: s[::-1].rolling(10, min_periods=10).max()[::-1].shift(-1))
    lo = g["low"].transform(lambda s: s[::-1].rolling(10, min_periods=10).min()[::-1].shift(-1))
    df["mfe10"] = (hi / df["close"] - 1) * 100
    df["mae10"] = (lo / df["close"] - 1) * 100
    for k in HORIZONS:   # baseline: same-day average of the list
        df[f"x{k}"] = df[f"f{k}"] - df.groupby(["d", "list"])[f"f{k}"].transform("mean")
    return df


def _stats(sub: pd.DataFrame, sign: int) -> dict:
    out = {"n": int(len(sub))}
    for k in HORIZONS:
        r = sub[f"f{k}"].dropna() * sign
        if len(r) == 0:
            continue
        pos, neg = r[r > 0].sum(), -r[r < 0].sum()
        out[f"{k}d"] = {"hit": round((r > 0).mean() * 100, 1), "avg": round(r.mean(), 2),
                        "excess": round((sub[f"x{k}"].dropna() * sign).mean(), 2),
                        "pf": round(pos / neg, 2) if neg > 0 else None}
    if sign > 0:
        out["mfe10"], out["mae10"] = round(sub["mfe10"].mean(), 2), round(sub["mae10"].mean(), 2)
    else:
        out["mfe10"], out["mae10"] = round(-sub["mae10"].mean(), 2), round(-sub["mfe10"].mean(), 2)
    return out


def events(df: pd.DataFrame, col: str, thr: float, cooldown: int = 10) -> pd.DataFrame:
    """First day a stock's score reaches thr, then nothing for `cooldown` sessions."""
    keep = []
    for _, s in df[df[col] >= thr].groupby("symbol"):
        last = -10**9
        for i, row in zip(s["di"], s.index):
            if i - last >= cooldown:
                keep.append(row)
                last = i
    return df.loc[keep]


def trades(df: pd.DataFrame, ev: pd.DataFrame, side: str, cfg: dict) -> pd.DataFrame:
    """Entry next day beyond the signal-day high/low, SL/T1/T2 from model.levels, half at T1 then SL to
    entry, max hold N days. If SL and target fall in the same day, SL is assumed (conservative)."""
    t = cfg["trade"]
    by_sym = {s: g.reset_index(drop=True) for s, g in df.groupby("symbol")}
    pos = {s: {d: i for i, d in enumerate(g["d"])} for s, g in by_sym.items()}
    out = []
    bull = side == "BULL"
    for _, e in ev.iterrows():
        g = by_sym[e["symbol"]]
        i = pos[e["symbol"]][e["d"]]
        if i + 1 >= len(g):
            continue
        lv = model.levels(e, side, cfg)
        entry, sl, t1, t2 = lv["entry"], lv["sl"], lv["t1"], lv["t2"]
        risk = abs(entry - sl)
        if risk <= 0:
            continue
        n = g.iloc[i + 1]
        if (bull and n["high"] < entry) or (not bull and n["low"] > entry):
            continue   # never triggered
        fill = max(n["open"], entry) if bull else min(n["open"], entry)
        res, half, stop, exit_d = None, False, sl, None
        for j in range(i + 1, min(len(g), i + 1 + t["max_hold_days"])):
            b = g.iloc[j]
            hit_sl = b["low"] <= stop if bull else b["high"] >= stop
            hit_t1 = b["high"] >= t1 if bull else b["low"] <= t1
            hit_t2 = b["high"] >= t2 if bull else b["low"] <= t2
            if hit_sl:
                px = min(stop, b["open"]) if bull else max(stop, b["open"])
                leg = (px - fill) if bull else (fill - px)
                res = (0.5 * (t1 - fill if bull else fill - t1) + 0.5 * leg) if half else leg
                exit_d, why = b["d"], "SL" if not half else "BE after T1"
                break
            if not half and hit_t1:
                half, stop = True, fill
            if half and hit_t2:
                res = 0.5 * abs(t1 - fill) + 0.5 * abs(t2 - fill)
                exit_d, why = b["d"], "T2"
                break
        if res is None:
            last = g.iloc[min(len(g) - 1, i + t["max_hold_days"])]
            if min(len(g) - 1, i + t["max_hold_days"]) == len(g) - 1 and len(g) - 1 < i + t["max_hold_days"]:
                continue   # still open at end of data: not counted
            leg = (last["close"] - fill) if bull else (fill - last["close"])
            res = (0.5 * abs(t1 - fill) + 0.5 * leg) if half else leg
            exit_d, why = last["d"], "TIME"
        cost = t["cost_pct"] / 100 * fill
        out.append({"symbol": e["symbol"], "signal_d": e["d"], "entry_d": n["d"], "exit_d": exit_d,
                    "score": e["bull" if bull else "bear"], "r": round((res - cost) / risk, 3), "exit": why,
                    "sector": e["sector"]})
    return pd.DataFrame(out)


def trade_stats(tr: pd.DataFrame) -> dict:
    if tr.empty:
        return {"trades": 0}
    r = tr.sort_values("entry_d")["r"]
    eq = r.cumsum()
    dd = (eq - eq.cummax()).min()
    return {"trades": int(len(r)), "win_pct": round((r > 0).mean() * 100, 1), "avg_r": round(r.mean(), 3),
            "expectancy_r": round(r.mean(), 3), "total_r": round(r.sum(), 1), "max_dd_r": round(float(dd), 1),
            "best_r": round(r.max(), 2), "worst_r": round(r.min(), 2)}


KEEP = ["d", "symbol", "open", "high", "low", "close", "atr14", "l20", "h20", "lot", "sector", "is_fno",
        "in_n500", "turnover20", "bull", "bear", "phase", "vol_ratio", "deliv_ratio", "deliv_pct_vs_avg",
        "accum_days_10", "distrib_days_10", "base_width", "dist_from_20dh", "dist_from_20dl", "oi_chg5_pct", "rs",
        "b_accum", "b_deliv", "b_close", "b_absorb", "b_base", "b_near", "b_trend", "b_oi", "b_rs",
        "s_distrib", "s_deliv", "s_close", "s_absorb", "s_base", "s_near", "s_trend", "s_oi", "s_rs"]


def list_mask(df: pd.DataFrame, lst: str, cfg: dict):
    lc = cfg["lists"]
    if lst == "FNO":
        return df["is_fno"] & (df["turnover20"] >= lc["fno_min_turnover_cr"])
    return df["in_n500"] & (df["turnover20"] >= lc["inv_min_turnover_cr"])


def prepare(cfg: dict, df: pd.DataFrame | None = None, ind: pd.DataFrame | None = None,
            lst: str | None = None) -> pd.DataFrame:
    """Scores + forward returns. With lst only that list is kept (less memory)."""
    if ind is None:
        df = df if df is not None else model.load_frame()
        ind = model.indicators(df, cfg)
    lists = [lst] if lst else ["FNO", "INV"]
    parts = []
    for L in lists:
        sub = ind[list_mask(ind, L, cfg)]
        sc = model.score(sub, cfg)
        parts.append(sc[[k for k in KEEP if k in sc.columns]].assign(list=L))
    out = pd.concat(parts, ignore_index=True) if len(parts) > 1 else parts[0].reset_index(drop=True)
    days = sorted(ind["d"].unique())
    di = {d: i for i, d in enumerate(days)}
    out["di"] = out["d"].map(di)
    return forward(out).dropna(subset=["bull", "bear"])


def _months(sub, col, sign):
    sub = sub.assign(m=sub["d"].str[:7])
    res = []
    for m, g in sub.groupby("m"):
        top = g[g[col] >= 8]["x10"].dropna() * sign
        mid = g[(g[col] >= 4) & (g[col] < 6)]["x10"].dropna() * sign
        if len(top) >= 5 and len(mid) >= 5:
            res.append({"month": m, "top_8plus": round(top.mean(), 2), "mid_4_5": round(mid.mean(), 2),
                        "n_top": int(len(top)), "top_wins": bool(top.mean() > mid.mean())})
    return res


def _variant(cfg, kind):
    c = copy.deepcopy(cfg)
    if kind == "equal weights":
        for w in ("bull_weights", "bear_weights"):
            c[w] = {k: 1.0 for k in c[w]}
    elif kind == "no delivery points":
        c["bull_weights"].update(accum_days=0.0, delivery_spike=0.0, absorption=0.0)
        c["bear_weights"].update(distrib_days=0.0, delivery_spike=0.0, absorption=0.0)
    return c


def run(cfg: dict | None = None, df: pd.DataFrame | None = None, test_days: int = 120, progress=None) -> dict:
    cfg = cfg or model.load_cfg()
    raw = df if df is not None else model.load_frame()
    if progress: progress("computing indicators")
    ind = model.indicators(raw, cfg)
    del raw
    fcols = ind.select_dtypes("float64").columns
    ind[fcols] = ind[fcols].astype("float32")
    days = sorted(ind["d"].unique())
    if len(days) < 110:
        return {"ok": False, "error": f"only {len(days)} trading days stored: need at least 110 (run data update)"}
    split = days[-test_days] if len(days) > test_days + 90 else days[len(days) // 2]
    out = {"ok": True, "days": len(days), "from": days[0], "to": days[-1], "test_from": split,
           "lists": {}, "notes": [
               "Backtest is on the underlying (cash close). Option P&L will differ (premium, IV, theta).",
               "First ~50 days are warm-up (EMA50, 20-day averages) and are not scored.",
               "De-duplicated events = first day a stock reaches the score, then 10-day cooldown.",
               "'excess' = return minus the average of all stocks in the same list on the same day.",
               "Nifty 500 list is today's constituents (survivorship bias for the investment list); "
               "F&O list uses each day's actual F&O stocks."]}
    for lst in ("FNO", "INV"):
        if progress: progress(f"scoring {lst}")
        L = prepare(cfg, ind=ind, lst=lst)
        variants = {k: prepare(_variant(cfg, k), ind=ind, lst=lst)[["d", "symbol", "di", "bull", "bear", "x10", "f10",
                                                                      "list"]]
                    for k in ("equal weights", "no delivery points")}
        for side, col, sign in (("BULL", "bull", 1), ("BEAR", "bear", -1)):
            if progress: progress(f"backtest {lst} {side}")
            res = {}
            for per, sub in (("train", L[L["d"] < split]), ("test", L[L["d"] >= split])):
                b = {"baseline_all": _stats(sub, sign)}
                for name, lo, hi in BUCKETS:
                    b[name] = _stats(sub[(sub[col] >= lo) & (sub[col] < hi)], sign)
                ev, tr_rows = {}, {}
                for thr in (6, 8):
                    e = events(sub, col, thr)
                    ev[f">={thr}"] = _stats(e, sign)
                    tr_rows[f">={thr}"] = trade_stats(trades(L, e, side, cfg))
                res[per] = {"buckets_stock_days": b, "events": ev, "trade_rule": tr_rows}
            res["months"] = _months(L, col, sign)
            sens = {}
            for kind, S in (("base", L), *variants.items()):
                e = events(S[S["d"] >= split], col, 8)
                x = e["x10"].dropna() * sign
                f = e["f10"].dropna() * sign
                sens[kind] = {"events_8plus": int(len(e)), "excess_10d": round(float(x.mean()), 2) if len(x) else None,
                              "hit_10d": round(float((f > 0).mean() * 100), 1) if len(f) else None}
            res["sensitivity_test"] = sens
            res["verdict"] = verdict(res)
            out["lists"][f"{lst}_{side}"] = res
        del L, variants
    rec = [v["verdict"].get("min_score") for v in out["lists"].values() if v["verdict"].get("min_score")]
    out["recommended_min_score"] = min(rec) if rec else None
    return out


def verdict(res: dict) -> dict:
    t = res["test"]
    base_hit = (t["buckets_stock_days"]["baseline_all"].get("10d") or {}).get("hit")
    months = res["months"]
    mw = round(sum(m["top_wins"] for m in months) / len(months) * 100) if months else None
    best = None
    for thr in (">=8", ">=6"):
        e, tr = t["events"][thr], t["trade_rule"][thr]
        d10 = e.get("10d") or {}
        if e["n"] < 30:
            continue
        good = (d10.get("excess", -1) > 0.5 and d10.get("hit", 0) > (base_hit or 0)
                and tr.get("expectancy_r", -1) > 0 and (mw is None or mw >= 60))
        best = {"threshold": thr, "events": e["n"], "excess_10d": d10.get("excess"), "hit_10d": d10.get("hit"),
                "baseline_hit_10d": base_hit, "trade_expectancy_r": tr.get("expectancy_r"),
                "trade_win_pct": tr.get("win_pct"), "months_top_beats_mid_pct": mw}
        if good:
            best.update(label="EDGE LOOKS REAL", min_score=int(thr[2:]),
                        text="Test period lo high score stocks market kanna better ga vellayi. Inka 1 month paper trade.")
            return best
        if d10.get("excess", -1) > 0:
            best.update(label="WEAK / UNCLEAR", min_score=None,
                        text="Konchem better kani anni checks pass avvaledu. Real money ki ready kaadu.")
        else:
            best.update(label="NO EDGE", min_score=None,
                        text="High score stocks average kanna better ga vellaledu. Ee side use cheyakandi.")
        if thr == ">=8" and best["label"] != "NO EDGE":
            return best
    return best or {"label": "NOT ENOUGH DATA", "min_score": None,
                    "text": "Test period lo 30 kanna takkuva signals. Inka data kavali."}
