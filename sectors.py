"""Sector lookup with a small on-disk cache (data/sector_cache.json). Fails open: unknown sector = '' = no sector limit applied."""
import threading
from concurrent.futures import ThreadPoolExecutor

import store

try:
    import yfinance as yf
except ImportError:
    yf = None

_lock = threading.Lock()


def cached(symbol):
    """Sector from the cache only (no network) - safe to call while rendering a page."""
    return (store.read_json("sector_cache.json", {}) or {}).get(symbol, "")


def sector_of(symbol, timeout=6):
    """Cached sector, else a time-boxed Yahoo lookup (stored, including misses, so a symbol is only asked once)."""
    cache = store.read_json("sector_cache.json", {}) or {}
    if symbol in cache:
        return cache[symbol]
    sec = ""
    if yf is not None:
        ex = ThreadPoolExecutor(max_workers=1)
        try:
            info = ex.submit(lambda: yf.Ticker(f"{symbol}.NS").info or {}).result(timeout=timeout)
            sec = str(info.get("sector") or "")
        except Exception:
            return ""          # do not cache a failure - try again next time
        finally:
            ex.shutdown(wait=False)
    with _lock:
        cache = store.read_json("sector_cache.json", {}) or {}
        cache[symbol] = sec
        store.write_json("sector_cache.json", cache)
    return sec
