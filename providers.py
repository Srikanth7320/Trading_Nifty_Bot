"""Market-data access: Upstox Analytics (primary, read-only) with optional Yahoo fallback.

Daily history is cached per symbol per day (it only changes after the close), so a scan costs
one live-quote call per symbol that survives the cheap price/liquidity pre-filter.
"""
import gzip
import json
import threading
import time
from datetime import timedelta
from urllib.parse import quote as urlquote

import pandas as pd
import requests

import config

try:
    import yfinance as yf
except ImportError:  # optional
    yf = None

IST = config.IST


class ProviderError(Exception):
    pass


_hist_cache = {}
_cache_lock = threading.Lock()
_inst_map = {}
_inst_lock = threading.Lock()
LAST = {"provider": "none", "time": ""}


# ---------------------------------------------------------------- Upstox helpers
def _upstox_get(url, params=None):
    if not config.UPSTOX_TOKEN:
        raise ProviderError("UPSTOX_ANALYTICS_TOKEN not configured")
    r = requests.get(url, params=params, timeout=config.HTTP_TIMEOUT,
                     headers={"Authorization": f"Bearer {config.UPSTOX_TOKEN}", "Accept": "application/json"})
    r.raise_for_status()
    payload = r.json()
    if payload.get("status") == "error":
        raise ProviderError("Upstox API returned an error")
    return payload


def _instrument_key(symbol):
    with _inst_lock:
        if not _inst_map:
            cache = config.DATA_DIR / "upstox_instruments.json"
            rows = None
            if cache.exists() and time.time() - cache.stat().st_mtime < 24 * 3600:
                try:
                    rows = json.loads(cache.read_text(encoding="utf-8"))
                except ValueError:
                    rows = None
            if rows is None:
                url = "https://assets.upstox.com/market-quote/instruments/exchange/complete.json.gz"
                try:
                    r = requests.get(url, timeout=60)
                    r.raise_for_status()
                    raw = r.content
                    raw = gzip.decompress(raw) if raw[:2] == b"\x1f\x8b" else raw
                    rows = json.loads(raw.decode("utf-8"))
                    cache.write_text(json.dumps(rows), encoding="utf-8")
                except Exception as exc:
                    if cache.exists():
                        rows = json.loads(cache.read_text(encoding="utf-8"))
                    else:
                        raise ProviderError(f"Upstox instrument master unavailable ({type(exc).__name__})")
            for row in rows:
                if isinstance(row, dict) and str(row.get("segment", "")).upper() == "NSE_EQ":
                    sym = str(row.get("trading_symbol", "")).strip().upper()
                    if sym and row.get("instrument_key"):
                        _inst_map[sym] = str(row["instrument_key"])
        key = _inst_map.get(symbol)
    if not key:
        raise ProviderError(f"{symbol} not found in Upstox NSE_EQ instruments")
    return key


def _upstox_history(symbol):
    key = _instrument_key(symbol)
    today = config.now_ist().date()
    url = ("https://api.upstox.com/v3/historical-candle/" + urlquote(key, safe="") +
           f"/days/1/{today.isoformat()}/{(today - timedelta(days=400)).isoformat()}")
    candles = _upstox_get(url).get("data", {}).get("candles", [])
    if not candles:
        raise ProviderError("Upstox returned no candles")
    df = pd.DataFrame(candles, columns=["Datetime", "Open", "High", "Low", "Close", "Volume", "OI"][:len(candles[0])])
    df["Datetime"] = pd.to_datetime(df["Datetime"], utc=True, errors="coerce").dt.tz_convert(IST).dt.tz_localize(None)
    return df.dropna(subset=["Datetime", "Open", "High", "Low", "Close"]).set_index("Datetime").sort_index()


def _depth_extras(q, price):
    """Optional liquidity / circuit information carried by a full Upstox quote (all keys may be absent)."""
    extra = {}
    try:
        depth = q.get("depth") or {}
        bid = float((depth.get("buy") or [{}])[0].get("price") or 0)
        ask = float((depth.get("sell") or [{}])[0].get("price") or 0)
        if bid > 0 and ask > 0 and ask >= bid:
            extra.update(bid=bid, ask=ask, spread_pct=round((ask - bid) / ((ask + bid) / 2) * 100, 3))
    except (TypeError, ValueError, IndexError, AttributeError):
        pass
    try:
        up, lo = float(q.get("upper_circuit_limit") or 0), float(q.get("lower_circuit_limit") or 0)
        if up > 0:
            extra["upper_circuit"] = up
            extra["at_upper_circuit"] = price >= up * 0.9995
        if lo > 0:
            extra["lower_circuit"] = lo
            extra["at_lower_circuit"] = price <= lo * 1.0005
        ohlc = q.get("ohlc") or {}
        for k in ("open", "high", "low"):
            if ohlc.get(k):
                extra["day_" + k] = float(ohlc[k])
    except (TypeError, ValueError, AttributeError):
        pass
    return extra


