"""Optional advisory checks: manual external-data CSV (veto only on known bad values) and news sentiment."""
import html
import re
import xml.etree.ElementTree as ET
from urllib.parse import quote_plus

import pandas as pd
import requests

import config
import store

NEGATIVE_WORDS = ("fraud", "sebi probe", "sebi order", "raid", "default", "insolvency", "downgrade", "resigns", "resignation",
                  "penalty", "scam", "arrest", "bankruptcy", "profit warning", "plunge", "crash", "ed probe", "investigation")


def external_veto(symbol):
    """data/sop_external_checks.csv is OPTIONAL. If you add a row for today, known-bad values block the entry."""
    try:
        df = pd.read_csv(store.path("sop_external_checks.csv"), dtype=str, keep_default_na=False)
    except Exception:
        return False, "no external checks supplied (optional)"
    today = config.now_ist().date().isoformat()
    rows = df[(df.get("symbol", "").str.upper() == symbol) & (df.get("as_of_date", "") == today)]
    if rows.empty:
        return False, "no external row for today (optional)"
    r = rows.iloc[-1]
    stage = str(r.get("surveillance_stage", "")).upper().replace("STAGE ", "")
    if stage.isdigit() and int(stage) >= 2:
        return True, f"ASM/GSM stage {stage} (from external checks)"
    try:
        if float(r.get("promoter_pledge_pct", "") or 0) >= 10:
            return True, "promoter pledge >= 10% (from external checks)"
    except ValueError:
        pass
    if str(r.get("equity_dilution_status", "")).upper() in {"PENDING", "YES", "FAIL"}:
        return True, "equity dilution flagged (from external checks)"
    meeting = str(r.get("board_meeting_date", "")).strip()
    if meeting:
        try:
            md = pd.Timestamp(meeting).date()
            today_d = config.now_ist().date()
            if md >= today_d and config.trading_days_between(today_d, md) <= 4:
                return True, f"board meeting on {meeting} within 3 trading days"
        except Exception:
            pass
    return False, "external checks clear"


def _headlines(symbol):
    out = []
    if config.NEWS_API_KEY:
        try:
            r = requests.get("https://newsapi.org/v2/everything", timeout=config.HTTP_TIMEOUT,
                             params={"q": f'"{symbol}" AND (NSE OR India OR shares)', "language": "en", "sortBy": "publishedAt",
                                     "pageSize": 8, "apiKey": config.NEWS_API_KEY})
            if r.ok:
                out += [a.get("title", "") for a in r.json().get("articles", []) if a.get("title")]
        except requests.RequestException:
            pass
    try:
        r = requests.get(f"https://news.google.com/rss/search?q={quote_plus(symbol + ' share NSE')}&hl=en-IN&gl=IN&ceid=IN:en",
                         timeout=config.HTTP_TIMEOUT)
        if r.ok:
            out += [i.findtext("title", "") for i in ET.fromstring(r.content).iter("item")][:8]
    except Exception:
        pass
    seen, uniq = set(), []
    for t in out:
        k = re.sub(r"\W+", " ", t.lower()).strip()
        if k and k not in seen:
            seen.add(k)
            uniq.append(t)
    return uniq[:10]


def headlines_for(query, n=10):
    """Generic Google News RSS headlines for any query (market-level topics, events)."""
    out = []
    try:
        r = requests.get(f"https://news.google.com/rss/search?q={quote_plus(query)}&hl=en-IN&gl=IN&ceid=IN:en", timeout=config.HTTP_TIMEOUT)
        if r.ok:
            out = [i.findtext("title", "") for i in ET.fromstring(r.content).iter("item")]
    except Exception:
        pass
    seen, uniq = set(), []
    for t in out:
        k = re.sub(r"\W+", " ", t.lower()).strip()
        if k and k not in seen:
            seen.add(k)
            uniq.append(t)
    return uniq[:n]


POSITIVE_WORDS = ("surge", "rally", "record high", "beats", "upgrade", "gains", "jumps", "soars", "strong results", "order win",
                  "bullish", "buyback", "dividend", "profit rises", "outperform")


def keyword_sentiment(titles):
    """(score -1..+1, negative hits, positive hits) from headline keywords - a crude, transparent sentiment proxy."""
    neg = [t for t in titles if any(w in t.lower() for w in NEGATIVE_WORDS + ("falls", "slump", "tumble", "selloff", "sell-off", "weak", "loss"))]
    pos = [t for t in titles if any(w in t.lower() for w in POSITIVE_WORDS)]
    n = max(1, len(titles))
    return max(-1.0, min(1.0, (len(pos) - len(neg)) / n * 2)), neg, pos


def _gemini(symbol, titles):
    if not config.GEMINI_API_KEY:
        return None
    try:
        from google import genai
        client = genai.Client(api_key=config.GEMINI_API_KEY)
    except Exception:
        return None
    prompt = (f"You screen news for an NSE stock ({symbol}) before a short-term paper trade. Headlines:\n- " + "\n- ".join(titles) +
              "\nAnswer with exactly one word first: POSITIVE, NEUTRAL or NEGATIVE (NEGATIVE only for serious adverse news such "
              "as fraud, probes, defaults, big downgrades), then a short reason. Use only the headlines above.")
    for model in config.GEMINI_MODELS:
        try:
            text = (client.models.generate_content(model=model, contents=prompt).text or "").strip()
            word = text.split()[0].strip(".,:").upper() if text else ""
            if word in {"POSITIVE", "NEUTRAL", "NEGATIVE"}:
                return word, text[:200]
        except Exception:
            continue
    return None


def news_check(symbol):
    """Returns dict(verdict=POSITIVE|NEUTRAL|NEGATIVE|UNAVAILABLE, note). Advisory unless REQUIRE_NEWS_CONFIRMATION=true."""
    titles = _headlines(symbol)
    if not titles:
        return {"verdict": "UNAVAILABLE", "note": "no headlines found"}
    ai = _gemini(symbol, titles)
    if ai:
        return {"verdict": ai[0], "note": f"AI: {ai[1]}"}
    bad = [t for t in titles if any(w in t.lower() for w in NEGATIVE_WORDS)]
    if bad:
        return {"verdict": "NEGATIVE", "note": "keyword alert: " + bad[0][:140]}
    return {"verdict": "NEUTRAL", "note": f"{len(titles)} headlines, no red-flag keywords"}


def e(s):
    return html.escape(str(s))
