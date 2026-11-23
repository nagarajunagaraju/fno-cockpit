"""End-of-day "repu watchlist" (v3.1): FII mood + stock futures build-up, built each evening.

Sources (nothing is invented; a source that cannot be fetched is reported as unavailable):
* NSE participant-wise F&O OI file (fao_participant_oi_DDMMYYYY.csv) -> FII mood. NSE publishes
  participant totals only (index futures, stock futures, index/stock options). There is NO public
  per-stock FII position data, so the stock list is built from total futures OI build-up.
* Kite daily candles (cash + current & next month futures with OI) -> per-stock build-up and trend.
* NSE F&O ban list (fo_secban.csv) and NSE event calendar (financial results) -> skip lists.

The report is stored in kv("eod_report") with for_date = next weekday; the live scanner uses it
only on that date.
"""
from __future__ import annotations

import csv
import datetime as dt
import io
import json
import logging
import threading
import time
from pathlib import Path

import httpx

from . import store
from .config import IST

log = logging.getLogger("cockpit.eod")
CACHE = store.DATA / "cache" / "nse"
CACHE.mkdir(parents=True, exist_ok=True)
UA = {"User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
                     "Chrome/124.0 Safari/537.36"),
      "Accept": "*/*", "Accept-Language": "en-US,en;q=0.9", "Referer": "https://www.nseindia.com/"}
ARCHIVES = ["https://nsearchives.nseindia.com/content/nsccl/fao_participant_oi_{d}.csv",
            "https://archives.nseindia.com/content/nsccl/fao_participant_oi_{d}.csv"]
BAN_URL = "https://nsearchives.nseindia.com/content/fo/fo_secban.csv"
EVENTS_URL = "https://www.nseindia.com/api/event-calendar"


def next_weekday(d: dt.date) -> dt.date:
    n = d + dt.timedelta(days=1)
    while n.weekday() >= 5:
        n += dt.timedelta(days=1)
    return n


# ------------------------------------------------------------ participant OI -> FII mood
def parse_participant(text: str) -> dict:
    """-> {"FII": {"fut_idx_long": .., ...}, "DII": .., "Pro": .., "Client": ..}"""
    rows = list(csv.reader(io.StringIO(text)))
    hdr_i = next(i for i, r in enumerate(rows) if r and r[0].strip().lower() == "client type")
    hdr = [h.strip().lower() for h in rows[hdr_i]]
    keys = {"future index long": "fut_idx_long", "future index short": "fut_idx_short",
            "future stock long": "fut_stk_long", "future stock short": "fut_stk_short",
            "option index call long": "idx_call_long", "option index put long": "idx_put_long",
            "option index call short": "idx_call_short", "option index put short": "idx_put_short",
            "option stock call long": "stk_call_long", "option stock put long": "stk_put_long",
            "option stock call short": "stk_call_short", "option stock put short": "stk_put_short"}
    out = {}
    for r in rows[hdr_i + 1:]:
        if not r or not r[0].strip():
            continue
        name = r[0].strip()
        rec = {}
        for j, h in enumerate(hdr):
            if h in keys and j < len(r):
                rec[keys[h]] = int(float(r[j].strip().replace(",", "") or 0))
        if len(rec) == len(keys):
            out[name.upper() if name.upper() in ("FII", "DII") else name.capitalize()] = rec
    if "FII" not in out:
        raise ValueError("FII row not found in participant file")
    return out


def _net(p: dict) -> dict:
    return {"idx_fut": p["fut_idx_long"] - p["fut_idx_short"],
            "stk_fut": p["fut_stk_long"] - p["fut_stk_short"],
            "idx_opt": (p["idx_call_long"] - p["idx_call_short"]) - (p["idx_put_long"] - p["idx_put_short"]),
            "idx_long_pct": round(p["fut_idx_long"] / max(1, p["fut_idx_long"] + p["fut_idx_short"]) * 100, 1)}


def fii_mood(today: dict, prev: dict | None) -> dict:
    """3 votes from the day's CHANGE in FII net positions: index futures, stock futures, index options.
    >= +2 BULLISH, <= -2 BEARISH, else NEUTRAL. Without the previous file the change is unknown."""
    t = _net(today["FII"])
    res = {"idx_long_pct": t["idx_long_pct"], "net": t, "participants": {}}
    for who in ("FII", "DII", "Pro", "Client"):
        if who in today:
            n = _net(today[who])
            if prev and who in prev:
                p = _net(prev[who])
                n["chg"] = {k: n[k] - p[k] for k in ("idx_fut", "stk_fut", "idx_opt")}
            res["participants"][who] = n
    if not prev or "FII" not in prev:
        res.update(mood=None, votes=None, reason="previous day's participant file unavailable: change unknown")
        return res
    c = res["participants"]["FII"]["chg"]
    votes = {k: (1 if c[k] > 0 else -1 if c[k] < 0 else 0) for k in ("idx_fut", "stk_fut", "idx_opt")}
    s = sum(votes.values())
    res.update(votes=votes, vote_sum=s, mood="BULLISH" if s >= 2 else "BEARISH" if s <= -2 else "NEUTRAL")
    res["reason"] = (f"FII change: index futures {c['idx_fut']:+,}, stock futures {c['stk_fut']:+,}, "
                     f"index options (calls-puts) {c['idx_opt']:+,} contracts")
    return res


