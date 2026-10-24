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


def quality_fails(c, h, l):
    """Price-independent tradability checks (apply equally to penny and high-priced stocks).

    Locked-circuit sessions (high == low) and repeated +/-9.5% daily jumps are classic signs of a stock that is
    manipulated, suspended-and-resumed or impossible to exit at the stop price."""
    fails = []
    try:
        if int(((h.iloc[-5:] - l.iloc[-5:]).abs() < 1e-9).sum()) >= 2:
            fails.append("Locked-circuit sessions in the last 5 days (cannot be exited reliably)")
        jumps = int((c.pct_change().iloc[-10:].abs() >= 0.095).sum())
        if jumps >= config.MAX_ERRATIC_JUMPS:
            fails.append(f"Erratic: {jumps} daily moves of +/-9.5% in the last 10 sessions (manipulation / circuit risk)")
    except Exception:
        pass
    return fails


def completed(hist, session_date):
    """Daily candles of sessions that are finished (drops today's partial candle if the provider returns one)."""
    return hist[[d < session_date for d in hist.index.date]]


def evaluate(hist, price, day_volume=None, now=None, market_open=True, enforce_filters=True):
    """Return a dict: decision (FILTERED/REJECT/WATCH/SIGNAL), score, checks, metrics, stop/targets.

    enforce_filters=False (stock-analysis page) still scores a stock that fails the price/liquidity/quality filters and lists
    them in res["filter_fails"]; it can then never be a SIGNAL."""
    res = {"decision": "FILTERED", "score": 0, "checks": [], "metrics": {}, "hard_fails": [], "filter_fails": [], "headline": ""}
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
    res["metrics"]["penny"] = price < config.PENNY_PRICE
    res["hard_fails"] += quality_fails(c, h, l)
    if res["hard_fails"] and enforce_filters:
        res["headline"] = res["hard_fails"][0]
        return res
    res["filter_fails"] = list(res["hard_fails"])

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
    if res["filter_fails"]:
        res["decision"] = "FILTERED"
    elif score >= config.MIN_SIGNAL_SCORE and not blockers:
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


# ---------------------------------------------------------------- strategy classification
INTRADAY, SHORT_TERM = "INTRADAY", "SHORT_TERM"
LABEL = {INTRADAY: "Intraday", SHORT_TERM: "Short-Term"}


def _clip(x, lo, hi):
    return max(lo, min(hi, x))


def day_change_pct(hist, price):
    """Move versus the previous COMPLETED session's close."""
    try:
        return (price / float(hist["Close"].iloc[-1]) - 1) * 100
    except Exception:
        return 0.0


def levels(strategy, price, atr):
    """Stop / targets / risk for a strategy. Short-Term keeps the original swing rules unchanged."""
    if strategy == INTRADAY:
        mult, cap, r1, r2 = config.INTRADAY_STOP_ATR, config.INTRADAY_MAX_STOP_PCT, config.INTRADAY_T1_R, config.INTRADAY_T2_R
    else:
        mult, cap, r1, r2 = config.ATR_STOP_MULT, config.MAX_STOP_PCT, config.TARGET1_R, config.TARGET2_R
    risk = min(mult * atr, price * cap / 100)
    return {"risk": risk, "stop": price - risk, "target1": price + r1 * risk, "target2": price + r2 * risk,
            "rr1": r1, "rr2": r2, "capped": mult * atr > price * cap / 100}


