"""
Fallback-page Visitor Logger  ·  v2.1
======================================
Serves templates/index.html and logs every visitor with:
  - IP address + best-effort country via ip-api.com
  - Device model (from navigator.userAgentData, Chromium only)
  - User-Agent string
  - Connect / disconnect timestamps
  - Live in-place duration ticker — active visitors' time on site
    updates every second in the terminal without scrolling
  - Stale-session detection (background sweeper)
  - /health endpoint for uptime monitors

Run:
    pip install flask flask-socketio eventlet requests
    python app.py

Open:  http://localhost:5000  (or behind Render / Railway / Nginx).
"""

# ──────────────────────────────────────────────────────────────────────────────
#  Stdlib
# ──────────────────────────────────────────────────────────────────────────────
import os
import sys
import time
import signal
import threading
from datetime import datetime

# ──────────────────────────────────────────────────────────────────────────────
#  Third-party
# ──────────────────────────────────────────────────────────────────────────────
import requests as http_requests
from flask import Flask, render_template, request
from flask_socketio import SocketIO

# ══════════════════════════════════════════════════════════════════════════════
#  TTY detection  (colours + cursor tricks only work in a real terminal)
# ══════════════════════════════════════════════════════════════════════════════
_IS_TTY = sys.stdout.isatty()


class C:
    """ANSI colour / style tokens — all empty strings when not a TTY."""
    def _t(s): return s if _IS_TTY else ""   # noqa: E301  (local helper)
    RESET    = _t("\033[0m")
    BOLD     = _t("\033[1m")
    DIM      = _t("\033[2m")
    WHITE    = _t("\033[97m")
    CYAN     = _t("\033[96m")
    GREEN    = _t("\033[92m")
    YELLOW   = _t("\033[93m")
    RED      = _t("\033[91m")
    MAGENTA  = _t("\033[95m")
    BLUE     = _t("\033[94m")
    GRAY     = _t("\033[90m")
    BG_GREEN  = _t("\033[42m")
    BG_RED    = _t("\033[41m")
    BG_YELLOW = _t("\033[43m")
    # Cursor control
    ERASE_LINE   = _t("\033[2K")   # clear whole line (cursor stays)
    CURSOR_UP    = _t("\033[{n}A") # move cursor up n lines (use .format)
    CURSOR_SHOW  = _t("\033[?25h")
    CURSOR_HIDE  = _t("\033[?25l")


# ══════════════════════════════════════════════════════════════════════════════
#  Live-display state
#   The terminal layout is:
#     ┌──────────────────────────────────┐
#     │  (scrolling static event log)    │  ← printed with log_static()
#     ├──────────────────────────────────┤
#     │  ┌─ LIVE ACTIVE VISITORS ──────┐ │  ← redrawn in-place every second
#     │  │  IP  Country Device  Time  │ │
#     │  │  …   …       …       tick  │ │
#     │  └─────────────────────────────┘ │
#     └──────────────────────────────────┘
# ══════════════════════════════════════════════════════════════════════════════
_output_lock  = threading.Lock()   # guards ALL terminal writes
_live_height  = 0                  # how many lines the live table currently occupies

LIVE_TICK_S   = 1                  # redraw interval (seconds)
TABLE_W       = 88                 # visual width of the live table


def _cursor_up(n: int) -> str:
    return f"\033[{n}A" if _IS_TTY and n > 0 else ""


def _write(text: str) -> None:
    """Raw, unbuffered write — caller must hold _output_lock."""
    sys.stdout.write(text)
    sys.stdout.flush()


# ──────────────────────────────────────────────────────────────────────────────
#  Core output primitives
# ──────────────────────────────────────────────────────────────────────────────
def _static_log(line: str, extra: str = "") -> None:
    """
    Print a static (scrolling) log line above the live table.
    Steps:
      1. Move cursor above the live table
      2. Print the new line(s)  — terminal scrolls the live table down
      3. Redraw the live table at its new position
    """
    global _live_height
    with _output_lock:
        if _IS_TTY and _live_height > 0:
            _write(_cursor_up(_live_height))
        _write(line + "\n")
        if extra:
            _write(extra + "\n")
        _redraw_live_table_locked()


