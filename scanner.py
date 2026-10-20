"""One market scan = manage open positions, evaluate a batch of the NSE universe, log everything."""
import threading
from datetime import timedelta
from concurrent.futures import ThreadPoolExecutor

import pandas as pd

import config
import news
import portfolio
import providers
import store
import strategy
import telegram_bot as tg

_SCAN_LOCK = threading.Lock()
_POS_LOCK = threading.Lock()


# ---------------------------------------------------------------- helpers
def load_universe():
    p = store.path("nse_equity_symbols.csv")
    try:
        df = pd.read_csv(p, dtype=str, keep_default_na=False)
    except Exception:
        return []
    df.columns = [c.strip() for c in df.columns]
    if "SERIES" in df.columns:
        df = df[df["SERIES"].str.strip().str.upper() == "EQ"]
    col = "Symbol" if "Symbol" in df.columns else df.columns[0]
    return list(dict.fromkeys(s.strip().upper() for s in df[col] if s.strip()))


def _completed(hist, q):
    sd = q["session_date"]
    keep = [d < sd for d in hist.index.date]
    return hist[keep]


def _held_sessions(buy_time):
    try:
        return max(1, config.trading_days_between(pd.Timestamp(buy_time).date(), config.now_ist().date()))
    except Exception:
        return 1


def _prepare(sym):
    """Fetch + cheap pre-filter. Returns (sym, hist, quote, decision, reason)."""
    try:
        hist = providers.load_history(sym)
    except Exception as exc:
        return sym, None, None, "NO_DATA", str(exc)[:160]
    if len(hist) < config.MIN_HISTORY:
        return sym, hist, None, "FILTERED", f"Only {len(hist)} sessions of history (need {config.MIN_HISTORY})"
    last = float(hist["Close"].iloc[-1])
    if not (config.MIN_PRICE * 0.8 <= last <= config.MAX_PRICE * 1.2):
        return sym, hist, None, "FILTERED", f"Price Rs{last:.2f} outside Rs{config.MIN_PRICE:g}-{config.MAX_PRICE:g}"
    av = float(hist["Volume"].iloc[-20:].mean())
    to = float((hist["Close"] * hist["Volume"]).iloc[-20:].mean())
    if av < config.MIN_AVG_VOLUME or to < config.MIN_AVG_TURNOVER:
        return sym, hist, None, "FILTERED", f"Low liquidity (avg vol {av:,.0f}, turnover Rs{to / 1e7:.2f} cr)"
    try:
        return sym, hist, providers.load_quote(sym), None, None
    except Exception as exc:
        return sym, hist, None, "NO_DATA", "quote: " + str(exc)[:150]


# ---------------------------------------------------------------- position management
def manage_positions(mode="TRADING", notify=True):
    """Apply stop/target/trailing rules. Safe to call from the scan and from the worker's 5-minute loop."""
    result = {"sells": 0, "lines": []}
    with _POS_LOCK:
        cache = store.read_json("quotes_cache.json", {})
        for pos in portfolio.positions():
            sym = pos["symbol"]
            try:
                hist, q = providers.load_history(sym), providers.load_quote(sym)
            except Exception as exc:
                result["lines"].append(f"{sym}: could not get data ({str(exc)[:80]}) - position left unchanged")
                continue
            price = q["price"]
            cache[sym] = {"price": price, "time": q["time"], "provider": q["provider"]}
            if mode != "TRADING":
                continue
            m = strategy.manage(pos, price, _completed(hist, q), _held_sessions(pos["buy_time"]))
            if m["action"] in ("EXIT", "PARTIAL"):
                pnl = portfolio.sell(sym, price, m["qty"], m["reason"])
                if pnl is not None:
                    result["sells"] += 1
                    result["lines"].append(f"{sym}: {m['action']} {m['qty']} @ {price:.2f} ({m['reason']}) P&L Rs{pnl:,.2f}")
                    if notify:
                        tg.send(tg.msg_exit(sym, m["action"], price, m["qty"], pnl, m["reason"]))
                    if m["action"] == "PARTIAL":
                        portfolio.update_position(sym, stop_loss=round(m["new_stop"], 2), target1_hit=True, peak_price=round(m["peak"], 2))
            else:
                changed = (m["new_stop"] > pos["stop_loss"] + 0.005 or m["peak"] > pos["peak_price"] + 0.005
                           or m["t1_hit"] != pos["target1_hit"])
                if changed:
                    portfolio.update_position(sym, stop_loss=round(m["new_stop"], 2), peak_price=round(m["peak"], 2),
                                              target1_hit=m["t1_hit"])
                    if m["new_stop"] > pos["stop_loss"] + 0.005:
                        result["lines"].append(f"{sym}: stop raised to Rs{m['new_stop']:.2f} ({m['reason'] or 'trailing'})")
                result["lines"].append(f"{sym}: hold @ {price:.2f} (stop {m['new_stop']:.2f})")
        store.write_json("quotes_cache.json", cache)
    return result


# ---------------------------------------------------------------- the scan
def run_scan(trigger="scheduled"):
    if not _SCAN_LOCK.acquire(blocking=False):
        return {"status": "already_running"}
    try:
        return _run(trigger)
    finally:
        _SCAN_LOCK.release()