def intraday_check(m, day_chg, price, now, market_open, quote=None):
    """Return (qualifies, passes, fails) for the Intraday strategy using metrics from evaluate()."""
    ok, bad = [], []
    atr_pct = m.get("atr", 0) / price * 100 if price else 0

    def chk(cond, good, fail):
        (ok if cond else bad).append(good if cond else fail)

    chk(config.INTRADAY_ENABLED, "intraday enabled", "intraday strategy disabled in config")
    chk(market_open, "market open", "market closed - intraday needs a live session")
    if market_open and now is not None:
        chk(now.time() <= config.INTRADAY_ENTRY_CUTOFF, "enough session left",
            f"after {config.INTRADAY_ENTRY_CUTOFF:%H:%M} - too little session left before the {config.INTRADAY_SQUARE_OFF:%H:%M} square-off")
    vr = m.get("volume_ratio", 0)
    chk(vr >= config.INTRADAY_MIN_VOL_RATIO, f"volume surge {vr:.1f}x", f"volume {vr:.1f}x < {config.INTRADAY_MIN_VOL_RATIO:g}x needed for intraday")
    chk(config.INTRADAY_MIN_ATR_PCT <= atr_pct <= config.INTRADAY_MAX_ATR_PCT, f"daily range {atr_pct:.1f}% of price (ATR)",
        f"ATR {atr_pct:.1f}% outside {config.INTRADAY_MIN_ATR_PCT:g}-{config.INTRADAY_MAX_ATR_PCT:g}% (too quiet or too wild)")
    chk(config.INTRADAY_MIN_DAY_GAIN <= day_chg <= config.INTRADAY_MAX_DAY_GAIN, f"up {day_chg:+.1f}% today with room left",
        f"day move {day_chg:+.1f}% outside {config.INTRADAY_MIN_DAY_GAIN:g} to {config.INTRADAY_MAX_DAY_GAIN:g}% (no momentum yet, or already extended)")
    chk(m.get("avg_turnover_20", 0) >= config.INTRADAY_MIN_TURNOVER, "high turnover",
        f"turnover Rs{m.get('avg_turnover_20', 0) / 1e7:.1f} cr < Rs{config.INTRADAY_MIN_TURNOVER / 1e7:.1f} cr needed for intraday")
    ph = m.get("prior_20d_high", 0)
    chk(ph > 0 and price <= ph * 1.015, "fresh breakout (within 1.5% of the level)",
        "already more than 1.5% above the breakout level (late entry)")
    return not bad, ok, bad


def build_plan(ev, hist, price, now=None, market_open=True, quote=None, market=None):
    """Classify an entry signal as INTRADAY or SHORT_TERM *at signal time* and describe the whole trade plan.

    Never returns a long-term plan. `blocks` lists reasons the entry should not be taken (spread, circuit,
    weak index); the caller decides whether to trade."""
    m = ev["metrics"]
    now = now or config.now_ist()
    day_chg = day_change_pct(hist, price)
    atr = m.get("atr", 0.0)
    intr_ok, intr_pass, intr_fail = intraday_check(m, day_chg, price, now, market_open, quote)
    strat = INTRADAY if intr_ok else SHORT_TERM
    lv = levels(strat, price, atr)
    ph = float(m.get("prior_20d_high", 0))
    ext_atr = (price - m.get("ema20", price)) / atr if atr else 0.0
    extended = ext_atr > config.EXTENDED_ENTRY_ATR or day_chg > 8.0
    if strat == INTRADAY:
        hold = f"Same session - exit by {config.INTRADAY_SQUARE_OFF:%H:%M} IST (no overnight carry)" if not config.INTRADAY_ALLOW_CARRY \
            else "Same session; carry allowed by configuration"
        why = ("Intraday: " + "; ".join(intr_pass[:5]) + ". Tight ATR stop, quick targets, forced square-off.")
        inval = [f"Price falls back below the breakout level Rs{ph:.2f}", f"Stop-loss Rs{lv['stop']:.2f} is hit",
                 "Volume dries up / momentum fades before the first target", f"Still open at {config.INTRADAY_SQUARE_OFF:%H:%M} IST"]
        max_hold, be_r, trail = 1, config.INTRADAY_BE_R, config.INTRADAY_TRAIL_ATR
    else:
        hold = f"About 1-4 weeks (hard limit {config.MAX_HOLD_SESSIONS} trading sessions)"
        why = ("Short-Term swing: daily breakout above the 20-day high with volume and trend support; it did not qualify for "
               "intraday because " + (intr_fail[0] if intr_fail else "of its profile") + ".")
        inval = [f"Daily close back below the breakout level Rs{ph:.2f} (breakout fails)",
                 "Daily close below EMA20 together with negative MACD", f"Stop-loss Rs{lv['stop']:.2f} is hit (raised to break-even after +{config.BREAKEVEN_AT_R:g}R)",
                 f"No target within {config.MAX_HOLD_SESSIONS} sessions (time exit)"]
        max_hold, be_r, trail = config.MAX_HOLD_SESSIONS, config.BREAKEVEN_AT_R, config.TRAIL_ATR_MULT
    blocks = []
    q = quote or {}
    if q.get("spread_pct") is not None and q["spread_pct"] > config.MAX_SPREAD_PCT:
        blocks.append(f"bid-ask spread {q['spread_pct']:.2f}% > {config.MAX_SPREAD_PCT:g}% (hard to enter/exit)")
    if q.get("at_upper_circuit"):
        blocks.append("stock is at its upper circuit limit (buy cannot be filled realistically)")
    if q.get("at_lower_circuit"):
        blocks.append("stock is at its lower circuit limit")
    if market and market.get("available") and config.MARKET_FILTER_ENABLED:
        if market.get("day_change_pct", 0) <= config.MARKET_BEARISH_DAY_PCT:
            blocks.append(f"index is {market['day_change_pct']:+.1f}% today (market-wide selling)")
        elif strat == SHORT_TERM and market.get("trend") == "BEARISH":
            blocks.append("broader market trend is bearish (new Short-Term entries paused)")
    return {"strategy": strat, "label": LABEL[strat], "expected_hold": hold, "entry": price,
            "entry_low": ph if ph and ph < price else price, "entry_high": price * 1.005,
            "stop": lv["stop"], "target1": lv["target1"], "target2": lv["target2"], "initial_risk": lv["risk"],
            "rr1": lv["rr1"], "rr2": lv["rr2"], "why": why, "invalidation": inval, "max_hold_sessions": max_hold,
            "be_r": be_r, "trail_mult": trail, "entry_level": ph, "extended": extended, "ext_atr": ext_atr,
            "day_change_pct": day_chg, "intraday_pass": intr_pass, "intraday_fail": intr_fail, "blocks": blocks,
            "stop_capped": lv["capped"], "penny": price < config.PENNY_PRICE}


