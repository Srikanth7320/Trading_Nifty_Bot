"""One market scan = manage open positions, evaluate a batch of the NSE universe, log everything."""
import threading
from datetime import timedelta
from concurrent.futures import ThreadPoolExecutor

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
    return strategy.completed(hist, q["session_date"])


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
def _log_signal(scan_id, rows):
    if rows:
        store.append_rows("signal_log.csv", rows)
        store.trim_csv("signal_log.csv", 30000)


def _backfill(pos, hc):
    """Legacy positions have no breakout level / initial risk: derive them once from history before the buy date."""
    upd = {}
    try:
        if not pos["entry_level"]:
            bd = pd.Timestamp(pos["buy_time"]).date()
            prior = hc[[d < bd for d in hc.index.date]]
            if len(prior) >= 20:
                upd["entry_level"] = round(float(prior["High"].iloc[-20:].max()), 2)
        if not pos["initial_risk"]:
            upd["initial_risk"] = round(strategy.initial_risk_of(pos), 4)
    except Exception:
        pass
    return upd


def manage_positions(mode="TRADING", notify=True, soft=False, scan_id=""):
    """Evaluate every open position.

    Hard rules (stop, targets, square-off, trend-break, time exit) run on every call. With soft=True (each 15-minute scan)
    the thesis monitor also runs: warnings, hysteresis counters and exit recommendations. Sells only happen in TRADING mode."""
    result = {"sells": 0, "lines": [], "checked": 0, "warnings": 0, "exit_signals": 0}
    with _POS_LOCK:
        cache = store.read_json("quotes_cache.json", {})
        mkt = market.context() if (soft and config.MARKET_FILTER_ENABLED) else None
        now, logs = config.now_ist(), []
        for pos in portfolio.positions():
            sym = pos["symbol"]
            try:
                hist, q = providers.load_history(sym), providers.load_quote(sym)
            except Exception as exc:
                result["lines"].append(f"{sym}: could not get data ({str(exc)[:80]}) - position left unchanged")
                continue
            price = q["price"]
            cache[sym] = {"price": price, "time": q["time"], "provider": q["provider"]}
            hc = _completed(hist, q)
            result["checked"] += 1
            upd = _backfill(pos, hc)
            if upd:
                portfolio.update_position(sym, **upd)
                pos = {**pos, **upd}
            held = _held_sessions(pos["buy_time"])
            m = strategy.manage(pos, price, hc, held, now)
            a = monitor.assess(pos, price, hc, m, q, mkt, held, count_scan=soft)
            tag = pos["strategy"].replace("_", "-")
            pnl_pct = (price / pos["buy_price"] - 1) * 100
            prior_state = pos["signal_state"]
            sell_reason, sell_qty, kind = None, 0, None
            if mode == "TRADING" and m["action"] in ("EXIT", "PARTIAL"):
                sell_reason, sell_qty, kind = m["reason"], m["qty"], m["action"]
            elif (mode == "TRADING" and soft and a["state"] == monitor.EXIT_REC and config.AUTO_EXIT_ON_RECOMMENDATION
                  and a["exit_reason"]):
                sell_reason, sell_qty, kind = a["exit_reason"], pos["quantity"], "EXIT"
            if a["state"] == monitor.WARN:
                result["warnings"] += 1
            if a["state"] in (monitor.EXIT_REC, monitor.STOP):
                result["exit_signals"] += 1
            if soft or sell_reason or a["state"] != prior_state:
                logs.append({"time": now.strftime("%Y-%m-%d %H:%M:%S"), "scan_id": scan_id, "symbol": sym, "event": a["state"],
                             "strategy": pos["strategy"], "price": round(price, 2), "pnl_pct": round(pnl_pct, 2),
                             "thesis_valid": str(a["thesis_valid"]).lower(), "detail": a["note"][:300]})
            if sell_reason:
                pnl_val = portfolio.sell(sym, price, sell_qty, sell_reason)
                if pnl_val is not None:
                    result["sells"] += 1
                    result["lines"].append(f"{sym} [{tag}]: {monitor.STATE_LABEL[a['state']].upper()} - {kind} {sell_qty} @ {price:.2f} "
                                           f"({sell_reason}) P&L Rs{pnl_val:,.2f}")
                    if notify:
                        tg.send(tg.msg_exit(sym, kind, price, sell_qty, pnl_val, sell_reason, tag))
                    if kind == "PARTIAL":
                        portfolio.update_position(sym, stop_loss=round(m["new_stop"], 2), target1_hit=True, peak_price=round(m["peak"], 2),
                                                  signal_state=monitor.HOLD, last_assessed=now.strftime("%Y-%m-%d %H:%M:%S"),
                                                  last_note="Target 1 hit: half sold, stop at break-even")
                continue
            changed = (m["new_stop"] > pos["stop_loss"] + 0.005 or m["peak"] > pos["peak_price"] + 0.005 or m["t1_hit"] != pos["target1_hit"])
            fields = {"signal_state": a["state"], "warn_score": a["score"], "last_assessed": now.strftime("%Y-%m-%d %H:%M:%S"),
                      "last_note": a["note"][:200], "thesis_valid": str(a["thesis_valid"]).lower()}
            if soft:
                fields["exit_confirm"] = a["exit_confirm"]
            if changed:
                fields.update(stop_loss=round(m["new_stop"], 2), peak_price=round(m["peak"], 2), target1_hit=m["t1_hit"])
                if m["new_stop"] > pos["stop_loss"] + 0.005:
                    result["lines"].append(f"{sym} [{tag}]: stop raised to Rs{m['new_stop']:.2f} ({m['reason'] or 'trailing'})")
            portfolio.update_position(sym, **fields)
            result["lines"].append(f"{sym} [{tag}]: {monitor.STATE_LABEL[a['state']].upper()} @ {price:.2f} ({pnl_pct:+.1f}%, stop {m['new_stop']:.2f}) - {a['note'][:160]}")
            if notify and soft and a["state"] in (monitor.WARN, monitor.EXIT_REC) and a["state"] != prior_state:
                tg.send(tg.msg_position_alert(sym, tag, a["state"], price, pnl_pct, a["note"], mode == "TRADING" and config.AUTO_EXIT_ON_RECOMMENDATION))
        store.write_json("quotes_cache.json", cache)
        _log_signal(scan_id, logs)
    try:
        pnl.snapshot()
    except Exception:
        pass
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
           "filtered": 0, "rejected": 0, "watch": 0, "signals": 0, "buys": 0, "sells": 0, "providers": "", "note": "", "positions_checked": 0, "warnings": 0,
           "exit_signals": 0}
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
    try:
        mres = manage_positions(mode, notify=True, soft=True, scan_id=scan_id)
    except Exception as exc:  # never let position management block the market scan
        import traceback
        store.append_text("scan_journal.txt", f"!! manage_positions failed: {type(exc).__name__}: {str(exc)[:200]}\n"
                          + traceback.format_exc()[-600:])
        mres = {"sells": 0, "lines": [f"position check failed: {type(exc).__name__}"], "checked": 0, "warnings": 0, "exit_signals": 0}
    run.update(sells=mres["sells"], positions_checked=mres.get("checked", 0), warnings=mres.get("warnings", 0),
               exit_signals=mres.get("exit_signals", 0))
    journal += ([f"Open positions ({mres.get('checked', 0)} checked, {mres.get('warnings', 0)} warning(s), {mres.get('exit_signals', 0)} exit signal(s)):"]
                + [f"  {x}" for x in mres["lines"]]) if mres["lines"] else ["Open positions: none"]

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
            signals.append((sym, ev, q, row, _completed(hist, q)))
        rows.append(row)
    run["providers"] = ", ".join(sorted(provs))

    # 3) act on signals (best score first). Every signal is classified INTRADAY or SHORT_TERM before anything else happens.
    held = {p["symbol"]: p for p in portfolio.positions()}
    cooling = portfolio.cooling_symbols()
    cash = pnl.account()["cash"]
    mkt = market.context() if (signals and config.MARKET_FILTER_ENABLED) else None
    signals.sort(key=lambda s: (s[1]["score"], s[1]["metrics"].get("volume_ratio", 0)), reverse=True)
    sig_lines, slog = [], []
    for sym, ev, q, row, hc in signals:
        price = q["price"]
        plan = strategy.build_plan(ev, hc, price, started, status == "OPEN", q, mkt)
        tag = plan["label"]
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
        elif plan["blocks"]:
            block = plan["blocks"][0]
        alloc = {"qty": 0, "value": 0.0, "limit": "", "why": ""}
        if not block:
            alloc = strategy.allocate(price, plan["stop"], ev["score"], cash, len(held), avg_turnover=ev["metrics"].get("avg_turnover_20"),
                                      extended=plan["extended"])
            if alloc["qty"] < 1:
                block = "capital allocation: " + alloc["why"]
        qty = alloc["qty"]
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
            row["reason"] = f"SIGNAL [{tag}] but not traded: {block}. {ev['headline']}"
            sig_lines.append(f"  {sym} [{tag}]: score {ev['score']} - NOT traded ({block})")
            slog.append({"time": started.strftime("%Y-%m-%d %H:%M:%S"), "scan_id": scan_id, "symbol": sym, "event": monitor.NEW_BUY,
                         "strategy": plan["strategy"], "price": round(price, 2), "pnl_pct": "", "thesis_valid": "",
                         "detail": f"not traded: {block}"[:300]})
            if mode == "TRADING":
                tg.send(tg.msg_signal(sym, ev, price, 0, plan["stop"], False, block, plan))
            continue
        why = f"[{tag}] score {ev['score']}; {ev['headline']}; {nws}"
        if portfolio.add_position(sym, price, qty, plan["stop"], plan["target1"], plan["target2"], ev["metrics"]["atr"], ev["score"], why, plan):
            run["buys"] += 1
            held[sym] = {"symbol": sym}
            cash -= qty * price * (1 + config.COST_PCT_PER_SIDE / 100)
            row["decision"] = "BOUGHT"
            row["reason"] = f"BOUGHT {qty} @ {price:.2f} [{tag}], stop {plan['stop']:.2f}. {ev['headline']}"
            sig_lines.append(f"  {sym} [{tag}]: score {ev['score']} - BOUGHT {qty} @ {price:.2f} = Rs{qty * price:,.0f} (limit: {alloc['limit']}) "
                             f"stop {plan['stop']:.2f}, T1 {plan['target1']:.2f}, T2 {plan['target2']:.2f}, hold: {plan['expected_hold']} {nws}")
            slog.append({"time": started.strftime("%Y-%m-%d %H:%M:%S"), "scan_id": scan_id, "symbol": sym, "event": monitor.NEW_BUY,
                         "strategy": plan["strategy"], "price": round(price, 2), "pnl_pct": "", "thesis_valid": "true",
                         "detail": f"BOUGHT {qty}: {plan['why']}"[:300]})
            tg.send(tg.msg_signal(sym, ev, price, qty, plan["stop"], True, "", plan, alloc))
    _log_signal(scan_id, slog)

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