def _get(url: str, client: httpx.Client) -> str:
    r = client.get(url, headers=UA, timeout=20, follow_redirects=True)
    r.raise_for_status()
    return r.text


def participant_file(d: dt.date, client: httpx.Client | None = None) -> dict | None:
    """Parsed participant file for date d (cached on disk). None if NSE has no file (holiday / not yet out)."""
    f = CACHE / f"part_{d:%d%m%Y}.csv"
    if f.exists():
        return parse_participant(f.read_text())
    own = client is None
    client = client or httpx.Client()
    try:
        last = None
        for u in ARCHIVES:
            try:
                txt = _get(u.format(d=f"{d:%d%m%Y}"), client)
                parsed = parse_participant(txt)
                f.write_text(txt)
                return parsed
            except httpx.HTTPStatusError as e:
                last = e
                if e.response.status_code == 404:
                    continue
            except Exception as e:  # noqa: BLE001 - reported to caller via None + last error
                last = e
        if last and not (isinstance(last, httpx.HTTPStatusError) and last.response.status_code == 404):
            raise last
        return None
    finally:
        if own:
            client.close()


def mood_for(d: dt.date, client=None, max_back: int = 6) -> dict:
    """FII mood computed from day d's file and the previous available file."""
    today = participant_file(d, client)
    if today is None:
        return {"mood": None, "reason": f"NSE participant file for {d} not published (yet / holiday)"}
    prev, p = None, d
    for _ in range(max_back):
        p -= dt.timedelta(days=1)
        if p.weekday() >= 5:
            continue
        prev = participant_file(p, client)
        if prev is not None:
            break
    res = fii_mood(today, prev)
    res["data_date"] = d.isoformat()
    res["prev_date"] = p.isoformat() if prev else None
    return res


def mood_history(days: list[str], progress: dict | None = None) -> tuple[dict, list[str]]:
    """{trading_day: mood_from_previous_trading_day} for the backtest. Uses cached files."""
    notes, files = [], {}
    with httpx.Client() as client:
        for i, d in enumerate(days):
            try:
                files[d] = participant_file(dt.date.fromisoformat(d), client)
            except Exception as e:  # blocked / network -> stop, report
                notes.append(f"FII participant history unavailable ({e}); FII filter not backtested")
                return {}, notes
            if progress is not None:
                progress.update(stage=f"FII history {i + 1}/{len(days)}")
            time.sleep(0.2)
    out = {}
    for i in range(2, len(days)):
        a, b = files.get(days[i - 1]), files.get(days[i - 2])
        if a and b:
            out[days[i]] = fii_mood(a, b)["mood"]
    return out, notes


# ------------------------------------------------------------ ban list / results calendar
def parse_ban(text: str) -> list[str]:
    out = []
    for r in csv.reader(io.StringIO(text)):
        if len(r) >= 2 and r[0].strip().isdigit():
            out.append(r[1].strip().upper())
    return out


def fetch_ban(client) -> tuple[list[str] | None, str | None]:
    try:
        return parse_ban(_get(BAN_URL, client)), None
    except Exception as e:
        return None, f"F&O ban list unavailable: {e}"


def fetch_results(client, days: set[dt.date]) -> tuple[dict | None, str | None]:
    """{SYMBOL: 'YYYY-MM-DD'} for financial-results board meetings on the given dates."""
    try:
        client.get("https://www.nseindia.com/", headers=UA, timeout=20)
        r = client.get(EVENTS_URL, headers=UA, timeout=20)
        r.raise_for_status()
        out = {}
        for e in r.json():
            purpose = f"{e.get('purpose', '')} {e.get('bm_desc', '')}".lower()
            if "result" not in purpose:
                continue
            try:
                d = dt.datetime.strptime(e["date"], "%d-%b-%Y").date()
            except Exception:
                continue
            if d in days:
                out[e["symbol"].upper()] = d.isoformat()
        return out, None
    except Exception as e:
        return None, f"results calendar unavailable: {e}"


# ------------------------------------------------------------ per-stock build-up from Kite
def sma(xs, n):
    return sum(xs[-n:]) / n if len(xs) >= n else None