def _redraw_live_table_locked() -> None:
    """Redraw the live table in-place. Caller must hold _output_lock."""
    global _live_height

    now = datetime.now()
    with _sessions_lock:
        active = [
            (sid, dict(info))
            for sid, info in sessions.items()
            if info.get("logged_entry")
        ]

    # ── Build table lines ────────────────────────────────────────────────────
    SEP  = C.GRAY + "─" * TABLE_W + C.RESET
    SEP2 = C.GRAY + "═" * TABLE_W + C.RESET

    hdr = (
        f"  {C.BOLD}{C.BLUE}{'IP':<18}{C.RESET}"
        f"  {C.BOLD}{C.BLUE}{'Country':<16}{C.RESET}"
        f"  {C.BOLD}{C.BLUE}{'Device':<26}{C.RESET}"
        f"  {C.BOLD}{C.BLUE}{'Connected At':<21}{C.RESET}"
        f"  {C.BOLD}{C.CYAN}{'On Site':>10}{C.RESET}"
        f"  {C.BOLD}{C.BLUE}SID{C.RESET}"
    )

    table_lines = [SEP2, hdr, SEP]

    if active:
        for sid, info in active:
            dur   = _format_duration(now - info["connected_at"])
            con   = info["connected_at"].strftime("%H:%M:%S")
            ip    = info["ip"]
            code  = COUNTRY_CODES.get(info.get("country") or "", "??")
            model = (info.get("model") or "Unknown")[:26]
            row = (
                f"  {C.GREEN}{ip:<18}{C.RESET}"
                f"  {C.GRAY}[{C.CYAN}{code}{C.GRAY}]{C.RESET} {C.CYAN}{(info.get('country') or 'Unknown'):<13}{C.RESET}"
                f"  {C.WHITE}{model:<26}{C.RESET}"
                f"  {C.GRAY}{con:<21}{C.RESET}"
                f"  {C.MAGENTA}{C.BOLD}{dur:>10}{C.RESET}"
                f"  {C.GRAY}{_short_sid(sid)}{C.RESET}"
            )
            table_lines.append(row)
    else:
        table_lines.append(
            f"  {C.GRAY}(no active visitors){C.RESET}"
        )

    table_lines.append(SEP2)
    new_height = len(table_lines)

    if _IS_TTY:
        # Erase old lines (move up, clear each)
        if _live_height > 0:
            _write(_cursor_up(_live_height))
        for _ in range(max(new_height, _live_height)):
            _write(C.ERASE_LINE + "\n")
        # Move back to top of erased block and write fresh content
        _write(_cursor_up(max(new_height, _live_height)))
        for line in table_lines:
            _write(C.ERASE_LINE + line + "\n")
    else:
        # Non-TTY: just dump (used in log files / piped output)
        for line in table_lines:
            _write(line + "\n")

    _live_height = new_height


# ──────────────────────────────────────────────────────────────────────────────
#  Formatted log helpers
# ──────────────────────────────────────────────────────────────────────────────
def _now_str() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _sep(char="─", width=66) -> str:
    return C.GRAY + char * width + C.RESET


def _country_flag(country: str) -> str:
    code = COUNTRY_CODES.get(country, "??")
    return f"{C.GRAY}[{C.CYAN}{code}{C.GRAY}]{C.RESET}"


def _format_duration(delta) -> str:
    total = int(delta.total_seconds())
    h, rem = divmod(total, 3600)
    m, s   = divmod(rem, 60)
    if h:
        return f"{h}h {m:02d}m {s:02d}s"
    if m:
        return f"{m}m {s:02d}s"
    return f"{s}s"


def _short_sid(sid: str) -> str:
    return sid[:8] if sid else "????????"


