"""NSE end-of-day downloads for the Smart Money Footprint scanner.

Every file is fetched from NSE's public archives with browser-like headers, a session cookie from the
NSE home page, and retry with backoff. Nothing is ever filled in or guessed: a file that does not
exist (holiday / not yet published) returns None; a network/blocked error raises NseError.
"""
from __future__ import annotations

import csv
import datetime as dt
import io
import logging
import time
import zipfile

import httpx

log = logging.getLogger("smf.nse")

UA = {"User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
                     "Chrome/124.0 Safari/537.36"),
      "Accept": "*/*", "Accept-Language": "en-US,en;q=0.9", "Referer": "https://www.nseindia.com/"}

URLS = {
    "equity": ["https://nsearchives.nseindia.com/products/content/sec_bhavdata_full_{ddmmyyyy}.csv",
               "https://archives.nseindia.com/products/content/sec_bhavdata_full_{ddmmyyyy}.csv"],
    "fo": ["https://nsearchives.nseindia.com/content/fo/BhavCopy_NSE_FO_0_0_0_{yyyymmdd}_F_0000.csv.zip",
           "https://archives.nseindia.com/content/fo/BhavCopy_NSE_FO_0_0_0_{yyyymmdd}_F_0000.csv.zip"],
    "index": ["https://nsearchives.nseindia.com/content/indices/ind_close_all_{ddmmyyyy}.csv",
              "https://archives.nseindia.com/content/indices/ind_close_all_{ddmmyyyy}.csv"],
}
N500_URL = "https://nsearchives.nseindia.com/content/indices/ind_nifty500list.csv"
BAN_URL = "https://nsearchives.nseindia.com/content/fo/fo_secban.csv"


class NseError(Exception):
    """NSE could not be reached (blocked, network down, server error) - not the same as 'no file'."""


class Client:
    def __init__(self, timeout=25, pause=0.4):
        self.http = httpx.Client(headers=UA, timeout=timeout, follow_redirects=True)
        self.pause = pause
        self._cookie = False
        self.requests = 0

    def close(self):
        self.http.close()

    def _warm(self):
        if not self._cookie:
            try:
                self.http.get("https://www.nseindia.com/")
            except Exception:  # archives usually work without the cookie
                pass
            self._cookie = True

    def get(self, url: str) -> bytes | None:
        """bytes, or None for 404 (no file). Raises NseError after retries for anything else."""
        self._warm()
        delay, last = 2.0, None
        for _ in range(4):
            time.sleep(self.pause)
            self.requests += 1
            try:
                r = self.http.get(url)
                if r.status_code == 404:
                    return None
                if r.status_code == 200 and r.content:
                    return r.content
                last = f"HTTP {r.status_code}"
            except Exception as e:
                last = str(e)
            time.sleep(delay)
            delay *= 2
        raise NseError(f"{url.split('/')[-1]}: {last}")

    def first(self, kind: str, d: dt.date) -> bytes | None:
        last_err = None
        for u in URLS[kind]:
            try:
                b = self.get(u.format(ddmmyyyy=f"{d:%d%m%Y}", yyyymmdd=f"{d:%Y%m%d}"))
                if b is not None:
                    return b
            except NseError as e:
                last_err = e
        if last_err:
            raise last_err
        return None


# ------------------------------------------------------------------ parsers
def _num(x):
    x = (x or "").strip().replace(",", "")
    if x in ("", "-", "NA", "nan"):
        return None
    try:
        return float(x)
    except ValueError:
        return None


