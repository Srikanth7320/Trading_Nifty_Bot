"""Scored swing-breakout strategy. Every check is reported so the user can see exactly why.

Indicators use completed daily sessions; the live price decides breakout / trend position and the
live (or last) session volume decides the volume check. Nothing here sends orders.
"""
import math

import pandas as pd

import config
import indicators as ind


def _check(checks, key, label, max_pts, pts, detail):
    checks.append({"key": key, "label": label, "max": max_pts, "points": pts, "detail": detail,
                   "status": "pass" if pts >= max_pts else ("partial" if pts > 0 else "fail")})


def volume_ratio(avg_vol, day_volume, last_session_vol, now=None, market_open=True):
    """Return (ratio, basis). Intraday volume is scaled by the share of the session elapsed."""
    if avg_vol <= 0:
        return 0.0, "no average volume"
    if day_volume:
        if market_open:
            now = now or config.now_ist()
            elapsed = (now.hour * 60 + now.minute) - (9 * 60 + 15)
            frac = min(1.0, max(0.12, elapsed / 375))
            return day_volume / (avg_vol * frac), f"today so far vs 20-day avg (pace-adjusted, {frac:.0%} of day)"
        return day_volume / avg_vol, "full session vs 20-day avg"
    return last_session_vol / avg_vol, "last completed session vs 20-day avg"