# ---------------------------------------------------------------- capital allocation
def allocate(price, stop, score, cash, held_count, capital=None, avg_turnover=None, extended=False):
    """Decide how many shares to buy so up to MAX_OPEN_POSITIONS positions stay fundable.

    quantity = min of: fair share of free cash per empty slot (scaled by signal strength), the per-position cap,
    cash minus a reserve for the other empty slots, the 1R risk budget, and a liquidity participation cap.
    Returns dict(qty, value, limit, why)."""
    capital = capital or config.CAPITAL
    slots_left = config.MAX_OPEN_POSITIONS - held_count
    out = {"qty": 0, "value": 0.0, "limit": "", "why": ""}
    if slots_left <= 0:
        out["why"] = f"all {config.MAX_OPEN_POSITIONS} position slots are in use"
        return out
    risk_ps = price - stop
    if price <= 0 or risk_ps <= 0:
        out["why"] = "stop is not below the entry price"
        return out
    slot_budget = capital / config.MAX_OPEN_POSITIONS
    strength = config.ALLOC_STRENGTH_MIN + (config.ALLOC_STRENGTH_MAX - config.ALLOC_STRENGTH_MIN) * _clip(
        (score - config.MIN_SIGNAL_SCORE) / max(1, 100 - config.MIN_SIGNAL_SCORE), 0, 1)
    if extended:
        strength *= 0.7
    cost = 1 + config.COST_PCT_PER_SIDE / 100
    limits = {
        "signal-strength share of free cash per empty slot": max(0.0, cash) / slots_left * strength,
        f"max {config.MAX_POSITION_PCT:g}% of capital per stock": capital * config.MAX_POSITION_PCT / 100,
        "free cash after reserving the other empty slots": cash / cost - (slots_left - 1) * slot_budget * config.SLOT_RESERVE_FRACTION,
        f"{config.RISK_PER_TRADE_PCT:g}% risk-per-trade at the stop": capital * config.RISK_PER_TRADE_PCT / 100 / risk_ps * price,
    }
    if avg_turnover:
        limits[f"{config.MAX_PARTICIPATION_PCT:g}% of average daily turnover (liquidity)"] = avg_turnover * config.MAX_PARTICIPATION_PCT / 100
    name, alloc = min(limits.items(), key=lambda kv: kv[1])
    qty = int(math.floor(max(0.0, alloc) / price))
    out.update(limit=name, qty=qty, value=round(qty * price, 2))
    if qty < 1:
        out["why"] = (f"Rs{max(0.0, alloc):,.0f} allowed for one position (limited by {name}) is less than the share price Rs{price:,.2f}"
                      if cash > price else f"free cash Rs{max(0.0, cash):,.0f} is below the share price Rs{price:,.2f}")
    return out


