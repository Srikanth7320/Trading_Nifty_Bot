"""Round 3: loss-avoidance rules, manual sell, signal display limits, sortable tables."""
import shutil
import subprocess
import sys
import unittest
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from tests.test_enhancements import add, at, quotes  # noqa: E402  (sets up the shared test environment)
from tests.test_bot import TMP, make, reset  # noqa: E402

import config, market, monitor, news, pnl, portfolio, providers, reports, scanner, sectors, store, strategy  # noqa: E402


def pos(**kw):
    p = {"symbol": "X", "buy_price": 100.0, "quantity": 10, "stop_loss": 94.0, "target1": 112.0, "target2": 120.0, "peak_price": 100.0,
         "entry_atr": 2.0, "target1_hit": False, "strategy": "SHORT_TERM", "entry_level": 100.0, "initial_risk": 6.0, "exit_confirm": 0,
         "max_hold_sessions": 15, "lost_scans": 0}
    p.update(kw)
    return p


HOLD = {"action": "HOLD", "reason": "", "qty": 0, "new_stop": 94.0, "peak": 100.0, "t1_hit": False}


class FailedBreakoutTests(unittest.TestCase):
    def hist(self):
        return make(n=160, end="2026-10-20")

    def test_deep_dead_breakout_is_not_carried_overnight(self):
        a = monitor.assess(pos(lost_scans=8), 96.0, self.hist(), HOLD, count_scan=True, now=at(15, 16))   # -0.67R, below the level
        self.assertEqual(a["state"], monitor.EXIT_REC)
        self.assertTrue(a["exit_reason"].startswith("Strategy deviation: failed breakout"))
        self.assertFalse(a["thesis_valid"])

    def test_not_triggered_when_it_should_not(self):
        h = self.hist()
        self.assertNotEqual(monitor.assess(pos(lost_scans=8), 96.0, h, HOLD, now=at(11, 0))["state"], monitor.EXIT_REC)          # intraday-time scan
        self.assertNotEqual(monitor.assess(pos(lost_scans=6), 96.0, h, HOLD, now=at(15, 16))["state"], monitor.EXIT_REC)         # 6 + this scan = 7 < 8
        self.assertNotEqual(monitor.assess(pos(lost_scans=8), 98.0, h, HOLD, now=at(15, 16))["state"], monitor.EXIT_REC)         # only -0.33R
        self.assertNotEqual(monitor.assess(pos(lost_scans=8), 101.0, h, HOLD, now=at(15, 16))["state"], monitor.EXIT_REC)        # back above the level
        self.assertNotEqual(monitor.assess(pos(lost_scans=8, strategy="INTRADAY"), 96.0, h, HOLD, now=at(15, 16))["state"], monitor.EXIT_REC)
        self.assertNotEqual(monitor.assess(pos(lost_scans=8), 96.0, h, HOLD, count_scan=False, now=at(15, 16))["state"], monitor.EXIT_REC)  # 5-min check
        old = config.FAILED_BREAKOUT_EXIT
        config.FAILED_BREAKOUT_EXIT = False
        try:
            self.assertNotEqual(monitor.assess(pos(lost_scans=8), 96.0, h, HOLD, now=at(15, 16))["state"], monitor.EXIT_REC)
        finally:
            config.FAILED_BREAKOUT_EXIT = old

    def test_counter_counts_and_resets(self):
        h = self.hist()
        a = monitor.assess(pos(lost_scans=3), 90.0, h, HOLD, now=at(11, 0))
        self.assertEqual(a["lost_scans"], 4)
        self.assertEqual(monitor.assess(pos(lost_scans=4), 101.0, h, HOLD, now=at(11, 0))["lost_scans"], 0)
        self.assertEqual(monitor.assess(pos(lost_scans=4), 99.8, h, HOLD, now=at(11, 0))["lost_scans"], 4)       # inside the noise buffer: unchanged
        self.assertEqual(monitor.assess(pos(lost_scans=4), 90.0, h, HOLD, count_scan=False, now=at(11, 0))["lost_scans"], 4)

    def test_tcs_cyient_replay(self):
        """Their real 27-Oct close: TCS 2500.3 (entry 2575.1, stop 2457.95, level 2533.3), CYIENT 1180.8 (entry 1249, level 1224.4)."""
        h = self.hist()
        tcs = pos(buy_price=2575.1, stop_loss=2457.95, entry_level=2533.3, initial_risk=117.15, entry_atr=78.1, lost_scans=15)
        cy = pos(buy_price=1249.0, stop_loss=1172.82, entry_level=1224.4, initial_risk=76.18, entry_atr=50.79, lost_scans=19)
        self.assertEqual(monitor.assess(tcs, 2500.3, h, HOLD, now=at(15, 16))["state"], monitor.EXIT_REC)
        self.assertEqual(monitor.assess(cy, 1180.8, h, HOLD, now=at(15, 16))["state"], monitor.EXIT_REC)


