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
MAX_POSITION_PCT = _f("MAX_POSITION_PCT_OF_CAPITAL", 25)
MAX_OPEN_POSITIONS = _i("MAX_OPEN_POSITIONS", 8)
MAX_BUYS_PER_SCAN = _i("MAX_BUYS_PER_SCAN", 2)
MAX_TRADES_PER_DAY = _i("MAX_TRADES_PER_DAY", 4)
MAX_DAILY_LOSS_PCT = _f("MAX_DAILY_LOSS_PCT", 2.0)
COOLING_HOURS = _i("COOLING_PERIOD_HOURS", 48)
COST_PCT_PER_SIDE = _f("COST_PCT_PER_SIDE", 0.10)  # brokerage+slippage assumption
MAX_HOLD_SESSIONS = _i("MAX_HOLD_SESSIONS", 15)

# ---- universe & liquidity -----------------------------------------------------
MIN_PRICE = _f("MIN_STOCK_PRICE", 50)
MAX_PRICE = _f("MAX_STOCK_PRICE", 5000)
MIN_AVG_VOLUME = _f("MIN_AVG_DAILY_VOLUME", 100000)
MIN_AVG_TURNOVER = _f("MIN_AVG_DAILY_TURNOVER_INR", 20_000_000)
MIN_HISTORY = _i("MIN_HISTORY_SESSIONS", 130)

# ---- strategy -------------------------------------------------------------------
MIN_SIGNAL_SCORE = _i("MIN_SIGNAL_SCORE", 70)
WATCH_SCORE = _i("WATCH_SCORE", 55)
RSI_OVERHEATED = _f("RSI_OVERHEATED", 75)
ATR_STOP_MULT = _f("ATR_STOP_MULT", 1.5)
MAX_STOP_PCT = _f("MAX_STOP_PCT", 8.0)
TARGET1_R = _f("TARGET1_R", 2.0)
TARGET2_R = _f("TARGET2_R", 3.5)
TRAIL_ATR_MULT = _f("TRAIL_ATR_MULT", 2.0)
REQUIRE_NEWS_OK = _b("REQUIRE_NEWS_CONFIRMATION", False)  # NEGATIVE news always blocks when checked

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
