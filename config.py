"""Central configuration, market calendar and time helpers (all times IST)."""
import os
from datetime import date, datetime, time as dtime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")
IST = ZoneInfo("Asia/Kolkata")


def _s(name, default=""):
    return os.getenv(name, default).strip()


def _f(name, default):
    try:
        return float(_s(name, str(default)))
    except ValueError:
        return float(default)


def _i(name, default):
    try:
        return int(float(_s(name, str(default))))
    except ValueError:
        return int(default)


def _b(name, default):
    return _s(name, "true" if default else "false").lower() in {"1", "true", "yes", "on"}


def _t(name, default):
    h, m = _s(name, default).split(":")
    return dtime(int(h), int(m))


DATA_DIR = Path(_s("DATA_DIR", str(BASE_DIR / "data")))

# ---- secrets / integrations -------------------------------------------------
TELEGRAM_TOKEN = _s("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = _s("TELEGRAM_CHAT_ID")
UPSTOX_TOKEN = _s("UPSTOX_ANALYTICS_TOKEN") or _s("UPSTOX_ACCESS_TOKEN")
YAHOO_FALLBACK = _b("UPSTOX_YAHOO_FALLBACK_ENABLED", True)
UPSTOX_MAX_QUOTE_AGE_MIN = _i("UPSTOX_MAX_QUOTE_AGE_MINUTES", 30)
HTTP_TIMEOUT = _i("HTTP_TIMEOUT_SECONDS", 15)
NEWS_API_KEY = _s("NEWS_API_KEY")
GEMINI_API_KEY = _s("GEMINI_API_KEY")
GEMINI_MODELS = [m for m in [_s("GEMINI_MODEL", "gemini-3.6-flash")] +
                 [x.strip() for x in _s("GEMINI_FALLBACK_MODELS", "gemini-3.5-flash-lite,gemini-3.1-flash-lite").split(",")]
                 if m]

# ---- dashboard ---------------------------------------------------------------
DASHBOARD_PASSWORD = _s("DASHBOARD_PASSWORD")
FLASK_SECRET_KEY = _s("FLASK_SECRET_KEY")
COOKIE_SECURE = _b("COOKIE_SECURE", False)

# ---- capital & risk (paper trading) -----------------------------------------
CAPITAL = _f("TRADING_CAPITAL_INR", 100000)
RISK_PER_TRADE_PCT = min(3.0, max(0.1, _f("MAX_RISK_PER_TRADE_PCT", 1.0)))
MAX_POSITION_PCT = _f("MAX_POSITION_PCT_OF_CAPITAL", 10)   # hard cap for ONE position (was 25)
MAX_OPEN_POSITIONS = _i("MAX_OPEN_POSITIONS", 15)           # was 8
MAX_BUYS_PER_SCAN = _i("MAX_BUYS_PER_SCAN", 3)
MAX_TRADES_PER_DAY = _i("MAX_TRADES_PER_DAY", 10)
# capital allocation: each new buy gets (free cash / free slots) scaled by signal strength, never more than the cap above
SLOT_RESERVE_FRACTION = _f("SLOT_RESERVE_FRACTION", 0.5)    # keep this share of an average slot free for every still-empty slot
ALLOC_STRENGTH_MIN = _f("ALLOC_STRENGTH_MIN", 0.75)         # multiplier on the average slot for a barely-qualifying signal
ALLOC_STRENGTH_MAX = _f("ALLOC_STRENGTH_MAX", 1.25)         # ... and for a top-scoring signal
EXTENDED_ENTRY_ATR = _f("EXTENDED_ENTRY_ATR", 3.0)          # price more than this many ATR above EMA20 = "extended" (size x0.7)
MAX_DAILY_LOSS_PCT = _f("MAX_DAILY_LOSS_PCT", 2.0)
COOLING_HOURS = _i("COOLING_PERIOD_HOURS", 48)
COST_PCT_PER_SIDE = _f("COST_PCT_PER_SIDE", 0.10)  # brokerage+slippage assumption
MAX_HOLD_SESSIONS = min(20, max(5, _i("MAX_HOLD_SESSIONS", 15)))  # Short-Term max holding (sessions); 5-20 = 1-4 weeks

# ---- universe & liquidity -----------------------------------------------------
MIN_PRICE = _f("MIN_STOCK_PRICE", 5)                        # was 50; lower-priced / penny stocks are eligible if they pass quality filters
PENNY_PRICE = _f("PENNY_PRICE_BELOW", 20)                    # label only: below this a stock is tagged PENNY
MAX_PRICE = _f("MAX_STOCK_PRICE", 5000)
MIN_AVG_VOLUME = _f("MIN_AVG_DAILY_VOLUME", 100000)
MIN_AVG_TURNOVER = _f("MIN_AVG_DAILY_TURNOVER_INR", 20_000_000)
MIN_HISTORY = _i("MIN_HISTORY_SESSIONS", 130)
MAX_SPREAD_PCT = _f("MAX_SPREAD_PCT", 1.0)                   # best-ask vs best-bid; checked when the quote carries market depth
MAX_PARTICIPATION_PCT = _f("MAX_PARTICIPATION_PCT", 1.0)      # order value may not exceed this % of the 20-day average daily turnover
MAX_ERRATIC_JUMPS = _i("MAX_ERRATIC_JUMPS", 3)                # >=N daily moves of +/-9.5% in the last 10 sessions = manipulation/circuit risk

# ---- strategy -------------------------------------------------------------------
MIN_SIGNAL_SCORE = _i("MIN_SIGNAL_SCORE", 70)
WATCH_SCORE = _i("WATCH_SCORE", 55)
RSI_OVERHEATED = _f("RSI_OVERHEATED", 75)
ATR_STOP_MULT = _f("ATR_STOP_MULT", 1.5)
MAX_STOP_PCT = _f("MAX_STOP_PCT", 8.0)
TARGET1_R = _f("TARGET1_R", 2.0)
TARGET2_R = _f("TARGET2_R", 3.5)
TRAIL_ATR_MULT = _f("TRAIL_ATR_MULT", 2.0)
BREAKEVEN_AT_R = _f("BREAKEVEN_AT_R", 1.0)                  # Short-Term: move stop to break-even after +1R (0 = off)
REQUIRE_NEWS_OK = _b("REQUIRE_NEWS_CONFIRMATION", False)  # NEGATIVE news always blocks when checked

# ---- strategy classification: INTRADAY vs SHORT_TERM (no long-term) --------------------
INTRADAY_ENABLED = _b("INTRADAY_ENABLED", True)
INTRADAY_ENTRY_CUTOFF = _t("INTRADAY_ENTRY_CUTOFF", "13:30")
INTRADAY_SQUARE_OFF = _t("INTRADAY_SQUARE_OFF", "15:15")
INTRADAY_ALLOW_CARRY = _b("INTRADAY_ALLOW_CARRY", False)     # explicit exception: let an intraday position survive past square-off
INTRADAY_MIN_VOL_RATIO = _f("INTRADAY_MIN_VOLUME_RATIO", 2.0)
INTRADAY_MIN_TURNOVER = _f("INTRADAY_MIN_TURNOVER_INR", 50_000_000)
INTRADAY_MIN_ATR_PCT = _f("INTRADAY_MIN_ATR_PCT", 1.5)
INTRADAY_MAX_ATR_PCT = _f("INTRADAY_MAX_ATR_PCT", 6.0)
INTRADAY_MIN_DAY_GAIN = _f("INTRADAY_MIN_DAY_GAIN_PCT", 1.0)
INTRADAY_MAX_DAY_GAIN = _f("INTRADAY_MAX_DAY_GAIN_PCT", 6.0)
INTRADAY_STOP_ATR = _f("INTRADAY_STOP_ATR_MULT", 1.0)
INTRADAY_MAX_STOP_PCT = _f("INTRADAY_MAX_STOP_PCT", 3.0)
INTRADAY_T1_R = _f("INTRADAY_TARGET1_R", 1.5)
INTRADAY_T2_R = _f("INTRADAY_TARGET2_R", 2.5)
INTRADAY_BE_R = _f("INTRADAY_BREAKEVEN_AT_R", 0.8)
INTRADAY_TRAIL_ATR = _f("INTRADAY_TRAIL_ATR_MULT", 1.0)
MARKET_FILTER_ENABLED = _b("MARKET_FILTER_ENABLED", True)    # block NEW entries on a clearly bearish index day / trend
MARKET_BEARISH_DAY_PCT = _f("MARKET_BEARISH_DAY_PCT", -1.5)

# ---- continuous position monitoring (every scan) ---------------------------------------------
WARN_SCORE = _i("MONITOR_WARN_SCORE", 2)                     # warning points that turn HOLD into HOLD_WITH_WARNING
EXIT_WARN_SCORE = _i("MONITOR_EXIT_SCORE", 4)                # points that make an exit a candidate
EXIT_CONFIRM_SCANS = max(1, _i("MONITOR_EXIT_CONFIRM_SCANS", 2))  # consecutive assessments required (hysteresis)
AUTO_EXIT_ON_RECOMMENDATION = _b("AUTO_EXIT_ON_RECOMMENDATION", True)  # paper-sell once an exit recommendation is confirmed
BREAKDOWN_TOL_ATR = _f("BREAKDOWN_TOLERANCE_ATR", 0.5)       # noise buffer below the breakout level before it counts as lost
GIVEBACK_PCT = _f("GIVEBACK_PCT", 50)                        # warn when this % of the peak open profit (>=1R) has been given back

# ---- next-day outlook --------------------------------------------------------------------------
OUTLOOK_EVENING = _t("OUTLOOK_EVENING_TIME", "16:00")
OUTLOOK_MORNING = _t("OUTLOOK_MORNING_TIME", "08:30")
GIFT_NIFTY_TICKER = _s("GIFT_NIFTY_TICKER")                  # optional Yahoo ticker for a Nifty futures proxy; blank = not used

# ---- scheduling -------------------------------------------------------------------
SCAN_INTERVAL_MIN = max(1, _i("SCAN_INTERVAL_MINUTES", 15))
POSITION_CHECK_MIN = max(1, _i("POSITION_CHECK_MINUTES", 5))
SCAN_BATCH_SIZE = max(1, _i("SCAN_BATCH_SIZE", 400))
SCAN_WORKERS = max(1, _i("SCAN_WORKERS", 6))
ENTRY_START = _t("ENTRY_WINDOW_START", "10:00")
ENTRY_END = _t("ENTRY_WINDOW_END", "14:30")
TELEGRAM_SCAN_SUMMARY = _s("TELEGRAM_SCAN_SUMMARY", "daily").lower()  # daily | every | off
MAX_DETAIL_ROWS = _i("MAX_SCAN_DETAIL_ROWS", 60000)

MARKET_OPEN = dtime(9, 15)
MARKET_CLOSE = dtime(15, 30)

# ---- NSE trading holidays (equity). Update yearly or add via NSE_HOLIDAYS_EXTRA. ----
HOLIDAYS = {
    "2026-01-15": "Municipal elections (Maharashtra)", "2026-01-26": "Republic Day",
    "2026-03-03": "Holi", "2026-03-26": "Shri Ram Navami", "2026-03-31": "Mahavir Jayanti",
    "2026-04-03": "Good Friday", "2026-04-14": "Ambedkar Jayanti", "2026-05-01": "Maharashtra Day",
    "2026-05-28": "Bakri Id", "2026-06-26": "Muharram", "2026-09-14": "Ganesh Chaturthi",
    "2026-10-02": "Gandhi Jayanti", "2026-10-20": "Dussehra", "2026-11-10": "Diwali-Balipratipada",
    "2026-11-24": "Guru Nanak Jayanti", "2026-12-25": "Christmas",
}
for _d in _s("NSE_HOLIDAYS_EXTRA").split(","):
    if _d.strip():
        HOLIDAYS[_d.strip()] = "Extra holiday (from .env)"
HOLIDAY_YEARS = {d[:4] for d in HOLIDAYS}


def now_ist():
    return datetime.now(IST)


def holiday_name(d):
    return HOLIDAYS.get(d.isoformat())


def is_trading_day(d):
    return d.weekday() < 5 and d.isoformat() not in HOLIDAYS


def market_status(now=None):
    """Return (code, human text). Codes: OPEN, PRE_OPEN, CLOSED, HOLIDAY, WEEKEND."""
    now = now or now_ist()
    d, t = now.date(), now.time()
    if d.weekday() >= 5:
        return "WEEKEND", "Weekend - market closed"
    name = holiday_name(d)
    if name:
        return "HOLIDAY", f"NSE holiday today ({name}) - market closed"
    if t < MARKET_OPEN:
        return "PRE_OPEN", "Market opens at 09:15 IST"
    if t > MARKET_CLOSE:
        return "CLOSED", "Market closed for the day (15:30 IST)"
    return "OPEN", "Market is open"


def market_is_open(now=None):
    return market_status(now)[0] == "OPEN"


def entry_window_open(now=None):
    now = now or now_ist()
    return market_is_open(now) and ENTRY_START <= now.time() <= ENTRY_END


def next_trading_day(d):
    d = d + timedelta(days=1)
    while not is_trading_day(d):
        d += timedelta(days=1)
    return d


def trading_days_between(start, end):
    """Number of trading days in [start, end], both inclusive."""
    n, d = 0, start
    while d <= end:
        n += is_trading_day(d)
        d += timedelta(days=1)
    return n


def holiday_calendar_warning():
    year = str(now_ist().year)
    if year not in HOLIDAY_YEARS:
        return f"The built-in NSE holiday list has no entries for {year}. Add dates in NSE_HOLIDAYS_EXTRA."
    return ""