class ResultsRiskTests(unittest.TestCase):
    def test_detection(self):
        yes = ["Cyient Q2 results today: what to expect", "Cyient to announce Q2 results on Oct 23", "Wipro board meeting tomorrow to consider dividend",
               "Q2 earnings tonight: Infosys in focus"]
        no = ["TCS shares rally after results", "Infosys share price today rises 2%", "Stocks to watch today: Tata Motors", "Titan reports quarterly profit"]
        for t in yes:
            self.assertTrue(news.results_risk([t])[0], t)
        for t in no:
            self.assertFalse(news.results_risk([t])[0], t)
        self.assertFalse(news.results_risk([])[0])


class ScanRules(unittest.TestCase):
    def setUp(self):
        reset()
        self.hists = {"AAA": make(seed=3), "BBB": make(seed=4)}
        self.price = {s: h.High.iloc[-20:].max() * 1.015 for s, h in self.hists.items()}
        self.o = (providers.load_history, providers.load_quote, config.market_status, config.entry_window_open, config.market_is_open,
                  news.news_check, market.context, sectors.sector_of)
        providers.load_history = lambda s: self.hists[s]
        providers.load_quote = lambda s: {"price": self.price[s], "day_volume": 2.0e6, "time": "x", "session_date": date(2026, 10, 21),
                                          "provider": "Test", "ts_source": "exchange"}
        news.news_check = lambda s: {"verdict": "NEUTRAL", "note": "t", "titles": []}
        market.context = lambda: {"available": False, "trend": "UNKNOWN", "day_change_pct": 0.0}
        sectors.sector_of = lambda s: ""
        config.market_status = lambda now=None: ("OPEN", "Market is open")
        config.entry_window_open = lambda now=None: True
        config.market_is_open = lambda now=None: True

    def tearDown(self):
        (providers.load_history, providers.load_quote, config.market_status, config.entry_window_open, config.market_is_open,
         news.news_check, market.context, sectors.sector_of) = self.o

    def not_traded(self):
        return " ".join(store.read_csv("signal_log.csv").query("event == 'NEW_BUY_OPPORTUNITY'")["detail"])

    def test_results_day_entry_is_vetoed(self):
        news.news_check = lambda s: {"verdict": "NEUTRAL", "note": "t", "titles": [f"{s} Q2 results today: what to expect"]}
        run = scanner.run_scan("scheduled")
        self.assertEqual(run["buys"], 0)
        self.assertGreaterEqual(run["signals"], 1)
        self.assertIn("results/board-meeting event", self.not_traded())
        config.EARNINGS_VETO = False
        try:
            store.write_json("scan_cursor.json", {"next": 0})
            self.assertGreaterEqual(scanner.run_scan("scheduled")["buys"], 1)
        finally:
            config.EARNINGS_VETO = True

    def test_sector_cap_blocks_the_next_same_sector_entry(self):
        sectors.sector_of = lambda s: "Technology"
        for i in range(3):                                                   # Rs 99k of tech already held on a Rs 100k test capital
            portfolio.add_position(f"T{i}", 100, 110, 95, 110, 120, 3, 80, "x",
                                   {"strategy": "SHORT_TERM", "sector": "Technology", "initial_risk": 5, "invalidation": ["x"], "why": "w"})
        quotes(T0=100, T1=100, T2=100)
        run = scanner.run_scan("scheduled")
        self.assertEqual(run["buys"], 0)
        self.assertIn("sector limit: Technology", self.not_traded())

    def test_unknown_sector_never_blocks(self):
        self.assertGreaterEqual(scanner.run_scan("scheduled")["buys"], 1)

    def test_signal_alerts_limited_but_files_keep_all(self):
        sent = []
        orig = scanner.tg.send
        scanner.tg.send = lambda m: sent.append(m)
        try:
            config.MAX_OPEN_POSITIONS_BAK = config.MAX_OPEN_POSITIONS
            for i in range(config.MAX_OPEN_POSITIONS):
                add(f"Z{i}", 10, 10, 9.5)
            for i in range(4):                                               # more not-traded signals than the display limit
                self.hists[f"S{i}"] = make(seed=20 + i)
                self.price[f"S{i}"] = self.hists[f"S{i}"].High.iloc[-20:].max() * 1.015
            scanner.load_universe = lambda: ["AAA", "BBB"] + [f"S{i}" for i in range(4)]
            scanner.run_scan("scheduled")
        finally:
            scanner.tg.send = orig
        alerts = [m for m in sent if "SIGNAL (not traded)" in m]
        self.assertLessEqual(len(alerts), config.SHOW_SIGNALS_PER_SCAN)
        logged = store.read_csv("signal_log.csv").query("event == 'NEW_BUY_OPPORTUNITY'")
        self.assertGreaterEqual(len(logged), len(alerts))


