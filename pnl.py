"""Portfolio accounting and P&L views (single source of truth for dashboard, Telegram and the scanner).

Definitions - used identically everywhere so nothing is double counted:
  realized P&L    = sum of `profit` in sold_stocks.csv (every partial/full exit, after both-side costs)
  unrealized P&L  = open quantity x (last price - buy price), minus estimated round-trip costs
  combined P&L    = realized + unrealized            (equity = capital + combined)
  available cash  = capital + realized - invested    (invested = buy price x open quantity)
A sold stock leaves active_stocks.csv but stays in sold_stocks.csv / trades_history.csv forever, so its realized
P&L never disappears.
"""
import re
from datetime import datetime

import pandas as pd

import config
import portfolio
import store

LEGACY = "SHORT_TERM"


def _f(v, d=0.0):
    try:
        return float(v)
    except (TypeError, ValueError):
        return d


def net_unrealized(buy, price, qty):
    return (price - buy) * qty - (price + buy) * qty * config.COST_PCT_PER_SIDE / 100


def position_view():
    """Open positions with latest cached price, P&L and monitoring state. Price age is always reported."""
    cache = store.read_json("quotes_cache.json", {})
    out = []
    for p in portfolio.positions():
        q = cache.get(p["symbol"], {})
        price = q.get("price")
        have = bool(price)
        px = price if have else p["buy_price"]
        invested = p["buy_price"] * p["quantity"]
        upnl = net_unrealized(p["buy_price"], px, p["quantity"]) if have else 0.0
        out.append({**p, "price": price, "price_known": have, "quote_time": q.get("time", ""), "quote_provider": q.get("provider", ""),
                    "invested": round(invested, 2), "market_value": round(px * p["quantity"], 2),
                    "pnl": round(upnl, 2) if have else None, "pnl_pct": round(upnl / invested * 100, 2) if (have and invested) else None,
                    "over_cap": invested > config.CAPITAL * config.MAX_POSITION_PCT / 100 * 1.001})
    return out


def realized_total():
    return round(sum(_f(x) for x in store.read_csv("sold_stocks.csv")["profit"]), 2)


def account(rows=None):
    rows = position_view() if rows is None else rows
    sold = store.read_csv("sold_stocks.csv")
    invested = round(sum(r["invested"] for r in rows), 2)
    mv = round(sum(r["market_value"] for r in rows), 2)
    unreal = round(sum(r["pnl"] or 0.0 for r in rows), 2)
    realized = realized_total()
    closed = int((sold["exit_type"] != "PARTIAL").sum()) if len(sold) else 0
    times = [r["quote_time"] for r in rows if r["quote_time"]]
    return {"capital": config.CAPITAL, "cash": round(config.CAPITAL + realized - invested, 2), "invested": invested,
            "market_value": mv, "unrealized": unreal, "realized": realized, "combined": round(realized + unreal, 2),
            "equity": round(config.CAPITAL + realized + unreal, 2), "open_positions": len(rows),
            "max_positions": config.MAX_OPEN_POSITIONS, "slots_free": max(0, config.MAX_OPEN_POSITIONS - len(rows)),
            "closed_trades": closed, "partial_exits": int((sold["exit_type"] == "PARTIAL").sum()) if len(sold) else 0,
            "prices_missing": [r["symbol"] for r in rows if not r["price_known"]],
            "oldest_quote": min(times) if times else "", "newest_quote": max(times) if times else ""}


