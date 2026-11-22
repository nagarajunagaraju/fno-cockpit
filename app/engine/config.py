"""All engine thresholds in one place. Every value can be changed from the
Risk/Settings screen (stored in SQLite); nothing here is hard-wired in logic."""
from __future__ import annotations

import datetime as dt

IST = dt.timezone(dt.timedelta(hours=5, minutes=30))

DEFAULTS: dict = {
    # ---- session timing (IST) -------------------------------------------
    "market_open": "09:15",
    "market_close": "15:30",
    "opening_range_minutes": 10,      # no entry trigger before 09:25 (first 10 min = opening range)
    "last_entry_time": "14:45",       # no new signals after this
    "square_off_time": "15:15",       # paper positions closed at this time

    # ---- layer 1: market / sector ----------------------------------------
    "index_conflict_pct": 0.5,        # NIFTY moving this much against the trade = conflict
    "reject_on_index_conflict": True,  # both NIFTY & BANKNIFTY against -> reject
    "sector_min_rs_pct": 0.0,         # sector change minus NIFTY change must favour direction

    # ---- layer 2: price action / volume -------------------------------------
    "rvol_strong": 2.0,               # RVOL >= this -> 2 points
    "rvol_ok": 1.5,                   # RVOL >= this -> 1 point
    "rvol_lookback_sessions": 10,
    "open_eq_tolerance_pct": 0.10,    # Open = Low / Open = High tolerance
    "chg_min_pct": 1.0,               # preferred move range
    "chg_max_pct": 2.8,
    "chase_limit_pct": 3.5,           # beyond this: reject (no chasing)
    "require_vwap_side": True,        # mandatory: bull above VWAP, bear below VWAP
    "circuit_buffer_pct": 0.5,        # reject when price this close to circuit limit

    # ---- layer 3: derivatives ---------------------------------------------
    "min_price_move_pct": 0.3,        # build-up needs a real move
    "min_oi_move_pct": 1.0,
    "require_futures_oi": True,       # mandatory: futures OI data must exist
    "require_option_chain": True,     # mandatory: option chain data must exist
    "option_check_min_prescore": 4,   # only fetch option OI for stocks with >= this pre-score
    "option_oi_strikes_each_side": 2, # near-ATM strikes used for OI confirmation
    "option_refresh_seconds": 60,

    # ---- layer 4: scoring --------------------------------------------------
    "weights": {"rvol": 2, "fut_oi": 2, "option_chain": 2, "open_eq": 1,
                "vwap_pdh": 1, "sector": 1, "momentum": 1},
    "min_score": 7,
    "watch_min_score": 5,

    # ---- contract selection ------------------------------------------------
    "min_dte": 5,                     # skip expiry closer than this (days)
    "strikes_each_side": 4,           # evaluate ATM +/- this many strikes
    "target_delta": 0.55,
    "min_delta": 0.35,
    "max_delta": 0.80,
    "max_spread_pct": 3.0,
    "min_oi_lots": 50,
    "min_volume_lots": 20,
    "max_iv_premium_pct": 15.0,       # contract IV may exceed ATM IV by at most this %
    "max_iv_vs_ref": 1.5,             # ATM IV / reference IV above this = abnormal IV -> reject

    # ---- trade plan ---------------------------------------------------------
    "min_u_sl_pct": 0.35,             # underlying stop distance clamp
    "max_u_sl_pct": 1.5,
    "t1_r": 1.5,                      # targets in multiples of underlying risk
    "t2_r": 2.5,
    "sl_reprice_minutes": 30,         # premium SL = contract repriced at stop level after this time
    "target_reprice_minutes": 60,
    "min_premium_sl_pct": 8.0,        # premium SL never tighter than this (noise)
    "max_premium_sl_pct": 45.0,       # premium SL wider than this -> reject
    "min_rr": 1.3,                    # premium reward:risk to T1
    "entry_slippage_pct": 1.0,        # entry range upper bound above ask

    # ---- risk (paper by default) --------------------------------------------
    "capital": 300000,
    "risk_per_trade_pct": 1.0,
    "max_premium_pct": 10.0,
    "max_daily_loss_pct": 2.0,
    "max_open_positions": 2,
    "max_exposure_pct": 25.0,
    "max_signals_per_symbol_day": 2,
    "resignal_cooldown_min": 30,

    # ---- paper trading --------------------------------------------------------
    "fill_window_minutes": 3,         # entry must fill within this time else EXPIRED (missed fill)
    "paper_slippage_pct": 0.5,
    "t1_book_fraction": 0.5,

    # ---- charges (approx. Zerodha options, editable) ----------------------------
    "brokerage_per_order": 20.0,
    "stt_sell_pct": 0.1,
    "exchange_txn_pct": 0.03503,
    "sebi_per_crore": 10.0,
    "stamp_buy_pct": 0.003,
    "gst_pct": 18.0,

    # ---- data health ------------------------------------------------------------
    "stale_seconds": 20,
    "scan_interval_seconds": 5,
}

MANDATORY = ["data_fresh", "direction", "vwap_side", "not_chasing", "index_conflict",
             "circuit", "futures_oi_data", "option_chain_data", "min_score"]


def merged(saved: dict | None) -> dict:
    cfg = {k: (dict(v) if isinstance(v, dict) else v) for k, v in DEFAULTS.items()}
    for k, v in (saved or {}).items():
        if k not in DEFAULTS:
            continue
        if isinstance(DEFAULTS[k], dict) and isinstance(v, dict):
            cfg[k].update({kk: float(vv) for kk, vv in v.items() if kk in DEFAULTS[k]})
        elif isinstance(DEFAULTS[k], bool):
            cfg[k] = v if isinstance(v, bool) else str(v).lower() in ("1", "true", "yes")
        elif isinstance(DEFAULTS[k], int) and not isinstance(DEFAULTS[k], bool):
            cfg[k] = int(float(v))
        elif isinstance(DEFAULTS[k], float):
            cfg[k] = float(v)
        else:
            cfg[k] = v
    return cfg


def hm(s: str) -> dt.time:
    h, m = s.split(":")
    return dt.time(int(h), int(m))


def minute_index(t: dt.datetime | dt.time, cfg: dict) -> int:
    """Minutes since market open (09:15 -> 0)."""
    o = hm(cfg["market_open"])
    tt = t.time() if isinstance(t, dt.datetime) else t
    return (tt.hour * 60 + tt.minute) - (o.hour * 60 + o.minute)