class ManualSellTests(unittest.TestCase):
    def setUp(self):
        reset()
        self.o = (providers.load_quote, scanner.tg.send)
        self.sent = []
        scanner.tg.send = lambda m: self.sent.append(m)
        self.live = {"AAA": 110.0, "BBB": 40.0}
        providers.load_quote = lambda s: {"price": self.live[s], "time": "2026-10-21T14:00:00+05:30", "provider": "T", "day_volume": 1, "session_date": date(2026, 10, 21)}
        add("AAA", 100, 10, 95)
        add("BBB", 50, 20, 45)
        add("CCC", 20, 5, 18)
        quotes(AAA=105, BBB=48, CCC=19)

    def tearDown(self):
        providers.load_quote, scanner.tg.send = self.o

    def test_sells_selected_only_in_full_at_latest_price(self):
        res = scanner.manual_exit(["AAA", "BBB"])
        self.assertTrue(all(r["ok"] for r in res))
        self.assertEqual({p["symbol"] for p in portfolio.positions()}, {"CCC"})
        by = {r["symbol"]: r for r in res}
        self.assertEqual((by["AAA"]["price"], by["AAA"]["qty"]), (110.0, 10))
        self.assertGreater(by["AAA"]["pnl"], 0)
        self.assertLess(by["BBB"]["pnl"], 0)
        closed = {c["symbol"]: c for c in pnl.closed_positions()}
        self.assertEqual(closed["AAA"]["exit_class"], "Manual exit")
        self.assertEqual(closed["AAA"]["exit_type"], "FULL")
        self.assertAlmostEqual(pnl.account()["realized"], by["AAA"]["pnl"] + by["BBB"]["pnl"])
        self.assertEqual(len(self.sent), 2)
        self.assertIn("POSITION_CLOSED", set(store.read_csv("signal_log.csv")["event"]))

    def test_partial_position_sells_remaining_quantity(self):
        portfolio.sell("AAA", 108, 4, "Target 1 reached: sold half")
        res = scanner.manual_exit(["AAA"])
        self.assertEqual(res[0]["qty"], 6)
        self.assertEqual(len(portfolio.positions()), 2)

    def test_unknown_duplicate_and_no_price(self):
        res = scanner.manual_exit(["AAA", "AAA", "NOPE", "  "])
        self.assertEqual([r["symbol"] for r in res], ["AAA", "NOPE"])
        self.assertTrue(res[0]["ok"])
        self.assertFalse(res[1]["ok"])
        self.assertFalse(scanner.manual_exit(["AAA"])[0]["ok"])                        # already sold - never sold twice
        providers.load_quote = lambda s: (_ for _ in ()).throw(providers.ProviderError("down"))
        r = scanner.manual_exit(["BBB"])[0]
        self.assertTrue(r["ok"])
        self.assertIn("last known price", r["note"])                                  # falls back to cache and says so
        store.write_json("quotes_cache.json", {})
        r = scanner.manual_exit(["CCC"])[0]
        self.assertFalse(r["ok"])
        self.assertEqual({p["symbol"] for p in portfolio.positions()}, {"CCC"})