# ---------------------------------------------------------------- closed positions
def closed_positions(limit=500):
    """Sold rows enriched with buy time, strategy, values, % and holding time (legacy rows are back-filled)."""
    sold = store.read_csv("sold_stocks.csv")
    if sold.empty:
        return []
    trades = store.read_csv("trades_history.csv")
    buys = trades[trades["type"] == "BUY"]
    out = []
    for r in sold.to_dict(orient="records"):
        buy, sellp, qty, pnl = _f(r["buy_price"]), _f(r["sold_price"]), int(_f(r["quantity"])), _f(r["profit"])
        bt, strat, legacy = r.get("buy_time") or "", r.get("strategy") or "", False
        if not bt or not strat:
            cand = buys[(buys["symbol"] == r["symbol"]) & (buys["time"] <= r["sold_time"])]
            if len(cand):
                bt = bt or cand.iloc[-1]["time"]
                strat = strat or cand.iloc[-1].get("strategy", "")
        if not strat:
            strat, legacy = LEGACY, True
        bv = _f(r.get("buy_value")) or buy * qty
        sv = _f(r.get("sell_value")) or sellp * qty
        hd = _f(r.get("hold_days"), None) if r.get("hold_days") not in (None, "") else None
        if hd is None and bt:
            try:
                hd = round((pd.Timestamp(r["sold_time"]) - pd.Timestamp(bt)).total_seconds() / 86400, 2)
            except Exception:
                hd = None
        out.append({**r, "buy_time": bt, "strategy": strat, "strategy_legacy": legacy, "buy_value": round(bv, 2), "sell_value": round(sv, 2),
                    "qty": qty, "pnl": pnl, "pnl_pct": round(pnl / bv * 100, 2) if bv else None, "hold_days": hd,
                    "company": r.get("company") or portfolio.company_name(r["symbol"]), "exit_type": r.get("exit_type") or "FULL",
                    "exit_class": exit_class(r.get("reason", ""))})
    return out[::-1][:limit]


_stops_cache = {"mtime": None, "map": {}}


def _bought_stops():
    """Legacy trades: the stop at entry only survives in the scan log text ('BOUGHT 238 @ 52.44, stop 48.24')."""
    p = store.path("scan_details.csv")
    try:
        mt = p.stat().st_mtime
        if _stops_cache["mtime"] == mt:
            return _stops_cache["map"]
        df = pd.read_csv(p, dtype=str, keep_default_na=False, usecols=["scan_id", "symbol", "decision", "reason"])
        out = {}
        for r in df[df["decision"] == "BOUGHT"].to_dict(orient="records"):
            m = re.search(r"stop (\d+(?:\.\d+)?)", r["reason"])
            if m:
                out[(r["symbol"], r["scan_id"][:8])] = float(m.group(1))
        _stops_cache.update(mtime=mt, map=out)
        return out
    except Exception:
        return {}


def exit_class(reason):
    t = (reason or "").lower()
    if "stop-loss" in t or "protective stop" in t:
        return "Stop-loss triggered" if "stop-loss" in t else "Protective / trailing stop"
    if "target 2" in t or "target 1" in t:
        return "Target achieved"
    if "deviation" in t or "thesis" in t or "trend broke" in t or "exit recommended" in t:
        return "Strategy deviation"
    if "time exit" in t:
        return "Time exit"
    if "square-off" in t or "carried past" in t:
        return "Intraday square-off"
    if "manual" in t:
        return "Manual exit"
    return "Other"


def review_closed(c):
    """Plain-English post-mortem for one closed trade, built only from what was recorded."""
    notes = []
    strat = c["strategy"]
    if c["strategy_legacy"]:
        notes.append("Strategy was not recorded (trade pre-dates classification). It was entered under the original daily-breakout "
                     "swing rules, i.e. treated as SHORT_TERM - not because of how long it was held.")
    else:
        notes.append(f"Classified {strat.replace('_', ' ').title()} when the buy signal was generated.")
    hd = c.get("hold_days")
    if hd is not None:
        notes.append(f"Held {hd:.1f} day(s).")
        if strat == "INTRADAY" and hd >= 1:
            notes.append("Intraday position survived overnight - the square-off did not run; investigate the worker.")
    ist = _f(c.get("initial_stop"), 0)
    if not ist and c.get("buy_time"):
        ist = _bought_stops().get((c["symbol"], str(c["buy_time"])[:10].replace("-", "")), 0.0)
    stop = _f(c.get("stop_loss"), 0) or ist or 0
    if "stop" in (c.get("reason") or "").lower() and stop:
        slip = (stop - _f(c["sold_price"])) / stop * 100
        if slip > 0.3:
            notes.append(f"Exit filled {slip:.1f}% below the stop (Rs{stop:.2f}): the stop is evaluated at each position check and filled at the observed price, so fast moves/gaps slip past it.")
    if ist and _f(c["buy_price"]):
        sp = (_f(c["buy_price"]) - ist) / _f(c["buy_price"]) * 100
        notes.append(f"Initial stop Rs{ist:.2f} was {sp:.1f}% below entry" + (" (hit the maximum-stop cap)" if sp >= config.MAX_STOP_PCT - 0.1 else "") +
                     f"; the trade lost {abs(c['pnl_pct']):.1f}% after costs." if c["pnl"] < 0 else f"; the trade made {c['pnl_pct']:.1f}% after costs.")
    pk = _f(c.get("peak_price"), 0)
    if pk and _f(c["buy_price"]):
        mfe = (pk / _f(c["buy_price"]) - 1) * 100
        notes.append(f"Best price after entry Rs{pk:.2f} ({mfe:+.1f}%); finished {c['pnl_pct']:+.1f}% - " +
                     ("gave back all open profit" if mfe > 3 and c["pnl"] < 0 else "see exit reason"))
    return notes


