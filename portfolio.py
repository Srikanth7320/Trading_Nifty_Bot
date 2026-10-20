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
                    "target1_hit": str(r["target1_hit"]).lower() == "true", "score": _f(r["score"])})
    return out


def add_position(symbol, price, qty, stop, t1, t2, atr, score, reason):
    with store.locked():
        df = store.read_csv("active_stocks.csv")
        if (df["symbol"] == symbol).any():
            return False
        row = {"symbol": symbol, "buy_price": round(price, 2), "quantity": qty, "original_quantity": qty,
               "buy_time": _ts(), "stop_loss": round(stop, 2), "target1": round(t1, 2), "target2": round(t2, 2),
               "peak_price": round(price, 2), "entry_atr": round(atr, 2), "target1_hit": "false",
               "score": round(score, 1), "reason": reason[:300]}
        store.write_csv("active_stocks.csv", pd.concat([df, pd.DataFrame([row])], ignore_index=True))
        store.append_rows("trades_history.csv", [{"time": _ts(), "symbol": symbol, "type": "BUY", "price": round(price, 2),
                                                 "quantity": qty, "pnl": 0, "reason": reason[:300]}])
    return True


def update_position(symbol, **fields):
    with store.locked():
        df = store.read_csv("active_stocks.csv")
        mask = df["symbol"] == symbol
        for k, v in fields.items():
            if k in df.columns:
                df.loc[mask, k] = str(v).lower() if isinstance(v, bool) else v
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
        store.append_rows("sold_stocks.csv", [{"symbol": symbol, "buy_price": round(buy, 2), "sold_price": round(price, 2),
                                              "quantity": qty, "profit": pnl, "sold_time": _ts(), "reason": reason[:300]}])
        store.append_rows("trades_history.csv", [{"time": _ts(), "symbol": symbol, "type": "SELL", "price": round(price, 2),
                                                 "quantity": qty, "pnl": pnl, "reason": reason[:300]}])
        if qty >= held:
            store.write_csv("active_stocks.csv", df[~mask])
            store.append_rows("cooling_period.csv", [{"symbol": symbol, "time": _ts()}])
        else:
            df.loc[mask, "quantity"] = held - qty
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