def evaluate(hist, price, day_volume=None, now=None, market_open=True):
    """Return a dict: decision (FILTERED/REJECT/WATCH/SIGNAL), score, checks, metrics, stop/targets."""
    res = {"decision": "FILTERED", "score": 0, "checks": [], "metrics": {}, "hard_fails": [], "headline": ""}
    if hist is None or len(hist) < config.MIN_HISTORY:
        res["hard_fails"].append(f"Only {0 if hist is None else len(hist)} sessions of history (need {config.MIN_HISTORY})")
        res["headline"] = res["hard_fails"][0]
        return res
    c, h, l, v = (hist[k].astype(float) for k in ("Close", "High", "Low", "Volume"))
    avg_vol = float(v.iloc[-20:].mean())
    turnover = float((c * v).iloc[-20:].mean())
    res["metrics"].update(avg_volume_20=avg_vol, avg_turnover_20=turnover)
    if not (config.MIN_PRICE <= price <= config.MAX_PRICE):
        res["hard_fails"].append(f"Price Rs{price:.2f} outside Rs{config.MIN_PRICE:g}-{config.MAX_PRICE:g}")
    if avg_vol < config.MIN_AVG_VOLUME:
        res["hard_fails"].append(f"Low liquidity: avg volume {avg_vol:,.0f} < {config.MIN_AVG_VOLUME:,.0f}")
    if turnover < config.MIN_AVG_TURNOVER:
        res["hard_fails"].append(f"Low liquidity: avg turnover Rs{turnover/1e7:.2f} cr < Rs{config.MIN_AVG_TURNOVER/1e7:.2f} cr")
    if res["hard_fails"]:
        res["headline"] = res["hard_fails"][0]
        return res

    e9, e20 = ind.ema(c, 9), ind.ema(c, 20)
    s50 = c.rolling(50).mean()
    s150 = c.rolling(min(150, len(c))).mean()
    rsi_v = float(ind.rsi(c).iloc[-1])
    macd_line, macd_sig, macd_hist = ind.macd(c)
    atr_v = float(ind.atr(h, l, c).iloc[-1])
    prior_high = float(h.iloc[-20:].max())
    lo52, hi52 = float(l.tail(252).min()), float(h.tail(252).max())
    range_pos = (price - lo52) / (hi52 - lo52) if hi52 > lo52 else 0.0
    vr, basis = volume_ratio(avg_vol, day_volume, float(v.iloc[-1]), now, market_open)
    rng = (h - l)
    tight = float(rng.iloc[-5:].mean()) <= 0.9 * float(rng.iloc[-20:-5].mean())
    res["metrics"].update(rsi=rsi_v, atr=atr_v, ema9=float(e9.iloc[-1]), ema20=float(e20.iloc[-1]), sma50=float(s50.iloc[-1]),
                          prior_20d_high=prior_high, volume_ratio=vr, volume_basis=basis, range_position_52w=range_pos,
                          macd_hist=float(macd_hist.iloc[-1]))
    ch = res["checks"]
    E20, S50 = float(e20.iloc[-1]), float(s50.iloc[-1])
    pts = 15 if price > E20 > S50 else (8 if price > E20 else 0)
    _check(ch, "trend", "Trend: price > EMA20 > SMA50", 15, pts, f"price {price:.2f}, EMA20 {E20:.2f}, SMA50 {S50:.2f}")
    up = e9.iloc[-1] > e9.iloc[-4] and e20.iloc[-1] > e20.iloc[-4]
    pts = 10 if (e9.iloc[-1] > e20.iloc[-1] and up) else (5 if e9.iloc[-1] > e20.iloc[-1] else 0)
    _check(ch, "ema", "Momentum: EMA9 above EMA20 and rising", 10, pts, f"EMA9 {e9.iloc[-1]:.2f} vs EMA20 {E20:.2f}")
    _check(ch, "long", "Long-term trend: price above ~30-week average", 10, 10 if price > float(s150.iloc[-1]) else 0,
           f"150-day average {float(s150.iloc[-1]):.2f}")
    pts = 10 if 55 <= rsi_v <= 68 else (5 if (50 <= rsi_v < 55 or 68 < rsi_v <= 72) else 0)
    _check(ch, "rsi", "RSI in the 55-68 sweet spot", 10, pts, f"RSI {rsi_v:.1f}")
    mh, mh1 = float(macd_hist.iloc[-1]), float(macd_hist.iloc[-2])
    pts = (6 if (macd_line.iloc[-1] > macd_sig.iloc[-1] and mh > 0) else 0) + (4 if mh > mh1 else 0)
    _check(ch, "macd", "MACD positive and strengthening", 10, pts, f"histogram {mh:.3f} (prev {mh1:.3f})")
    broke = price > prior_high
    pts = 15 if broke else (7 if price >= 0.99 * prior_high else 0)
    _check(ch, "breakout", "Breakout above the prior 20-day high", 15, pts, f"price {price:.2f} vs 20-day high {prior_high:.2f}")
    pts = 15 if vr >= 1.5 else (10 if vr >= 1.2 else (4 if vr >= 0.9 else 0))
    _check(ch, "volume", "Volume confirmation (>=1.5x average)", 15, pts, f"{vr:.2f}x - {basis}")
    pts = 10 if range_pos >= 0.85 else (7 if range_pos >= 0.70 else (3 if range_pos >= 0.5 else 0))
    _check(ch, "strength", "Strength: near the top of the 52-week range", 10, pts, f"{range_pos:.0%} of 52-week range")
    _check(ch, "base", "Tight consolidation before the move", 5, 5 if tight else 0, "recent daily ranges narrower than before")

    score = sum(x["points"] for x in ch)
    res["score"] = score
    if not (atr_v > 0 and math.isfinite(atr_v)):
        res["hard_fails"].append("ATR unavailable")
        res["headline"] = "ATR unavailable"
        return res
    risk = min(config.ATR_STOP_MULT * atr_v, price * config.MAX_STOP_PCT / 100)
    stop = price - risk
    res.update(stop=stop, risk_per_share=risk, target1=price + config.TARGET1_R * risk, target2=price + config.TARGET2_R * risk)
    overheated = rsi_v >= config.RSI_OVERHEATED
    gates = [(broke, "no breakout above the 20-day high yet"), (vr >= 1.2, f"volume only {vr:.1f}x average (need 1.2x)"),
             (price > E20, "price below EMA20"), (rsi_v >= 50, f"RSI {rsi_v:.0f} too weak (need 50+)"),
             (not overheated, f"RSI {rsi_v:.0f} overheated (limit {config.RSI_OVERHEATED:g})")]
    blockers = [msg for ok, msg in gates if not ok]
    res["blockers"] = blockers
    if score >= config.MIN_SIGNAL_SCORE and not blockers:
        res["decision"] = "SIGNAL"
    elif score >= config.WATCH_SCORE:
        res["decision"] = "WATCH"
    else:
        res["decision"] = "REJECT"
    lost = sorted([x for x in ch if x["points"] < x["max"]], key=lambda x: x["points"] - x["max"])[:3]
    weak = "; ".join(f"{x['label'].split(':')[0].split(' (')[0]} ({x['detail']})" for x in lost)
    if res["decision"] == "SIGNAL":
        res["headline"] = f"Score {score}/100 with breakout" + (f"; weakest: {weak}" if weak else "")
    elif res["decision"] == "WATCH":
        why = "; ".join(blockers) if blockers else f"score below {config.MIN_SIGNAL_SCORE}"
        res["headline"] = f"Score {score}/100 - {why}; weakest: {weak}"
    else:
        res["headline"] = f"Score {score}/100 - weakest: {weak}"
    return res


