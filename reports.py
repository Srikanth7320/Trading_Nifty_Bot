"""Read-only views over the data files: used by the dashboard and the Telegram commands."""
import html
from datetime import datetime

import config
import pnl
import portfolio
import store

E = html.escape


def _f(v, d=0.0):
    try:
        return float(v)
    except (TypeError, ValueError):
        return d


def worker_state():
    w = store.read_json("worker_status.json", {})
    beat = w.get("heartbeat")
    alive = False
    if beat:
        try:
            alive = (config.now_ist() - datetime.fromisoformat(beat)).total_seconds() < 90
        except ValueError:
            pass
    return {"alive": alive, "heartbeat": beat, "detail": w.get("detail", ""), "next_scan": w.get("next_scan", "")}


def explain_state():
    """One plain-English sentence on what the bot is doing right now."""
    status, text = config.market_status()
    ctl, w = store.control(), worker_state()
    if not w["alive"]:
        return "The background worker is NOT running, so nothing is being scanned. Start it with: sudo systemctl start niftybot-worker."
    if not ctl["enabled"]:
        return "Paper trading is paused. The bot still monitors open positions and will resume when you press Resume."
    if status == "OPEN":
        if config.entry_window_open():
            return "Market is open and the bot is scanning for breakout setups and managing open positions."
        return (f"Market is open. The bot is monitoring positions; new entries are only taken between "
                f"{config.ENTRY_START:%H:%M} and {config.ENTRY_END:%H:%M} IST (scans continue to build the watchlist).")
    nxt = config.next_trading_day(config.now_ist().date())
    if status in ("HOLIDAY", "WEEKEND") or config.now_ist().time() > config.MARKET_CLOSE:
        return f"{text}. No scans or trades until the market reopens on {nxt:%A, %d %b}. You can press Scan now for an analysis-only run."
    return f"{text}. Scanning starts automatically at 09:15 IST."


def last_scan():
    return store.read_json("last_scan.json", {})


def runs(n=30, today_only=False):
    df = store.read_csv("scan_runs.csv")
    if today_only:
        df = df[df["started"].str.startswith(portfolio.today_str())]
    return df.tail(n).iloc[::-1].to_dict(orient="records")


def run_details(scan_id, decision=None):
    df = store.read_csv("scan_details.csv")
    df = df[df["scan_id"] == scan_id]
    if decision:
        df = df[df["decision"] == decision]
    order = {"BOUGHT": 0, "SIGNAL": 1, "WATCH": 2, "REJECT": 3, "FILTERED": 4, "NO_DATA": 5}
    df = df.assign(_o=df["decision"].map(order).fillna(9), _s=df["score"].map(_f)).sort_values(["_o", "_s"], ascending=[True, False])
    return df.drop(columns=["_o", "_s"]).to_dict(orient="records")


def symbol_history(sym, n=15):
    df = store.read_csv("scan_details.csv")
    return df[df["symbol"] == sym].tail(n).iloc[::-1].to_dict(orient="records")


def watchlist():
    wl = store.read_json("watchlist.json", {})
    return sorted(({"symbol": k, **v} for k, v in wl.items()), key=lambda x: -x.get("score", 0))


def position_rows():
    """Open positions with latest cached price, P&L, strategy and monitoring state (see pnl.position_view)."""
    return pnl.position_view()


def today_stats():
    rs = runs(500, today_only=True)
    s = lambda k: sum(int(_f(r[k])) for r in rs)
    return {"scans": len([r for r in rs if r["status"] != "SKIPPED"]), "evaluated": s("evaluated"), "signals": s("signals"),
            "buys": s("buys"), "sells": s("sells"), "realized": portfolio.realized_today(), "open": len(portfolio.positions()),
            "warnings": sum(int(_f(r.get("warnings"))) for r in rs[:1]), "exit_signals": s("exit_signals")}


# ---------------------------------------------------------------- Telegram text
def status_text():
    st, txt = config.market_status()
    ctl, w = store.control(), worker_state()
    ls = last_scan()
    lines = [f"<b>Status</b>", E(txt), f"Trading: {'ON' if ctl['enabled'] else 'PAUSED'} | Worker: {'alive' if w['alive'] else 'NOT RUNNING'}",
             f"Open positions: {len(portfolio.positions())}/{config.MAX_OPEN_POSITIONS} | Realised today: Rs{portfolio.realized_today():,.2f}"]
    if ls:
        lines.append(f"Last scan {E(ls.get('started', ''))}: checked {ls.get('evaluated')}, watch {ls.get('watch')}, signals {ls.get('signals')}, bought {ls.get('buys')}")
    lines.append(E(explain_state()))
    return "\n".join(lines)


def positions_text():
    rows = position_rows()
    if not rows:
        return "No open paper positions."
    out = [f"<b>Open paper positions ({len(rows)}/{config.MAX_OPEN_POSITIONS})</b>"]
    for r in rows:
        pnl_s = f" | P&amp;L Rs{r['pnl']:,.0f} ({r['pnl_pct']:+.1f}%)" if r["pnl"] is not None else ""
        out.append(f"{E(r['symbol'])} [{E(r['strategy'].replace('_', '-'))}]: {r['quantity']} @ {r['buy_price']:.2f} | stop {r['stop_loss']:.2f} | T1 {r['target1']:.2f}{pnl_s} | {E(r['signal_state'].replace('_', ' ').title())}")
    return "\n".join(out)


