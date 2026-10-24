"""On-demand, read-only stock analysis (the /analyze page). Never places or modifies a trade or a position.

Uses the bot's real rules (strategy.evaluate / build_plan / monitor) so the page agrees with what the scanner would do,
plus extra context (candles, levels, fundamentals, news). Every section degrades gracefully when its data is missing."""
import re
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutTimeout

import pandas as pd

import config
import market
import monitor
import news
import pnl
import portfolio
import providers
import store
import strategy
import technicals

try:
    import yfinance as yf
except ImportError:
    yf = None

SECTOR_INDEX = {"Financial Services": "^NSEBANK", "Technology": "^CNXIT", "Consumer Cyclical": "^CNXAUTO", "Healthcare": "^CNXPHARMA",
                "Consumer Defensive": "^CNXFMCG", "Basic Materials": "^CNXMETAL", "Energy": "^CNXENERGY", "Real Estate": "^CNXREALTY",
                "Industrials": "^CNXINFRA"}


# ---------------------------------------------------------------- search / validation
def _universe():
    try:
        df = pd.read_csv(store.path("nse_equity_symbols.csv"), dtype=str, keep_default_na=False)
    except Exception:
        return []
    df.columns = [c.strip() for c in df.columns]
    ncol = next((c for c in df.columns if "NAME" in c.upper()), None)
    scol = "Symbol" if "Symbol" in df.columns else df.columns[0]
    if "SERIES" in df.columns:
        df = df[df["SERIES"].str.strip().str.upper() == "EQ"]
    return [(str(r[scol]).strip().upper(), str(r[ncol]).strip() if ncol else "") for _, r in df.iterrows()]


def resolve(query):
    """Validate user input. Returns dict(status: ok|choose|not_found|invalid, symbol, name, matches)."""
    q = (query or "").strip()
    if not q:
        return {"status": "invalid", "message": "Enter a stock symbol (e.g. TCS) or part of a company name."}
    q = re.sub(r"^(NSE|BSE)[:\s]+", "", q, flags=re.I)
    q = re.sub(r"\.(NS|BO)$", "", q, flags=re.I).strip()
    if len(q) > 40 or not re.fullmatch(r"[A-Za-z0-9&\-\. ]+", q):
        return {"status": "invalid", "message": "Use letters, numbers, & or - only."}
    uni = _universe()
    sym = q.upper().replace(" ", "")
    for s, n in uni:
        if s == sym:
            return {"status": "ok", "symbol": s, "name": n, "exchange": "NSE (EQ series)"}
    low = q.lower()
    hits = [(s, n) for s, n in uni if low in n.lower() or s.startswith(sym)]
    hits.sort(key=lambda x: (not x[1].lower().startswith(low), not x[0].startswith(sym), len(x[1])))
    if len(hits) == 1:
        return {"status": "ok", "symbol": hits[0][0], "name": hits[0][1], "exchange": "NSE (EQ series)"}
    if hits:
        return {"status": "choose", "matches": [{"symbol": s, "name": n} for s, n in hits[:12]]}
    return {"status": "not_found", "message": f"'{q}' is not in the NSE equity list loaded by the bot (data/nse_equity_symbols.csv). "
            "Check the spelling, or try the company name. Stocks outside the EQ series (e.g. trade-for-trade) are not analysed."}


# ---------------------------------------------------------------- optional context fetchers (each isolated + time-boxed)
def _info(sym):
    if yf is None:
        return {}
    try:
        return yf.Ticker(f"{sym}.NS").info or {}
    except Exception:
        return {}


def _events(sym):
    if yf is None:
        return []
    out = []
    try:
        cal = yf.Ticker(f"{sym}.NS").calendar
        if isinstance(cal, dict):
            for k, v in cal.items():
                if v:
                    out.append(f"{k}: {v[0] if isinstance(v, (list, tuple)) and v else v}")
    except Exception:
        pass
    return out[:4]