def log_info(msg: str) -> None:
    _static_log(f"{C.GRAY}[{_now_str()}]{C.RESET}  {C.BLUE}INFO   {C.RESET}  {msg}")


def log_entry(ip, country, model, ua, sid_short) -> None:
    flag = _country_flag(country)
    line = (
        f"{C.GRAY}[{_now_str()}]{C.RESET}  "
        f"{C.BG_GREEN}{C.BOLD}  CONNECT  {C.RESET}  "
        f"{C.GREEN}{C.BOLD}{ip:<18}{C.RESET}"
        f"  {flag} {C.CYAN}{country:<20}{C.RESET}"
        f"  {C.YELLOW}device{C.RESET}: {C.WHITE}{model:<28}{C.RESET}"
        f"  {C.GRAY}sid:{sid_short}{C.RESET}"
    )
    extra = f"{'':>32}{C.GRAY}ua: {ua[:96]}{C.RESET}" if ua else ""
    _static_log(line, extra)


def log_exit(ip, country, model, duration_str, sid_short) -> None:
    flag = _country_flag(country)
    _static_log(
        f"{C.GRAY}[{_now_str()}]{C.RESET}  "
        f"{C.BG_RED}{C.BOLD}  DISCONN  {C.RESET}  "
        f"{C.RED}{ip:<18}{C.RESET}"
        f"  {flag} {C.CYAN}{country:<20}{C.RESET}"
        f"  {C.YELLOW}device{C.RESET}: {C.WHITE}{model:<28}{C.RESET}"
        f"  {C.MAGENTA}on-site{C.RESET}: {C.BOLD}{duration_str}{C.RESET}"
        f"  {C.GRAY}sid:{sid_short}{C.RESET}"
    )


def log_stale(ip, sid_short) -> None:
    _static_log(
        f"{C.GRAY}[{_now_str()}]{C.RESET}  "
        f"{C.BG_YELLOW}{C.BOLD}  STALE    {C.RESET}  "
        f"{C.YELLOW}{ip:<18}{C.RESET}  "
        f"no heartbeat for >{STALE_TIMEOUT}s  {C.GRAY}sid:{sid_short}{C.RESET}"
    )


def log_stats() -> None:
    with _sessions_lock:
        count = len(sessions)
    _static_log(
        f"{C.GRAY}[{_now_str()}]{C.RESET}  "
        f"{C.BLUE}STATS  {C.RESET}  "
        f"active visitors: {C.BOLD}{C.CYAN}{count}{C.RESET}"
    )


# ══════════════════════════════════════════════════════════════════════════════
#  Geo-lookup  (best-effort, degrades gracefully to "Unknown")
# ══════════════════════════════════════════════════════════════════════════════
_geo_cache: dict = {}
_geo_lock = threading.Lock()

COUNTRY_CODES: dict = {
    "Bangladesh": "BD", "United States": "US", "United Kingdom": "GB",
    "India": "IN", "Germany": "DE", "France": "FR", "Canada": "CA",
    "Australia": "AU", "Japan": "JP", "China": "CN", "Brazil": "BR",
    "Russia": "RU", "Pakistan": "PK", "Netherlands": "NL", "Singapore": "SG",
    "South Korea": "KR", "Turkey": "TR", "Italy": "IT", "Spain": "ES",
    "Mexico": "MX", "Indonesia": "ID", "Saudi Arabia": "SA", "Nigeria": "NG",
    "Unknown": "??",
}


def _lookup_country(ip: str) -> str:
    """Background geo lookup via ip-api.com (free, no key, ~45 req/min)."""
    if ip in ("127.0.0.1", "::1", "localhost"):
        return "Localhost"
    with _geo_lock:
        if ip in _geo_cache:
            return _geo_cache[ip]
    try:
        resp = http_requests.get(
            f"http://ip-api.com/json/{ip}?fields=country",
            timeout=2,
        )
        country = resp.json().get("country") or "Unknown"
    except Exception:
        country = "Unknown"
    with _geo_lock:
        _geo_cache[ip] = country
    return country


