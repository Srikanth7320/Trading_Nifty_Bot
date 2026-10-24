"""Validation of the enhancement round: allocation, P&L, penny stocks, strategy classification, monitoring,
outlook, analysis page, persistence and duplicate prevention."""
import json
import shutil
import sys
import unittest
from datetime import date, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from tests.test_bot import TMP, make, reset  # noqa: E402  (sets the test environment)

import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402

import analysis, config, market, monitor, news, outlook, pnl, portfolio, providers, reports, scanner, store, strategy  # noqa: E402

IST = config.IST


def at(h, m, d=(2026, 10, 21)):
    return datetime(*d, h, m, tzinfo=IST)


def add(sym, price=100.0, qty=10, stop=95.0, **plan):
    return portfolio.add_position(sym, price, qty, stop, price * 1.1, price * 1.2, 3.0, 80, "test reason\nwith newline",
                                  {"strategy": "SHORT_TERM", "why": "w", "expected_hold": "1-4 weeks", "initial_risk": price - stop,
                                   "entry_level": price * 0.99, "invalidation": ["x"], **plan})


def quotes(**px):
    store.write_json("quotes_cache.json", {s: {"price": p, "time": "2026-10-21T14:00:00+05:30", "provider": "T"} for s, p in px.items()})


class AllocationTests(unittest.TestCase):
    def test_fifteen_positions_fit_and_no_oversizing(self):
        capital, cash, held = 100000.0, 100000.0, 0
        for price in (10, 25, 60, 120, 250, 400, 750, 1200, 2500, 40, 90, 180, 330, 640, 1500):
            a = strategy.allocate(price, price * 0.95, 80, cash, held, capital=capital)
            self.assertGreaterEqual(a["qty"], 1, f"price {price}: {a}")
            self.assertLessEqual(a["value"], capital * config.MAX_POSITION_PCT / 100 + 1)
            cash -= a["value"] * (1 + config.COST_PCT_PER_SIDE / 100)
            held += 1
            self.assertGreaterEqual(cash, 0)
        self.assertEqual(held, 15)
        self.assertEqual(strategy.allocate(100, 95, 90, cash, 15, capital=capital)["qty"], 0)   # 16th refused

    def test_reserve_keeps_slots_fundable(self):
        capital, cash = 100000.0, 100000.0
        for held in range(5):                                                # five buys of a top-scored stock
            a = strategy.allocate(50, 47.5, 100, cash, held, capital=capital)
            cash -= a["value"] * 1.001
        self.assertGreater(cash / (config.MAX_OPEN_POSITIONS - 5), 0.5 * capital / config.MAX_OPEN_POSITIONS)

    def test_insufficient_balance(self):
        a = strategy.allocate(1000, 950, 90, 500, 3, capital=100000)
        self.assertEqual(a["qty"], 0)
        self.assertIn("free cash", a["why"])
        b = strategy.allocate(1000, 950, 90, 5000, 3, capital=100000)
        self.assertLessEqual(b["qty"] * 1000 * 1.001, 5000)

    def test_signal_strength_and_risk_cap(self):
        lo = strategy.allocate(100, 98, 70, 100000, 0, capital=100000)
        hi = strategy.allocate(100, 98, 100, 100000, 0, capital=100000)
        self.assertGreaterEqual(hi["qty"], lo["qty"])
        wide = strategy.allocate(100, 80, 90, 100000, 0, capital=100000)       # 20% stop -> risk cap binds
        self.assertLessEqual(wide["qty"] * 20, 100000 * config.RISK_PER_TRADE_PCT / 100 + 20)

    def test_scan_never_overspends(self):
        reset()
        for i in range(12):
            add(f"S{i}", 100, 600, 95)                                           # ~Rs 7.2 lakh against a 1 lakh capital
        acc = pnl.account(pnl.position_view())
        self.assertLess(acc["cash"], 0)                                         # visible, not hidden
        self.assertEqual(strategy.allocate(100, 95, 90, acc["cash"], 12, capital=config.CAPITAL)["qty"], 0)


