import os, sys, tempfile, unittest, shutil
from pathlib import Path
from datetime import date

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
TMP = tempfile.mkdtemp()
os.environ.update(DATA_DIR=TMP, DASHBOARD_PASSWORD="secret-pw", TELEGRAM_BOT_TOKEN="", UPSTOX_ANALYTICS_TOKEN="",
                  SCAN_BATCH_SIZE="6", SCAN_WORKERS="2", MAX_BUYS_PER_SCAN="2")
import numpy as np, pandas as pd
import config, store, portfolio, strategy, scanner, providers, news, reports


def make(n=200, seed=3, vol=1_000_000, end="2026-10-19"):
    rng = np.random.default_rng(seed)
    idx = pd.bdate_range(end=end, periods=n)
    c = 100 * np.cumprod(1 + rng.normal(0.0012, 0.012, n))
    o = c * (1 + rng.normal(0, 0.003, n))
    h = np.maximum(o, c) * (1 + abs(rng.normal(0.005, 0.003, n)))
    l = np.minimum(o, c) * (1 - abs(rng.normal(0.005, 0.003, n)))
    v = vol * (1 + rng.normal(0, 0.1, n)).clip(0.5)
    return pd.DataFrame({"Open": o, "High": h, "Low": l, "Close": c, "Volume": v}, index=idx)


def reset():
    shutil.rmtree(TMP, ignore_errors=True)
    os.makedirs(TMP)
    store.init_files()
    pd.DataFrame({"Symbol": ["AAA", "BBB", "CCC", "DDD", "EEE", "FFF"], " SERIES": ["EQ"] * 6}).to_csv(Path(TMP) / "nse_equity_symbols.csv", index=False)


class StrategyTests(unittest.TestCase):
    def test_signal_reject_filtered(self):
        h = make()
        top = h.High.iloc[-20:].max()
        ev = strategy.evaluate(h, top * 1.015, 2e6, market_open=False)
        self.assertEqual(ev["decision"], "SIGNAL")
        self.assertTrue(ev["stop"] < top and ev["target1"] > top)
        self.assertEqual(strategy.evaluate(h, top * 0.9, 1e6, market_open=False)["decision"] in ("REJECT", "WATCH"), True)
        self.assertEqual(strategy.evaluate(make(vol=100), 100, None)["decision"], "FILTERED")
        self.assertEqual(strategy.evaluate(h.head(50), 100, None)["decision"], "FILTERED")

    def test_low_volume_blocks_signal(self):
        h = make()
        ev = strategy.evaluate(h, h.High.iloc[-20:].max() * 1.015, 0.5e6, market_open=False)
        self.assertNotEqual(ev["decision"], "SIGNAL")
        self.assertTrue(any("volume" in b for b in ev["blockers"]))

    def test_position_size(self):
        self.assertEqual(strategy.position_size(100, 95, capital=100000), 200)  # 1% risk = 1000 / 5, cap 25% = 250
        self.assertEqual(strategy.position_size(100, 99.9, capital=100000), 250)
        self.assertEqual(strategy.position_size(100, 100), 0)

    def test_manage(self):
        pos = dict(symbol="X", buy_price=100, quantity=10, stop_loss=95, target1=110, target2=120, peak_price=100,
                   entry_atr=3, target1_hit=False)
        self.assertEqual(strategy.manage(pos, 94, None, 1)["action"], "EXIT")
        m = strategy.manage(pos, 111, None, 1)
        self.assertEqual((m["action"], m["qty"], m["new_stop"]), ("PARTIAL", 5, 100))
        self.assertEqual(strategy.manage({**pos, "target1_hit": True, "stop_loss": 100}, 121, None, 1)["action"], "EXIT")
        self.assertEqual(strategy.manage(pos, 102, None, 16)["action"], "EXIT")
        self.assertEqual(strategy.manage(pos, 102, None, 3)["action"], "HOLD")