def _news(sym):
    titles = news._headlines(sym)
    if not titles:
        return {"verdict": "UNAVAILABLE", "note": "no headlines found", "titles": []}
    r = news.news_check(sym)
    return {**r, "titles": titles[:8]}


def _timed(fn, arg, default, secs=12):
    ex = ThreadPoolExecutor(max_workers=1)
    fut = ex.submit(fn, arg)
    try:
        return fut.result(timeout=secs)
    except (FutTimeout, Exception):
        return default
    finally:
        ex.shutdown(wait=False)


def _fund(info):
    keys = [("Market cap (Rs cr)", "marketCap", lambda v: f"{v / 1e7:,.0f}"), ("P/E (trailing)", "trailingPE", lambda v: f"{v:.1f}"),
            ("P/B", "priceToBook", lambda v: f"{v:.2f}"), ("Return on equity", "returnOnEquity", lambda v: f"{v * 100:.1f}%"),
            ("Debt / equity", "debtToEquity", lambda v: f"{v:.0f}%"), ("Profit margin", "profitMargins", lambda v: f"{v * 100:.1f}%"),
            ("Revenue growth", "revenueGrowth", lambda v: f"{v * 100:.1f}%"), ("Dividend yield", "dividendYield", lambda v: f"{v:.2f}%"),
            ("Beta", "beta", lambda v: f"{v:.2f}")]
    out = []
    for label, k, fmt in keys:
        v = info.get(k)
        if isinstance(v, (int, float)) and v == v:
            out.append({"label": label, "value": fmt(v)})
    return out


# ---------------------------------------------------------------- the analysis
def _signal_label(held, state, decision, eligible, filtered, down):
    if held:
        return "Exit Recommended" if state in (monitor.EXIT_REC, monitor.STOP) else "Hold"
    if eligible:
        return "Buy Candidate"
    if filtered or down:
        return "Avoid"
    return "Wait"