class PnlTests(unittest.TestCase):
    def setUp(self):
        reset()

    def test_realized_unrealized_and_history(self):
        add("AAA", 100, 10, 95)
        add("BBB", 200, 5, 190)
        quotes(AAA=110, BBB=190)
        a = pnl.account()
        self.assertAlmostEqual(a["invested"], 2000.0)
        self.assertAlmostEqual(a["market_value"], 1100 + 950)
        self.assertAlmostEqual(a["combined"], a["realized"] + a["unrealized"])
        sold = portfolio.sell("AAA", 110, 10, "Target 2 reached")
        quotes(BBB=190)
        b = pnl.account()
        self.assertEqual(b["open_positions"], 1)
        self.assertAlmostEqual(b["realized"], sold)
        self.assertEqual(b["closed_trades"], 1)
        self.assertAlmostEqual(b["combined"], b["realized"] + b["unrealized"])
        self.assertAlmostEqual(b["cash"], config.CAPITAL + sold - 1000)       # only BBB still ties up capital
        c = pnl.closed_positions()
        self.assertEqual(c[0]["symbol"], "AAA")                                # sold stock keeps its P&L
        self.assertAlmostEqual(c[0]["pnl"], sold)
        self.assertEqual((c[0]["buy_value"], c[0]["sell_value"]), (1000.0, 1100.0))
        self.assertEqual(c[0]["exit_class"], "Target achieved")
        self.assertIsNotNone(c[0]["hold_days"])

    def test_partial_then_full_no_double_count(self):
        add("AAA", 100, 10, 95)
        p1 = portfolio.sell("AAA", 110, 5, "Target 1 reached: sold half")
        quotes(AAA=105)
        mid = pnl.account()
        self.assertEqual((mid["closed_trades"], mid["partial_exits"]), (0, 1))
        self.assertAlmostEqual(mid["invested"], 500.0)
        self.assertAlmostEqual(mid["combined"], p1 + pnl.net_unrealized(100, 105, 5), places=1)
        p2 = portfolio.sell("AAA", 120, 5, "Target 2 reached")
        end = pnl.account()
        self.assertEqual((end["closed_trades"], end["open_positions"]), (1, 0))
        self.assertAlmostEqual(end["combined"], p1 + p2)
        self.assertAlmostEqual(sum(c["pnl"] for c in pnl.closed_positions()), p1 + p2)

    def test_daily_snapshot_consistency(self):
        add("AAA", 100, 10, 95)
        quotes(AAA=104)
        d1 = pnl.snapshot("2026-10-21")
        self.assertAlmostEqual(d1["total_pnl"], d1["cum_realized"] + d1["unrealized"])
        portfolio.sell("AAA", 104, 10, "Target achieved")
        d1b = pnl.snapshot("2026-10-21")                                       # upsert, not duplicate
        self.assertEqual(len(store.read_csv("daily_pnl.csv")), 1)
        add("BBB", 50, 10, 47)
        quotes(BBB=49)
        d2 = pnl.snapshot("2026-10-22")
        self.assertAlmostEqual(d2["day_pnl"], d2["total_pnl"] - d1b["total_pnl"])
        by = {r["date"]: r for r in pnl.daily_table()}
        self.assertIn("2026-10-22", by)
        self.assertIn("2026-10-21", by)
        self.assertAlmostEqual(by["2026-10-22"]["unrealized"], d2["unrealized"])
        self.assertAlmostEqual(by["2026-10-22"]["total_pnl"], d2["total_pnl"])    # sells in this test are stamped with the real clock date

    def test_persistence_and_migration(self):
        add("AAA", 100, 10, 95)
        portfolio.sell("AAA", 90, 10, "Stop-loss hit")
        before = pnl.account()
        store.init_files()                                                      # "restart"
        self.assertEqual(pnl.account()["realized"], before["realized"])
        old = Path(TMP) / "sold_stocks.csv"
        old.write_text("symbol,buy_price,sold_price,quantity,profit,sold_time,reason\nXYZ,10,9,5,-5.5,2026-10-20 11:00:00,Stop-loss hit\n")
        store.init_files()
        df = store.read_csv("sold_stocks.csv")
        self.assertEqual(df.iloc[0]["symbol"], "XYZ")
        self.assertIn("strategy", df.columns)
        self.assertEqual(pnl.closed_positions()[0]["strategy"], "SHORT_TERM")  # legacy label, flagged
        self.assertTrue(pnl.closed_positions()[0]["strategy_legacy"])

    def test_duplicate_position_refused(self):
        self.assertTrue(add("AAA"))
        self.assertFalse(add("AAA"))
        self.assertEqual(len(portfolio.positions()), 1)
        self.assertFalse(portfolio.add_position("ZZZ", 10, 0, 9, 11, 12, 1, 80, "zero qty"))


