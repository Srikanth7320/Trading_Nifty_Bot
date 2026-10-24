"""Shared technical snapshot used by the stock-analysis page and the next-day outlook (pure functions)."""
import numpy as np
import pandas as pd

import config
import indicators as ind
import patterns


def latest_complete(hist, now=None):
    """Candles of finished sessions. During the trading day today's partial candle is dropped; after the close
    (or on non-trading days) every candle up to today is kept."""
    now = now or config.now_ist()
    today = now.date()
    if config.market_status(now)[0] in ("OPEN", "PRE_OPEN") and config.is_trading_day(today):
        return hist[[d < today for d in hist.index.date]]
    return hist[[d <= today for d in hist.index.date]]


def gap_stats(hist, n=60):
    """Historical open-vs-previous-close gaps (%). A statistical estimate of the typical gap range, NOT a prediction."""
    h = hist.tail(n + 1)
    if len(h) < 20:
        return None
    gaps = (h["Open"].astype(float).values[1:] / h["Close"].astype(float).values[:-1] - 1) * 100
    return {"p10": round(float(np.percentile(gaps, 10)), 2), "p90": round(float(np.percentile(gaps, 90)), 2),
            "avg_abs": round(float(np.mean(np.abs(gaps))), 2), "worst": round(float(gaps.min()), 2), "sessions": len(gaps)}


def snapshot(hc, price, day_volume=None, market_open=False):
    """Trend, momentum, volume, levels and candlestick read of completed candles `hc` with `price` as the latest price."""
    c, h, l, v = (hc[k].astype(float) for k in ("Close", "High", "Low", "Volume"))
    e9, e20 = float(ind.ema(c, 9).iloc[-1]), float(ind.ema(c, 20).iloc[-1])
    s50 = float(c.rolling(50).mean().iloc[-1]) if len(c) >= 50 else None
    s150 = float(c.rolling(min(150, len(c))).mean().iloc[-1])
    rsi_v = float(ind.rsi(c).iloc[-1])
    ml, ms, mh = ind.macd(c)
    atr_v = float(ind.atr(h, l, c).iloc[-1])
    avg_vol = float(v.iloc[-20:].mean())
    last_vol = float(v.iloc[-1])
    prior_high, prior_low = float(h.iloc[-20:].max()), float(l.iloc[-20:].min())
    lo52, hi52 = float(l.tail(252).min()), float(h.tail(252).max())
    rng = float(h.iloc[-1] - l.iloc[-1])
    close_loc = float((c.iloc[-1] - l.iloc[-1]) / rng) if rng > 0 else 0.5
    prev_close = float(c.iloc[-1])
    up_trend = price > e20 and (s50 is None or e20 > s50)
    down_trend = price < e20 and (s50 is not None and e20 < s50)
    trend = "UPTREND" if up_trend else ("DOWNTREND" if down_trend else "SIDEWAYS")
    mom = "positive" if (float(mh.iloc[-1]) > 0 and float(mh.iloc[-1]) >= float(mh.iloc[-2])) else (
        "negative" if (float(mh.iloc[-1]) < 0 and float(mh.iloc[-1]) <= float(mh.iloc[-2])) else "mixed")
    vr_last = last_vol / avg_vol if avg_vol else 0.0
    day_move = (c.iloc[-1] / c.iloc[-2] - 1) * 100 if len(c) > 1 else 0.0
    sr = patterns.support_resistance(hc, price)
    breakout = ("BREAKOUT" if price > prior_high else ("NEAR BREAKOUT" if price >= 0.99 * prior_high else
                ("BREAKDOWN" if price < prior_low else ("NEAR BREAKDOWN" if price <= 1.01 * prior_low else "INSIDE RANGE"))))
    return {"price": price, "prev_close": prev_close, "ema9": e9, "ema20": e20, "sma50": s50, "sma150": s150, "rsi": rsi_v,
            "macd": float(ml.iloc[-1]), "macd_signal": float(ms.iloc[-1]), "macd_hist": float(mh.iloc[-1]), "macd_hist_prev": float(mh.iloc[-2]),
            "atr": atr_v, "atr_pct": atr_v / price * 100 if price else 0, "avg_volume_20": avg_vol, "last_volume": last_vol,
            "last_volume_ratio": vr_last, "last_day_move_pct": day_move, "prior_20d_high": prior_high, "prior_20d_low": prior_low,
            "high_52w": hi52, "low_52w": lo52, "range_pos_52w": (price - lo52) / (hi52 - lo52) if hi52 > lo52 else 0.0,
            "close_location": close_loc, "trend": trend, "momentum": mom, "breakout_state": breakout,
            "patterns": patterns.detect(hc), "support": sr["support"], "resistance": sr["resistance"],
            "gap_stats": gap_stats(hc), "last_session": str(hc.index[-1].date()), "sessions": len(hc)}