def analyze(symbol, name=""):
    """Full analysis dict for template/Telegram. `error` is set (with partial data) when a critical input failed."""
    now = config.now_ist()
    res = {"symbol": symbol, "company": name or portfolio.company_name(symbol), "exchange": "NSE (EQ)", "generated": now.strftime("%Y-%m-%d %H:%M:%S IST"),
           "error": "", "limits": []}
    try:
        hist = providers.load_history(symbol)
    except Exception as exc:
        res["error"] = f"Market data unavailable for {symbol}: {str(exc)[:200]}"
        return res
    status, status_text = config.market_status(now)
    mopen = status == "OPEN"
    q, quote_err = None, ""
    try:
        q = providers.load_quote(symbol)
    except Exception as exc:
        quote_err = str(exc)[:160]
    if q:
        hc = strategy.completed(hist, q["session_date"])
        price, day_vol = q["price"], q["day_volume"]
        asof = q["time"]
    else:
        hc = technicals.latest_complete(hist, now)
        price, day_vol, asof = float(hc["Close"].iloc[-1]), None, str(hc.index[-1])
        res["limits"].append(f"Live quote unavailable ({quote_err}); using the last completed daily close.")
    if len(hc) < 30:
        res["error"] = f"Only {len(hc)} sessions of history - not enough to analyse {symbol}."
        return res
    snap = technicals.snapshot(hc, price, day_vol, mopen)
    ev = strategy.evaluate(hc, price, day_vol, now, mopen, enforce_filters=False)
    mkt = market.context() if config.MARKET_FILTER_ENABLED else None
    with ThreadPoolExecutor(max_workers=3) as ex:     # slow optional sources in parallel, time-boxed
        f_info, f_news, f_ev = ex.submit(_info, symbol), ex.submit(_news, symbol), ex.submit(_events, symbol)
        def get(f, d, t):
            try:
                return f.result(timeout=t)
            except Exception:
                return d
        info = get(f_info, {}, 10)
        nw = get(f_news, {"verdict": "UNAVAILABLE", "note": "news lookup timed out or failed", "titles": []}, 14)
        corp = get(f_ev, [], 6)
    pv = next((p for p in pnl.position_view() if p["symbol"] == symbol), None)

    # ---- overview
    prev = snap["prev_close"]
    res["overview"] = {"price": round(price, 2), "prev_close": round(prev, 2), "change": round(price - prev, 2), "change_pct": round((price / prev - 1) * 100, 2),
                       "day_volume": day_vol, "avg_volume_20": round(snap["avg_volume_20"]), "turnover_cr": round(ev["metrics"].get("avg_turnover_20", 0) / 1e7, 2),
                       "spread_pct": (q or {}).get("spread_pct"), "sector": info.get("sector") or "n/a", "industry": info.get("industry") or "n/a",
                       "high_52w": round(snap["high_52w"], 2), "low_52w": round(snap["low_52w"], 2), "penny": price < config.PENNY_PRICE,
                       "as_of": asof, "provider": (q or {}).get("provider", "history only"), "market": status_text}
    liq = res["overview"]["turnover_cr"]
    res["overview"]["liquidity"] = ("High" if liq >= 25 else "Adequate" if liq >= config.MIN_AVG_TURNOVER / 1e7 else "Thin")
    if snap["avg_volume_20"] < config.MIN_AVG_VOLUME:
        res["overview"]["liquidity"] = "Thin"
    # ---- technical
    res["technical"] = {k: (round(v, 3) if isinstance(v, float) else v) for k, v in snap.items() if k not in ("patterns", "support", "resistance", "gap_stats")}
    res["technical"].update(patterns=snap["patterns"], support=snap["support"], resistance=snap["resistance"], gap_stats=snap["gap_stats"])
    res["rules"] = {"decision": ev["decision"], "score": ev["score"], "checks": ev["checks"], "headline": ev["headline"],
                    "filter_fails": ev.get("filter_fails", []), "blockers": ev.get("blockers", [])}
    bull = [c["label"].split(":")[0] for c in ev["checks"] if c["status"] == "pass"]
    bear = []
    if snap["trend"] == "DOWNTREND":
        bear.append("Downtrend: price below EMA20 and EMA20 below SMA50")
    if snap["momentum"] == "negative":
        bear.append("MACD histogram negative and falling")
    if snap["rsi"] < 45:
        bear.append(f"Weak RSI {snap['rsi']:.0f}")
    if snap["rsi"] >= config.RSI_OVERHEATED:
        bear.append(f"RSI {snap['rsi']:.0f} overheated")
    bear += [p["text"] for p in snap["patterns"] if p["bias"] < 0]
    if snap["breakout_state"] in ("BREAKDOWN", "NEAR BREAKDOWN"):
        bear.append(f"Price is at/below the 20-day low {snap['prior_20d_low']:.2f}")
    res["bullish_signals"] = bull + [p["text"] for p in snap["patterns"] if p["bias"] > 0]
    res["bearish_signals"] = bear
    # ---- fundamentals / news / events
    res["fundamentals"] = _fund(info)
    sec = SECTOR_INDEX.get(info.get("sector", ""))
    sq = market.quote_change(sec) if sec else None
    res["context"] = {"news": nw, "events": corp, "market": mkt, "sector_move": sq, "sector_index": sec,
                      "external_check": news.external_veto(symbol)[1]}
    risks = []
    if res["overview"]["liquidity"] == "Thin":
        risks.append("Thin liquidity: entries/exits may slip")
    if res["overview"]["penny"]:
        risks.append(f"Low-priced stock (below Rs{config.PENNY_PRICE:g}): larger percentage swings, higher manipulation risk")
    if nw.get("verdict") == "NEGATIVE":
        risks.append("Negative news flagged: " + nw.get("note", "")[:120])
    if mkt and mkt.get("available") and mkt["trend"] == "BEARISH":
        risks.append("Broad market trend is bearish")
    risks += ev.get("filter_fails", [])
    # ---- strategy assessment
    atr = snap["atr"]
    ph = snap["prior_20d_high"]
    ma = ev["metrics"]
    held = pv is not None
    day_chg = strategy.day_change_pct(hc, price)
    ok_i, pass_i, fail_i = strategy.intraday_check(ma, day_chg, price, now, mopen, q)
    plan = strategy.build_plan(ev, hc, price, now, mopen, q, mkt) if ev["metrics"].get("atr") else None
    sig = ev["decision"] == "SIGNAL"
    short_blocks = list((plan or {}).get("blocks", []))
    short_ok = sig and not short_blocks
    intra_ok = sig and ok_i and not short_blocks
    down = snap["trend"] == "DOWNTREND" or snap["breakout_state"] == "BREAKDOWN"
    filtered = ev["decision"] == "FILTERED"
    assess = None
    if held and pv:
        pos = next(p for p in portfolio.positions() if p["symbol"] == symbol)
        hard = strategy.manage(pos, price, hc, 1 if pos["strategy"] == "INTRADAY" else max(1, int(config.trading_days_between(pd.Timestamp(pos["buy_time"]).date(), now.date()))), now)
        assess = monitor.assess(pos, price, hc, hard, q, mkt, 1, count_scan=False)

    def build(strat):
        lv = strategy.levels(strat, price, atr)
        if strat == strategy.INTRADAY:
            elig, why_ok, why_no = intra_ok, pass_i, fail_i
            hold = f"Same session - exit by {config.INTRADAY_SQUARE_OFF:%H:%M} IST"
            sig_label = "Buy Candidate" if elig else ("Avoid" if (filtered or down) else "Wait")
        else:
            elig = short_ok                                    # a qualifying breakout signal is always a valid short-term setup
            why_ok = [c["label"].split(":")[0] + f" ({c['detail']})" for c in ev["checks"] if c["status"] == "pass"]
            why_no = list(ev.get("blockers", [])) + (ev.get("filter_fails", [])) + short_blocks
            if ev["decision"] == "WATCH":
                why_no.insert(0, f"score {ev['score']}/{config.MIN_SIGNAL_SCORE} needed")
            hold = f"About 1-4 weeks (limit {config.MAX_HOLD_SESSIONS} sessions)"
            sig_label = "Buy Candidate" if elig else ("Avoid" if (filtered or down) else "Wait")
        if held:
            sig_label = "Exit Recommended" if (assess and assess["state"] in (monitor.EXIT_REC, monitor.STOP)) else "Hold"
        entry_low = ph if (ph and ph > price) else price
        return {"strategy": strat, "label": strategy.LABEL[strat], "eligible": bool(elig), "signal": sig_label, "hold": hold,
                "entry_range": (round(min(price, ph), 2) if ph and ph < price else round(price, 2), round(max(price * 1.005, ph * 1.01 if ph > price else price * 1.005), 2)),
                "entry_trigger": (round(ph, 2) if ph and ph > price else None),
                "stop": round(lv["stop"], 2), "target1": round(lv["target1"], 2), "target2": round(lv["target2"], 2),
                "rr1": lv["rr1"], "rr2": lv["rr2"], "evidence": why_ok[:6], "missing": why_no[:6],
                "invalidation": ([f"Price falls back below the breakout level Rs{ph:.2f}", f"Stop Rs{lv['stop']:.2f} is hit", "Volume dries up"] +
                                 ([f"Still open at {config.INTRADAY_SQUARE_OFF:%H:%M}"] if strat == strategy.INTRADAY else
                                  ["Daily close below EMA20 with negative MACD", f"No target within {config.MAX_HOLD_SESSIONS} sessions"]))}
    res["strategies"] = [build(strategy.INTRADAY), build(strategy.SHORT_TERM)] if atr and atr > 0 else []
    primary = next((s for s in res["strategies"] if s["eligible"]), None)
    if plan and sig and not plan["blocks"]:
        primary = next((s for s in res["strategies"] if s["strategy"] == plan["strategy"]), primary)
    # ---- portfolio integration
    if held and pv:
        pos = next(p for p in portfolio.positions() if p["symbol"] == symbol)
        row = store.read_csv("active_stocks.csv").query("symbol == @symbol").iloc[0]
        res["holding"] = {"qty": pv["quantity"], "avg_price": pv["buy_price"], "invested": pv["invested"], "market_value": pv["market_value"],
                          "pnl": pv["pnl"], "pnl_pct": pv["pnl_pct"], "strategy": pos["strategy"], "bought": pos["buy_time"],
                          "stop": pos["stop_loss"], "target1": pos["target1"], "target2": pos["target2"],
                          "original_reason": row.get("reason", ""), "strategy_reason": row.get("strategy_reason", ""),
                          "invalidation": row.get("invalidation", ""), "stored_state": pos["signal_state"],
                          "stored_note": row.get("last_note", ""), "last_assessed": row.get("last_assessed", ""),
                          "live_state": assess["state"] if assess else "", "live_warnings": [w["text"] for w in assess["warnings"]] if assess else [],
                          "thesis_valid": assess["thesis_valid"] if assess else None}
    closed = [c for c in pnl.closed_positions(2000) if c["symbol"] == symbol]
    if closed:
        res["last_closed"] = closed[0]
    # ---- summary
    overall = ("HELD - " + ("exit recommended" if (assess and assess["state"] in (monitor.EXIT_REC, monitor.STOP)) else "hold" if assess and assess["thesis_valid"] else "hold with warnings")) if held else (
        f"Buy candidate ({primary['label']})" if primary else ("Avoid - " + (ev.get("filter_fails") or [ev["headline"]])[0] if (filtered or down) else "Wait - no trade identified yet"))
    conf_pts = 3 - (0 if mopen else 1) - (0 if q else 1) - (1 if len(hc) < config.MIN_HISTORY else 0) - (1 if (not info and nw.get("verdict") == "UNAVAILABLE") else 0)
    res["summary"] = {"overall": overall, "intraday": next((s["signal"] for s in res["strategies"] if s["strategy"] == "INTRADAY"), "n/a"),
                      "short_term": next((s["signal"] for s in res["strategies"] if s["strategy"] == "SHORT_TERM"), "n/a"),
                      "entry_range": primary["entry_range"] if primary else None, "target": primary["target1"] if primary else None,
                      "stop": primary["stop"] if primary else None, "risks": risks[:6],
                      "reasons": (bull[:4] if primary or sig else (ev.get("blockers", [])[:3] or [ev["headline"]])),
                      "confidence": "High" if conf_pts >= 3 else "Medium" if conf_pts == 2 else "Low",
                      "data_note": f"price as of {asof[:19]} via {res['overview']['provider']}; {status_text}" + ("" if q else "; no live quote")}
    if not primary and not held:
        res["summary"]["none"] = "No suitable Intraday or Short-Term trade is identified right now."
    if not info:
        res["limits"].append("Fundamental data (sector, ratios) unavailable from the configured source.")
    if nw.get("verdict") == "UNAVAILABLE":
        res["limits"].append("No recent news found / news source unavailable.")
    if mkt is None or not mkt.get("available"):
        res["limits"].append("Index data unavailable - broader-market context not applied.")
    return res


def text_summary(r):
    """Short Telegram version of analyze()."""
    if r.get("error"):
        return r["error"]
    s, o = r["summary"], r["overview"]
    lines = [f"<b>{r['symbol']}</b> {r['company']} - Rs{o['price']} ({o['change_pct']:+.2f}%)", f"Overall: {s['overall']}",
             f"Intraday: {s['intraday']} | Short-Term: {s['short_term']}"]
    if s["entry_range"]:
        lines.append(f"Entry {s['entry_range'][0]}-{s['entry_range'][1]} | Target {s['target']} | Stop {s['stop']}")
    if s["risks"]:
        lines.append("Risks: " + "; ".join(s["risks"][:3]))
    lines.append(f"Confidence {s['confidence']} - {s['data_note']}")
    lines.append("<i>Analysis only - no order placed. Not investment advice.</i>")
    return "\n".join(lines)
