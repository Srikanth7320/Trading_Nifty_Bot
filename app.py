"""Dashboard (Flask). Read-only over the data files; it never trades. Login required."""
import hmac
import secrets
import time
from datetime import timedelta

from flask import Flask, abort, flash, jsonify, redirect, render_template, request, session, url_for
from werkzeug.middleware.proxy_fix import ProxyFix

import analysis
import config
import outlook
import pnl
import portfolio
import reports
import store

app = Flask(__name__)
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1)
app.config.update(SECRET_KEY=config.FLASK_SECRET_KEY or secrets.token_hex(32), SESSION_COOKIE_HTTPONLY=True,
                  SESSION_COOKIE_SAMESITE="Lax", SESSION_COOKIE_SECURE=config.COOKIE_SECURE,
                  PERMANENT_SESSION_LIFETIME=timedelta(hours=12))
store.init_files()
_fails = {}  # ip -> (count, first_time)


def csrf():
    if "csrf" not in session:
        session["csrf"] = secrets.token_urlsafe(24)
    return session["csrf"]


app.jinja_env.globals["csrf"] = csrf
app.jinja_env.filters["inr"] = lambda v: "-" if v in (None, "") else f"{float(v):,.2f}"
app.jinja_env.filters["pct"] = lambda v: "-" if v in (None, "") else f"{float(v):+.2f}%"
app.jinja_env.filters["tone"] = lambda v: "" if v in (None, "") else ("text-success" if float(v) > 0 else "text-danger" if float(v) < 0 else "")
app.jinja_env.filters["nice"] = lambda v: str(v or "").replace("_", " ").title()
app.jinja_env.filters["hhmm"] = lambda v: str(v or "")[:16].replace("T", " ")


@app.before_request
def guard():
    if request.endpoint in ("healthz", "static"):
        return
    if request.method == "POST" and request.endpoint != "login":
        if not hmac.compare_digest(request.form.get("csrf", ""), session.get("csrf", "x")):
            abort(400, "Invalid form token - refresh the page and try again.")
    if request.endpoint != "login" and not session.get("auth"):
        return redirect(url_for("login", next=request.path))


@app.after_request
def headers(resp):
    resp.headers.update({"X-Frame-Options": "DENY", "X-Content-Type-Options": "nosniff", "Referrer-Policy": "same-origin",
                         "Cache-Control": "no-store"})
    return resp


@app.route("/healthz")
def healthz():
    return jsonify(ok=True, worker_alive=reports.worker_state()["alive"])


@app.route("/login", methods=["GET", "POST"])
def login():
    ip = request.remote_addr or "?"
    cnt, first = _fails.get(ip, (0, time.time()))
    if time.time() - first > 600:
        cnt, first = 0, time.time()
    locked = cnt >= 5
    if request.method == "POST":
        if not config.DASHBOARD_PASSWORD:
            flash("DASHBOARD_PASSWORD is not set in .env, so login is disabled.", "danger")
        elif locked:
            flash("Too many failed attempts. Try again in 10 minutes.", "danger")
        elif hmac.compare_digest(request.form.get("password", ""), config.DASHBOARD_PASSWORD):
            session.clear()
            session["auth"], session.permanent = True, True
            _fails.pop(ip, None)
            nxt = request.args.get("next", "/")
            return redirect(nxt if nxt.startswith("/") and not nxt.startswith("//") else "/")
        else:
            _fails[ip] = (cnt + 1, first)
            flash("Wrong password.", "danger")
    return render_template("login.html", configured=bool(config.DASHBOARD_PASSWORD))


@app.route("/logout", methods=["POST"])
def logout():
    session.clear()
    return redirect(url_for("login"))


@app.route("/")
def dashboard():
    rows = pnl.position_view()
    return render_template("dashboard.html", market=config.market_status(), ctl=store.control(), worker=reports.worker_state(),
                           explain=reports.explain_state(), stats=reports.today_stats(), last=reports.last_scan(),
                           positions=rows, acct=pnl.account(rows), closed=pnl.closed_positions(6), watch=reports.watchlist()[:10],
                           runs=reports.runs(6), warn=config.holiday_calendar_warning(), cfg=config)


