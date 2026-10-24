"""Soft (thesis-based) monitoring of open positions on every scan.

strategy.manage() applies the HARD rules (stop, targets, square-off, trend-break, time exit). This module adds the
early-warning layer: it re-checks whether the original buy thesis is still valid and turns warning points into one of
the position states. A single noisy reading never sells anything: an exit needs EXIT_WARN_SCORE points on
EXIT_CONFIRM_SCANS consecutive scans (hysteresis), and hard stop-loss always has priority.
"""
import config
import indicators as ind
import patterns
import strategy

NEW_BUY = "NEW_BUY_OPPORTUNITY"
HOLD = "CONTINUE_HOLDING"
WARN = "HOLD_WITH_WARNING"
EXIT_REC = "EXIT_RECOMMENDED"
STOP = "STOP_LOSS_TRIGGERED"
TARGET = "TARGET_ACHIEVED"
CLOSED = "POSITION_CLOSED"
STATE_LABEL = {NEW_BUY: "New buy opportunity", HOLD: "Continue holding", WARN: "Hold with warning", EXIT_REC: "Exit recommended",
               STOP: "Stop-loss triggered", TARGET: "Target achieved", CLOSED: "Position closed"}


def _f(v, d=0.0):
    try:
        return float(v)
    except (TypeError, ValueError):
        return d


def warnings_for(pos, price, hist, quote=None, market=None, held_sessions=1):
    """List of dict(code, text, pts) describing what has deteriorated since entry. `hist` = completed daily candles."""
    w = []
    buy = pos["buy_price"]
    atr = pos.get("entry_atr") or 0
    risk = strategy.initial_risk_of(pos)
    level = pos.get("entry_level") or 0
    strat = pos.get("strategy") or strategy.SHORT_TERM
    if level and atr:
        tol = config.BREAKDOWN_TOL_ATR * atr
        if price < level - tol:
            w.append({"code": "BREAKOUT_LOST", "pts": 2,
                      "text": f"Price {price:.2f} is back below the breakout level {level:.2f} (beyond the {tol:.2f} noise buffer)"})
    peak = max(pos["peak_price"], price)
    if risk > 0 and peak - buy >= risk:
        gain_peak, gain_now = peak - buy, price - buy
        if gain_now <= gain_peak * (1 - config.GIVEBACK_PCT / 100):
            w.append({"code": "GIVEBACK", "pts": 2,
                      "text": f"Gave back {100 * (1 - max(gain_now, 0) / gain_peak):.0f}% of the peak open profit (peak {peak:.2f})"})
    if strat == strategy.INTRADAY and risk > 0 and price < buy - 0.5 * risk:
        w.append({"code": "NOT_WORKING", "pts": 1, "text": "Intraday trade is more than half-way to the stop without follow-through"})
    if strat != strategy.INTRADAY and risk > 0 and held_sessions >= 0.6 * (pos.get("max_hold_sessions") or config.MAX_HOLD_SESSIONS) \
            and price - buy < 0.5 * risk:
        w.append({"code": "STALLED", "pts": 1, "text": f"Held {held_sessions} sessions with less than +0.5R progress (time decay)"})
    if hist is not None and len(hist) >= 30:
        c = hist["Close"].astype(float)
        e20 = float(ind.ema(c, 20).iloc[-1])
        rsi_v = float(ind.rsi(c).iloc[-1])
        mh = ind.macd(c)[2]
        if price < e20:
            w.append({"code": "BELOW_EMA20", "pts": 2, "text": f"Price {price:.2f} is below the 20-day EMA {e20:.2f} (trend support lost)"})
        if rsi_v < 45:
            w.append({"code": "RSI_WEAK", "pts": 1, "text": f"RSI {rsi_v:.0f} has fallen below 45 (momentum weak)"})
        if float(mh.iloc[-1]) < 0 and float(mh.iloc[-1]) < float(mh.iloc[-2]):
            w.append({"code": "MACD_WEAK", "pts": 1, "text": "MACD histogram negative and still falling"})
        low5 = float(hist["Low"].astype(float).iloc[-5:].min())
        if price < low5:
            w.append({"code": "SUPPORT_BREAK", "pts": 1, "text": f"Price broke below the 5-session low {low5:.2f} (support breakdown)"})
        bear = [p for p in patterns.detect(hist) if p["bias"] < 0 and p["name"] != "Gap down"]
        if bear and price < float(hist["Close"].iloc[-1]):
            w.append({"code": "BEARISH_PATTERN", "pts": 1, "text": f"Bearish candle on the last session ({bear[0]['name']}) and price is lower"})
        prev_close = float(c.iloc[-1])
        day_chg = (price / prev_close - 1) * 100
        q = quote or {}
        dv, av = q.get("day_volume"), float(hist["Volume"].astype(float).iloc[-20:].mean())
        if dv and av:
            vr, _ = strategy.volume_ratio(av, dv, 0, None, config.market_is_open())
            if day_chg <= -3.0 and vr >= 1.8:
                w.append({"code": "HEAVY_SELLING", "pts": 2, "text": f"Down {day_chg:.1f}% today on {vr:.1f}x normal volume (distribution)"})
    if market and market.get("available") and market.get("day_change_pct", 0) <= config.MARKET_BEARISH_DAY_PCT:
        w.append({"code": "MARKET_WEAK", "pts": 1, "text": f"Index is {market['day_change_pct']:+.1f}% today"})
    return w