def position_size(price, stop, capital=None, open_value=0.0):
    """(legacy helper, kept for compatibility) Shares such that loss at the stop is RISK_PER_TRADE_PCT of capital."""
    capital = capital or config.CAPITAL
    risk_per_share = price - stop
    if risk_per_share <= 0:
        return 0
    by_risk = capital * config.RISK_PER_TRADE_PCT / 100 / risk_per_share
    by_cap = capital * 25 / 100 / price
    by_free = max(0.0, capital - open_value) / price
    return int(max(0, math.floor(min(by_risk, by_cap, by_free))))


# ---------------------------------------------------------------- exits for open positions
def initial_risk_of(pos):
    r = pos.get("initial_risk") or 0
    if r and r > 0:
        return r
    atr_v = pos.get("entry_atr") or 0
    bp = pos.get("buy_price") or 0
    return min(config.ATR_STOP_MULT * atr_v, bp * config.MAX_STOP_PCT / 100) if (atr_v and bp) else 0


def manage(pos, price, hist, held_sessions, now=None):
    """Decide what to do with an open paper position (hard rules only; soft warnings live in monitor.py).

    Returns dict(action=EXIT|PARTIAL|HOLD, reason, qty, new_stop, peak, t1_hit)."""
    now = now or config.now_ist()
    strat = pos.get("strategy") or SHORT_TERM
    peak = max(pos["peak_price"], price)
    stop, qty, t1_hit = pos["stop_loss"], pos["quantity"], pos["target1_hit"]
    out = {"action": "HOLD", "reason": "", "qty": 0, "new_stop": stop, "peak": peak, "t1_hit": t1_hit}
    if price <= stop:
        return {**out, "action": "EXIT", "qty": qty, "reason": "Stop-loss hit" if stop < pos["buy_price"] else "Protective stop hit"}
    if strat == INTRADAY and not config.INTRADAY_ALLOW_CARRY:
        bday = str(pos.get("buy_time") or "")[:10]
        if bday and now.date().isoformat() > bday:
            return {**out, "action": "EXIT", "qty": qty, "reason": "Intraday position carried past its session - closed at first check"}
        if now.time() >= config.INTRADAY_SQUARE_OFF:
            return {**out, "action": "EXIT", "qty": qty, "reason": f"Intraday square-off at {config.INTRADAY_SQUARE_OFF:%H:%M}"}
    atr_v = pos["entry_atr"] or 0
    if not t1_hit and price >= pos["target1"]:
        out["t1_hit"], out["new_stop"] = True, max(stop, pos["buy_price"])
        if qty >= 2:
            return {**out, "action": "PARTIAL", "qty": qty // 2, "reason": "Target 1 reached: sold half, stop moved to break-even"}
        out["reason"] = "Target 1 reached: stop moved to break-even"
    risk = initial_risk_of(pos)
    be_r = pos.get("be_r") if pos.get("be_r") not in (None, "") else (config.INTRADAY_BE_R if strat == INTRADAY else config.BREAKEVEN_AT_R)
    be_r = float(be_r or 0)
    if not out["t1_hit"] and be_r > 0 and risk > 0 and peak - pos["buy_price"] >= be_r * risk:
        be_stop = pos["buy_price"] * (1 + 2 * config.COST_PCT_PER_SIDE / 100)   # break-even incl. round-trip cost
        if be_stop > out["new_stop"]:
            out["new_stop"] = be_stop
            out["reason"] = f"Stop raised to break-even after +{be_r:g}R of open profit"
    trail = float(pos.get("trail_mult") or (config.INTRADAY_TRAIL_ATR if strat == INTRADAY else config.TRAIL_ATR_MULT))
    if atr_v > 0 and (out["t1_hit"] or (strat == INTRADAY and risk > 0 and peak - pos["buy_price"] >= risk)):
        out["new_stop"] = max(out["new_stop"], peak - trail * atr_v)
    if price >= pos["target2"]:
        return {**out, "action": "EXIT", "qty": qty, "reason": "Target 2 reached"}
    if hist is not None and len(hist) >= 30 and held_sessions >= 2:
        c = hist["Close"].astype(float)
        if c.iloc[-1] < ind.ema(c, 20).iloc[-1] and ind.macd(c)[2].iloc[-1] < 0:
            return {**out, "action": "EXIT", "qty": qty, "reason": "Trend broke: closed below EMA20 with weak MACD"}
    max_hold = int(float(pos.get("max_hold_sessions") or 0)) or config.MAX_HOLD_SESSIONS
    if strat != INTRADAY and held_sessions >= max_hold:
        return {**out, "action": "EXIT", "qty": qty, "reason": f"Time exit after {held_sessions} sessions"}
    return out
