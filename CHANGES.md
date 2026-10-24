# NiftyBot enhancement round - what changed

## New modules
| File | Purpose |
|---|---|
| `pnl.py` | Single source of truth for capital, realized/unrealized/combined P&L, closed-trade history, daily snapshots, trade post-mortems |
| `monitor.py` | Per-scan thesis check of open positions -> Continue / Warning / Exit recommended / Stop / Target (with hysteresis) |
| `market.py` | Nifty trend/day move (gates new entries) + global & sector readings (outlook). Fails open, never guesses |
| `patterns.py`, `technicals.py` | Candlestick patterns, support/resistance, shared technical snapshot |
| `outlook.py` | Next-session market + per-stock outlook (DATA vs ESTIMATE, limitations, confidence) -> `data/outlook.json` |
| `analysis.py` | Read-only stock search/analysis behind the new `/analyze` page |
| `reports_util.py` | tiny shared helper |

## Modified
`config.py` (new settings, new defaults), `store.py` (new columns + non-destructive migration, task requests), `strategy.py` (quality filters, Intraday/Short-Term classification, allocation, strategy-aware exits), `portfolio.py` (strategy/thesis fields, richer sold ledger, signal log), `scanner.py` (classification, allocation, position monitoring each scan), `worker.py` (outlook jobs, EOD P&L snapshot), `providers.py` (bid/ask spread, circuit info), `news.py` (market headlines, keyword sentiment), `reports.py`, `telegram_bot.py` (/pnl /closed /outlook /analyze, richer alerts), `app.py` + templates (dashboard, positions, closed, performance, signals, outlook, analyze), `.env.example`, `tests/`.

## Logic
1. **Allocation** (`strategy.allocate`): qty = min( free cash / empty slots x strength(0.75-1.25) , MAX_POSITION_PCT of capital, cash minus reserve for the other empty slots, 1R risk budget at the stop, 1% of average daily turnover ). Extended entries (>3 ATR above EMA20 or >8% day gain) get x0.7. 15 slots max; fewer if signals are scarce; never more than available cash.
2. **P&L**: realized = sold ledger (after both-side costs); unrealized = open qty x (last - buy) less estimated costs; combined = sum; cash = capital + realized - invested. Sold stocks stay in `sold_stocks.csv`/Closed page forever. `daily_pnl.csv` keeps one row per trading day (day P&L = change in total, so nothing is double counted).
3. **Universe**: MIN_STOCK_PRICE default 50 -> 5. Same score/gates for every price. Added for all prices: locked-circuit and erratic-jump filters, bid-ask spread cap, circuit-limit check, order <= 1% of average turnover.
4. **Classification** (`strategy.build_plan`), decided at signal time and stored: INTRADAY needs volume >= 2x, ATR 1.5-6%, day move +1..+6%, fresh breakout, turnover >= Rs5 cr, before 13:30, market open -> tight stop (1 ATR, max 3%), targets 1.5R/2.5R, square-off 15:15. Otherwise SHORT_TERM (original rules, max hold 15 sessions, configurable 5-20). Each signal records strategy, expected hold, entry/target/stop, R:R, why, invalidation conditions.
5. **Monitoring** (every 15-min scan): hard rules first (stop, targets, square-off, trend break, time exit), then warnings (breakout level lost beyond ATR buffer, below EMA20, RSI/MACD weakness, support break, bearish candle, heavy selling, profit give-back, market weak). 2 points = Hold with warning; 4+ points on 2 consecutive scans = Exit recommended and (default) paper exit "Strategy deviation". The 5-minute price checks only run hard rules and never advance the confirmation counter. All states -> `signal_log.csv` and the Signals page. Duplicate buys are refused at scan level and inside `add_position`.
6. **Outlook**: 16:00 and 08:30 (and on demand). Market direction from Nifty trend, US/Asia overnight, VIX, crude, USD/INR, headline sentiment; per position/candidate: trend, candles, levels, volume, news, historical gap range, thesis state -> Bullish/Bearish/Neutral, gap risk, Hold / Monitor Closely / Exit Recommended / Avoid New Entry, confidence, evidence.
7. **/analyze**: symbol or company-name search, overview, technicals, bot rules, fundamentals/news (time-boxed), separate Intraday and Short-Term assessments, holding/closed-trade integration. GET-only; cannot trade.

## Behaviour changes you should know about (smallest safe changes)
- Defaults changed: max positions 8->15, per-stock cap 25%->10%, buys/scan 2->3, trades/day 4->10, min price 50->5. **Your `.env` overrides defaults - update it** (see `.env.example`).
- New: Short-Term stop moves to break-even after +1R (`BREAKEVEN_AT_R`, 0 disables). This is the fix for the DCMSRIND pattern (spike then reversal through the stop). It is untested on history - paper-trade it before trusting it.
- New: index-day filter blocks new entries when Nifty is down >1.5% (and new Short-Term entries in a bearish index trend). Disable with `MARKET_FILTER_ENABLED=false`.
- Existing positions are not touched: those larger than the new cap (e.g. TCS, IGIL, CYIENT, SSWL) are flagged "above per-stock cap" and will only exit by their normal rules. Legacy positions are labelled Short-Term (legacy) because they were entered under the original swing rules.
- Exit fills still happen at the observed price at each check, so a fast move can fill below the stop (DCMSRIND exited 2.1% below its stop).

## Limits
Not verified against live Upstox/Yahoo (no network where this was built): confirm on the VM that `^NSEI`, sector tickers (`^CNXIT` ...) and depth/circuit fields return data - missing inputs show as "unavailable" and lower confidence. No GIFT Nifty / pre-market feed or economic calendar is configured; events come from headline keywords only.

## Tests (`python -m unittest tests.test_bot tests.test_enhancements`) - 47 pass
Allocation (15 slots, reserve, insufficient cash, risk cap, 16th refused), P&L (realized/unrealized, partial+full, no double count, daily snapshots, restart persistence, old-ledger migration), penny/low-priced eligibility and quality filters, classification (Intraday/Short-Term only, square-off, no carry, market filter, spread), DCMSRIND break-even scenario, monitor (noise ignored, hysteresis, hard-rule priority, give-back), scanner integration (monitoring every scan, no duplicates, stop -> closed, analysis mode never sells, full-slot refusal), outlook (missing data honest, bearish + broken thesis), analysis (resolve, held/sold/not held, no state change, graceful failures), web pages and CSRF, plus the original 11 regression tests.
