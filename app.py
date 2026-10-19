"""
Fallback-page Visitor Logger  ·  v2.3
======================================
Terminal layout
  • Static event lines scroll normally (CONNECT / DISCONN / STALE / STATS)
  • One live status line at the bottom — rewritten in-place with \\r every
    second, showing every active visitor and their ticking duration.
    No multi-line tables, no ANSI cursor tricks, no duplicates.

  NEW in v2.3:
  • SQLite log storage  — every event is persisted to logs.db
  • /logs               — beautiful real-time log viewer page
  • /api/logs           — returns full log history as JSON
  • /api/logs/clear     — deletes all logs from the DB
  • SocketIO 'log_entry' event — pushed to all connected log-viewer clients

Run:
    pip install flask flask-socketio eventlet requests
    python app.py
"""

import os
import sys
import time
import signal
import sqlite3
import threading
from datetime import datetime

import requests as http_requests
from flask import Flask, render_template, request, jsonify
from flask_socketio import SocketIO

# ──────────────────────────────────────────────────────────────────────────────
#  Colour palette  (stripped automatically when stdout is not a TTY)
# ──────────────────────────────────────────────────────────────────────────────
_TTY = sys.stdout.isatty()


def _c(s):
    return s if _TTY else ""


class C:
    RESET   = _c("\033[0m")
    BOLD    = _c("\033[1m")
    DIM     = _c("\033[2m")
    WHITE   = _c("\033[97m")
    CYAN    = _c("\033[96m")
    GREEN   = _c("\033[92m")
    YELLOW  = _c("\033[93m")
    RED     = _c("\033[91m")
    MAGENTA = _c("\033[95m")
    BLUE    = _c("\033[94m")
    GRAY    = _c("\033[90m")
    BG_GREEN  = _c("\033[42m")
    BG_RED    = _c("\033[41m")
    BG_YELLOW = _c("\033[43m")


# ──────────────────────────────────────────────────────────────────────────────
#  Single output lock — every terminal write goes through here
# ──────────────────────────────────────────────────────────────────────────────
_out_lock = threading.Lock()
_live_shown = False          # is there a live line currently printed?
_LIVE_PAD   = 120            # spaces used to erase a previous live line


def _erase_live():
    """Overwrite the live line with blanks and reset cursor to col 0."""
    global _live_shown
    if _live_shown:
        sys.stdout.write("\r" + " " * _LIVE_PAD + "\r")
        sys.stdout.flush()
        _live_shown = False


def _static(line: str, extra: str = "") -> None:
    """Print a scrolling log line (clears the live line first)."""
    with _out_lock:
        _erase_live()
        print(line, flush=True)
        if extra:
            print(extra, flush=True)


def _draw_live() -> None:
    """Rewrite the single-line live status. Called by the ticker thread."""
    global _live_shown
    now = datetime.now()
    with _sessions_lock:
        active = [
            (sid, dict(info))
            for sid, info in sessions.items()
            if info.get("logged_entry")
        ]

    with _out_lock:
        if not active:
            _erase_live()
            return

        parts = []
        for sid, info in active:
            dur   = _fmt_dur(now - info["connected_at"])
            code  = COUNTRY_CODES.get(info.get("country") or "", "??")
            model = (info.get("model") or "?")[:18]
            parts.append(
                f"{C.GREEN}{info['ip']}{C.RESET} "
                f"{C.GRAY}[{C.CYAN}{code}{C.GRAY}]{C.RESET} "
                f"{C.WHITE}{model}{C.RESET} "
                f"{C.MAGENTA}{C.BOLD}{dur}{C.RESET}"
            )

        sep  = f"  {C.GRAY}|{C.RESET}  "
        line = f"  {C.CYAN}●{C.RESET}  " + sep.join(parts)

        sys.stdout.write("\r" + line + "   ")   # trailing spaces erase tail
        sys.stdout.flush()
        _live_shown = True


# ──────────────────────────────────────────────────────────────────────────────
#  Formatting helpers
# ──────────────────────────────────────────────────────────────────────────────
def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _fmt_dur(delta) -> str:
    t = int(delta.total_seconds())
    h, r = divmod(t, 3600)
    m, s = divmod(r, 60)
    if h:
        return f"{h}h {m:02d}m {s:02d}s"
    if m:
        return f"{m}m {s:02d}s"
    return f"{s}s"


