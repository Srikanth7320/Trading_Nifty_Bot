"""Dashboard (Flask). Read-only over the data files; it never trades. Login required."""
import hmac
import secrets
import time
from datetime import timedelta

from flask import Flask, abort, flash, jsonify, redirect, render_template, request, session, url_for
from werkzeug.middleware.proxy_fix import ProxyFix

import config
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
    return render_template("dashboard.html", market=config.market_status(), ctl=store.control(), worker=reports.worker_state(),
                           explain=reports.explain_state(), stats=reports.today_stats(), last=reports.last_scan(),
                           positions=reports.position_rows(), watch=reports.watchlist()[:10], runs=reports.runs(6),
                           warn=config.holiday_calendar_warning(), cfg=config)


@app.route("/positions")
def positions():
    return render_template("positions.html", positions=reports.position_rows(), cooling=portfolio.cooling_symbols())


@app.route("/history")
def history():
    return render_template("history.html", sold=store.read_csv("sold_stocks.csv").iloc[::-1].head(300).to_dict(orient="records"),
                           perf=portfolio.performance())


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
