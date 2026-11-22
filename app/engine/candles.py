"""1-minute candle building from ticks + intraday helpers (VWAP, opening range, RVOL).

Only *completed* minute bars are used by the signal engine, so a signal can
never depend on a candle that is still forming.
"""
from __future__ import annotations

import datetime as dt
import threading

from .config import IST, minute_index


class BarBuilder:
    """Turns a stream of (timestamp, ltp, cumulative_volume, oi) into 1-min bars."""

    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.lock = threading.Lock()
        self.bars: list[dict] = []          # completed bars, oldest first
        self.cur: dict | None = None        # forming bar
        self.cum_at_bar_start = None
        self.last_cum = None
        self.last_oi = None
        self.last_ts: dt.datetime | None = None

    def set_backfill(self, bars: list[dict]):
        with self.lock:
            self.bars = sorted(bars, key=lambda b: b["m"])
            if self.bars:
                self.last_cum = sum(b["v"] for b in self.bars)

    def on_tick(self, ts: dt.datetime, ltp: float, cum_volume: float | None = None, oi: float | None = None):
        if ltp is None or ltp <= 0:
            return
        ts = ts.astimezone(IST)
        m = minute_index(ts, self.cfg)
        if m < 0:
            return
        with self.lock:
            self._roll(m)
            if self.cur is None:
                if self.last_cum is not None:
                    start_cum = self.last_cum
                elif m == 0:      # first minute: include the opening (pre-open) volume like exchange candles do
                    start_cum = 0.0
                else:             # started mid-day without backfill: volume of this bar only from now on
                    start_cum = cum_volume
                self.cur = {"m": m, "t": ts.strftime("%H:%M"), "o": ltp, "h": ltp, "l": ltp, "c": ltp, "v": 0.0, "oi": oi}
                self.cum_at_bar_start = start_cum
            c = self.cur
            c["h"] = max(c["h"], ltp)
            c["l"] = min(c["l"], ltp)
            c["c"] = ltp
            if cum_volume is not None and self.cum_at_bar_start is not None:
                c["v"] = max(0.0, float(cum_volume) - float(self.cum_at_bar_start))
            if oi is not None:
                c["oi"] = oi
                self.last_oi = oi
            if cum_volume is not None:
                self.last_cum = cum_volume
            self.last_ts = ts

    def _roll(self, m: int):
        if self.cur is not None and m > self.cur["m"]:
            if not self.bars or self.bars[-1]["m"] < self.cur["m"]:
                self.bars.append(self.cur)
            self.cur = None

    def flush(self, now: dt.datetime):
        """Complete the forming bar once its minute is over (even if no new tick)."""
        with self.lock:
            self._roll(minute_index(now.astimezone(IST), self.cfg))

    def completed(self) -> list[dict]:
        with self.lock:
            return list(self.bars)


def vwap(bars: list[dict]) -> float | None:
    pv = sum((b["h"] + b["l"] + b["c"]) / 3 * b["v"] for b in bars)
    v = sum(b["v"] for b in bars)
    return pv / v if v > 0 else None


def opening_range(bars: list[dict], minutes: int) -> tuple[float, float] | None:
    orb = [b for b in bars if b["m"] < minutes]
    if len(orb) < max(1, minutes // 2):
        return None
    return max(b["h"] for b in orb), min(b["l"] for b in orb)


def cum_volume(bars: list[dict]) -> float:
    return sum(b["v"] for b in bars)


def build_rvol_baseline(sessions: list[list[dict]]) -> dict[int, float]:
    """sessions: list of past days, each a list of 1-min bars with 'm' and 'v'.
    Returns {minute_index: average cumulative volume up to and including that minute}."""
    per_min: dict[int, list[float]] = {}
    for bars in sessions:
        cum = 0.0
        for b in sorted(bars, key=lambda x: x["m"]):
            cum += b["v"]
            per_min.setdefault(b["m"], []).append(cum)
    return {m: sum(v) / len(v) for m, v in per_min.items() if v}


def rvol(bars: list[dict], baseline: dict[int, float] | None) -> float | None:
    if not bars or not baseline:
        return None
    m = bars[-1]["m"]
    base = baseline.get(m) or baseline.get(str(m))
    if not base:
        return None
    return cum_volume(bars) / base