def _short(sid: str) -> str:
    return sid[:8] if sid else "????????"


def _flag(country: str) -> str:
    code = COUNTRY_CODES.get(country, "??")
    return f"{C.GRAY}[{C.CYAN}{code}{C.GRAY}]{C.RESET}"


# ──────────────────────────────────────────────────────────────────────────────
#  SQLite — persistent log storage
# ──────────────────────────────────────────────────────────────────────────────
DB_PATH = os.path.join(os.path.dirname(__file__), "logs.db")
_db_lock = threading.Lock()


def _db_connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def _db_init() -> None:
    with _db_lock:
        conn = _db_connect()
        conn.execute("""
            CREATE TABLE IF NOT EXISTS logs (
                id        INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT    NOT NULL,
                level     TEXT    NOT NULL,
                ip        TEXT,
                country   TEXT,
                model     TEXT,
                duration  TEXT,
                message   TEXT
            )
        """)
        conn.commit()
        conn.close()


def _db_save(level: str, ip: str = None, country: str = None,
             model: str = None, duration: str = None, message: str = None) -> dict:
    """Insert one log row; return it as a dict (for SocketIO broadcast)."""
    ts = _now()
    with _db_lock:
        conn = _db_connect()
        cur  = conn.execute(
            "INSERT INTO logs (timestamp, level, ip, country, model, duration, message) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (ts, level, ip, country, model, duration, message)
        )
        row_id = cur.lastrowid
        conn.commit()
        conn.close()
    return {
        "id": row_id, "timestamp": ts, "level": level,
        "ip": ip, "country": country, "model": model,
        "duration": duration, "message": message,
    }


def _db_all() -> list:
    with _db_lock:
        conn = _db_connect()
        rows = conn.execute(
            "SELECT id, timestamp, level, ip, country, model, duration, message "
            "FROM logs ORDER BY id ASC"
        ).fetchall()
        conn.close()
    return [dict(r) for r in rows]


def _db_clear() -> None:
    with _db_lock:
        conn = _db_connect()
        conn.execute("DELETE FROM logs")
        conn.commit()
        conn.close()


# ──────────────────────────────────────────────────────────────────────────────
#  Named log levels  — each saves to DB then broadcasts via SocketIO
# ──────────────────────────────────────────────────────────────────────────────
def log_info(msg: str) -> None:
    _static(f"{C.GRAY}[{_now()}]{C.RESET}  {C.BLUE}INFO   {C.RESET}  {msg}")
    _broadcast(_db_save("INFO", message=msg))


def log_entry(ip, country, model, ua, sid_short) -> None:
    _static(
        f"{C.GRAY}[{_now()}]{C.RESET}  "
        f"{C.BG_GREEN}{C.BOLD} CONNECT {C.RESET}  "
        f"{C.GREEN}{C.BOLD}{ip:<18}{C.RESET}  "
        f"{_flag(country)} {C.CYAN}{country:<18}{C.RESET}  "
        f"{C.YELLOW}device{C.RESET}: {C.WHITE}{model}{C.RESET}"
    )
    _broadcast(_db_save("CONNECT", ip=ip, country=country, model=model))


def log_exit(ip, country, model, dur, sid_short) -> None:
    _static(
        f"{C.GRAY}[{_now()}]{C.RESET}  "
        f"{C.BG_RED}{C.BOLD} DISCONN {C.RESET}  "
        f"{C.RED}{ip:<18}{C.RESET}  "
        f"{_flag(country)} {C.CYAN}{country:<18}{C.RESET}  "
        f"{C.YELLOW}device{C.RESET}: {C.WHITE}{model:<22}{C.RESET}  "
        f"{C.MAGENTA}on-site{C.RESET}: {C.BOLD}{dur}{C.RESET}"
    )
    _broadcast(_db_save("DISCONN", ip=ip, country=country, model=model, duration=dur))


def log_stale(ip, sid_short) -> None:
    msg = f"no heartbeat >{STALE_TIMEOUT}s"
    _static(
        f"{C.GRAY}[{_now()}]{C.RESET}  "
        f"{C.BG_YELLOW}{C.BOLD} STALE   {C.RESET}  "
        f"{C.YELLOW}{ip:<18}{C.RESET}  "
        f"{msg}"
    )
    _broadcast(_db_save("STALE", ip=ip, message=msg))