def _upstox_quote(symbol):
    key = _instrument_key(symbol)
    data = _upstox_get("https://api.upstox.com/v2/market-quote/quotes", {"instrument_key": key}).get("data", {})
    q = data.get(key) or next((v for v in data.values() if isinstance(v, dict) and str(v.get("instrument_token")) == key), None)
    if q is None and len(data) == 1:
        q = next(iter(data.values()))
    if not isinstance(q, dict):
        raise ProviderError("Upstox quote missing")
    price = float(q.get("last_price") or 0)
    if price <= 0:
        raise ProviderError("Upstox returned an invalid price")
    ts_src, stamp = "retrieval_time", config.now_ist()
    raw = q.get("last_trade_time") or q.get("exchange_timestamp") or q.get("timestamp")
    if raw:
        try:
            if isinstance(raw, (int, float)) or str(raw).isdigit():
                n = float(raw)
                stamp = pd.Timestamp(n / 1000 if n > 1e12 else n, unit="s", tz="UTC").tz_convert(IST).to_pydatetime()
            else:
                t = pd.Timestamp(raw)
                stamp = (t.tz_localize(IST) if t.tzinfo is None else t.tz_convert(IST)).to_pydatetime()
            ts_src = "exchange"
        except Exception:
            ts_src, stamp = "retrieval_time", config.now_ist()
    if config.market_is_open() and ts_src == "exchange":
        age = (config.now_ist() - stamp).total_seconds() / 60
        if age > config.UPSTOX_MAX_QUOTE_AGE_MIN or age < -2:
            raise ProviderError(f"Upstox quote is stale ({age:.0f} min old)")
    vol = q.get("volume")
    out = {"price": price, "day_volume": float(vol) if vol else None, "time": stamp.isoformat(timespec="seconds"),
           "session_date": stamp.date() if ts_src == "exchange" else config.now_ist().date(),
           "provider": "Upstox", "ts_source": ts_src}
    out.update(_depth_extras(q, price))
    return out


# ---------------------------------------------------------------- Yahoo fallback
def _yahoo_daily(symbol):
    if yf is None:
        raise ProviderError("yfinance not installed")
    df = yf.Ticker(f"{symbol}.NS").history(period="2y", interval="1d", auto_adjust=False)
    if df is None or df.empty:
        raise ProviderError("Yahoo returned no daily data")
    idx = pd.DatetimeIndex(df.index)
    df.index = idx.tz_convert(IST).tz_localize(None) if idx.tz is not None else idx
    return df.dropna(subset=["Open", "High", "Low", "Close"])


def _yahoo_quote(symbol):
    daily = _yahoo_daily(symbol)
    if config.market_is_open():
        intr = yf.Ticker(f"{symbol}.NS").history(period="5d", interval="5m", auto_adjust=False)
        if intr is None or intr.empty:
            raise ProviderError("Yahoo intraday unavailable")
        intr = intr.dropna(subset=["Close"])
        last = pd.Timestamp(intr.index[-1])
        last = last.tz_localize(IST) if last.tzinfo is None else last.tz_convert(IST)
        age = (pd.Timestamp.now(tz=IST) - last).total_seconds() / 60
        if age > 30 or age < -2:
            raise ProviderError(f"Yahoo bar is stale ({age:.0f} min old)")
        idx = pd.DatetimeIndex(intr.index)
        idx = idx.tz_convert(IST) if idx.tz is not None else idx
        today_bars = intr[idx.date == config.now_ist().date()]
        return {"price": float(intr["Close"].iloc[-1]),
                "day_volume": float(today_bars["Volume"].sum()) if not today_bars.empty else None,
                "time": last.isoformat(timespec="seconds"), "session_date": config.now_ist().date(),
                "provider": "Yahoo (fallback)", "ts_source": "bar"}
    last = daily.iloc[-1]
    return {"price": float(last["Close"]), "day_volume": float(last["Volume"]), "time": config.now_ist().isoformat(timespec="seconds"),
            "session_date": daily.index[-1].date(), "provider": "Yahoo (fallback)", "ts_source": "last_close"}


# ---------------------------------------------------------------- public API
def _clean(df):
    df = df.copy()
    for c in ("Open", "High", "Low", "Close", "Volume"):
        df[c] = pd.to_numeric(df[c], errors="coerce")
    return df.dropna(subset=["Open", "High", "Low", "Close"])


def load_history(symbol):
    """Completed daily candles (cached per day). Raises ProviderError with a readable reason."""
    key = (symbol, config.now_ist().date())
    with _cache_lock:
        if key in _hist_cache:
            return _hist_cache[key]
    errors = []
    df = None
    if config.UPSTOX_TOKEN:
        try:
            df = _upstox_history(symbol)
        except Exception as exc:
            errors.append(f"Upstox: {str(exc)[:120]}")
    else:
        errors.append("Upstox token not set")
    if df is None and config.YAHOO_FALLBACK:
        try:
            df = _yahoo_daily(symbol)
        except Exception as exc:
            errors.append(f"Yahoo: {str(exc)[:120]}")
    if df is None:
        raise ProviderError("; ".join(errors))
    df = _clean(df)
    with _cache_lock:
        if len(_hist_cache) > 6000:
            _hist_cache.clear()
        _hist_cache[key] = df
    return df


def load_quote(symbol):
    errors = []
    if config.UPSTOX_TOKEN:
        try:
            q = _upstox_quote(symbol)
            LAST.update(provider=q["provider"], time=q["time"])
            return q
        except Exception as exc:
            errors.append(f"Upstox: {str(exc)[:120]}")
    if config.YAHOO_FALLBACK:
        try:
            q = _yahoo_quote(symbol)
            LAST.update(provider=q["provider"], time=q["time"])
            return q
        except Exception as exc:
            errors.append(f"Yahoo: {str(exc)[:120]}")
    raise ProviderError("; ".join(errors) or "no provider configured")


def health_check():
    """Return list of (name, ok, detail) for dashboards / Telegram."""
    out = []
    try:
        q = load_quote("RELIANCE")
        out.append(("Market data", True, f"{q['provider']} OK (RELIANCE {q['price']:.2f})"))
    except Exception as exc:
        out.append(("Market data", False, str(exc)[:200]))
    return out
