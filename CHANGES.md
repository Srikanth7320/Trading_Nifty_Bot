# Round 3 changes (31 Oct)

## Requested
1. **Loss review** - see the analysis in the chat reply; the Closed page now also shows "Result by strategy" and "Result by exit reason" tables and a per-trade review (no follow-through, square-off flat, stop slippage).
2. **Positions page** - the two portfolio-summary card rows (`row g-2 mb-2` / `row g-2 mb-3`) are removed from this page only (Dashboard and Performance keep them).
3. **Signals per scan** - web pages show the best `SHOW_SIGNALS_PER_SCAN` (2) signals per scan (journal view, scan detail, Signals page; buys are always shown). Telegram "signal not traded" alerts are limited the same way. `scan_details.csv`, `signal_log.csv` and `scan_journal.txt` keep everything.
4. **Sortable tables** - every table with a header row sorts on click (text A-Z/Z-A, numbers, dates; empty cells last). Logic is in `templates/base.html`; `tests/sort_check.js` runs it under node.
5. **Sell selected** - checkbox per position + select-all + "Sell selected (N)" with a confirm dialog; `POST /positions/sell` (CSRF-protected) -> `scanner.manual_exit`: full sale at live quote (fallback: last cached price, stated in the message), recorded as "Manual exit (dashboard)" in sold_stocks / trades / signal_log, Telegram note, snapshot refreshed.

## Loss-avoidance rules (all configurable, see .env.example)
- **Failed-breakout overnight exit** (`monitor.py`): Short-Term position, >= 8 scans below the breakout level, price still below it and <= -0.6R at the >= 15:00 scan -> exit as "Strategy deviation".
- **Results-day veto** (`news.results_risk`): headlines saying results / board meeting today or tomorrow block a new entry.
- **Sector cap** (`sectors.py`): at most 30% of capital in one sector (Yahoo sector, cached in `data/sector_cache.json`; unknown sector never blocks).

## Files
New: `sectors.py`, `tests/test_round3.py`, `tests/sort_check.js`. Modified: `config.py`, `store.py` (columns `lost_scans`, `sector`, signal_log `score`), `portfolio.py`, `monitor.py`, `scanner.py`, `news.py`, `reports.py`, `pnl.py`, `app.py`, `telegram_bot.py` (unchanged API), templates (`base`, `_macros`, `positions`, `history`, `signals`, `scan_detail`, `help`), `.env.example`, `README.md`.

## Tests: 67 pass (11 original + 36 round 2 + 20 new)
