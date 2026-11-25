"""SMF scanner tests on synthetic data (real NSE files are verified on the server)."""
import datetime as dt
import io
import os
import tempfile
import zipfile

os.environ.setdefault("COCKPIT_DATA_DIR", tempfile.mkdtemp())

import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402

from app.smf import backtest, collect, daily, db, model, nse  # noqa: E402

EQ_CSV = """SYMBOL, SERIES, DATE1, PREV_CLOSE, OPEN_PRICE, HIGH_PRICE, LOW_PRICE, LAST_PRICE, CLOSE_PRICE, AVG_PRICE, TTL_TRD_QNTY, TURNOVER_LACS, NO_OF_TRADES, DELIV_QTY, DELIV_PER
RELIANCE, EQ, 24-Nov-2026, 1500.00, 1505.00, 1520.00, 1498.00, 1515.00, 1516.10, 1510.2, 5000000, 75510.00, 120000, 2500000, 50.00
RELIANCE, BE, 24-Nov-2026, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, -, -
TINY, EQ, 24-Nov-2026, 10.00, 10.00, 10.50, 9.90, 10.2, 10.30, 10.2, 1000, 0.10, 10, -, -
"""

FO_CSV = ("TradDt,BizDt,Sgmt,Src,FinInstrmTp,FinInstrmId,ISIN,TckrSymb,SctySrs,XpryDt,FininstrmActlXpryDt,StrkPric,"
          "OptnTp,FinInstrmNm,OpnPric,HghPric,LwPric,ClsPric,LastPric,PrvsClsgPric,UndrlygPric,SttlmPric,OpnIntrst,"
          "ChngInOpnIntrst,TtlTradgVol,TtlTrfVal,TtlNbOfTxsExctd,SsnId,NewBrdLotQty,Rmks,Rsvd1,Rsvd2,Rsvd3,Rsvd4\n"
          "2026-11-24,2026-11-24,FO,NSE,STF,1,,RELIANCE,,2026-12-29,,,,X,1,1,1,1520.5,1,1,1,1,1000000,50000,1,1,1,F1,500,,,,,\n"
          "2026-11-24,2026-11-24,FO,NSE,STF,2,,RELIANCE,,2027-01-26,,,,X,1,1,1,1530.0,1,1,1,1,200000,10000,1,1,1,F1,500,,,,,\n"
          "2026-11-24,2026-11-24,FO,NSE,STO,3,,RELIANCE,,2026-12-29,,1500,CE,X,1,1,1,30,1,1,1,1,999999,1,1,1,1,F1,500,,,,,\n")


def _zip(text):
    b = io.BytesIO()
    with zipfile.ZipFile(b, "w") as z:
        z.writestr("BhavCopy_NSE_FO_0_0_0_20261124_F_0000.csv", text)
    return b.getvalue()


def test_parsers():
    eq = nse.parse_equity(EQ_CSV.encode())
    assert [r["symbol"] for r in eq] == ["RELIANCE", "TINY"]
    assert eq[0]["deliv_per"] == 50.0 and eq[0]["turnover_cr"] == 755.1 and eq[1]["deliv_qty"] is None
    fo = nse.parse_fo(_zip(FO_CSV))
    assert len(fo) == 1 and fo[0]["fut_oi"] == 1200000 and fo[0]["fut_oi_chg"] == 60000
    assert fo[0]["fut_close"] == 1520.5 and fo[0]["lot"] == 500           # near month, options ignored
    idx = "Index Name,Index Date,Open Index Value,High Index Value,Low Index Value,Closing Index Value\nNifty 50,24-11-2026,1,1,1,26100.5\n"
    assert nse.parse_index(idx.encode()) == 26100.5
    assert nse.parse_ban(b"Securities in Ban For Trade Date 25-NOV-2026:\n1,SAIL\n") == ["SAIL"]