def position_size(price, stop, capital=None, open_value=0.0):
    """Shares such that loss at the stop is RISK_PER_TRADE_PCT of capital, capped by position size."""
    capital = capital or config.CAPITAL
    risk_per_share = price - stop
    if risk_per_share <= 0:
        return 0
    by_risk = capital * config.RISK_PER_TRADE_PCT / 100 / risk_per_share
    by_cap = capital * config.MAX_POSITION_PCT / 100 / price
    by_free = max(0.0, capital - open_value) / price
    return int(max(0, math.floor(min(by_risk, by_cap, by_free))))


def manage(pos, price, hist, held_sessions):
    """Decide what to do with an open paper position.

    Returns dict(action=EXIT|PARTIAL|HOLD, reason, qty, new_stop, peak, t1_hit)."""
    peak = max(pos["peak_price"], price)
    stop, qty, t1_hit = pos["stop_loss"], pos["quantity"], pos["target1_hit"]
    out = {"action": "HOLD", "reason": "", "qty": 0, "new_stop": stop, "peak": peak, "t1_hit": t1_hit}
    if price <= stop:
        return {**out, "action": "EXIT", "qty": qty, "reason": "Stop-loss hit" if stop < pos["buy_price"] else "Protective stop hit"}
    atr_v = pos["entry_atr"] or 0
    if not t1_hit and price >= pos["target1"]:
        out["t1_hit"], out["new_stop"] = True, max(stop, pos["buy_price"])
        if qty >= 2:
            return {**out, "action": "PARTIAL", "qty": qty // 2, "reason": "Target 1 reached: sold half, stop moved to break-even"}
        out["reason"] = "Target 1 reached: stop moved to break-even"
    if out["t1_hit"] and atr_v > 0:
        out["new_stop"] = max(out["new_stop"], peak - config.TRAIL_ATR_MULT * atr_v)
    if price >= pos["target2"]:
        return {**out, "action": "EXIT", "qty": qty, "reason": "Target 2 reached"}
    if hist is not None and len(hist) >= 30 and held_sessions >= 2:
        c = hist["Close"].astype(float)
        if c.iloc[-1] < ind.ema(c, 20).iloc[-1] and ind.macd(c)[2].iloc[-1] < 0:
            return {**out, "action": "EXIT", "qty": qty, "reason": "Trend broke: closed below EMA20 with weak MACD"}
    if held_sessions >= config.MAX_HOLD_SESSIONS:
        return {**out, "action": "EXIT", "qty": qty, "reason": f"Time exit after {held_sessions} sessions"}
    return out
