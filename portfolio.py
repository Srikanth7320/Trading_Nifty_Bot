"""Paper positions, trade ledger, cooldowns and P&L. No real orders are ever placed."""
import pandas as pd

import config
import store


def _f(v, default=0.0):
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def _ts():
    return config.now_ist().strftime("%Y-%m-%d %H:%M:%S")


def _clean(text, n=300):
    """Single-line text: AI/news notes contain newlines that would make the CSV unreadable."""
    return " ".join(str(text or "").split())[:n]


_names = {}


def company_name(symbol):
    """Company name from data/nse_equity_symbols.csv ('' when unknown)."""
    if not _names:
        try:
            df = pd.read_csv(store.path("nse_equity_symbols.csv"), dtype=str, keep_default_na=False)
            df.columns = [c.strip() for c in df.columns]
            ncol = next((c for c in df.columns if "NAME" in c.upper()), None)
            scol = "Symbol" if "Symbol" in df.columns else df.columns[0]
            if ncol:
                for sym, nm in zip(df[scol], df[ncol]):
                    _names[str(sym).strip().upper()] = str(nm).strip()
        except Exception:
            pass
        _names.setdefault("", "")
    return _names.get(str(symbol).strip().upper(), "")


def positions():
    df = store.read_csv("active_stocks.csv")
    out = []
    for r in df.to_dict(orient="records"):
        if not r.get("symbol"):
            continue
        out.append({**r, "buy_price": _f(r["buy_price"]), "quantity": int(_f(r["quantity"])),
                    "original_quantity": int(_f(r["original_quantity"] or r["quantity"])),
                    "stop_loss": _f(r["stop_loss"]), "target1": _f(r["target1"]), "target2": _f(r["target2"]),
                    "peak_price": _f(r["peak_price"] or r["buy_price"]), "entry_atr": _f(r["entry_atr"]),
                    "target1_hit": str(r["target1_hit"]).lower() == "true", "score": _f(r["score"]),
                    "strategy": r.get("strategy") or "SHORT_TERM", "strategy_legacy": not r.get("strategy"),
                    "company": r.get("company") or company_name(r["symbol"]),
                    "initial_risk": _f(r.get("initial_risk")), "entry_level": _f(r.get("entry_level")),
                    "max_hold_sessions": int(_f(r.get("max_hold_sessions"))), "warn_score": int(_f(r.get("warn_score"))),
                    "exit_confirm": int(_f(r.get("exit_confirm"))),
                    "thesis_valid": (r.get("thesis_valid") or "").lower() != "false",
                    "signal_state": r.get("signal_state") or "CONTINUE_HOLDING"})
    return out


def add_position(symbol, price, qty, stop, t1, t2, atr, score, reason, plan=None):
    """Open a paper position. `plan` (strategy.build_plan) records strategy, holding period and invalidation rules."""
    plan = plan or {}
    with store.locked():
        df = store.read_csv("active_stocks.csv")
        if (df["symbol"] == symbol).any() or qty < 1:     # never a duplicate row / empty position
            return False
        reason = _clean(reason)
        row = {"symbol": symbol, "buy_price": round(price, 2), "quantity": qty, "original_quantity": qty,
               "buy_time": _ts(), "stop_loss": round(stop, 2), "target1": round(t1, 2), "target2": round(t2, 2),
               "peak_price": round(price, 2), "entry_atr": round(atr, 2), "target1_hit": "false",
               "score": round(score, 1), "reason": reason,
               "company": company_name(symbol), "strategy": plan.get("strategy", "SHORT_TERM"),
               "strategy_reason": _clean(plan.get("why", ""), 400), "expected_hold": plan.get("expected_hold", ""),
               "max_hold_sessions": plan.get("max_hold_sessions", config.MAX_HOLD_SESSIONS),
               "initial_risk": round(plan.get("initial_risk", price - stop), 4), "entry_level": round(plan.get("entry_level", 0) or 0, 2),
               "invalidation": _clean(" | ".join(plan.get("invalidation", [])), 500), "be_r": plan.get("be_r", ""),
               "trail_mult": plan.get("trail_mult", ""), "signal_state": "CONTINUE_HOLDING", "warn_score": 0,
               "exit_confirm": 0, "last_assessed": _ts(), "last_note": "new position", "thesis_valid": "true"}
        store.write_csv("active_stocks.csv", pd.concat([df, pd.DataFrame([row])], ignore_index=True))
        store.append_rows("trades_history.csv", [{"time": _ts(), "symbol": symbol, "type": "BUY", "price": round(price, 2),
                                                 "quantity": qty, "pnl": 0, "reason": reason,
                                                 "strategy": plan.get("strategy", "SHORT_TERM")}])
    return True