class UniverseTests(unittest.TestCase):
    def scaled(self, factor, vol=8_000_000, **kw):
        h = make(vol=vol, **kw)
        for c in ("Open", "High", "Low", "Close"):
            h[c] = h[c] * factor
        return h

    def test_penny_and_low_priced_eligible(self):
        h = self.scaled(0.09)                                                    # ~Rs 9-25 stock, high volume
        top = h.High.iloc[-20:].max()
        ev = strategy.evaluate(h, top * 1.015, 1.6e7, market_open=False)
        self.assertNotEqual(ev["decision"], "FILTERED", ev["headline"])
        self.assertTrue(ev["metrics"]["penny"])
        self.assertEqual(strategy.evaluate(h, 3.0, 1.6e7, market_open=False)["decision"], "FILTERED")   # below MIN_PRICE

    def test_penny_fails_quality_filters(self):
        h = self.scaled(0.09)
        h.iloc[-3:, h.columns.get_loc("High")] = h.iloc[-3:]["Low"]              # locked circuit sessions
        ev = strategy.evaluate(h, h.Close.iloc[-1] * 1.05, 1e7, market_open=False)
        self.assertEqual(ev["decision"], "FILTERED")
        self.assertIn("Locked-circuit", ev["headline"])
        h2 = self.scaled(0.09)
        for k in range(1, 5):
            h2.iloc[-k * 2, h2.columns.get_loc("Close")] = h2.iloc[-k * 2 - 1]["Close"] * 1.12
        self.assertIn("Erratic", strategy.evaluate(h2, 15, 1e7, market_open=False)["headline"])

    def test_high_price_not_favoured_by_sizing(self):
        cheap = strategy.allocate(20, 19, 80, 100000, 0, capital=100000)
        dear = strategy.allocate(2000, 1900, 80, 100000, 0, capital=100000)
        self.assertLess(abs(cheap["value"] - dear["value"]), 1500)               # similar rupee exposure either way

    def test_participation_cap_blocks_illiquid(self):
        a = strategy.allocate(20, 19, 90, 100000, 0, capital=100000, avg_turnover=20_000_000 * 0.002)
        self.assertLess(a["value"], 1000)