def pnl_text():
    a = pnl.account()
    return (f"<b>Portfolio (paper)</b>\nCapital Rs{a['capital']:,.0f} | available cash Rs{a['cash']:,.0f}\n"
            f"Invested Rs{a['invested']:,.0f} | market value Rs{a['market_value']:,.0f}\n"
            f"Unrealised P&amp;L Rs{a['unrealized']:,.2f} | Realised P&amp;L Rs{a['realized']:,.2f}\n<b>Combined Rs{a['combined']:,.2f}</b>\n"
            f"Positions {a['open_positions']}/{a['max_positions']} (free slots {a['slots_free']}) | completed trades {a['closed_trades']}"
            + (f"\nPrices missing for: {E(', '.join(a['prices_missing']))}" if a['prices_missing'] else ""))


def closed_text(n=8):
    rows = pnl.closed_positions(n)
    if not rows:
        return "No sold positions yet."
    out = ["<b>Recent sold positions</b>"]
    for c in rows:
        out.append(f"{E(c['symbol'])} [{E(c['strategy'].replace('_', '-'))}] {c['qty']} @ {c['buy_price']} -> {c['sold_price']} | Rs{c['pnl']:,.2f} ({c['pnl_pct']:+.1f}%) | {E(c['exit_class'])} | {c['sold_time'][:16]}")
    return "\n".join(out)


def outlook_text():
    import outlook
    o = outlook.load()
    if not o:
        return "No outlook has been generated yet. Open the Outlook page and press Refresh, or wait for the 16:00 / 08:30 run."
    m = o["market"]
    lines = [f"<b>Outlook for {E(o['for_session'])}</b> (generated {E(o['generated'])})",
             f"Market: <b>{E(m['direction'])}</b> (confidence {E(m['confidence'])}, {E(m['data_points'])} inputs)"]
    if m.get("events"):
        lines.append("Events in the news: " + E(", ".join(m["events"])))
    for r in o["stocks"]:
        if r["kind"] == "POSITION" or r.get("action") == "Monitor Closely":
            lines.append(f"{E(r['symbol'])} [{E(r['kind'].title())}]: {E(r.get('direction', '?'))} | {E(r.get('gap_risk', ''))} | <b>{E(r.get('action', ''))}</b> ({E(r.get('confidence', ''))})")
    lines.append("<i>Forecast only - opening prices are unknown until 09:15 IST.</i>")
    return "\n".join(lines)[:3900]


def analysis_text(sym):
    import analysis
    r = analysis.resolve(sym)
    if r["status"] != "ok":
        return E(r.get("message") or "Several matches: " + ", ".join(m["symbol"] for m in r.get("matches", [])))
    return analysis.text_summary(analysis.analyze(r["symbol"], r["name"]))


def watchlist_text():
    wl = watchlist()[:10]
    if not wl:
        return "Watchlist is empty - nothing is close to a signal right now."
    return "<b>Closest to a signal</b>\n" + "\n".join(f"{E(w['symbol'])}: {w['score']}/100 @ {w['price']} - {E(w['reason'][:110])}" for w in wl)


def today_text():
    t = today_stats()
    return (f"<b>Today</b>\nScans: {t['scans']} | stocks checked: {t['evaluated']}\nSignals: {t['signals']} | buys: {t['buys']} | sells: {t['sells']}\n"
            f"Realised P&amp;L: Rs{t['realized']:,.2f} | open positions: {t['open']}")


def performance_text():
    p = portfolio.performance()
    if not p["closed_trades"]:
        return "No closed paper trades yet."
    return (f"<b>Performance (paper, after costs)</b>\nClosed trades: {p['closed_trades']} | win rate {p['win_rate']}%\nNet P&amp;L: Rs{p['net_pnl']:,.2f}\n"
            f"Avg win Rs{p['avg_win']:,.2f} | avg loss Rs{p['avg_loss']:,.2f} | profit factor {p['profit_factor']}")


def why_text(sym):
    rows = symbol_history(sym, 3)
    if not rows:
        return f"{E(sym)} has not been evaluated yet. It is scanned when its turn comes in the rotation (the full list is covered over several scans)."
    out = [f"<b>{E(sym)}</b> - latest evaluations"]
    for r in rows:
        out.append(f"{r['scan_id']}: {r['decision']} (score {r['score'] or '-'}) - {E(r['reason'][:200])}")
    return "\n".join(out)


def daily_summary_text():
    t = today_stats()
    wl = watchlist()[:5]
    a = pnl.account()
    lines = [f"📅 <b>End-of-day summary - {portfolio.today_str()}</b>", f"Scans: {t['scans']} | stocks checked: {t['evaluated']}",
             f"Signals: {t['signals']} | buys: {t['buys']} | sells: {t['sells']} | realised today Rs{t['realized']:,.2f}",
             f"Open unrealised Rs{a['unrealized']:,.2f} | cumulative realised Rs{a['realized']:,.2f} | <b>combined Rs{a['combined']:,.2f}</b>", positions_text()]
    if t["signals"] == 0:
        lines.append("No signal today - the bot only trades clean breakouts, and waiting is a valid decision.")
    if wl:
        lines.append("<b>Watch tomorrow:</b> " + ", ".join(f"{E(w['symbol'])} ({w['score']})" for w in wl))
    return "\n".join(lines)
