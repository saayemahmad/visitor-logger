"""
Fallback-page visitor logger.
Serves templates/index.html and prints, in the terminal, for every visitor:
  - IP address
  - device model (from navigator.userAgentData high-entropy values, Chromium only)
  - connect time, disconnect time, and total time on the page

Run:
    pip install flask flask-socketio eventlet
    python app.py
Then open http://localhost:5000  (or deploy behind your real domain / reverse proxy).
"""

import os
import sys
from datetime import datetime
from flask import Flask, render_template, request
from flask_socketio import SocketIO


def log(msg):
    # Render (and most host logs) buffer stdout unless we flush explicitly.
    print(msg, flush=True)
    sys.stdout.flush()

app = Flask(__name__)
app.config["SECRET_KEY"] = "change-this-secret"
socketio = SocketIO(app, cors_allowed_origins="*")

# sid -> {"ip": str, "model": str, "connected_at": datetime}
sessions = {}


def client_ip():
    # If you're behind a reverse proxy (Nginx, Cloudflare, Render, etc.) use the
    # forwarded header instead of request.remote_addr; take the first IP in the list.
    fwd = request.headers.get("X-Forwarded-For")
    if fwd:
        return fwd.split(",")[0].strip()
    return request.remote_addr


@app.route("/")
def index():
    return render_template("index.html")


@socketio.on("connect")
def on_connect():
    sessions[request.sid] = {
        "ip": client_ip(),
        "model": None,
        "connected_at": datetime.now(),
    }


@socketio.on("client_info")
def on_client_info(data):
    info = sessions.get(request.sid)
    if not info:
        return
    info["model"] = (data or {}).get("model") or "Unknown"
    ts = info["connected_at"].strftime("%Y-%m-%d %H:%M:%S")
    log(f"[ENTRY]  {ts}  {info['ip']}  {info['model']}")


@socketio.on("disconnect")
def on_disconnect():
    info = sessions.pop(request.sid, None)
    if not info:
        return
    now = datetime.now()
    duration = now - info["connected_at"]
    ts = now.strftime("%Y-%m-%d %H:%M:%S")
    model = info["model"] or "Unknown"
    log(f"[EXIT]   {ts}  {info['ip']}  {model}  (on site: {duration})")


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))  # Render injects PORT; 5000 is the local fallback
    socketio.run(app, host="0.0.0.0", port=port)