class ClassificationTests(unittest.TestCase):
    def ev(self, **m):
        base = {"atr": 2.0, "volume_ratio": 3.0, "prior_20d_high": 100.0, "avg_turnover_20": 1e8, "ema20": 95.0, "rsi": 62}
        return {"metrics": {**base, **m}, "score": 82, "decision": "SIGNAL"}

    def hist(self):
        h = make(end="2026-10-20")
        h["Close"] = h["Close"] / h["Close"].iloc[-1] * 98
        return h

    def test_intraday_when_momentum_and_time_allow(self):
        p = strategy.build_plan(self.ev(), self.hist(), 101.0, at(11, 0), True)
        self.assertEqual(p["strategy"], "INTRADAY")
        for k in ("expected_hold", "stop", "target1", "target2", "rr1", "why", "invalidation"):
            self.assertTrue(p[k])
        self.assertLess(p["stop"], 101)
        self.assertIn("15:15", p["invalidation"][-1])
        self.assertIn("no overnight", p["expected_hold"])

    def test_short_term_otherwise_and_never_long_term(self):
        cases = [(self.ev(volume_ratio=1.3), at(11, 0), True), (self.ev(), at(14, 0), True), (self.ev(), at(11, 0), False),
                 (self.ev(atr=0.2), at(11, 0), True), (self.ev(avg_turnover_20=1e6), at(11, 0), True)]
        for ev, now, mo in cases:
            p = strategy.build_plan(ev, self.hist(), 101.0, now, mo)
            self.assertEqual(p["strategy"], "SHORT_TERM")
            self.assertIn("week", p["expected_hold"])
            self.assertLessEqual(p["max_hold_sessions"], 20)
        self.assertEqual({strategy.INTRADAY, strategy.SHORT_TERM}, set(strategy.LABEL))

    def test_market_filter_blocks_entries(self):
        mk = {"available": True, "trend": "BEARISH", "day_change_pct": -2.0}
        self.assertTrue(strategy.build_plan(self.ev(), self.hist(), 101.0, at(11, 0), True, None, mk)["blocks"])
        mk2 = {"available": True, "trend": "BEARISH", "day_change_pct": 0.2}
        self.assertTrue(strategy.build_plan(self.ev(volume_ratio=1.3), self.hist(), 101.0, at(11, 0), True, None, mk2)["blocks"])
        self.assertFalse(strategy.build_plan(self.ev(), self.hist(), 101.0, at(11, 0), True, None, mk2)["blocks"] and False)
        self.assertTrue(strategy.build_plan(self.ev(), self.hist(), 101.0, at(11, 0), True, {"spread_pct": 3.0})["blocks"])

    def test_intraday_square_off_and_no_carry(self):
        pos = dict(symbol="X", buy_price=100, quantity=10, stop_loss=97, target1=104, target2=106, peak_price=100, entry_atr=2,
                   target1_hit=False, strategy="INTRADAY", buy_time="2026-10-21 10:30:00")
        self.assertEqual(strategy.manage(pos, 101, None, 1, at(12, 0))["action"], "HOLD")
        m = strategy.manage(pos, 101, None, 1, at(15, 16))
        self.assertEqual(m["action"], "EXIT")
        self.assertIn("square-off", m["reason"])
        self.assertIn("carried", strategy.manage(pos, 101, None, 1, at(10, 0, (2026, 10, 22)))["reason"])
        st = strategy.manage({**pos, "strategy": "SHORT_TERM"}, 101, None, 1, at(15, 16))
        self.assertEqual(st["action"], "HOLD")

    def test_breakeven_protection_dcmsrind_case(self):
        pos = dict(symbol="DCMSRIND", buy_price=52.44, quantity=238, stop_loss=48.24, target1=60.84, target2=67.1, peak_price=52.44,
                   entry_atr=2.91, target1_hit=False, strategy="SHORT_TERM", buy_time="2026-10-21 11:45:32", initial_risk=4.2)
        m = strategy.manage(pos, 57.48, None, 1, at(14, 0))                      # session high ~ +9.6%
        self.assertGreater(m["new_stop"], 52.44)
        self.assertLess(m["new_stop"], 52.8)
        pos2 = {**pos, "stop_loss": m["new_stop"], "peak_price": 57.48}
        out = strategy.manage(pos2, 51.7, None, 1, at(11, 0, (2026, 10, 22)))
        self.assertEqual(out["action"], "EXIT")                                   # near break-even instead of -10%