def log_stats() -> None:
    with _sessions_lock:
        n = len(sessions)
    msg = f"active visitors: {n}"
    _static(
        f"{C.GRAY}[{_now()}]{C.RESET}  "
        f"{C.BLUE}STATS  {C.RESET}  "
        f"active visitors: {C.BOLD}{C.CYAN}{n}{C.RESET}"
    )
    _broadcast(_db_save("STATS", message=msg))


def _broadcast(log_dict: dict) -> None:
    """Emit 'log_entry' to all SocketIO clients (log viewer)."""
    try:
        socketio.emit("log_entry", log_dict)
    except Exception:
        pass  # server may not be ready yet during startup


# ──────────────────────────────────────────────────────────────────────────────
#  Geo lookup
# ──────────────────────────────────────────────────────────────────────────────
_geo_cache: dict = {}
_geo_lock = threading.Lock()

COUNTRY_CODES: dict = {
    "Bangladesh": "BD", "United States": "US", "United Kingdom": "GB",
    "India": "IN", "Germany": "DE", "France": "FR", "Canada": "CA",
    "Australia": "AU", "Japan": "JP", "China": "CN", "Brazil": "BR",
    "Russia": "RU", "Pakistan": "PK", "Netherlands": "NL", "Singapore": "SG",
    "South Korea": "KR", "Turkey": "TR", "Italy": "IT", "Spain": "ES",
    "Mexico": "MX", "Indonesia": "ID", "Saudi Arabia": "SA", "Nigeria": "NG",
    "Unknown": "??", "Localhost": "LC",
}


def _geo(ip: str) -> str:
    if ip in ("127.0.0.1", "::1", "localhost"):
        return "Localhost"
    with _geo_lock:
        if ip in _geo_cache:
            return _geo_cache[ip]
    try:
        r = http_requests.get(
            f"http://ip-api.com/json/{ip}?fields=country", timeout=2
        )
        country = r.json().get("country") or "Unknown"
    except Exception:
        country = "Unknown"
    with _geo_lock:
        _geo_cache[ip] = country
    return country


# ──────────────────────────────────────────────────────────────────────────────
#  Flask / SocketIO
# ──────────────────────────────────────────────────────────────────────────────
app = Flask(__name__)
app.config["SECRET_KEY"] = os.environ.get("SECRET_KEY", "change-this-in-production")
app.config["TEMPLATES_AUTO_RELOAD"] = True
socketio = SocketIO(
    app,
    cors_allowed_origins="*",
    async_mode="eventlet",
    ping_timeout=60,
    ping_interval=25,
    logger=False,
    engineio_logger=False,
)

sessions: dict = {}
_sessions_lock = threading.Lock()

STALE_TIMEOUT  = 120
SWEEP_INTERVAL = 30
TICK_INTERVAL  = 1


def _ip() -> str:
    fwd = request.headers.get("X-Forwarded-For")
    return fwd.split(",")[0].strip() if fwd else (request.remote_addr or "0.0.0.0")


# ──────────────────────────────────────────────────────────────────────────────
#  Routes
# ──────────────────────────────────────────────────────────────────────────────
@app.route("/")
def index():
    return render_template("index.html")


@app.route("/logs")
def logs_viewer():
    return render_template("logs.html")


@app.route("/health")
def health():
    with _sessions_lock:
        n = len(sessions)
    return {"status": "ok", "active_visitors": n, "ts": _now()}, 200


# ── Log API endpoints ─────────────────────────────────────────────────────────
@app.route("/api/logs", methods=["GET"])
def api_logs():
    """Return all stored logs as JSON (oldest first)."""
    logs = _db_all()
    return jsonify({"logs": logs, "count": len(logs)})


@app.route("/api/logs/clear", methods=["POST"])
def api_logs_clear():
    """Delete all logs from the database."""
    _db_clear()
    return jsonify({"status": "ok", "message": "All logs cleared"})


