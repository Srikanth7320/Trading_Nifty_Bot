"""Background worker: schedules scans, position checks, Telegram commands, daily messages."""
import signal
import threading
import time
from datetime import timedelta

import config
import portfolio
import providers
import reports
import scanner
import store
import telegram_bot as tg


def beat(detail, next_scan=""):
    store.write_json("worker_status.json", {"heartbeat": config.now_ist().isoformat(timespec="seconds"), "detail": detail,
                                           "next_scan": next_scan, "pid": __import__("os").getpid()})


def once(day, name):
    """True the first time (day, name) is seen; persisted so a worker restart never repeats a daily message."""
    flags = store.read_json("daily_flags.json", {})
    if flags.get("day") != day:
        flags = {"day": day, "done": []}
    if name in flags["done"]:
        return False
    flags["done"].append(name)
    store.write_json("daily_flags.json", flags)
    return True


def morning_check(state):
    st, text = config.market_status()
    ok = providers.health_check()
    lines = [f"🌅 <b>Good morning</b> - {tg.E(text)}"]
    if st == "HOLIDAY" or st == "WEEKEND":
        nxt = config.next_trading_day(config.now_ist().date())
        lines.append(f"No scans or paper trades today. Next trading day: {nxt:%A %d %b}.")
    else:
        lines.append("Scans run every %d min from 09:15; new entries only %s-%s IST." % (
            config.SCAN_INTERVAL_MIN, config.ENTRY_START.strftime("%H:%M"), config.ENTRY_END.strftime("%H:%M")))
    lines += [f"{'✅' if o else '❌'} {tg.E(n)}: {tg.E(d)}" for n, o, d in ok]
    warn = config.holiday_calendar_warning()
    if warn:
        lines.append("⚠️ " + tg.E(warn))
    lines.append(reports.positions_text())
    tg.send("\n".join(lines))
    store.append_text("scan_journal.txt", f"---- {config.now_ist():%Y-%m-%d} 08:00 morning check: {text}; " +
                      "; ".join(f"{n}: {'OK' if o else 'FAIL'}" for n, o, _ in ok))


def end_of_day(state):
    if config.is_trading_day(config.now_ist().date()):
        scanner.manage_positions(mode="ANALYSIS", notify=False)  # refresh closing prices only
        txt = reports.daily_summary_text()
        store.append_text("scan_journal.txt", f"---- {config.now_ist():%Y-%m-%d} END OF DAY ----")
        if config.TELEGRAM_SCAN_SUMMARY != "off":
            tg.send(txt)


def main():
    store.init_files()
    stop = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    if tg.enabled():
        threading.Thread(target=tg.poll_forever, args=(stop,), daemon=True, name="telegram").start()
    state = {"last_scan": 0.0, "last_pos": 0.0, "done": set()}
    store.append_text("scan_journal.txt", f"==== Worker started {config.now_ist():%Y-%m-%d %H:%M} IST ====")
    tg.send("🤖 NiftyBot worker started. Send /help for commands.")
    while not stop.is_set():
        try:
            now = config.now_ist()
            day = now.date().isoformat()
            status = config.market_status(now)[0]
            nxt = ""
            hhmm = now.strftime("%H:%M")
            if "08:00" <= hhmm < "09:15" and once(day, "morning"):
                morning_check(state)
            elif hhmm >= "09:15":
                once(day, "morning")  # started late: never send a stale morning message
            if status == "OPEN":
                t = time.time()
                if t - state["last_scan"] >= config.SCAN_INTERVAL_MIN * 60:
                    state["last_scan"] = t
                    beat("scanning")
                    scanner.run_scan("scheduled")
                elif t - state["last_pos"] >= config.POSITION_CHECK_MIN * 60:
                    state["last_pos"] = t
                    scanner.manage_positions("TRADING" if store.control()["enabled"] else "ANALYSIS")
                nxt = (config.now_ist() + timedelta(seconds=max(0, config.SCAN_INTERVAL_MIN * 60 - (time.time() - state["last_scan"])))).strftime("%H:%M")
            elif hhmm >= "15:45" and status == "CLOSED" and once(day, "eod"):
                end_of_day(state)
            if store.pop_scan_request():
                beat("manual scan")
                scanner.run_scan("manual")
            beat("idle - " + config.market_status(now)[1], nxt)
        except Exception as exc:  # keep the worker alive; the error is visible in the journal
            import traceback
            store.append_text("scan_journal.txt", f"!! worker error: {type(exc).__name__}: {str(exc)[:200]} | "
                              + traceback.format_exc().strip().splitlines()[-3].strip())
        stop.wait(5)


if __name__ == "__main__":
    main()
