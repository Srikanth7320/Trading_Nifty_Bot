"""Broad-market context: index trend/day move (used to gate new entries) and global/sector readings (used by the outlook).

Everything fails open: if no index data can be fetched the context is flagged `available: False` and trading is not blocked,
but the outlook says the data is missing instead of guessing."""
import threading
import time

import pandas as pd

import config
import indicators as ind

try:
    import yfinance as yf
except ImportError:
    yf = None

_lock = threading.Lock()
_cache = {}   # key -> (time, value)

INDIA_INDEX = "^NSEI"
GLOBAL = [("S&P 500 (US)", "^GSPC"), ("Nasdaq (US)", "^IXIC"), ("Nikkei 225 (Japan)", "^N225"), ("Hang Seng (HK)", "^HSI"),
          ("FTSE 100 (UK)", "^FTSE"), ("DAX (Germany)", "^GDAXI"), ("India VIX", "^INDIAVIX"), ("US VIX", "^VIX"),
          ("Brent crude", "BZ=F"), ("USD/INR", "INR=X"), ("US 10y yield", "^TNX")]
SECTORS = [("Bank", "^NSEBANK"), ("IT", "^CNXIT"), ("Auto", "^CNXAUTO"), ("Pharma", "^CNXPHARMA"), ("FMCG", "^CNXFMCG"),
           ("Metal", "^CNXMETAL"), ("Energy", "^CNXENERGY"), ("Realty", "^CNXREALTY"), ("Infra", "^CNXINFRA")]


def _cached(key, ttl, fn):
    with _lock:
        hit = _cache.get(key)
        if hit and time.time() - hit[0] < ttl:
            return hit[1]
    try:
        val = fn()
    except Exception:
        val = None
    with _lock:
        _cache[key] = (time.time(), val)
    return val


def _daily(ticker, period="6mo"):
    if yf is None:
        return None
    df = yf.Ticker(ticker).history(period=period, interval="1d", auto_adjust=False)
    if df is None or df.empty:
        return None
    return df.dropna(subset=["Close"])


def quote_change(ticker):
    """Latest close vs previous close for a Yahoo ticker -> dict(last, change_pct, as_of) or None."""
    def go():
        df = _daily(ticker, "10d")
        if df is None or len(df) < 2:
            return None
        last, prev = float(df["Close"].iloc[-1]), float(df["Close"].iloc[-2])
        return {"last": round(last, 2), "change_pct": round((last / prev - 1) * 100, 2), "as_of": str(df.index[-1])[:16]}
    return _cached("q:" + ticker, 600, go)


def context():
    """Nifty 50 trend. dict(available, trend BULLISH|NEUTRAL|BEARISH, day_change_pct, close, ema20, sma50, as_of, source)."""
    def go():
        df = _daily(INDIA_INDEX, "8mo")
        if df is None or len(df) < 55:
            return None
        c = df["Close"].astype(float)
        e20, s50 = float(ind.ema(c, 20).iloc[-1]), float(c.rolling(50).mean().iloc[-1])
        last, prev = float(c.iloc[-1]), float(c.iloc[-2])
        trend = "BULLISH" if (last > e20 > s50) else ("BEARISH" if (last < e20 and e20 < s50) else "NEUTRAL")
        return {"available": True, "trend": trend, "day_change_pct": round((last / prev - 1) * 100, 2), "close": round(last, 2),
                "ema20": round(e20, 2), "sma50": round(s50, 2), "as_of": str(df.index[-1])[:16], "source": "Yahoo ^NSEI"}
    val = _cached("ctx", 600, go)
    return val or {"available": False, "trend": "UNKNOWN", "day_change_pct": 0.0, "source": "unavailable"}


def global_snapshot():
    return [{"name": n, "ticker": t, **(quote_change(t) or {"last": None, "change_pct": None, "as_of": ""})} for n, t in GLOBAL]


def sector_snapshot():
    rows = [{"name": n, "ticker": t, **(quote_change(t) or {"last": None, "change_pct": None, "as_of": ""})} for n, t in SECTORS]
    return rows