# ──────────────────────────────────────────────────────────────────────────────
#  SocketIO events
# ──────────────────────────────────────────────────────────────────────────────
@socketio.on("connect")
def on_connect():
    # Do not track or log connections from the /logs viewer page
    if request.args.get("role") == "viewer":
        return

    sid, ip, now = request.sid, _ip(), datetime.now()
    with _sessions_lock:
        sessions[sid] = {
            "ip": ip, "country": None, "model": None,
            "ua": request.headers.get("User-Agent", ""),
            "connected_at": now, "last_heartbeat": now,
            "logged_entry": False,
        }

    def _resolve():
        country = _geo(ip)
        with _sessions_lock:
            info = sessions.get(sid)
            if info:
                info["country"] = country
        _maybe_log(sid)

    threading.Thread(target=_resolve, daemon=True).start()
    with _sessions_lock:
        n = len(sessions)
    socketio.emit("visitor_count", {"count": n})


@socketio.on("client_info")
def on_client_info(data):
    sid = request.sid
    with _sessions_lock:
        info = sessions.get(sid)
        if not info:
            return
        info["model"] = (data or {}).get("model") or "Unknown"
        info["ua"]    = (data or {}).get("ua") or info["ua"]
    _maybe_log(sid)


@socketio.on("heartbeat")
def on_heartbeat():
    with _sessions_lock:
        info = sessions.get(request.sid)
        if info:
            info["last_heartbeat"] = datetime.now()


@socketio.on("disconnect")
def on_disconnect():
    sid = request.sid
    with _sessions_lock:
        info  = sessions.pop(sid, None)
        count = len(sessions)
    if not info:
        return
    dur = _fmt_dur(datetime.now() - info["connected_at"])
    log_exit(info["ip"], info.get("country") or "Unknown",
             info.get("model") or "Unknown", dur, _short(sid))
    log_stats()
    socketio.emit("visitor_count", {"count": count})


def _maybe_log(sid: str) -> None:
    with _sessions_lock:
        info = sessions.get(sid)
        if not info or info["logged_entry"]:
            return
        if info["country"] is None or info["model"] is None:
            return
        info["logged_entry"] = True
        ip, country, model, ua = (
            info["ip"], info["country"], info["model"], info["ua"]
        )
    log_entry(ip, country, model, ua, _short(sid))
    log_stats()


# ──────────────────────────────────────────────────────────────────────────────
#  Background threads
# ──────────────────────────────────────────────────────────────────────────────
def _ticker():
    while True:
        time.sleep(TICK_INTERVAL)
        _draw_live()


def _sweeper():
    while True:
        time.sleep(SWEEP_INTERVAL)
        now = datetime.now()
        with _sessions_lock:
            stale = [
                (sid, dict(info)) for sid, info in sessions.items()
                if (now - info["last_heartbeat"]).total_seconds() > STALE_TIMEOUT
            ]
        for sid, info in stale:
            log_stale(info["ip"], _short(sid))


# ──────────────────────────────────────────────────────────────────────────────
#  Banner & shutdown
# ──────────────────────────────────────────────────────────────────────────────
def _banner(port: int) -> None:
    sk = os.environ.get("SECRET_KEY")
    _static(f"  {C.BOLD}{C.BLUE}Visitor Logger v2.3{C.RESET}")
    _static(f"  {C.CYAN}local{C.RESET}   →  http://localhost:{port}")
    _static(f"  {C.CYAN}logs{C.RESET}    →  http://localhost:{port}/logs")
    _static(f"  {C.CYAN}health{C.RESET}  →  http://localhost:{port}/health")
    _static(
        f"  SECRET_KEY: "
        + (f"{C.GREEN}set ✓{C.RESET}" if sk else f"{C.RED}not set (default){C.RESET}")
    )
    _static(f"  {C.GREEN}Ctrl+C to stop.{C.RESET}")
    _static(f"{C.GRAY}{'─' * 72}{C.RESET}")


def _shutdown(sig, _):
    with _out_lock:
        _erase_live()
        print(f"\n{C.YELLOW}[{_now()}]  signal {sig} — shutting down…{C.RESET}", flush=True)
    sys.exit(0)


signal.signal(signal.SIGTERM, _shutdown)
signal.signal(signal.SIGINT,  _shutdown)


# ──────────────────────────────────────────────────────────────────────────────
#  Entry point
# ──────────────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    _db_init()
    threading.Thread(target=_sweeper, daemon=True).start()
    threading.Thread(target=_ticker,  daemon=True).start()
    _banner(port)
    log_info(f"SocketIO server starting on 0.0.0.0:{port}")
    socketio.run(app, host="0.0.0.0", port=port, debug=False)
