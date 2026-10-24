"""Next-session market and stock outlook (pre-market risk assessment).

Honest by construction: every item is tagged DATA (a confirmed reading with its own timestamp) or ESTIMATE (a model
inference). Opening prices are never claimed - gap figures are statistical ranges from history. Missing inputs are listed as
limitations and lower the confidence."""
import re

import config
import market
import monitor
import news
import pnl
import portfolio
import providers
import reports_util
import store
import strategy
import technicals

EVENT_WORDS = {"RBI policy / rates": ("rbi", "repo rate", "mpc"), "Inflation / GDP data": ("cpi", "inflation", "gdp", "iip"),
               "US Fed / macro": ("fed ", "fomc", "powell", "us jobs", "nonfarm"), "Budget / policy": ("budget", "gst council", "sebi"),
               "Results season": ("q2 results", "q3 results", "earnings", "quarterly results", "q1 results"),
               "Derivatives expiry": ("expiry", "f&o")}
LIMITS = ["Opening prices are unknown until the market opens at 09:15 IST; the gap ranges below are statistical (last ~60 sessions), not forecasts of tomorrow's open.",
          "Pre-market (09:00-09:08) quotes are not used: they exist only on the morning itself and no reliable feed is configured.",
          "This is a probabilistic risk assessment, not a prediction or advice. Re-run it (Refresh) when new information arrives."]


def _stamp():
    return config.now_ist().strftime("%Y-%m-%d %H:%M IST")


def _chg(x):
    return None if x is None else x.get("change_pct")


def market_outlook():
    ctx = market.context()
    glob = market.global_snapshot()
    sect = market.sector_snapshot()
    gift = market.quote_change(config.GIFT_NIFTY_TICKER) if config.GIFT_NIFTY_TICKER else None
    titles = news.headlines_for("Sensex Nifty stock market India today", 12) + news.headlines_for("Wall Street Fed global markets", 6)
    sent, neg, pos = news.keyword_sentiment(titles)
    ev, score, avail, total = [], 0.0, 0, 0

    def add(label, value, pts, kind, as_of="", note=""):
        nonlocal score, avail, total
        total += 1
        if value is not None:
            avail += 1
            score += pts
        ev.append({"label": label, "value": "unavailable" if value is None else value, "points": pts if value is not None else 0,
                   "kind": kind, "as_of": as_of, "note": note})

    if ctx["available"]:
        p = (1 if ctx["trend"] == "BULLISH" else -1 if ctx["trend"] == "BEARISH" else 0) + (1 if ctx["day_change_pct"] > 0.7 else -1 if ctx["day_change_pct"] < -0.7 else 0)
        add("Nifty 50 trend & last session", f"{ctx['trend']}, {ctx['day_change_pct']:+.2f}% (close {ctx['close']:.0f}; EMA20 {ctx['ema20']:.0f}, SMA50 {ctx['sma50']:.0f})", p, "DATA", ctx["as_of"])
    else:
        add("Nifty 50 trend & last session", None, 0, "DATA")
    g = {x["name"]: x for x in glob}
    us = [_chg(g.get("S&P 500 (US)")), _chg(g.get("Nasdaq (US)"))]
    us = [x for x in us if x is not None]
    add("US overnight (S&P 500 / Nasdaq)", (f"{sum(us) / len(us):+.2f}% average" if us else None),
        (1 if us and sum(us) / len(us) > 0.5 else -1 if us and sum(us) / len(us) < -0.5 else 0), "DATA", (g.get("S&P 500 (US)") or {}).get("as_of", ""))
    asia = [_chg(g.get("Nikkei 225 (Japan)")), _chg(g.get("Hang Seng (HK)"))]
    asia = [x for x in asia if x is not None]
    add("Asia (Nikkei / Hang Seng)", (f"{sum(asia) / len(asia):+.2f}% average" if asia else None),
        (0.5 if asia and sum(asia) / len(asia) > 0.5 else -0.5 if asia and sum(asia) / len(asia) < -0.5 else 0), "DATA", (g.get("Nikkei 225 (Japan)") or {}).get("as_of", ""))
    iv, uv = g.get("India VIX"), g.get("US VIX")
    add("India VIX (fear gauge)", (f"{iv['last']} ({iv['change_pct']:+.1f}%)" if iv and iv.get("last") else None),
        (-1 if iv and iv.get("change_pct") and iv["change_pct"] > 5 else 0), "DATA", (iv or {}).get("as_of", ""))
    add("US VIX", (f"{uv['last']} ({uv['change_pct']:+.1f}%)" if uv and uv.get("last") else None),
        (-0.5 if uv and uv.get("change_pct") and uv["change_pct"] > 8 else 0), "DATA", (uv or {}).get("as_of", ""))
    cr, fx = g.get("Brent crude"), g.get("USD/INR")
    add("Brent crude", (f"{cr['last']} ({cr['change_pct']:+.1f}%)" if cr and cr.get("last") else None),
        (-0.5 if cr and cr.get("change_pct") and cr["change_pct"] > 2 else 0), "DATA", (cr or {}).get("as_of", ""), "higher crude is negative for India")
    add("USD/INR", (f"{fx['last']} ({fx['change_pct']:+.2f}%)" if fx and fx.get("last") else None),
        (-0.5 if fx and fx.get("change_pct") and fx["change_pct"] > 0.4 else 0), "DATA", (fx or {}).get("as_of", ""), "weaker rupee is a mild negative")
    if config.GIFT_NIFTY_TICKER:
        add("Index futures proxy", (f"{gift['change_pct']:+.2f}%" if gift else None), (1 if gift and gift["change_pct"] > 0.4 else -1 if gift and gift["change_pct"] < -0.4 else 0), "DATA", (gift or {}).get("as_of", ""))
    add("Headline sentiment (keyword scan)", (f"{len(pos)} positive / {len(neg)} negative of {len(titles)} headlines" if titles else None),
        (1 if sent > 0.3 else -1 if sent < -0.3 else 0), "ESTIMATE", _stamp())
    events = sorted({k for t in titles for k, ws in EVENT_WORDS.items() if any(w in t.lower() for w in ws)})
    direction = "Bullish" if score >= 1.5 else ("Bearish" if score <= -1.5 else "Neutral")
    conf = "High" if avail >= 0.75 * total and avail >= 6 else ("Medium" if avail >= 0.5 * total else "Low")
    limits = list(LIMITS)
    if not config.GIFT_NIFTY_TICKER:
        limits.insert(1, "No index-futures / GIFT Nifty feed is configured (set GIFT_NIFTY_TICKER to a Yahoo ticker to add one).")
    miss = [e["label"] for e in ev if e["value"] == "unavailable"]
    if miss:
        limits.append("Unavailable right now: " + ", ".join(miss))
    return {"direction": direction, "score": round(score, 2), "confidence": conf, "evidence": ev, "events": events,
            "sectors": sect, "headlines": titles[:8], "limitations": limits, "data_points": f"{avail}/{total}", "ctx": ctx}