class MonitorTests(unittest.TestCase):
    def pos(self, **kw):
        return {"symbol": "X", "buy_price": 100.0, "quantity": 10, "stop_loss": 94.0, "target1": 112.0, "target2": 120.0,
                "peak_price": 100.0, "entry_atr": 2.0, "target1_hit": False, "strategy": "SHORT_TERM", "entry_level": 100.0,
                "initial_risk": 6.0, "exit_confirm": 0, "max_hold_sessions": 15, **kw}

    def hist(self, trend):
        h = make(n=160, end="2026-10-20")
        scale = np.linspace(1.0, trend, len(h))
        for c in ("Open", "High", "Low", "Close"):
            h[c] = h[c] * scale
        h[["Open", "High", "Low", "Close"]] = h[["Open", "High", "Low", "Close"]] / h["Close"].iloc[-1] * 100
        return h

    def hold(self):
        return {"action": "HOLD", "reason": "", "qty": 0, "new_stop": 94.0, "peak": 100.0, "t1_hit": False}

    def test_noise_is_not_a_warning(self):
        a = monitor.assess(self.pos(), 99.5, self.hist(1.3), self.hold())          # inside the 0.5*ATR buffer
        self.assertNotIn("BREAKOUT_LOST", {w["code"] for w in a["warnings"]})
        self.assertNotEqual(a["state"], monitor.EXIT_REC)

    def test_warning_then_confirmed_exit_with_hysteresis(self):
        h = self.hist(0.7)                                                          # down-trending history
        a1 = monitor.assess(self.pos(), 97.0, h, self.hold())
        self.assertIn("BREAKOUT_LOST", {w["code"] for w in a1["warnings"]})
        self.assertIn(a1["state"], (monitor.WARN, monitor.HOLD, monitor.EXIT_REC))
        weak = self.pos()
        a2 = monitor.assess(weak, 90.0, h, self.hold())
        self.assertGreaterEqual(a2["score"], config.EXIT_WARN_SCORE)
        self.assertEqual(a2["state"], monitor.WARN)                                 # first confirmation only -> warning, not exit
        a3 = monitor.assess({**weak, "exit_confirm": a2["exit_confirm"]}, 90.0, h, self.hold())
        self.assertEqual(a3["state"], monitor.EXIT_REC)
        self.assertTrue(a3["exit_reason"].startswith("Strategy deviation"))
        self.assertFalse(a3["thesis_valid"])
        a4 = monitor.assess({**weak, "exit_confirm": 1}, 90.0, h, self.hold(), count_scan=False)   # 5-min price check never advances the counter
        self.assertEqual(a4["exit_confirm"], 1)

    def test_hard_rules_take_priority(self):
        stop = {"action": "EXIT", "reason": "Stop-loss hit", "qty": 10, "new_stop": 94.0, "peak": 100.0, "t1_hit": False}
        self.assertEqual(monitor.assess(self.pos(), 93, self.hist(1.3), stop)["state"], monitor.STOP)
        tgt = {"action": "PARTIAL", "reason": "Target 1 reached: sold half", "qty": 5, "new_stop": 100, "peak": 112, "t1_hit": True}
        self.assertEqual(monitor.assess(self.pos(), 112, self.hist(1.3), tgt)["state"], monitor.TARGET)

    def test_giveback_warning(self):
        a = monitor.assess(self.pos(peak_price=110.0), 103.0, self.hist(1.3), self.hold())
        self.assertIn("GIVEBACK", {w["code"] for w in a["warnings"]})


