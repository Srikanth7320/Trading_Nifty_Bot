Data files (all created/maintained by the bot - safe to open in Excel, do not edit while the bot runs)

LEDGERS (paper trading)
  active_stocks.csv     open paper positions (stop, targets, peak)
  sold_stocks.csv       closed/partially closed trades with profit after costs
  trades_history.csv    every BUY and SELL
  cooling_period.csv    symbols blocked from re-entry for COOLING_PERIOD_HOURS

WHAT HAPPENED IN EACH SCAN
  scan_runs.csv         one row per scan: time, mode, how many stocks checked / filtered / rejected / watch / signals / bought
  scan_details.csv      one row per stock per scan: result (BOUGHT, SIGNAL, WATCH, REJECT, FILTERED, NO_DATA), score, price, RSI, volume ratio and the reason
  scan_journal.txt      plain-language narrative of every scan, morning check and end-of-day summary
  last_scan.json        summary of the latest scan (used by the dashboard)
  watchlist.json        stocks that scored well but are not signals yet

INPUT
  nse_equity_symbols.csv  the NSE EQ-series universe that is scanned (replace with a fresh NSE list any time)
  sop_external_checks.csv OPTIONAL. Add a row (symbol, today's as_of_date, surveillance_stage, promoter_pledge_pct, board_meeting_date,
                          equity_dilution_status) to make the bot skip a stock you know is risky. Empty = no effect.

INTERNAL
  scan_cursor.json, quotes_cache.json, bot_control.json, worker_status.json, daily_flags.json, scan_request.json,
  upstox_instruments.json (downloaded automatically)