# ------------------------------------------------------------------ synthetic market
def synth(n_days=260, n_sym=40, seed=7):
    """Random-walk stocks; half of them get a planted accumulation (quiet base, high delivery on green
    days) followed by a breakout, so a working score must rank them higher."""
    rng = np.random.default_rng(seed)
    days = pd.bdate_range("2025-10-01", periods=n_days).strftime("%Y-%m-%d").tolist()
    rows, nifty = [], 20000.0
    nif = {}
    for d in days:
        nifty *= 1 + rng.normal(0, 0.008)
        nif[d] = nifty
    for k in range(n_sym):
        sym = f"S{k:02d}"
        px, oi = 100 + k, 1e6
        planted = k % 2 == 0
        acc_start = set(range(60 + k * 3, n_days - 40, 70))
        phase = 0
        for i, d in enumerate(days):
            if planted and i in acc_start:
                phase = 25
            drift, vol, dq = 0.0, 0.015, 0.35
            if phase > 5:      # accumulation: tight, green-ish, heavy delivery
                drift, vol, dq = 0.001, 0.006, 0.7
            elif phase > 0:    # markup
                drift, vol, dq = 0.02, 0.02, 0.55
            phase = max(0, phase - 1)
            ret = rng.normal(drift, vol)
            o = px
            c = px * (1 + ret)
            h, lo = max(o, c) * (1 + abs(rng.normal(0, vol / 3))), min(o, c) * (1 - abs(rng.normal(0, vol / 3)))
            if phase > 5 and c < o:
                dq = 0.4
            v = rng.lognormal(13, 0.3) * (1.6 if phase > 0 else 1)
            oi *= 1 + (0.02 if phase > 0 and ret > 0 else rng.normal(0, 0.01))
            rows.append({"d": d, "symbol": sym, "open": o, "high": h, "low": lo, "close": c, "prev_close": px,
                         "volume": v, "turnover_cr": v * c / 1e7 * 60, "trades": 1000, "deliv_qty": v * dq,
                         "deliv_per": dq * 100, "fut_oi": oi if k < 30 else np.nan, "fut_oi_chg": 0.0,
                         "near_exp": "2026-12-29", "fut_close": c, "lot": 500 if k < 30 else np.nan,
                         "nifty": nif[d], "is_fno": k < 30, "in_n500": k >= 10, "sector": f"Sec{k % 4}"})
            px = c
    return pd.DataFrame(rows)


def test_indicators_no_lookahead_and_ranges():
    cfg = model.load_cfg()
    df = synth(120, 6)
    a = model.score(model.indicators(df, cfg), cfg)
    cut = df[df["d"] <= df["d"].unique()[80]]
    b = model.score(model.indicators(cut, cfg), cfg)
    m = a.merge(b, on=["d", "symbol"], suffixes=("", "_cut"))
    for col in ("vol_ratio", "deliv_ratio", "clv", "atr14", "ema50", "rs", "bull", "bear", "accum_days_10"):
        x, y = m[col], m[f"{col}_cut"]
        ok = (x.isna() & y.isna()) | (np.isclose(x, y, equal_nan=True))
        assert ok.all(), col           # adding future rows never changes past values
    s = a.dropna(subset=["bull"])
    assert s["clv"].between(-1, 1).all() and s["bull"].between(0, 10).all() and s["bear"].between(0, 10).all()
    assert set(s["phase"]) <= {"ACCUMULATION", "MARKUP", "DISTRIBUTION", "MARKDOWN", "NEUTRAL"}


