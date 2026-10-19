# DomainHub Visitor Logger

A lightweight fallback page for deleted/expired domains, with a real-time
visitor logger. Every connection is printed to the terminal with IP address,
device model, entry time, exit time, and total time on page.

<p>
  <img alt="Python" src="https://img.shields.io/badge/python-3.9%2B-3776AB?logo=python&logoColor=white">
  <img alt="Flask" src="https://img.shields.io/badge/flask-3.x-000000?logo=flask&logoColor=white">
  <img alt="Socket.IO" src="https://img.shields.io/badge/socket.io-4.7-010101?logo=socketdotio&logoColor=white">
  <img alt="License" src="https://img.shields.io/badge/license-MIT-green">
  <img alt="Build" src="https://img.shields.io/badge/build-passing-brightgreen">
  <img alt="PRs" src="https://img.shields.io/badge/PRs-welcome-blueviolet">
</p>

---

## Overview

When a domain is deleted, this page is shown to anyone who still visits it.
It tells the visitor the domain was deleted and what it costs to register
again, and it logs every visit in real time on the server side so the owner
can see traffic without any third-party analytics tool.

## Features

- Static fallback page — domain, deletion date, nameservers, re-registration price
- Real-time visitor tracking over WebSocket (Socket.IO)
- Per-visitor IP address, country lookup, and device model
- Live web-based log viewer (`/logs`) matching Render / Cloud console aesthetics
- SQLite persistent log storage (`logs.db`)
- Live Socket.IO streaming with automatic scroll-to-bottom

## Project structure

```
server/
├── app.py              # Flask + Socket.IO server & SQLite logging
├── requirements.txt    # Python dependencies
└── templates/
    ├── index.html      # Fallback page + client-side tracking script
    └── logs.html       # Real-time web log viewer (Render-style)
```

## Requirements

- Python 3.9+
- pip

## Installation

```bash
git clone <your-repo-url>
cd server
pip install -r requirements.txt
```

## Usage

```bash
python app.py
```

The site runs at `http://localhost:5000`. Open it in a browser to generate
a log line.

### Terminal output

```
[ENTRY]  2026-10-19 14:02:11  134.54.13.56  SM-2430
[EXIT]   2026-10-19 14:04:25  134.54.13.56  SM-2430  (on site: 0:02:14)
```

## Configuration

Edit the `CONFIG` object at the bottom of `templates/index.html` to set the
domain, deletion date, original expiry date, company name, and support email
shown on the page.

If you deploy behind a reverse proxy or CDN (Nginx, Cloudflare, Render,
etc.), the server reads the real visitor IP from the `X-Forwarded-For`
header automatically — no extra setup needed.

## Deployment

Any host that runs a persistent Python process works, for example:

- A VPS running `python app.py` behind Nginx or Cloudflare
- Render / Railway / Fly.io as a **Web Service** (not a static site, since
  this needs a live backend process)

Point your domain's DNS A/CNAME record at the server once it's running.

## Limitations

- `navigator.userAgentData` is a Chromium-only API. On Safari and Firefox,
  or on desktop devices without a model string, the device model is logged
  as `Unknown`.
- Logs are printed to the terminal only; there is no persistent storage.
  Pipe `python app.py` output to a file or logging service if you need
  history.

## License

MIT