def update_position(symbol, **fields):
    with store.locked():
        df = store.read_csv("active_stocks.csv")
        mask = df["symbol"] == symbol
        for k, v in fields.items():
            if k in df.columns:
                df[k] = df[k].astype(object)  # pandas 3 str dtype rejects floats/bools
                df.loc[mask, k] = str(v).lower() if isinstance(v, bool) else str(v)
        store.write_csv("active_stocks.csv", df)


def sell(symbol, price, qty, reason):
    """Sell qty shares (partial or full). Returns realised P&L after costs, or None if not held."""
    with store.locked():
        df = store.read_csv("active_stocks.csv")
        mask = df["symbol"] == symbol
        if not mask.any():
            return None
        row = df[mask].iloc[0]
        buy, held = _f(row["buy_price"]), int(_f(row["quantity"]))
        qty = max(1, min(int(qty), held))
        pnl = (price - buy) * qty - (price + buy) * qty * config.COST_PCT_PER_SIDE / 100
        pnl = round(pnl, 2)
        full = qty >= held
        reason = _clean(reason)
        strat = row.get("strategy") or "SHORT_TERM"
        bt = str(row.get("buy_time") or "")
        try:
            hold_days = round((config.now_ist().replace(tzinfo=None) - pd.Timestamp(bt).to_pydatetime()).total_seconds() / 86400, 2)
        except Exception:
            hold_days = ""
        store.append_rows("sold_stocks.csv", [{
            "symbol": symbol, "buy_price": round(buy, 2), "sold_price": round(price, 2), "quantity": qty, "profit": pnl,
            "sold_time": _ts(), "reason": reason, "company": row.get("company") or company_name(symbol), "strategy": strat,
            "buy_time": bt, "buy_value": round(buy * qty, 2), "sell_value": round(price * qty, 2),
            "pnl_pct": round(pnl / (buy * qty) * 100, 2) if buy * qty else "", "hold_days": hold_days,
            "exit_type": "FULL" if full else "PARTIAL", "peak_price": row.get("peak_price", ""),
            "thesis": _clean(row.get("strategy_reason") or row.get("reason"), 300), "stop_loss": row.get("stop_loss", ""),
            "initial_stop": round(buy - _f(row.get("initial_risk")), 2) if _f(row.get("initial_risk")) else ""}])
        store.append_rows("trades_history.csv", [{"time": _ts(), "symbol": symbol, "type": "SELL", "price": round(price, 2),
                                                 "quantity": qty, "pnl": pnl, "reason": reason, "strategy": strat}])
        store.append_rows("signal_log.csv", [{"time": _ts(), "scan_id": "", "symbol": symbol,
                                              "event": "POSITION_CLOSED" if full else "PARTIAL_EXIT", "strategy": strat,
                                              "price": round(price, 2), "pnl_pct": round(pnl / (buy * qty) * 100, 2) if buy * qty else "",
                                              "thesis_valid": "", "detail": reason}])
        if qty >= held:
            store.write_csv("active_stocks.csv", df[~mask])
            store.append_rows("cooling_period.csv", [{"symbol": symbol, "time": _ts()}])
        else:
            df["quantity"] = df["quantity"].astype(object)
            df.loc[mask, "quantity"] = str(held - qty)
            store.write_csv("active_stocks.csv", df)
    return pnl


def cooling_symbols():
    df = store.read_csv("cooling_period.csv")
    out, now = {}, config.now_ist()
    for r in df.to_dict(orient="records"):
        try:
            t = pd.Timestamp(r["time"]).tz_localize(config.IST).to_pydatetime()
            hrs = (now - t).total_seconds() / 3600
            if hrs < config.COOLING_HOURS:
                out[r["symbol"]] = round(config.COOLING_HOURS - hrs, 1)
        except Exception:
            out[r.get("symbol", "")] = config.COOLING_HOURS
    return out


def today_str():
    return config.now_ist().strftime("%Y-%m-%d")


def realized_today():
    df = store.read_csv("sold_stocks.csv")
    df = df[df["sold_time"].str.startswith(today_str())]
    return round(sum(_f(x) for x in df["profit"]), 2)


def buys_today():
    df = store.read_csv("trades_history.csv")
    return int(((df["type"] == "BUY") & df["time"].str.startswith(today_str())).sum())


def daily_loss_limit_hit():
    return realized_today() <= -(config.CAPITAL * config.MAX_DAILY_LOSS_PCT / 100)


def performance():
    df = store.read_csv("sold_stocks.csv")
    pnl = [_f(x) for x in df["profit"]]
    wins = [x for x in pnl if x > 0]
    losses = [x for x in pnl if x < 0]
    return {"closed_trades": len(pnl), "net_pnl": round(sum(pnl), 2),
            "win_rate": round(100 * len(wins) / len(pnl), 1) if pnl else 0.0,
            "avg_win": round(sum(wins) / len(wins), 2) if wins else 0.0,
            "avg_loss": round(sum(losses) / len(losses), 2) if losses else 0.0,
            "profit_factor": round(sum(wins) / abs(sum(losses)), 2) if losses else (None if not wins else float("inf"))}