def classify(price_pct, oi_pct, cfg):
    if price_pct is None or oi_pct is None:
        return None
    if oi_pct >= cfg["wl_min_oi_pct"] and price_pct >= cfg["wl_min_price_pct"]:
        return "Long Buildup"
    if oi_pct >= cfg["wl_min_oi_pct"] and price_pct <= -cfg["wl_min_price_pct"]:
        return "Short Buildup"
    return None


def stock_rows(kite, universe, ranks, cfg, today: dt.date, pause=0.34) -> tuple[list[dict], list[str]]:
    """Daily close/OI change for liquid F&O stocks. OI = current + next month futures (rollover safe)."""
    notes = []
    futs: dict[str, list] = {}
    for i in kite.instruments("NFO"):
        if i["instrument_type"] == "FUT" and i["expiry"] >= today:
            futs.setdefault(i["name"], []).append(i)
    rows = []
    for u in universe:
        sym = u["symbol"]
        rank = ranks.get(sym)
        if rank is None or rank > cfg["watch_rank_max"]:
            continue
        try:
            cash = kite.historical_data(u["cash_token"], today - dt.timedelta(days=110), today, "day")
            time.sleep(pause)
            oi_by_day: dict[str, float] = {}
            for f in sorted(futs.get(sym, []), key=lambda x: x["expiry"])[:2]:
                for c in kite.historical_data(f["instrument_token"], today - dt.timedelta(days=10), today, "day", oi=True):
                    k = c["date"].date().isoformat()
                    oi_by_day[k] = oi_by_day.get(k, 0) + (c.get("oi") or 0)
                time.sleep(pause)
        except Exception as e:
            notes.append(f"{sym}: {e}")
            continue
        if len(cash) < 2 or cash[-1]["date"].date() != today:
            continue
        closes = [c["close"] for c in cash]
        last, prev = cash[-1], cash[-2]
        d0, d1 = today.isoformat(), prev["date"].date().isoformat()
        oi0, oi1 = oi_by_day.get(d0), oi_by_day.get(d1)
        price_pct = (last["close"] - prev["close"]) / prev["close"] * 100
        oi_pct = (oi0 - oi1) / oi1 * 100 if oi0 and oi1 else None
        s20, s50 = sma(closes, 20), sma(closes, 50)
        trend = None
        if s20 and s50:
            trend = "UP" if last["close"] > s20 and last["close"] > s50 else \
                "DOWN" if last["close"] < s20 and last["close"] < s50 else "MIXED"
        rows.append({"symbol": sym, "rank": rank, "close": last["close"], "high": last["high"], "low": last["low"],
                     "price_pct": round(price_pct, 2), "oi_pct": round(oi_pct, 2) if oi_pct is not None else None,
                     "volume": last.get("volume"), "sma20": round(s20, 2) if s20 else None,
                     "sma50": round(s50, 2) if s50 else None, "trend": trend,
                     "buildup": classify(price_pct, oi_pct, cfg)})
    return rows, notes