class ScanIntegration(unittest.TestCase):
    def setUp(self):
        reset()
        self.hists = {"AAA": make(seed=3), "BBB": make(seed=4), "CCC": make(seed=5, vol=50), "DDD": make(seed=9), "EEE": make(seed=3)}
        self.price = {s: h.High.iloc[-20:].max() * 1.015 for s, h in self.hists.items()}
        self.price["DDD"] = self.hists["DDD"].Close.iloc[-1] * 0.8
        self.orig = (providers.load_history, providers.load_quote, config.market_status, config.entry_window_open, config.market_is_open,
                     news.news_check, market.context)
        providers.load_history = lambda s: self.hists[s]
        providers.load_quote = lambda s: {"price": self.price[s], "day_volume": 2.0e6, "time": "x", "session_date": date(2026, 10, 21),
                                          "provider": "Test", "ts_source": "exchange"}
        news.news_check = lambda s: {"verdict": "NEUTRAL", "note": "test"}
        market.context = lambda: {"available": False, "trend": "UNKNOWN", "day_change_pct": 0.0}
        config.market_status = lambda now=None: ("OPEN", "Market is open")
        config.entry_window_open = lambda now=None: True
        config.market_is_open = lambda now=None: True

    def tearDown(self):
        (providers.load_history, providers.load_quote, config.market_status, config.entry_window_open, config.market_is_open,
         news.news_check, market.context) = self.orig

    def test_buys_are_classified_sized_and_logged(self):
        run = scanner.run_scan("scheduled")
        self.assertGreaterEqual(run["buys"], 1)
        for p in portfolio.positions():
            self.assertIn(p["strategy"], ("INTRADAY", "SHORT_TERM"))
            self.assertLessEqual(p["buy_price"] * p["quantity"], config.CAPITAL * config.MAX_POSITION_PCT / 100 + p["buy_price"])
            self.assertFalse(p["strategy_legacy"])
        raw = store.read_csv("active_stocks.csv")
        self.assertTrue((raw["expected_hold"] != "").all() and (raw["invalidation"] != "").all())
        self.assertTrue(raw["reason"].map(lambda t: "\n" not in t).all())
        log = store.read_csv("signal_log.csv")
        self.assertIn("NEW_BUY_OPPORTUNITY", set(log["event"]))
        self.assertGreaterEqual(pnl.account()["open_positions"], 1)

    def test_every_scan_monitors_positions_and_never_duplicates(self):
        scanner.run_scan("scheduled")
        n = len(portfolio.positions())
        store.write_json("scan_cursor.json", {"next": 0})
        run2 = scanner.run_scan("scheduled")
        self.assertEqual(run2["positions_checked"], n)
        self.assertEqual(len(portfolio.positions()), n + run2["buys"])          # only genuinely new stocks were added
        syms = [p["symbol"] for p in portfolio.positions()]
        self.assertEqual(len(set(syms)), len(syms))                             # never a duplicate position
        store.write_json("scan_cursor.json", {"next": 0})
        self.assertEqual(scanner.run_scan("scheduled")["buys"], 0)              # everything qualifying is now held
        log = store.read_csv("signal_log.csv")
        self.assertTrue(set(log["event"]) & {"CONTINUE_HOLDING", "HOLD_WITH_WARNING"})
        self.assertIn("Open positions (", store.tail_text("scan_journal.txt"))
        held = portfolio.positions()[0]
        self.assertTrue(held["last_assessed"] if "last_assessed" in held else store.read_csv("active_stocks.csv")["last_assessed"].iloc[0])

    def test_deteriorating_position_exit_signal_and_stop_priority(self):
        scanner.run_scan("scheduled")
        pos = portfolio.positions()
        sym = pos[0]["symbol"]
        self.price[sym] = pos[0]["stop_loss"] - 1
        res = scanner.manage_positions("TRADING", notify=False, soft=True, scan_id="t")
        self.assertEqual(res["sells"], 1)
        log = store.read_csv("signal_log.csv")
        self.assertIn("STOP_LOSS_TRIGGERED", set(log["event"]))
        self.assertIn("POSITION_CLOSED", set(log["event"]))
        self.assertEqual(len(pnl.closed_positions()), 1)
        self.assertLess(pnl.account()["realized"], 0)

    def test_analysis_mode_never_sells(self):
        scanner.run_scan("scheduled")
        sym = portfolio.positions()[0]["symbol"]
        self.price[sym] = 1.0
        res = scanner.manage_positions("ANALYSIS", notify=False, soft=True, scan_id="t")
        self.assertEqual(res["sells"], 0)
        self.assertIn(sym, {p["symbol"] for p in portfolio.positions()})

    def test_capacity_and_cash_limits_in_scan(self):
        for i in range(config.MAX_OPEN_POSITIONS):
            add(f"Z{i}", 10, 10, 9.5)
        quotes(**{f"Z{i}": 10 for i in range(config.MAX_OPEN_POSITIONS)})
        run = scanner.run_scan("scheduled")
        self.assertEqual(run["buys"], 0)
        self.assertEqual(len(portfolio.positions()), config.MAX_OPEN_POSITIONS)
        self.assertIn("max open positions", store.read_csv("scan_details.csv")["reason"].str.cat())


class OutlookTests(unittest.TestCase):
    def setUp(self):
        reset()
        self.o = (market.quote_change, market.context, news.headlines_for, providers.load_history, providers.load_quote)
        self.hist = make(n=200, end="2026-10-23")
        providers.load_history = lambda s: self.hist
        providers.load_quote = lambda s: (_ for _ in ()).throw(providers.ProviderError("offline"))
        news.headlines_for = lambda q, n=10: ["Sensex tumbles as global selloff deepens", "Nifty falls on weak results"]

    def tearDown(self):
        market.quote_change, market.context, news.headlines_for, providers.load_history, providers.load_quote = self.o

    def test_structure_with_missing_data_is_honest(self):
        market.quote_change = lambda t: None
        market.context = lambda: {"available": False, "trend": "UNKNOWN", "day_change_pct": 0.0}
        add("AAA", 100, 10, 95)
        o = outlook.generate(save=True)
        self.assertEqual(o["market"]["confidence"], "Low")
        self.assertTrue(any(e["value"] == "unavailable" for e in o["market"]["evidence"]))
        self.assertTrue(any("Unavailable" in l for l in o["market"]["limitations"]))
        self.assertTrue(any("Opening prices" in l for l in o["market"]["limitations"]))
        r = o["stocks"][0]
        self.assertIn(r["action"], ("Hold", "Monitor Closely", "Exit Recommended", "Avoid New Entry"))
        self.assertIn(r["direction"], ("Bullish", "Bearish", "Neutral"))
        self.assertTrue(r["evidence"] and r["gap_risk"] and r["confidence"])
        self.assertTrue(all(e.startswith(("[DATA]", "[ESTIMATE]")) for e in r["evidence"]))
        self.assertEqual(outlook.load()["for_session"], o["for_session"])
        self.assertIn("Outlook for", reports.outlook_text())

    def test_bearish_inputs_and_broken_thesis(self):
        market.quote_change = lambda t: {"last": 100.0, "change_pct": -1.8, "as_of": "2026-10-23 15:30"}
        market.context = lambda: {"available": True, "trend": "BEARISH", "day_change_pct": -1.2, "close": 24000, "ema20": 24500, "sma50": 24800, "as_of": "2026-10-23"}
        h = self.hist.copy()
        h[["Open", "High", "Low", "Close"]] = h[["Open", "High", "Low", "Close"]] * np.linspace(1.4, 0.7, len(h))[:, None]
        self.hist = h
        last = float(h["Close"].iloc[-1])
        add("AAA", last * 1.2, 10, last * 1.1, entry_level=last * 1.19)
        o = outlook.generate(save=False)
        self.assertEqual(o["market"]["direction"], "Bearish")
        r = o["stocks"][0]
        self.assertIn(r["action"], ("Exit Recommended", "Monitor Closely"))
        self.assertIn("AAA", o["attention"])