# ══════════════════════════════════════════════════════════════════════════════
#  Flask / SocketIO
# ══════════════════════════════════════════════════════════════════════════════
app = Flask(__name__)
app.config["SECRET_KEY"] = os.environ.get("SECRET_KEY", "change-this-in-production")
socketio = SocketIO(
    app,
    cors_allowed_origins="*",
    async_mode="eventlet",
    ping_timeout=60,
    ping_interval=25,
    logger=False,
    engineio_logger=False,
)

# sid -> {ip, country, model, ua, connected_at, last_heartbeat, logged_entry}
sessions: dict = {}
_sessions_lock = threading.Lock()

STALE_TIMEOUT  = 120   # seconds without heartbeat → stale
SWEEP_INTERVAL = 30    # how often the sweeper wakes


# ══════════════════════════════════════════════════════════════════════════════
#  Helpers
# ══════════════════════════════════════════════════════════════════════════════
def _client_ip() -> str:
    fwd = request.headers.get("X-Forwarded-For")
    if fwd:
        return fwd.split(",")[0].strip()
    return request.remote_addr or "0.0.0.0"


# ══════════════════════════════════════════════════════════════════════════════
#  HTTP routes
# ══════════════════════════════════════════════════════════════════════════════
@app.route("/")
def index():
    return render_template("index.html")


@app.route("/health")
def health():
    with _sessions_lock:
        count = len(sessions)
    return {"status": "ok", "active_visitors": count, "ts": _now_str()}, 200


# ══════════════════════════════════════════════════════════════════════════════
#  SocketIO events
# ══════════════════════════════════════════════════════════════════════════════
@socketio.on("connect")
def on_connect():
    sid  = request.sid
    ip   = _client_ip()
    now  = datetime.now()
    with _sessions_lock:
        sessions[sid] = {
            "ip": ip,
            "country": None,
            "model": None,
            "ua": request.headers.get("User-Agent", ""),
            "connected_at": now,
            "last_heartbeat": now,
            "logged_entry": False,
        }

    def _geo_then_log():
        country = _lookup_country(ip)
        with _sessions_lock:
            info = sessions.get(sid)
            if info:
                info["country"] = country
        _maybe_log_entry(sid)

    threading.Thread(target=_geo_then_log, daemon=True).start()

    with _sessions_lock:
        count = len(sessions)
    socketio.emit("visitor_count", {"count": count})


@socketio.on("client_info")
def on_client_info(data):
    sid = request.sid
    with _sessions_lock:
        info = sessions.get(sid)
        if not info:
            return
        info["model"] = (data or {}).get("model") or "Unknown"
        info["ua"]    = (data or {}).get("ua") or info["ua"]
    _maybe_log_entry(sid)


@socketio.on("heartbeat")
def on_heartbeat():
    sid = request.sid
    now = datetime.now()
    with _sessions_lock:
        info = sessions.get(sid)
        if not info:
            return
        info["last_heartbeat"] = now


@socketio.on("disconnect")
def on_disconnect():
    sid = request.sid
    with _sessions_lock:
        info  = sessions.pop(sid, None)
        count = len(sessions)

    if not info:
        return

    now      = datetime.now()
    duration = _format_duration(now - info["connected_at"])
    country  = info.get("country") or "Unknown"
    model    = info.get("model")   or "Unknown"

    log_exit(info["ip"], country, model, duration, _short_sid(sid))
    log_stats()
    socketio.emit("visitor_count", {"count": count})


# ══════════════════════════════════════════════════════════════════════════════
#  Deferred entry logger — waits until both geo + model are ready
# ══════════════════════════════════════════════════════════════════════════════
def _maybe_log_entry(sid: str) -> None:
    with _sessions_lock:
        info = sessions.get(sid)
        if not info or info["logged_entry"]:
            return
        if info["country"] is None or info["model"] is None:
            return
        info["logged_entry"] = True
        ip      = info["ip"]
        country = info["country"]
        model   = info["model"]
        ua      = info["ua"]

    log_entry(ip, country, model, ua, _short_sid(sid))
    log_stats()