class WebTests(unittest.TestCase):
    def setUp(self):
        reset()
        import app as appmod
        appmod.app.config["TESTING"] = True
        appmod._fails.clear()
        self.c = appmod.app.test_client()
        self.assertEqual(self.c.post("/login", data={"password": "secret-pw"}).status_code, 302)
        self.o = providers.load_quote
        providers.load_quote = lambda s: {"price": 120.0, "time": "2026-10-21T14:00:00+05:30", "provider": "T", "day_volume": 1, "session_date": date(2026, 10, 21)}
        self._send = scanner.tg.send
        scanner.tg.send = lambda m: None
        add("AAA", 100, 10, 95)
        add("BBB", 50, 20, 45)
        quotes(AAA=105, BBB=48)

    def tearDown(self):
        providers.load_quote = self.o
        scanner.tg.send = self._send

    def token(self):
        self.c.get("/")
        with self.c.session_transaction() as s:
            return s["csrf"]

    def test_positions_page_layout(self):
        html = self.c.get("/positions").data.decode()
        self.assertNotIn("Available balance", html)                                   # portfolio summary cards removed from this page
        self.assertNotIn("row g-2 mb-2", html)
        self.assertNotIn("row g-2 mb-3", html)
        self.assertEqual(html.count('class="pick"'), 2)
        self.assertIn('id="selall"', html)
        self.assertIn("Sell selected", html)
        self.assertIn("Available balance", self.c.get("/").data.decode())             # dashboard keeps it

    def test_sell_selected_route(self):
        self.assertEqual(self.c.post("/positions/sell", data={"symbols": ["AAA"]}).status_code, 400)       # CSRF required
        tok = self.token()
        r = self.c.post("/positions/sell", data={"csrf": tok}, follow_redirects=True)
        self.assertIn("Select at least one", r.data.decode())
        self.assertEqual(len(portfolio.positions()), 2)
        r = self.c.post("/positions/sell", data={"csrf": tok, "symbols": ["AAA", "<script>", "ZZZ"]}, follow_redirects=True)
        body = r.data.decode()
        self.assertIn("Sold AAA", body)
        self.assertIn("ZZZ: not an open position", body)
        self.assertNotIn("<script>alert", body)
        self.assertEqual([p["symbol"] for p in portfolio.positions()], ["BBB"])
        self.assertIn("Manual exit", self.c.get("/history").data.decode())

    def test_signals_and_scan_pages_show_only_best_n(self):
        rows = []
        for i, sc in enumerate((92, 88, 81, 77)):
            rows.append({"time": "2026-10-21 10:15:00", "scan_id": "S1", "symbol": f"SIG{i}", "event": "NEW_BUY_OPPORTUNITY", "strategy": "SHORT_TERM",
                         "price": 10, "pnl_pct": "", "thesis_valid": "", "detail": "not traded: max open positions", "score": sc})
        rows.append({**rows[0], "symbol": "BOUGHT3", "detail": "BOUGHT 5: x"})
        store.append_rows("signal_log.csv", rows)
        html = self.c.get("/signals").data.decode()
        self.assertIn("SIG0", html)
        self.assertIn("SIG1", html)
        self.assertNotIn("SIG2", html)
        self.assertNotIn("SIG3", html)
        self.assertIn("BOUGHT3", html)
        self.assertIn("lower-ranked", html)
        self.assertIn("SIG3", store.read_csv("signal_log.csv").to_string())           # the file keeps everything

    def test_tables_are_sortable_and_pages_render(self):
        for p in ("/", "/positions", "/history", "/performance", "/signals", "/outlook", "/scans", "/watchlist", "/analyze"):
            html = self.c.get(p).data.decode()
            self.assertIn("Click a column header to sort", html, p)