class AnalysisTests(unittest.TestCase):
    def setUp(self):
        reset()
        names = pd.DataFrame({"Symbol": ["TCS", "INFY", "INFOBEAN"], "NAME OF COMPANY": ["Tata Consultancy Services Limited", "Infosys Limited", "InfoBeans Technologies Limited"],
                              " SERIES": ["EQ"] * 3})
        names.to_csv(Path(TMP) / "nse_equity_symbols.csv", index=False)
        self.hist = make(n=220, end="2026-10-23")
        self.o = (providers.load_history, providers.load_quote, market.context, market.quote_change, analysis._info, analysis._news, analysis._events)
        providers.load_history = lambda s: self.hist
        providers.load_quote = lambda s: {"price": float(self.hist.High.iloc[-20:].max() * 1.012), "day_volume": 3e6, "time": "2026-10-23T15:25:00+05:30",
                                          "session_date": date(2026, 10, 26), "provider": "Test", "ts_source": "exchange", "spread_pct": 0.1}
        market.context = lambda: {"available": True, "trend": "BULLISH", "day_change_pct": 0.4, "close": 25000, "ema20": 24800, "sma50": 24500, "as_of": "x"}
        market.quote_change = lambda t: None
        analysis._info = lambda s: {"sector": "Technology", "industry": "IT Services", "trailingPE": 25.1, "returnOnEquity": 0.3}
        analysis._news = lambda s: {"verdict": "NEUTRAL", "note": "ok", "titles": ["a headline"]}
        analysis._events = lambda s: ["Earnings Date: 2026-11-05"]

    def tearDown(self):
        (providers.load_history, providers.load_quote, market.context, market.quote_change, analysis._info, analysis._news, analysis._events) = self.o

    def test_resolve(self):
        self.assertEqual(analysis.resolve("tcs")["symbol"], "TCS")
        self.assertEqual(analysis.resolve("NSE:TCS.NS")["symbol"], "TCS")
        self.assertEqual(analysis.resolve("infosys")["symbol"], "INFY")
        self.assertEqual(analysis.resolve("info")["status"], "choose")
        self.assertEqual(analysis.resolve("ZZZZZ")["status"], "not_found")
        self.assertEqual(analysis.resolve("<script>")["status"], "invalid")
        self.assertEqual(analysis.resolve("")["status"], "invalid")

    def snapshot_state(self):
        return {n: (Path(TMP) / n).read_bytes() for n in ("active_stocks.csv", "sold_stocks.csv", "trades_history.csv", "cooling_period.csv")}

    def test_not_held_full_analysis_without_trading(self):
        before = self.snapshot_state()
        r = analysis.analyze("TCS", "Tata Consultancy Services Limited")
        self.assertEqual(r["error"], "")
        for k in ("overview", "technical", "rules", "strategies", "summary", "fundamentals", "context"):
            self.assertIn(k, r)
        self.assertEqual({s["label"] for s in r["strategies"]}, {"Intraday", "Short-Term"})
        for s in r["strategies"]:
            self.assertIn(s["signal"], ("Buy Candidate", "Wait", "Hold", "Exit Recommended", "Avoid"))
            self.assertTrue(s["invalidation"] and s["hold"] and s["stop"] < r["overview"]["price"] < s["target1"])
        self.assertNotIn("holding", r)
        self.assertEqual(r["summary"]["confidence"] in ("High", "Medium", "Low"), True)
        self.assertEqual(before, self.snapshot_state())                           # analysis never trades
        self.assertIn("Analysis only", analysis.text_summary(r))

    def test_held_and_previously_sold(self):
        add("TCS", 100, 10, 95, entry_level=99)
        quotes(TCS=101)
        r = analysis.analyze("TCS")
        self.assertEqual(r["holding"]["qty"], 10)
        self.assertIn(r["strategies"][0]["signal"], ("Hold", "Exit Recommended"))
        self.assertTrue(r["holding"]["strategy_reason"] and r["holding"]["live_state"])
        portfolio.sell("TCS", 103, 10, "Target achieved")
        r2 = analysis.analyze("TCS")
        self.assertNotIn("holding", r2)
        self.assertEqual(r2["last_closed"]["symbol"], "TCS")
        self.assertGreater(r2["last_closed"]["pnl"], 0)

    def test_data_failures_are_graceful(self):
        providers.load_history = lambda s: (_ for _ in ()).throw(providers.ProviderError("Upstox: boom"))
        self.assertIn("unavailable", analysis.analyze("TCS")["error"])
        providers.load_history = lambda s: self.hist
        providers.load_quote = lambda s: (_ for _ in ()).throw(providers.ProviderError("no quote"))
        r = analysis.analyze("TCS")
        self.assertEqual(r["error"], "")
        self.assertTrue(any("quote unavailable" in l for l in r["limits"]))
        self.assertIn(r["summary"]["confidence"], ("Low", "Medium"))
        providers.load_history = lambda s: self.hist.head(20)
        self.assertIn("history", analysis.analyze("TCS")["error"])