def _run(trigger):
    started = config.now_ist()
    scan_id = started.strftime("%Y%m%d-%H%M%S")
    status, status_text = config.market_status(started)
    enabled = store.control()["enabled"]
    mode = "TRADING" if (status == "OPEN" and enabled) else "ANALYSIS"
    run = {"scan_id": scan_id, "started": started.strftime("%Y-%m-%d %H:%M:%S"), "trigger": trigger, "mode": mode,
           "status": "OK", "universe_size": 0, "batch_start": 0, "batch_size": 0, "evaluated": 0, "no_data": 0,
           "filtered": 0, "rejected": 0, "watch": 0, "signals": 0, "buys": 0, "sells": 0, "providers": "", "note": ""}
    journal = [f"==== SCAN {scan_id} | {trigger} | {mode} | {started:%H:%M} IST ====", f"Market: {status_text}"]
    if mode == "ANALYSIS":
        journal.append("Analysis-only run: stocks are evaluated and logged, but NO paper trades are made. "
                       + ("(Bot is paused.)" if status == "OPEN" and not enabled else ""))
    if trigger == "scheduled" and mode != "TRADING":
        run.update(status="SKIPPED", finished=started.strftime("%Y-%m-%d %H:%M:%S"),
                   note="paused" if status == "OPEN" else status_text)
        journal.append("Skipped: " + run["note"])
        _write_logs(run, [], journal)
        return run

    universe = load_universe()
    run["universe_size"] = len(universe)
    if not universe:
        run.update(status="ERROR", note="Symbol list data/nse_equity_symbols.csv is missing or empty",
                   finished=config.now_ist().strftime("%Y-%m-%d %H:%M:%S"))
        journal.append("ERROR: " + run["note"])
        _write_logs(run, [], journal)
        return run

    # 1) existing positions first
    mres = manage_positions(mode, notify=True)
    run["sells"] = mres["sells"]
    journal += ["Open positions:"] + [f"  {x}" for x in mres["lines"]] if mres["lines"] else ["Open positions: none"]

    # 2) rotating batch of the universe
    cur = store.read_json("scan_cursor.json", {}).get("next", 0) % len(universe)
    n = min(config.SCAN_BATCH_SIZE, len(universe))
    batch = [universe[(cur + i) % len(universe)] for i in range(n)]
    store.write_json("scan_cursor.json", {"next": (cur + n) % len(universe), "updated": started.isoformat(timespec="seconds")})
    run.update(batch_start=cur, batch_size=n)
    journal.append(f"Universe {len(universe)} symbols; this scan covers positions {cur + 1}-{cur + n} (wraps around).")

    with ThreadPoolExecutor(max_workers=config.SCAN_WORKERS) as ex:
        prepared = list(ex.map(_prepare, batch))

    rows, signals, watch = [], [], []
    provs = set()
    for sym, hist, q, pre, why in prepared:
        row = {"scan_id": scan_id, "time": started.strftime("%H:%M:%S"), "symbol": sym, "decision": pre or "", "score": "",
               "price": "", "rsi": "", "volume_ratio": "", "reason": why or ""}
        if pre:
            run["no_data" if pre == "NO_DATA" else "filtered"] += 1
            rows.append(row)
            continue
        provs.add(q["provider"])
        ev = strategy.evaluate(_completed(hist, q), q["price"], q["day_volume"], started, status == "OPEN")
        run["evaluated"] += 1
        m = ev["metrics"]
        row.update(decision=ev["decision"], score=ev["score"], price=round(q["price"], 2), reason=ev["headline"],
                   rsi=round(m.get("rsi", 0), 1) if "rsi" in m else "",
                   volume_ratio=round(m.get("volume_ratio", 0), 2) if "volume_ratio" in m else "")
        if ev["decision"] == "FILTERED":
            run["filtered"] += 1
        elif ev["decision"] == "REJECT":
            run["rejected"] += 1
        elif ev["decision"] == "WATCH":
            run["watch"] += 1
            watch.append((sym, ev, q))
        elif ev["decision"] == "SIGNAL":
            run["signals"] += 1
            signals.append((sym, ev, q, row))
        rows.append(row)
    run["providers"] = ", ".join(sorted(provs))

    # 3) act on signals (best score first)
    held = {p["symbol"]: p for p in portfolio.positions()}
    cooling = portfolio.cooling_symbols()
    open_value = sum(p["buy_price"] * p["quantity"] for p in held.values())
    signals.sort(key=lambda s: (s[1]["score"], s[1]["metrics"].get("volume_ratio", 0)), reverse=True)
    sig_lines = []
    for sym, ev, q, row in signals:
        price = q["price"]
        block = None
        if mode != "TRADING":
            block = "analysis-only run (market closed or bot paused)"
        elif not config.entry_window_open(started):
            block = f"outside entry window {config.ENTRY_START:%H:%M}-{config.ENTRY_END:%H:%M}"
        elif sym in held:
            block = "already holding it"
        elif sym in cooling:
            block = f"cooling period ({cooling[sym]}h left)"
        elif len(held) >= config.MAX_OPEN_POSITIONS:
            block = f"max open positions ({config.MAX_OPEN_POSITIONS}) reached"
        elif run["buys"] >= config.MAX_BUYS_PER_SCAN:
            block = f"max buys per scan ({config.MAX_BUYS_PER_SCAN}) reached"
        elif portfolio.buys_today() >= config.MAX_TRADES_PER_DAY:
            block = f"max trades per day ({config.MAX_TRADES_PER_DAY}) reached"
        elif portfolio.daily_loss_limit_hit():
            block = f"daily loss limit ({config.MAX_DAILY_LOSS_PCT:g}%) reached"
        qty = strategy.position_size(price, ev["stop"], open_value=open_value) if not block else 0
        if not block and qty < 1:
            block = "risk/capital budget allows less than 1 share"
        nws = ""
        if not block:
            veto, ext = news.external_veto(sym)
            if veto:
                block = "external check: " + ext
            else:
                nr = news.news_check(sym)
                nws = f"news {nr['verdict']}: {nr['note']}"
                if nr["verdict"] == "NEGATIVE" or (config.REQUIRE_NEWS_OK and nr["verdict"] not in ("POSITIVE", "NEUTRAL")):
                    block = "news check: " + nr["note"]
        if block:
            row["reason"] = f"SIGNAL but not traded: {block}. {ev['headline']}"
            sig_lines.append(f"  {sym}: score {ev['score']} - NOT traded ({block})")
            if mode == "TRADING":
                tg.send(tg.msg_signal(sym, ev, price, 0, ev["stop"], False, block))
            continue
        if portfolio.add_position(sym, price, qty, ev["stop"], ev["target1"], ev["target2"], ev["metrics"]["atr"], ev["score"],
                                  f"score {ev['score']}; {ev['headline']}; {nws}"[:300]):
            run["buys"] += 1
            held[sym] = {"symbol": sym}
            open_value += price * qty
            row["decision"] = "BOUGHT"
            row["reason"] = f"BOUGHT {qty} @ {price:.2f}, stop {ev['stop']:.2f}. {ev['headline']}"
            sig_lines.append(f"  {sym}: score {ev['score']} - BOUGHT {qty} @ {price:.2f} (stop {ev['stop']:.2f}, "
                             f"T1 {ev['target1']:.2f}, T2 {ev['target2']:.2f}) {nws}")
            tg.send(tg.msg_signal(sym, ev, price, qty, ev["stop"], True, ""))

    # 4) watchlist file for dashboard/Telegram
    wl = store.read_json("watchlist.json", {})
    scanned = {r["symbol"] for r in rows}
    for s in list(wl):
        if s in scanned or wl[s].get("date", "") < (started.date() - timedelta(days=2)).isoformat():
            wl.pop(s, None)
    for sym, ev, q in watch:
        wl[sym] = {"score": ev["score"], "price": round(q["price"], 2), "reason": ev["headline"], "date": started.date().isoformat(),
                   "time": started.strftime("%H:%M"), "rsi": round(ev["metrics"].get("rsi", 0), 1),
                   "prior_20d_high": round(ev["metrics"].get("prior_20d_high", 0), 2)}
    store.write_json("watchlist.json", wl)

    # 5) journal + CSV logs
    journal.append(f"Result: checked {run['evaluated']} | no data {run['no_data']} | filtered {run['filtered']} | "
                   f"rejected {run['rejected']} | watch {run['watch']} | signals {run['signals']} | bought {run['buys']} | sold {run['sells']}")
    if sig_lines:
        journal += ["Signals:"] + sig_lines
    top = sorted(watch, key=lambda w: w[1]["score"], reverse=True)[:5]
    if top:
        journal += ["Closest to a signal (watchlist):"] + [f"  {s}: score {e['score']} - {e['headline']}" for s, e, _ in top]
    if not signals and not top:
        journal.append("No stock met the criteria this scan. That is a normal outcome - waiting is a valid decision.")
    if batch and run["no_data"] > 0.5 * len(batch):
        run["status"] = "DATA_PROBLEM"
        run["note"] = f"{run['no_data']} of {len(batch)} symbols had no data - check market-data token/connection"
        journal.append("WARNING: " + run["note"])
        tg.send("⚠️ " + news.e(run["note"]))
    run["finished"] = config.now_ist().strftime("%Y-%m-%d %H:%M:%S")
    _write_logs(run, rows, journal)
    store.write_json("last_scan.json", run)
    if config.TELEGRAM_SCAN_SUMMARY == "every" or trigger in ("manual", "telegram"):
        tg.send(tg.msg_scan_summary(run))
    return run


def _write_logs(run, rows, journal):
    run.setdefault("finished", run["started"])
    store.append_rows("scan_runs.csv", [run])
    store.append_rows("scan_details.csv", rows)
    store.trim_csv("scan_details.csv", config.MAX_DETAIL_ROWS)
    store.trim_csv("scan_runs.csv", 5000)
    store.append_text("scan_journal.txt", "\n".join(journal) + "\n")
    store.write_json("last_scan.json", run)
