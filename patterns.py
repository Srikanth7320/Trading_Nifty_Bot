"""Candlestick patterns and support/resistance from completed daily candles (pure functions, no I/O)."""
import numpy as np
import pandas as pd

import indicators as ind


def _body(o, c):
    return abs(c - o)


def detect(hist, n=3):
    """Patterns on the most recent completed sessions. Each item: dict(name, bias (+1 bullish / -1 bearish / 0 neutral), text)."""
    out = []
    if hist is None or len(hist) < 12:
        return out
    h = hist.tail(max(n, 12))
    O, H, L, C = (h[k].astype(float).values for k in ("Open", "High", "Low", "Close"))
    atr = float(np.mean(H[-10:] - L[-10:])) or 1e-9
    o, hi, lo, c = O[-1], H[-1], L[-1], C[-1]
    body, rng = _body(o, c), max(hi - lo, 1e-9)
    upper, lower = hi - max(o, c), min(o, c) - lo
    trend_dn = C[-1] < C[-6]          # last 5 sessions fell
    trend_up = C[-1] > C[-6]

    def add(name, bias, text):
        out.append({"name": name, "bias": bias, "text": text})

    if body / rng < 0.1 and rng > 0.3 * atr:
        add("Doji", 0, "Indecision: open and close almost equal")
    if lower >= 2 * body and upper <= 0.35 * rng and body > 0 and trend_dn:
        add("Hammer", +1, "Long lower shadow after a decline - buyers defended lower prices")
    if upper >= 2 * body and lower <= 0.35 * rng and body > 0 and trend_up:
        add("Shooting star", -1, "Long upper shadow after a rise - sellers rejected higher prices")
    if body >= 0.85 * rng and rng > 0.8 * atr:
        add("Bullish marubozu" if c > o else "Bearish marubozu", +1 if c > o else -1,
            "Strong one-direction session with almost no shadows")
    po, pc = O[-2], C[-2]
    if c > o and pc < po and c >= po and o <= pc and body > _body(po, pc):
        add("Bullish engulfing", +1, "Green candle fully engulfs the previous red candle")
    if c < o and pc > po and o >= pc and c <= po and body > _body(po, pc):
        add("Bearish engulfing", -1, "Red candle fully engulfs the previous green candle")
    if len(C) >= 3:
        o2, c2, o1, c1 = O[-3], C[-3], O[-2], C[-2]
        if c2 < o2 and abs(c1 - o1) < 0.35 * abs(c2 - o2) and c > o and c > (o2 + c2) / 2:
            add("Morning star", +1, "Down candle, small candle, then a strong up candle (reversal up)")
        if c2 > o2 and abs(c1 - o1) < 0.35 * abs(c2 - o2) and c < o and c < (o2 + c2) / 2:
            add("Evening star", -1, "Up candle, small candle, then a strong down candle (reversal down)")
    if hi < H[-2] and lo > L[-2]:
        add("Inside bar", 0, "Range contained within the previous session (consolidation)")
    gap = (o / C[-2] - 1) * 100
    if gap >= 1.0:
        add("Gap up", +1, f"Opened {gap:.1f}% above the previous close")
    elif gap <= -1.0:
        add("Gap down", -1, f"Opened {abs(gap):.1f}% below the previous close")
    return out


def support_resistance(hist, price, k=3, lookback=90):
    """Nearest support below and resistance above `price`, from swing points, moving averages and 20-day extremes."""
    if hist is None or len(hist) < 25:
        return {"support": [], "resistance": []}
    h = hist.tail(lookback)
    H, L, C = h["High"].astype(float), h["Low"].astype(float), h["Close"].astype(float)
    cands = {}
    for i in range(k, len(h) - k):
        if H.iloc[i] == H.iloc[i - k:i + k + 1].max():
            cands[round(float(H.iloc[i]), 2)] = "swing high"
        if L.iloc[i] == L.iloc[i - k:i + k + 1].min():
            cands[round(float(L.iloc[i]), 2)] = "swing low"
    cands[round(float(H.tail(20).max()), 2)] = "20-day high"
    cands[round(float(L.tail(20).min()), 2)] = "20-day low"
    for span, name in ((20, "EMA20"), (50, "SMA50")):
        v = ind.ema(hist["Close"].astype(float), 20).iloc[-1] if span == 20 and name == "EMA20" else hist["Close"].astype(float).rolling(50).mean().iloc[-1]
        if pd.notna(v):
            cands[round(float(v), 2)] = name
    sup = sorted([(p, n) for p, n in cands.items() if p < price * 0.9995], reverse=True)[:3]
    res = sorted([(p, n) for p, n in cands.items() if p > price * 1.0005])[:3]
    return {"support": [{"price": p, "kind": n} for p, n in sup], "resistance": [{"price": p, "kind": n} for p, n in res]}