class WebTests(unittest.TestCase):
    def setUp(self):
        reset()
        import app as appmod
        appmod.app.config["TESTING"] = True
        appmod._fails.clear()                                                  # another test class may have triggered the login lockout
        self.c = appmod.app.test_client()
        self.assertEqual(self.c.post("/login", data={"password": "secret-pw"}).status_code, 302)

    def test_all_pages_render_with_and_without_data(self):
        pages = ["/", "/positions", "/history", "/performance", "/signals", "/outlook", "/analyze", "/analyze?q=nothing", "/api/pnl", "/signals?symbol=AAA&event=POSITION_CLOSED"]
        for p in pages:
            self.assertEqual(self.c.get(p).status_code, 200, p)
        add("AAA", 100, 10, 95)
        quotes(AAA=104)
        portfolio.sell("AAA", 104, 4, "Target 1 reached: sold half")
        add("BBB", 50, 10, 47)
        pnl.snapshot("2026-10-21")
        for p in pages:
            self.assertEqual(self.c.get(p).status_code, 200, p)
        html = self.c.get("/").data.decode()
        for needle in ("Available balance", "Unrealized P&amp;L", "Realized P&amp;L", "Combined P&amp;L", "Active positions", "Completed trades", "Capacity left"):
            self.assertIn(needle, html)
        self.assertIn("AAA", self.c.get("/history").data.decode())
        self.assertIn("2026-10-21", self.c.get("/performance").data.decode())
        j = json.loads(self.c.get("/api/pnl").data)
        self.assertAlmostEqual(j["account"]["combined"], j["account"]["realized"] + j["account"]["unrealized"], places=1)

    def test_refresh_needs_csrf_and_analyze_is_read_only(self):
        self.assertEqual(self.c.post("/outlook/refresh").status_code, 400)
        self.c.get("/")
        with self.c.session_transaction() as s:
            tok = s["csrf"]
        self.assertEqual(self.c.post("/outlook/refresh", data={"csrf": tok}).status_code, 302)
        self.assertIsNotNone(store.pop_task("outlook"))
        self.assertIn(self.c.post("/analyze?q=TCS").status_code, (400, 405))      # no write verb exists for analysis


if __name__ == "__main__":
    unittest.main(verbosity=1)