def parse_equity(raw: bytes) -> list[dict]:
    """sec_bhavdata_full: EQ (and BE) series rows with delivery."""
    rows = csv.reader(io.StringIO(raw.decode("utf-8", "replace")))
    hdr = [h.strip().upper() for h in next(rows)]
    ix = {h: i for i, h in enumerate(hdr)}
    need = ["SYMBOL", "SERIES", "OPEN_PRICE", "HIGH_PRICE", "LOW_PRICE", "CLOSE_PRICE", "PREV_CLOSE",
            "TTL_TRD_QNTY", "TURNOVER_LACS", "NO_OF_TRADES", "DELIV_QTY", "DELIV_PER"]
    miss = [n for n in need if n not in ix]
    if miss:
        raise ValueError(f"equity bhavcopy: columns missing {miss}")
    out = []
    for r in rows:
        if len(r) < len(hdr):
            continue
        if r[ix["SERIES"]].strip() != "EQ":
            continue
        g = lambda k: _num(r[ix[k]])  # noqa: E731
        close = g("CLOSE_PRICE")
        if not close:
            continue
        out.append({"symbol": r[ix["SYMBOL"]].strip(), "open": g("OPEN_PRICE"), "high": g("HIGH_PRICE"),
                    "low": g("LOW_PRICE"), "close": close, "prev_close": g("PREV_CLOSE"),
                    "volume": g("TTL_TRD_QNTY"), "turnover_cr": (g("TURNOVER_LACS") or 0) / 100.0,
                    "trades": g("NO_OF_TRADES"), "deliv_qty": g("DELIV_QTY"), "deliv_per": g("DELIV_PER")})
    return out


def parse_fo(raw: bytes) -> list[dict]:
    """UDiFF F&O bhavcopy -> one row per stock: total futures OI (all expiries), OI change,
    near-month futures close, lot size."""
    with zipfile.ZipFile(io.BytesIO(raw)) as z:
        name = next(n for n in z.namelist() if n.lower().endswith(".csv"))
        text = z.read(name).decode("utf-8", "replace")
    rows = csv.reader(io.StringIO(text))
    hdr = [h.strip() for h in next(rows)]
    ix = {h: i for i, h in enumerate(hdr)}
    for k in ("FinInstrmTp", "TckrSymb", "XpryDt", "ClsPric", "OpnIntrst", "ChngInOpnIntrst"):
        if k not in ix:
            raise ValueError(f"F&O bhavcopy: column {k} missing")
    agg: dict[str, dict] = {}
    for r in rows:
        if len(r) < len(hdr) or r[ix["FinInstrmTp"]].strip() != "STF":
            continue
        sym = r[ix["TckrSymb"]].strip()
        exp = r[ix["XpryDt"]].strip()
        a = agg.setdefault(sym, {"symbol": sym, "fut_oi": 0.0, "fut_oi_chg": 0.0, "near_exp": None,
                                 "fut_close": None, "lot": None})
        a["fut_oi"] += _num(r[ix["OpnIntrst"]]) or 0
        a["fut_oi_chg"] += _num(r[ix["ChngInOpnIntrst"]]) or 0
        if a["near_exp"] is None or exp < a["near_exp"]:
            a["near_exp"] = exp
            a["fut_close"] = _num(r[ix["ClsPric"]])
            if "NewBrdLotQty" in ix:
                a["lot"] = _num(r[ix["NewBrdLotQty"]])
    return list(agg.values())


def parse_index(raw: bytes, name="Nifty 50") -> float | None:
    rows = csv.reader(io.StringIO(raw.decode("utf-8", "replace")))
    hdr = [h.strip().lower() for h in next(rows)]
    ni = hdr.index("index name")
    ci = next(i for i, h in enumerate(hdr) if h.startswith("closing index value"))
    for r in rows:
        if len(r) > ci and r[ni].strip().lower() == name.lower():
            return _num(r[ci])
    return None


def parse_n500(raw: bytes) -> list[dict]:
    rows = list(csv.DictReader(io.StringIO(raw.decode("utf-8", "replace"))))
    return [{"symbol": r["Symbol"].strip(), "industry": (r.get("Industry") or "").strip(),
             "name": (r.get("Company Name") or "").strip()} for r in rows if r.get("Symbol")]


def parse_ban(raw: bytes) -> list[str]:
    out = []
    for r in csv.reader(io.StringIO(raw.decode("utf-8", "replace"))):
        if len(r) >= 2 and r[0].strip().isdigit():
            out.append(r[1].strip().upper())
    return out