@app.route("/positions")
def positions():
    rows = pnl.position_view()
    return render_template("positions.html", positions=rows, acct=pnl.account(rows), cooling=portfolio.cooling_symbols(), cfg=config)


@app.route("/history")
def history():
    closed = pnl.closed_positions(300)
    for c in closed:
        c["review"] = pnl.review_closed(c)
    return render_template("history.html", closed=closed, perf=portfolio.performance(), acct=pnl.account())


@app.route("/performance")
def performance():
    return render_template("performance.html", days=pnl.daily_table(), acct=pnl.account(), perf=portfolio.performance())


@app.route("/signals")
def signals():
    df = store.read_csv("signal_log.csv")
    sym = (request.args.get("symbol") or "").upper()[:20]
    ev = (request.args.get("event") or "")[:30]
    if sym:
        df = df[df["symbol"] == sym]
    if ev:
        df = df[df["event"] == ev]
    return render_template("signals.html", rows=df.tail(400).iloc[::-1].to_dict(orient="records"), sym=sym, ev=ev)


@app.route("/outlook")
def outlook_page():
    return render_template("outlook.html", o=outlook.load(), cfg=config)


@app.route("/outlook/refresh", methods=["POST"])
def outlook_refresh():
    store.request_task("outlook")
    flash("Outlook refresh requested - the worker builds it within ~10 seconds (it can take a minute). Reload this page afterwards.", "info")
    return redirect("/outlook")


@app.route("/analyze")
def analyze_page():
    """Read-only stock analysis. GET only: it can never place, change or close a position."""
    q = (request.args.get("q") or "").strip()
    res, hit = None, None
    if q:
        hit = analysis.resolve(q)
        if hit["status"] == "ok":
            res = analysis.analyze(hit["symbol"], hit.get("name", ""))
    return render_template("analyze.html", q=q, hit=hit, r=res, cfg=config)


@app.route("/scans")
def scans():
    return render_template("scans.html", runs=reports.runs(200))


@app.route("/scans/<scan_id>")
def scan_detail(scan_id):
    dec = request.args.get("decision") or None
    rows = reports.run_details(scan_id, dec)
    run = next((r for r in reports.runs(5000) if r["scan_id"] == scan_id), None)
    if run is None:
        abort(404)
    return render_template("scan_detail.html", run=run, rows=rows[:1500], decision=dec, total=len(rows))


@app.route("/watchlist")
def watchlist():
    return render_template("watchlist.html", watch=reports.watchlist())


@app.route("/symbol/<sym>")
def symbol(sym):
    return render_template("symbol.html", sym=sym.upper()[:20], rows=reports.symbol_history(sym.upper()[:20], 40))


@app.route("/journal")
def journal():
    return render_template("journal.html", text=store.tail_text("scan_journal.txt", 80000))


@app.route("/help")
def help_page():
    return render_template("help.html", cfg=config)


@app.route("/api/pnl")
def api_pnl():
    return jsonify(account=pnl.account(), daily=pnl.daily_table()[:60])


@app.route("/api/status")
def api_status():
    return jsonify(market=config.market_status()[1], worker=reports.worker_state(), control=store.control(),
                   last_scan=reports.last_scan(), today=reports.today_stats())


@app.route("/control", methods=["POST"])
def control():
    act = request.form.get("action")
    if act == "pause":
        store.set_control(False)
        flash("Paper trading paused. Open positions are still monitored.", "warning")
    elif act == "resume":
        store.set_control(True)
        flash("Paper trading resumed.", "success")
    elif act == "scan":
        store.request_scan()
        flash("Scan requested - the worker picks it up within ~10 seconds. Refresh the Scans page to see the result."
              + (" (Market closed: analysis-only, no trades.)" if not config.market_is_open() else ""), "info")
    return redirect(request.referrer or "/")


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5000)
