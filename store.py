"""File-based storage with cross-process locking (web server + worker share data/)."""
import csv
import json
import os
import threading
from contextlib import contextmanager

import pandas as pd

import config

try:
    import fcntl
except ImportError:  # Windows dev machines
    fcntl = None

SCHEMAS = {
    "active_stocks.csv": ["symbol", "buy_price", "quantity", "original_quantity", "buy_time", "stop_loss",
                          "target1", "target2", "peak_price", "entry_atr", "target1_hit", "score", "reason",
                          # --- added: strategy classification, thesis and monitoring state (blank on legacy rows) ---
                          "company", "strategy", "strategy_reason", "expected_hold", "max_hold_sessions", "initial_risk",
                          "entry_level", "invalidation", "be_r", "trail_mult", "signal_state", "warn_score",
                          "exit_confirm", "last_assessed", "last_note", "thesis_valid"],
    "sold_stocks.csv": ["symbol", "buy_price", "sold_price", "quantity", "profit", "sold_time", "reason",
                        "company", "strategy", "buy_time", "buy_value", "sell_value", "pnl_pct", "hold_days",
                        "exit_type", "peak_price", "thesis", "stop_loss", "initial_stop"],
    "trades_history.csv": ["time", "symbol", "type", "price", "quantity", "pnl", "reason", "strategy"],
    "cooling_period.csv": ["symbol", "time"],
    "scan_runs.csv": ["scan_id", "started", "finished", "trigger", "mode", "status", "universe_size",
                      "batch_start", "batch_size", "evaluated", "no_data", "filtered", "rejected", "watch",
                      "signals", "buys", "sells", "providers", "note", "positions_checked", "warnings", "exit_signals"],
    "scan_details.csv": ["scan_id", "time", "symbol", "decision", "score", "price", "rsi", "volume_ratio",
                         "reason"],
    # one row per event: new buy opportunity / hold / warning / exit recommended / stop / target / closed
    "signal_log.csv": ["time", "scan_id", "symbol", "event", "strategy", "price", "pnl_pct", "thesis_valid", "detail"],
    # one row per trading day, upserted during the day (see pnl.snapshot)
    "daily_pnl.csv": ["date", "realized_day", "cum_realized", "unrealized", "total_pnl", "day_pnl", "invested",
                      "market_value", "open_positions", "closed_trades", "updated"],
}

_tlock = threading.RLock()
_local = threading.local()


def path(name):
    return config.DATA_DIR / name


@contextmanager
def locked():
    """Re-entrant exclusive lock (thread + process)."""
    depth = getattr(_local, "depth", 0)
    if depth:
        _local.depth = depth + 1
        try:
            yield
        finally:
            _local.depth -= 1
        return
    with _tlock:
        config.DATA_DIR.mkdir(parents=True, exist_ok=True)
        fh = open(path(".lock"), "a")
        try:
            if fcntl:
                fcntl.flock(fh, fcntl.LOCK_EX)
            _local.depth = 1
            yield
        finally:
            _local.depth = 0
            if fcntl:
                fcntl.flock(fh, fcntl.LOCK_UN)
            fh.close()


def _migrate(name, cols):
    """Add newly introduced columns to an existing ledger (old rows get blanks). Never drops or reorders data."""
    p = path(name)
    with open(p, "r", newline="", encoding="utf-8") as fh:
        header = next(csv.reader(fh), [])
    if not header or header == cols:
        return
    if header != cols[:len(header)]:
        # unknown/reordered header: keep every old column, append the missing ones, never lose data
        cols = header + [c for c in cols if c not in header]
    df = pd.read_csv(p, dtype=str, keep_default_na=False)
    for c in cols:
        if c not in df.columns:
            df[c] = ""
    tmp = str(p) + ".tmp"
    df[cols].to_csv(tmp, index=False)
    os.replace(tmp, p)


def init_files():
    """Create empty ledgers (header only) when missing; add new columns to old ledgers. Never deletes data."""
    with locked():
        for name, cols in SCHEMAS.items():
            if not path(name).exists():
                with open(path(name), "w", newline="", encoding="utf-8") as fh:
                    csv.writer(fh).writerow(cols)
            else:
                try:
                    _migrate(name, cols)
                except Exception:  # a damaged ledger must not stop the bot; read_csv still tolerates it
                    pass


def read_csv(name):
    cols = SCHEMAS.get(name)
    p = path(name)
    with locked():
        try:
            df = pd.read_csv(p, dtype=str, keep_default_na=False).astype(object)
        except (FileNotFoundError, pd.errors.EmptyDataError):
            df = pd.DataFrame(columns=cols or []).astype(object)
    for c in cols or []:
        if c not in df.columns:
            df[c] = ""
    return df


def write_csv(name, df):
    with locked():
        tmp = str(path(name)) + ".tmp"
        df.to_csv(tmp, index=False)
        os.replace(tmp, path(name))


def append_rows(name, rows):
    """rows: list of dicts; written in schema column order."""
    if not rows:
        return
    cols = SCHEMAS[name]
    with locked():
        new = not path(name).exists() or path(name).stat().st_size == 0
        if not new:
            try:
                _migrate(name, cols)  # header behind schema (e.g. deployed without restart): fix before appending
            except Exception:
                pass
        with open(path(name), "a", newline="", encoding="utf-8") as fh:
            w = csv.writer(fh)
            if new:
                w.writerow(cols)
            for r in rows:
                w.writerow([r.get(c, "") for c in cols])


def trim_csv(name, max_rows):
    with locked():
        df = read_csv(name)
        if len(df) > max_rows:
            write_csv(name, df.tail(int(max_rows * 0.8)))


def read_json(name, default=None):
    try:
        with open(path(name), "r", encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return {} if default is None else default


def write_json(name, obj):
    with locked():
        tmp = str(path(name)) + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(obj, fh, indent=1, default=str)
        os.replace(tmp, path(name))


def append_text(name, text, max_bytes=2_000_000):
    with locked():
        p = path(name)
        if p.exists() and p.stat().st_size > max_bytes:  # keep newest half
            data = p.read_text(encoding="utf-8", errors="ignore")
            p.write_text(data[len(data) // 2:], encoding="utf-8")
        with open(p, "a", encoding="utf-8") as fh:
            fh.write(text if text.endswith("\n") else text + "\n")


def tail_text(name, max_chars=60000):
    try:
        data = path(name).read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return ""
    return data[-max_chars:]


# ---- small control / status files ---------------------------------------------
def control():
    c = read_json("bot_control.json", {})
    c.setdefault("enabled", True)
    return c


def set_control(enabled, by="dashboard"):
    write_json("bot_control.json", {"enabled": bool(enabled), "updated": config.now_ist().isoformat(timespec="seconds"),
                                    "by": by})


def request_scan(by="dashboard"):
    write_json("scan_request.json", {"requested": config.now_ist().isoformat(timespec="seconds"), "by": by})


def request_task(name, by="dashboard"):
    """Ask the worker to run a named job (e.g. 'outlook'). One pending request per name."""
    write_json(f"task_{name}.json", {"requested": config.now_ist().isoformat(timespec="seconds"), "by": by})


def pop_task(name):
    with locked():
        req = read_json(f"task_{name}.json", {})
        if req:
            try:
                os.remove(path(f"task_{name}.json"))
            except OSError:
                pass
        return req or None


def pop_scan_request():
    with locked():
        req = read_json("scan_request.json", {})
        if req:
            try:
                os.remove(path("scan_request.json"))
            except OSError:
                pass
        return req or None