def test_levels_and_reason():
    cfg = model.load_cfg()
    row = {"high": 105.0, "low": 100.0, "atr14": 2.0, "l20": 99.0, "h20": 106.0, "lot": 500}
    lv = model.levels(row, "BULL", cfg)
    assert abs(lv["entry"] - 105.105) < 0.01 and abs(lv["sl"] - 102.105) < 0.01   # 1.5xATR closer than base low
    assert lv["t1"] > lv["entry"] and lv["cash_qty"] == int(2400 // (105.105 - 102.105))
    assert lv["fut_lots"] == 1 and lv["fut_risk_per_lot"] == 1500


def test_backtest_runs_and_finds_planted_edge():
    cfg = model.load_cfg()
    res = backtest.run(cfg, synth(), test_days=100)
    assert res["ok"] and set(res["lists"]) == {"FNO_BULL", "FNO_BEAR", "INV_BULL", "INV_BEAR"}
    fb = res["lists"]["FNO_BULL"]
    for per in ("train", "test"):
        assert "8-10" in fb[per]["buckets_stock_days"] and ">=6" in fb[per]["events"]
    assert fb["verdict"]["label"] in ("EDGE LOOKS REAL", "WEAK / UNCLEAR", "NO EDGE", "NOT ENOUGH DATA")
    b = fb["train"]["buckets_stock_days"]
    # planted accumulation -> later markup: high bucket must beat low bucket on 10-day excess
    assert b["6-7"]["10d"]["excess"] > b["0-3"]["10d"]["excess"]
    assert set(fb["sensitivity_test"]) == {"base", "equal weights", "no delivery points"}


def test_daily_pipeline_tracker_paper_excel(tmp_path):
    cfg = model.load_cfg()
    df = synth(200, 20)
    c = db.conn()
    cols = ["d", "symbol", "open", "high", "low", "close", "prev_close", "volume", "turnover_cr", "trades",
            "deliv_qty", "deliv_per"]
    c.execute("DELETE FROM equity"); c.execute("DELETE FROM fo"); c.execute("DELETE FROM days")
    c.execute("DELETE FROM idx"); c.execute("DELETE FROM paper"); c.execute("DELETE FROM watch")
    c.executemany(f"INSERT INTO equity VALUES ({','.join('?' * len(cols))})", df[cols].values.tolist())
    fo = df[df["is_fno"]]
    c.executemany("INSERT INTO fo VALUES (?,?,?,?,?,?,?)",
                  fo[["d", "symbol", "fut_oi", "fut_oi_chg", "near_exp", "fut_close", "lot"]].values.tolist())
    c.executemany("INSERT INTO idx VALUES (?,?)", df.drop_duplicates("d")[["d", "nifty"]].values.tolist())
    c.executemany("INSERT INTO days VALUES (?,?,?,?,?,?)", [(d, "OK", 1, 1, "", "") for d in df["d"].unique()])
    db.meta_set(c, "n500", [{"symbol": f"S{k:02d}", "industry": f"Sec{k % 4}", "name": ""} for k in range(10, 20)])
    db.meta_set(c, "ban", {"list": ["S01"]})
    c.commit(); c.close()
    cfg["lists"]["min_score"] = 3
    rep, full = daily.build(cfg)
    assert rep["ok"] and rep["date"] == df["d"].max()
    assert all(r["symbol"] != "S01" for r in rep["lists"]["FNO_BULL"])           # banned excluded
    assert "INV_BEAR" not in rep["lists"]
    for r in rep["lists"]["FNO_BULL"]:
        assert r["entry"] > r["sl"] and r["reason"] != "-"
    # tracker + paper over consecutive days
    days = sorted(full["d"].unique())
    for d in days[-30:]:
        daily.track(full, d)
    daily.paper_add(rep)
    assert daily.paper_update(full, cfg) >= 0
    pr = daily.paper_report()
    assert "all_time" in pr and "by_sector" in pr
    path = daily.excel(rep)
    assert os.path.exists(path)
    txt = daily.telegram_text(rep, [{"text": "BREAKOUT X"}])
    assert "F&O BULLISH" in txt and "BREAKOUT X" in txt
    cnt = collect.counts()
    assert cnt["trading_days"] == 200


def test_weekdays_back():
    w = collect.weekdays_back(dt.date(2026, 11, 25), 10)
    assert all(d.weekday() < 5 for d in w) and w[0] == dt.date(2026, 11, 25)
