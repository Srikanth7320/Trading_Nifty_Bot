"""Telegram notifications and command handling (paper trading only; only the configured chat is obeyed)."""
import html
import threading
import time

import requests

import config

E = html.escape


def enabled():
    return bool(config.TELEGRAM_TOKEN and config.TELEGRAM_CHAT_ID)


def send(text, chat_id=None):
    if not enabled():
        return False
    try:
        r = requests.post(f"https://api.telegram.org/bot{config.TELEGRAM_TOKEN}/sendMessage", timeout=15,
                          json={"chat_id": chat_id or config.TELEGRAM_CHAT_ID, "text": text[:4000], "parse_mode": "HTML",
                                "disable_web_page_preview": True})
        return r.ok
    except requests.RequestException:
        return False


def msg_signal(sym, ev, price, qty, stop, bought, note, plan=None, alloc=None):
    m = ev["metrics"]
    plan = plan or {}
    head = "🟢 <b>PAPER BUY</b>" if bought else "🔔 <b>SIGNAL (not traded)</b>"
    top = [c for c in ev["checks"] if c["status"] == "pass"][:4]
    tag = f" [{E(plan.get('label', ''))}]" if plan else ""
    lines = [f"{head} - <b>{E(sym)}</b>{tag}", f"Price Rs{price:.2f} | Score {ev['score']}/100 | RSI {m.get('rsi', 0):.0f} | Volume {m.get('volume_ratio', 0):.1f}x"]
    if plan:
        lines.append(f"Strategy: <b>{E(plan['label'])}</b> - {E(plan['expected_hold'])}")
        lines.append(f"Entry Rs{price:.2f} | Stop Rs{plan['stop']:.2f} | T1 Rs{plan['target1']:.2f} | T2 Rs{plan['target2']:.2f} | "
                     f"R:R 1:{plan['rr1']:g} (T1) / 1:{plan['rr2']:g} (T2)")
        lines.append("Why this strategy: " + E(plan["why"][:260]))
    if bought:
        lines.append(f"Qty {qty} = Rs{qty * price:,.0f}" + (f" (sized by: {E(alloc['limit'])})" if alloc else "") + f" | max loss at stop ≈ Rs{(price - stop) * qty:,.0f}")
    else:
        lines.append(f"Why not traded: {E(note)}")
    lines.append("Why: " + "; ".join(E(c["label"].split(":")[0]) for c in top))
    if plan:
        lines.append("Invalidated if: " + E("; ".join(plan["invalidation"][:3])))
    lines.append("<i>Paper trade - no real order was placed.</i>")
    return "\n".join(lines)


def msg_exit(sym, kind, price, qty, pnl, reason, tag=""):
    icon = "🟡" if kind == "PARTIAL" else ("🔴" if pnl < 0 else "✅")
    return (f"{icon} <b>PAPER {'PARTIAL SELL' if kind == 'PARTIAL' else 'EXIT'}</b> - <b>{E(sym)}</b>{' [' + E(tag) + ']' if tag else ''}\n"
            f"{qty} @ Rs{price:.2f} | P&amp;L Rs{pnl:,.2f}\nReason: {E(reason)}")


def msg_position_alert(sym, tag, state, price, pnl_pct, note, auto):
    icon = "⚠️" if state == "HOLD_WITH_WARNING" else "🚨"
    label = "HOLD WITH WARNING" if state == "HOLD_WITH_WARNING" else "EXIT RECOMMENDED"
    extra = "" if state == "HOLD_WITH_WARNING" else ("\nPaper exit will execute automatically." if auto else "\nAutomatic exit is OFF - review manually.")
    return f"{icon} <b>{label}</b> - <b>{E(sym)}</b> [{E(tag)}] @ Rs{price:.2f} ({pnl_pct:+.1f}%)\n{E(note[:400])}{extra}"


def msg_scan_summary(run):
    return (f"📊 <b>Scan {E(run['scan_id'])}</b> ({E(run['mode'])})\nChecked {run['evaluated']} | no data {run['no_data']} | "
            f"filtered {run['filtered']} | rejected {run['rejected']}\nWatch {run['watch']} | signals {run['signals']} | "
            f"bought {run['buys']} | sold {run['sells']}")


# ---------------------------------------------------------------- commands
HELP = ("<b>NiftyBot commands</b>\n/status - bot, market and positions\n/positions - open paper positions\n"
        "/watchlist - stocks close to a signal\n/today - today's scans and P&amp;L\n/why SYMBOL - why a stock was or wasn't picked\n"
        "/scan - run a scan now (analysis-only when market is closed)\n/pause and /resume - stop or restart paper trading\n"
        "/performance - closed-trade statistics\n/pnl - realized / unrealized P&amp;L and capital\n/closed - recent sold positions\n"
        "/outlook - next-session market and position outlook\n/analyze SYMBOL - independent stock analysis (no trade)\n/health - data-source check\n/help - this list\n"
        "<i>Everything here is paper trading; no broker orders are ever sent.</i>")


def handle(command, args):
    import providers
    import reports
    import scanner
    import store
    command = command.lower().split("@")[0]
    if command in ("/start", "/help"):
        return HELP
    if command == "/status":
        return reports.status_text()
    if command == "/positions":
        return reports.positions_text()
    if command == "/watchlist":
        return reports.watchlist_text()
    if command == "/today":
        return reports.today_text()
    if command == "/performance":
        return reports.performance_text()
    if command == "/pnl":
        return reports.pnl_text()
    if command == "/closed":
        return reports.closed_text()
    if command == "/outlook":
        return reports.outlook_text()
    if command == "/analyze":
        return reports.analysis_text(args[0].upper()) if args else "Usage: /analyze SYMBOL"
    if command == "/why":
        return reports.why_text(args[0].upper()) if args else "Usage: /why SYMBOL"
    if command == "/pause":
        store.set_control(False, "telegram")
        return "⏸ Paper trading paused. Open positions are still monitored. Use /resume to continue."
    if command == "/resume":
        store.set_control(True, "telegram")
        return "▶️ Paper trading resumed."
    if command == "/scan":
        store.request_scan("telegram")
        return "🔎 Scan requested. The worker will start it within a few seconds; results will follow."
    if command == "/health":
        lines = [f"{'✅' if ok else '❌'} {E(n)}: {E(d)}" for n, ok, d in providers.health_check()]
        warn = config.holiday_calendar_warning()
        return "\n".join(lines + ([f"⚠️ {E(warn)}"] if warn else []))
    return "Unknown command. Send /help."


def poll_forever(stop_event):
    if not enabled():
        return
    offset = None
    while not stop_event.is_set():
        try:
            r = requests.get(f"https://api.telegram.org/bot{config.TELEGRAM_TOKEN}/getUpdates", timeout=40,
                             params={"timeout": 25, "offset": offset})
            for upd in r.json().get("result", []):
                offset = upd["update_id"] + 1
                msg = upd.get("message") or {}
                text = (msg.get("text") or "").strip()
                if str((msg.get("chat") or {}).get("id")) != config.TELEGRAM_CHAT_ID or not text.startswith("/"):
                    continue
                parts = text.split()
                try:
                    send(handle(parts[0], parts[1:]))
                except Exception as exc:  # never let one bad command kill the listener
                    send(f"⚠️ Command failed: {E(type(exc).__name__)}")
        except Exception:
            time.sleep(5)
