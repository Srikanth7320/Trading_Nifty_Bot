
## Round 3 (31 Oct): loss review, sortable tables, manual sell
Positions page: select stocks and "Sell selected". All tables sort on header click. Web shows only the best 2 signals per scan
(`SHOW_SIGNALS_PER_SCAN`). New loss-avoidance rules (failed-breakout overnight exit, results-day veto, sector cap) are listed in
`.env.example`. See `CHANGES.md`. New ledger columns are added automatically; no data is removed. Tests: `python -m unittest tests.test_bot tests.test_enhancements tests.test_round3`