def assess(pos, price, hist, hard, quote=None, market=None, held_sessions=1, count_scan=True):
    """Combine the hard-rule result (`hard` from strategy.manage) with soft warnings into a position state.

    Returns dict(state, score, warnings, thesis_valid, exit_confirm, note, exit_reason). `count_scan=False` (the 5-minute
    price checks between scans) evaluates without advancing the confirmation counter."""
    ws = warnings_for(pos, price, hist, quote, market, held_sessions)
    score = sum(x["pts"] for x in ws)
    codes = {x["code"] for x in ws}
    prev_confirm = int(pos.get("exit_confirm") or 0)
    confirm = prev_confirm
    if count_scan:
        confirm = prev_confirm + 1 if score >= config.EXIT_WARN_SCORE else 0
    thesis_ok = not (score >= config.EXIT_WARN_SCORE or ("BREAKOUT_LOST" in codes and ({"BELOW_EMA20", "MACD_WEAK", "RSI_WEAK"} & codes)))
    r = (hard.get("reason") or "")
    out = {"score": score, "warnings": ws, "thesis_valid": thesis_ok, "exit_confirm": confirm, "exit_reason": ""}
    if r in ("Stop-loss hit", "Protective stop hit"):
        out.update(state=STOP, note=r)
    elif hard["action"] == "PARTIAL" or (hard["action"] == "EXIT" and r.startswith("Target")):
        out.update(state=TARGET, note=r)
    elif hard["action"] == "EXIT":
        out.update(state=EXIT_REC, note=r, exit_reason=r)
    elif confirm >= config.EXIT_CONFIRM_SCANS and score >= config.EXIT_WARN_SCORE:
        why = "; ".join(x["text"] for x in ws[:3])
        out.update(state=EXIT_REC, note=f"Thesis no longer valid on {confirm} consecutive checks: {why}",
                   exit_reason=f"Strategy deviation: {why}"[:280])
    elif score >= config.WARN_SCORE:
        out.update(state=WARN, note="; ".join(x["text"] for x in ws[:3]) +
                   (f" (exit candidate {confirm}/{config.EXIT_CONFIRM_SCANS})" if confirm else ""))
    else:
        out.update(state=HOLD, note=("; ".join(x["text"] for x in ws) if ws else "Original breakout thesis intact; no warnings"))
    return out