# ---------------------------------------------------------------- daily / historical
def snapshot(day=None):
    """Upsert today's row in daily_pnl.csv. total_pnl = cumulative realized + current unrealized; day_pnl is the change in
    total_pnl since the previous row, so realized and unrealized are never counted twice."""
    if day is None:
        d = config.now_ist().date()
        while not config.is_trading_day(d):          # weekends/holidays refresh the last session's row, never a phantom day
            d -= __import__("datetime").timedelta(days=1)
        day = d.isoformat()
    rows = position_view()
    acc = account(rows)
    sold = store.read_csv("sold_stocks.csv")
    realized_day = round(sum(_f(x) for x in sold[sold["sold_time"].str.startswith(day)]["profit"]), 2) if len(sold) else 0.0
    with store.locked():
        df = store.read_csv("daily_pnl.csv")
        prev = df[df["date"] < day]
        if len(prev):
            prev_total = _f(prev.iloc[-1]["total_pnl"])
        else:   # first snapshot ever: baseline = realized P&L booked before this day (earlier open P&L was never recorded)
            prev_total = round(sum(_f(x) for x in sold[sold["sold_time"].str[:10] < day]["profit"]), 2) if len(sold) else 0.0
        total = round(acc["realized"] + acc["unrealized"], 2)
        row = {"date": day, "realized_day": realized_day, "cum_realized": acc["realized"], "unrealized": acc["unrealized"],
               "total_pnl": total, "day_pnl": round(total - prev_total, 2), "invested": acc["invested"], "market_value": acc["market_value"],
               "open_positions": acc["open_positions"], "closed_trades": acc["closed_trades"],
               "updated": config.now_ist().strftime("%Y-%m-%d %H:%M:%S")}
        df = df[df["date"] != day]
        store.write_csv("daily_pnl.csv", pd.concat([df, pd.DataFrame([row])], ignore_index=True).sort_values("date"))
    return row


def daily_table():
    """One row per trading day with realized, unrealized (EOD mark) and totals. Days with trades but no snapshot still appear
    (realized from the ledger, unrealized shown as None)."""
    snaps = {r["date"]: r for r in store.read_csv("daily_pnl.csv").to_dict(orient="records")}
    sold = store.read_csv("sold_stocks.csv")
    by_day = {}
    for r in sold.to_dict(orient="records"):
        d = r["sold_time"][:10]
        x = by_day.setdefault(d, {"realized": 0.0, "trades": 0, "wins": 0, "losses": 0})
        p = _f(r["profit"])
        x["realized"] += p
        x["trades"] += 1
        x["wins"] += p > 0
        x["losses"] += p < 0
    out, cum = [], 0.0
    for d in sorted(set(snaps) | set(by_day)):
        x = by_day.get(d, {"realized": 0.0, "trades": 0, "wins": 0, "losses": 0})
        cum += x["realized"]
        s = snaps.get(d)
        out.append({"date": d, "realized_day": round(x["realized"], 2), "trades": x["trades"], "wins": x["wins"], "losses": x["losses"],
                    "cum_realized": round(cum, 2), "unrealized": _f(s["unrealized"]) if s else None,
                    "total_pnl": _f(s["total_pnl"]) if s else None, "day_pnl": _f(s["day_pnl"]) if s else None,
                    "open_positions": int(_f(s["open_positions"])) if s else None})
    return out[::-1]