def build_report(rows, mood: dict, ban, results, cfg, data_date: dt.date) -> dict:
    ban_s, res_s = set(ban or []), set(results or {})
    long_c, short_c, skipped = [], [], []
    for r in rows:
        if not r["buildup"]:
            continue
        why = []
        if r["symbol"] in ban_s:
            why.append("F&O ban")
        if r["symbol"] in res_s:
            why.append(f"results {results[r['symbol']]}")
        if r["buildup"] == "Long Buildup" and r["trend"] != "UP":
            why.append(f"trend {r['trend']} (needs above 20 & 50 DMA)")
        if r["buildup"] == "Short Buildup" and r["trend"] != "DOWN":
            why.append(f"trend {r['trend']} (needs below 20 & 50 DMA)")
        r = dict(r, strength=round(abs(r["price_pct"]) * r["oi_pct"], 2))
        if why:
            skipped.append({**r, "skip": ", ".join(why)})
        else:
            (long_c if r["buildup"] == "Long Buildup" else short_c).append(r)
    long_c.sort(key=lambda r: -r["strength"])
    short_c.sort(key=lambda r: -r["strength"])
    n = cfg["wl_size"]
    m = mood.get("mood")
    if m == "BULLISH":
        picks = [dict(r, side="CE") for r in long_c[:n]]
    elif m == "BEARISH":
        picks = [dict(r, side="PE") for r in short_c[:n]]
    else:   # neutral / unknown: show both, fewer each
        k = max(1, n // 2 + 1)
        picks = [dict(r, side="CE") for r in long_c[:k]] + [dict(r, side="PE") for r in short_c[:k]]
    for p in picks:
        up = p["side"] == "CE"
        p["plan"] = (f"Repu {'paiki' if up else 'kindiki'}: {p['high' if up else 'low']:.2f} "
                     f"{'break above' if up else 'break below'} + engine pullback confirmation")
    return {"data_date": data_date.isoformat(), "for_date": next_weekday(data_date).isoformat(),
            "created_at": dt.datetime.now(IST).isoformat(timespec="seconds"),
            "fii": mood, "watch": {p["symbol"]: p["side"] for p in picks}, "picks": picks,
            "long_buildup": long_c[:10], "short_buildup": short_c[:10], "skipped": skipped[:20],
            "ban": sorted(ban_s) if ban is not None else None, "results": results,
            "universe_checked": len(rows)}


def summary_text(rep: dict) -> tuple[str, str]:
    m = (rep.get("fii") or {}).get("mood") or "UNKNOWN"
    title = f"Repu watchlist ({rep['for_date']}): FII {m}"
    lines = []
    if rep["fii"].get("reason"):
        lines.append(rep["fii"]["reason"])
    for p in rep["picks"]:
        lines.append(f"{p['side']} {p['symbol']} {p['price_pct']:+.2f}% OI {p['oi_pct']:+.1f}% · {p['plan']}")
    if not rep["picks"]:
        lines.append("No stock passed (build-up + trend + ban/results). NO TRADE bias is valid.")
    return title, "\n".join(lines)


# ------------------------------------------------------------ evening scheduler
class EodJob:
    START, GIVE_UP_FII = dt.time(18, 30), dt.time(21, 0)
    END = dt.time(23, 0)

    def __init__(self, feed, cfg_getter, notify):
        self.feed, self.cfg, self.notify = feed, cfg_getter, notify
        self.state = {"last_try": None, "last_error": None, "running": False}
        self._lock = threading.Lock()

    def start(self):
        threading.Thread(target=self._loop, daemon=True, name="eod").start()

    def _loop(self):
        while True:
            try:
                now = dt.datetime.now(IST)
                rep = store.kv_get("eod_report") or {}
                due = (now.weekday() < 5 and self.START <= now.time() <= self.END
                       and rep.get("data_date") != now.date().isoformat())
                last = self.state["last_try"]
                if due and (last is None or time.time() - last > 600):
                    self.run(now)
            except Exception as e:  # never die
                self.state["last_error"] = str(e)
            time.sleep(60)

    def run(self, now: dt.datetime | None = None, force_without_fii: bool = False) -> dict:
        if not self._lock.acquire(blocking=False):
            raise RuntimeError("watchlist job already running")
        try:
            self.state.update(running=True, last_try=time.time(), last_error=None)
            now = now or dt.datetime.now(IST)
            today = now.date()
            cfg = self.cfg()
            src = getattr(self.feed, "src", None)
            if src is None or not getattr(src, "kite", None) or not src.ready():
                raise RuntimeError("Kite not logged in today: stock build-up needs today's Kite session")
            notes = []
            with httpx.Client() as client:
                try:
                    mood = mood_for(today, client)
                except Exception as e:
                    mood = {"mood": None, "reason": f"NSE participant file not reachable: {e}"}
                if mood.get("mood") is None and "not published" in (mood.get("reason") or "") \
                        and now.time() < self.GIVE_UP_FII and not force_without_fii:
                    raise RuntimeError(mood["reason"] + "; retrying every 10 min until 21:00")
                ban, e1 = fetch_ban(client)
                nxt = next_weekday(today)
                results, e2 = fetch_results(client, {nxt})
                notes += [e for e in (e1, e2) if e]
            rows, n2 = stock_rows(src.kite, src.universe, getattr(self.feed, "turnover_rank", {}) or {}, cfg, today)
            if not rows:
                raise RuntimeError("no daily candles for today from Kite (holiday or data not ready)")
            rep = build_report(rows, mood, ban, results, cfg, today)
            rep["notes"] = notes + n2[:5]
            store.kv_set("eod_report", rep)
            title, body = summary_text(rep)
            self.notify("WATCHLIST", title, body, {"report": {k: rep[k] for k in ("for_date", "watch", "picks")}})
            return rep
        except Exception as e:
            self.state["last_error"] = str(e)
            log.warning("eod watchlist: %s", e)
            raise
        finally:
            self.state["running"] = False
            self._lock.release()


def context_for(day: dt.date) -> dict:
    """What the live scanner uses on `day`: FII mood, watchlist, ban and results-skip lists."""
    rep = store.kv_get("eod_report") or {}
    if rep.get("for_date") != day.isoformat():
        return {"available": False}
    res = rep.get("results") or {}
    return {"available": True, "fii_mood": (rep.get("fii") or {}).get("mood"), "watch": rep.get("watch") or {},
            "ban": set(rep.get("ban") or []), "results": set(res), "ban_known": rep.get("ban") is not None,
            "results_known": rep.get("results") is not None}