# ══════════════════════════════════════════════════════════════════════════════
#  Background thread: live ticker (redraws the active-visitor table every sec)
# ══════════════════════════════════════════════════════════════════════════════
def _live_ticker():
    while True:
        time.sleep(LIVE_TICK_S)
        with _output_lock:
            _redraw_live_table_locked()


# ══════════════════════════════════════════════════════════════════════════════
#  Background thread: stale-session sweeper
# ══════════════════════════════════════════════════════════════════════════════
def _stale_sweeper():
    while True:
        time.sleep(SWEEP_INTERVAL)
        now = datetime.now()
        with _sessions_lock:
            stale = [
                (sid, info)
                for sid, info in sessions.items()
                if (now - info["last_heartbeat"]).total_seconds() > STALE_TIMEOUT
            ]
        for sid, info in stale:
            log_stale(info["ip"], _short_sid(sid))


# ══════════════════════════════════════════════════════════════════════════════
#  Startup banner
# ══════════════════════════════════════════════════════════════════════════════
def _print_banner(port: int) -> None:
    W = 62
    lines = [
        _sep("═", W),
        f"  {C.BOLD}{C.BLUE}{'Visitor Logger  v2.1':^{W - 2}}{C.RESET}",
        _sep("─", W),
        f"  {C.CYAN}Local   {C.RESET}→  http://localhost:{port}",
        f"  {C.CYAN}Health  {C.RESET}→  http://localhost:{port}/health",
        _sep("─", W),
    ]
    sk_ok = os.environ.get("SECRET_KEY")
    lines.append(
        f"  SECRET_KEY   : "
        + (f"{C.GREEN}set ✓{C.RESET}" if sk_ok else f"{C.RED}NOT set — using default{C.RESET}")
    )
    lines += [
        f"  Stale timeout: {STALE_TIMEOUT}s   Sweep: every {SWEEP_INTERVAL}s",
        f"  Live ticker  : every {LIVE_TICK_S}s",
        _sep("─", W),
        f"  {C.GREEN}Press Ctrl+C to stop.{C.RESET}",
        _sep("═", W),
    ]
    with _output_lock:
        for l in lines:
            _write(l + "\n")


# ══════════════════════════════════════════════════════════════════════════════
#  Graceful shutdown
# ══════════════════════════════════════════════════════════════════════════════
def _shutdown(signum, _frame):
    with _output_lock:
        _write(f"\n{C.YELLOW}[{_now_str()}]  signal {signum} — shutting down…{C.RESET}\n")
    with _sessions_lock:
        remaining = len(sessions)
    if remaining:
        with _output_lock:
            _write(f"{C.GRAY}  {remaining} active session(s) dropped.{C.RESET}\n")
    if _IS_TTY:
        sys.stdout.write(C.CURSOR_SHOW)
        sys.stdout.flush()
    sys.exit(0)


signal.signal(signal.SIGTERM, _shutdown)
signal.signal(signal.SIGINT,  _shutdown)


# ══════════════════════════════════════════════════════════════════════════════
#  Entry point
# ══════════════════════════════════════════════════════════════════════════════
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))

    if _IS_TTY:
        sys.stdout.write(C.CURSOR_HIDE)
        sys.stdout.flush()

    threading.Thread(target=_stale_sweeper, daemon=True).start()
    threading.Thread(target=_live_ticker,   daemon=True).start()

    _print_banner(port)
    log_info(f"SocketIO server starting on 0.0.0.0:{port}")

    # Draw the initial (empty) live table so the area is reserved
    with _output_lock:
        _redraw_live_table_locked()

    socketio.run(app, host="0.0.0.0", port=port, debug=False)