class ScanTests(unittest.TestCase):
    def setUp(self):
        reset()
        self.hists = {"AAA": make(seed=3), "BBB": make(seed=4), "CCC": make(seed=5, vol=50), "DDD": make(seed=9),
                      "EEE": make(seed=3)}
        self.price = {s: h.High.iloc[-20:].max() * 1.015 for s, h in self.hists.items()}
        self.price["DDD"] = self.hists["DDD"].Close.iloc[-1] * 0.8  # no breakout
        orig = (providers.load_history, providers.load_quote, config.market_status, config.entry_window_open,
                config.market_is_open, news.news_check)

        def lh(s):
            if s == "FFF":
                raise providers.ProviderError("Upstox: boom")
            return self.hists[s]

        def lq(s):
            return {"price": self.price[s], "day_volume": 2.0e6, "time": "x", "session_date": date(2026, 10, 21),
                    "provider": "Test", "ts_source": "exchange"}
        providers.load_history, providers.load_quote = lh, lq
        news.news_check = lambda s: {"verdict": "NEUTRAL", "note": "test"}
        self.orig = orig
        self.open()

    def open(self):
        config.market_status = lambda now=None: ("OPEN", "Market is open")
        config.entry_window_open = lambda now=None: True
        config.market_is_open = lambda now=None: True

    def tearDown(self):
        (providers.load_history, providers.load_quote, config.market_status, config.entry_window_open,
         config.market_is_open, news.news_check) = self.orig

    def test_trading_scan_logs_and_buys(self):
        run = scanner.run_scan("scheduled")
        self.assertEqual(run["mode"], "TRADING")
        self.assertEqual(run["no_data"], 1)       # FFF
        self.assertEqual(run["filtered"], 1)      # CCC illiquid
        self.assertGreaterEqual(run["buys"], 1)
        det = store.read_csv("scan_details.csv")
        self.assertEqual(len(det), 6)
        self.assertIn("BOUGHT", set(det.decision))
        self.assertTrue((det[det.symbol == "FFF"].reason.iloc[0]).startswith("Upstox"))
        pos = portfolio.positions()
        self.assertEqual({p["symbol"] for p in pos}, set(det[det.decision == "BOUGHT"].symbol))
        self.assertIn("SCAN", store.tail_text("scan_journal.txt"))
        self.assertEqual(len(store.read_csv("scan_runs.csv")), 1)
        self.assertTrue(reports.watchlist() is not None)
        # price collapse -> stop-loss exit + cooldown
        sym = pos[0]["symbol"]
        self.price[sym] = pos[0]["stop_loss"] - 1
        res = scanner.manage_positions("TRADING", notify=False)
        self.assertEqual(res["sells"], 1)
        self.assertIn(sym, portfolio.cooling_symbols())
        self.assertEqual(len(store.read_csv("sold_stocks.csv")), 1)
        self.assertLess(portfolio.realized_today(), 0)

    def test_analysis_only_when_closed(self):
        config.market_status = lambda now=None: ("HOLIDAY", "NSE holiday today (Dussehra) - market closed")
        self.assertEqual(scanner.run_scan("scheduled")["status"], "SKIPPED")
        run = scanner.run_scan("manual")
        self.assertEqual((run["mode"], run["buys"]), ("ANALYSIS", 0))
        self.assertGreaterEqual(run["signals"], 1)
        self.assertEqual(portfolio.positions(), [])
        det = store.read_csv("scan_details.csv")
        self.assertTrue(det.reason.str.contains("analysis-only").any())

    def test_pause_blocks_buys(self):
        store.set_control(False)
        run = scanner.run_scan("manual")
        self.assertEqual(run["buys"], 0)

    def test_caps(self):
        run = scanner.run_scan("scheduled")
        self.assertLessEqual(run["buys"], 2)
        store.write_json("scan_cursor.json", {"next": 0})
        run2 = scanner.run_scan("scheduled")
        self.assertEqual(run["buys"] + run2["buys"], len(portfolio.positions()))
        store.write_json("scan_cursor.json", {"next": 0})
        self.assertEqual(scanner.run_scan("scheduled")["buys"], 0)  # everything qualifying is already held


class WebTests(unittest.TestCase):
    def setUp(self):
        reset()
        import app as appmod
        appmod.app.config["TESTING"] = True
        self.c = appmod.app.test_client()

    def login(self):
        r = self.c.post("/login", data={"password": "secret-pw"})
        self.assertEqual(r.status_code, 302)

    def test_auth_and_pages(self):
        self.assertEqual(self.c.get("/").status_code, 302)
        self.assertEqual(self.c.get("/healthz").status_code, 200)
        self.assertIn(b"Wrong password", self.c.post("/login", data={"password": "bad"}).data)
        self.login()
        for p in ["/", "/positions", "/history", "/scans", "/watchlist", "/journal", "/help", "/symbol/TCS", "/api/status"]:
            self.assertEqual(self.c.get(p).status_code, 200, p)
        self.assertEqual(self.c.post("/control", data={"action": "pause"}).status_code, 400)  # no CSRF
        self.c.get("/")
        with self.c.session_transaction() as s:
            tok = s["csrf"]
        self.c.post("/control", data={"action": "pause", "csrf": tok})
        self.assertFalse(store.control()["enabled"])
        self.c.post("/control", data={"action": "scan", "csrf": tok})
        self.assertIsNotNone(store.pop_scan_request())

    def test_notices_wrong_pw_lockout(self):
        for _ in range(6):
            self.c.post("/login", data={"password": "x"})
        self.assertIn(b"Too many", self.c.post("/login", data={"password": "secret-pw"}).data)


class ConfigTests(unittest.TestCase):
    def test_holiday(self):
        self.assertEqual(config.market_status(pd.Timestamp("2026-10-20 11:00", tz=config.IST).to_pydatetime())[0], "HOLIDAY")
        self.assertEqual(config.market_status(pd.Timestamp("2026-10-21 11:00", tz=config.IST).to_pydatetime())[0], "OPEN")
        self.assertEqual(config.market_status(pd.Timestamp("2026-10-24 11:00", tz=config.IST).to_pydatetime())[0], "WEEKEND")


if __name__ == "__main__":
    unittest.main(verbosity=1)