def _stock(sym, pos, mkt, tag):
    """Outlook for one stock (held position or shortlisted candidate)."""
    out = {"symbol": sym, "company": portfolio.company_name(sym), "kind": tag, "evidence": [], "ok": False}
    try:
        hist = providers.load_history(sym)
        now = config.now_ist()
        hc = technicals.latest_complete(hist, now)
        price = float(hc["Close"].iloc[-1])
        try:
            q = providers.load_quote(sym)
            live = q["price"] if config.market_is_open() else price
        except Exception:
            q, live = None, price
        s = technicals.snapshot(hc, price)
    except Exception as exc:
        out["error"] = f"data unavailable: {str(exc)[:120]}"
        out.update(direction="Unknown", action="Monitor Closely" if pos else "Avoid New Entry", confidence="Low",
                   gap_risk="Unknown", thesis_valid=None, evidence=[f"[DATA] {out['error']}"])
        return out
    sc, ev = 0.0, []

    def add(pts, text, kind="DATA"):
        nonlocal sc
        sc += pts
        ev.append(f"[{kind}] {text} ({pts:+g})")

    add(1 if s["trend"] == "UPTREND" else -1 if s["trend"] == "DOWNTREND" else 0, f"daily trend {s['trend'].lower()} (close {price:.2f}, EMA20 {s['ema20']:.2f})")
    if 50 <= s["rsi"] <= 70:
        add(0.5, f"RSI {s['rsi']:.0f} healthy")
    elif s["rsi"] < 45:
        add(-0.5, f"RSI {s['rsi']:.0f} weak")
    elif s["rsi"] > 75:
        add(-0.5, f"RSI {s['rsi']:.0f} overheated (pullback risk)")
    if s["momentum"] != "mixed":
        add(0.5 if s["momentum"] == "positive" else -0.5, f"MACD histogram {s['momentum']}")
    pat = max(-1.0, min(1.0, sum(p["bias"] for p in s["patterns"] if p["name"] not in ("Gap up", "Gap down")) * 0.5))
    if pat:
        add(pat, "last-session candle: " + ", ".join(p["name"] for p in s["patterns"] if p["bias"]))
    if s["close_location"] >= 0.7:
        add(0.5, f"closed near the day's high ({s['close_location']:.0%} of range)")
    elif s["close_location"] <= 0.3:
        add(-0.5, f"closed near the day's low ({s['close_location']:.0%} of range)")
    if s["last_volume_ratio"] >= 1.5:
        add(0.5 if s["last_day_move_pct"] > 0 else -1.0, f"{s['last_volume_ratio']:.1f}x volume on a {s['last_day_move_pct']:+.1f}% day")
    sup = s["support"][0]["price"] if s["support"] else None
    res = s["resistance"][0]["price"] if s["resistance"] else None
    if sup and price < sup:
        add(-1, f"below support {sup:.2f}")
    if res and price >= res * 0.99:
        add(-0.5, f"within 1% of resistance {res:.2f}")
    thesis = None
    if pos:
        try:
            held = strategy.manage(pos, live, hc, 1, now)
            a = monitor.assess(pos, live, hc, held, q, mkt["ctx"] if mkt else None, 1, count_scan=False)
            thesis = a["thesis_valid"]
            if a["score"] >= config.WARN_SCORE:
                add(-1, "position warnings: " + "; ".join(w["text"] for w in a["warnings"][:2]))
            if not thesis:
                add(-1, "original buy thesis no longer valid")
            out["state"] = a["state"]
            out["position"] = {"qty": pos["quantity"], "buy": pos["buy_price"], "stop": pos["stop_loss"], "t1": pos["target1"], "strategy": pos["strategy"]}
        except Exception:
            pass
    titles = news.headlines_for(f"{sym} share NSE", 6)
    ns, nneg, npos = news.keyword_sentiment(titles)
    if titles and (nneg or npos):
        add(-1 if ns < -0.3 else 0.5 if ns > 0.3 else 0, f"{len(nneg)} negative / {len(npos)} positive headlines of {len(titles)}", "ESTIMATE")
    elif not titles:
        ev.append("[DATA] no recent headlines found")
    corp = sorted({k for t in titles for k, ws in EVENT_WORDS.items() if any(w in t.lower() for w in ws)})
    if mkt and mkt["direction"] != "Neutral":
        add(0.5 if mkt["direction"] == "Bullish" else -0.5, f"market outlook {mkt['direction']}", "ESTIMATE")
    direction = "Bullish" if sc >= 1.5 else "Bearish" if sc <= -1.5 else "Neutral"
    gs = s["gap_stats"]
    weak_close = s["close_location"] <= 0.3
    gap_risk = "Elevated gap-down risk" if (direction == "Bearish" or (weak_close and (mkt or {}).get("direction") == "Bearish") or
                                             (nneg and ns < -0.3)) else ("Moderate" if direction == "Neutral" else "Low")
    if pos:
        if thesis is False or out.get("state") in (monitor.EXIT_REC, monitor.STOP):
            action = "Exit Recommended"
        elif direction == "Bearish" or gap_risk.startswith("Elevated") or out.get("state") == monitor.WARN or (sup and price < sup):
            action = "Monitor Closely"
        else:
            action = "Hold"
    else:
        action = "Avoid New Entry" if (direction == "Bearish" or gap_risk.startswith("Elevated") or (mkt or {}).get("direction") == "Bearish") else "Monitor Closely"
    pos_pts = sum(1 for e in ev if re.search(r"\(\+", e))
    neg_pts = sum(1 for e in ev if re.search(r"\(-", e))
    aligned = pos_pts if direction == "Bullish" else neg_pts if direction == "Bearish" else min(pos_pts, neg_pts) + 1
    conf = "High" if aligned >= 4 else "Medium" if aligned >= 2 else "Low"
    out.update(ok=True, price=round(price, 2), as_of=s["last_session"], direction=direction, score=round(sc, 2), gap_risk=gap_risk,
               action=action, confidence=conf, thesis_valid=thesis, evidence=ev, events=corp,
               support=s["support"][:2], resistance=s["resistance"][:2], patterns=[p["name"] for p in s["patterns"]],
               gap_range=(f"{gs['p10']:+.1f}% to {gs['p90']:+.1f}% (typical, last {gs['sessions']} sessions; estimate)" if gs else "n/a"),
               rsi=round(s["rsi"], 1), trend=s["trend"])
    return out


def generate(save=True, max_candidates=8):
    """Build the outlook for the market, every open position and the top watchlist candidates; persist to data/outlook.json."""
    mk = market_outlook()
    rows = []
    for p in portfolio.positions():
        rows.append(_stock(p["symbol"], p, mk, "POSITION"))
    held = {p["symbol"] for p in portfolio.positions()}
    wl = [w for w in reports_util.watchlist() if w["symbol"] not in held][:max_candidates]
    for w in wl:
        rows.append(_stock(w["symbol"], None, mk, "CANDIDATE"))
    today = config.now_ist().date()
    open_later = config.is_trading_day(today) and config.now_ist().time() <= config.MARKET_CLOSE
    nxt = today if open_later else config.next_trading_day(today)
    attention = [r["symbol"] for r in rows if r["kind"] == "POSITION" and r.get("action") in ("Exit Recommended", "Monitor Closely")]
    out = {"generated": _stamp(), "for_session": nxt.isoformat(), "market": mk, "stocks": rows, "attention": attention}
    if save:
        store.write_json("outlook.json", out)
    return out


def load():
    return store.read_json("outlook.json", {})