class ReportHelpers(unittest.TestCase):
    def test_limit_signals(self):
        rows = [{"decision": "BOUGHT", "symbol": "A"}, {"decision": "SIGNAL", "symbol": "B"}, {"decision": "SIGNAL", "symbol": "C"},
                {"decision": "SIGNAL", "symbol": "D"}, {"decision": "WATCH", "symbol": "E"}]
        out, hidden = reports.limit_signals(rows, 2)
        self.assertEqual([r["symbol"] for r in out], ["A", "B", "C", "E"])
        self.assertEqual(hidden, 1)

    def test_journal_view(self):
        text = "==== SCAN 1 ====\nSignals:\n  A: score 90 - NOT traded (x)\n  B: score 80 - NOT traded (x)\n  C: score 70 - NOT traded (x)\n" \
               "  D [Intraday]: score 60 - BOUGHT 5 @ 1\nClosest to a signal (watchlist):\n  W1\n  W2\n  W3\n---- END ----"
        v = reports.journal_view(text, 2)
        self.assertIn("A: score 90", v)
        self.assertIn("B: score 80", v)
        self.assertNotIn("C: score 70", v)
        self.assertIn("BOUGHT 5", v)                                                    # buys never hidden
        self.assertIn("W2", v)
        self.assertNotIn("W3", v)
        self.assertIn("more kept in data/scan_details.csv", v)
        self.assertIn("---- END ----", v)

    def test_breakdown_and_review(self):
        reset()
        add("AAA", 100, 10, 95)
        portfolio.sell("AAA", 90, 10, "Stop-loss hit")
        add("BBB", 100, 10, 95)
        portfolio.sell("BBB", 106, 10, "Target 2 reached")
        b = pnl.breakdown(pnl.closed_positions())
        self.assertEqual({r["name"] for r in b["exit"]}, {"Stop-loss triggered", "Target achieved"})
        self.assertEqual(b["exit"][0]["name"], "Stop-loss triggered")                  # worst first
        self.assertEqual(sum(r["sells"] for r in b["strategy"]), 2)


@unittest.skipUnless(shutil.which("node"), "node not installed")
class SortScript(unittest.TestCase):
    def test_sort_logic_in_node(self):
        r = subprocess.run(["node", str(Path(__file__).parent / "sort_check.js")], capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        out = {l[:16].strip(): l[16:].split() for l in r.stdout.splitlines()}
        self.assertEqual(out["Symbol asc"], ["ACUTAAS", "CYIENT", "IGIL", "TCS", "WIPRO"])
        self.assertEqual(out["Symbol desc"], out["Symbol asc"][::-1])
        self.assertEqual(out["Qty asc"], ["ACUTAAS", "TCS", "WIPRO", "CYIENT", "IGIL"])         # 7 < 85 < 129 < 131 < 1000, not text order
        self.assertEqual(out["Bought asc"], ["WIPRO", "TCS", "CYIENT", "IGIL", "ACUTAAS"])      # oldest first
        self.assertEqual(out["Bought desc"][0], "ACUTAAS")
        self.assertEqual(out["P&L% asc"][-1], "WIPRO")                                          # n/a always last
        self.assertEqual(out["P&L% desc"][-1], "WIPRO")


if __name__ == "__main__":
    unittest.main(verbosity=1)
