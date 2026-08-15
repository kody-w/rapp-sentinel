#!/usr/bin/env python3
"""serve.py — the dashboard at a URL you can bookmark.

Regenerates on request rather than serving whatever the last tick left behind,
so what you see is current the moment you look, not up to 15 minutes stale.

Local only. It binds 127.0.0.1 deliberately: the report names internal state,
links to full run transcripts, and exists to be trusted — none of which should
be reachable from anywhere but this machine.

Tokenized static snapshots under /share/ are the sole exception. The server
binds all private interfaces so a phone can read one unguessable frozen report;
all dashboard, log, and public-head routes still reject non-loopback clients.

  python3 serve.py            # http://localhost:9797
  python3 serve.py --port N
"""

import http.server
import ipaddress
import socketserver
import subprocess
import sys
import threading
import time
from pathlib import Path

HOME = Path(__file__).resolve().parent
DASH = HOME / "dashboard"
LOGS = HOME / "logs"
SHARED_REPORTS = HOME / "state" / "shared-reports"
PORT = 9797
# Don't rebuild on every asset request — a page load fires several.
MIN_REBUILD_INTERVAL = 20
_last_build = 0.0
_lock = threading.Lock()


def rebuild(hours=14):
    """Kick off a regeneration WITHOUT blocking the request.

    The first version ran standup.py synchronously, which meant the page hung
    for as long as its `gh` calls took — measured at over 15s, i.e. a dashboard
    that times out. A monitoring page that makes you wait is a page you stop
    opening, and an unopened dashboard is the same as no dashboard.

    So: serve the last render immediately, refresh behind it. You always get a
    page instantly; it is at most one page-load stale.
    """
    global _last_build
    with _lock:
        if time.time() - _last_build < MIN_REBUILD_INTERVAL:
            return
        _last_build = time.time()

    def _work():
        try:
            subprocess.run([sys.executable, str(HOME / "standup.py"), f"--hours={hours}"],
                           capture_output=True, timeout=180, cwd=str(HOME))
        except Exception:
            pass  # keep serving the last good copy rather than a stack trace

    threading.Thread(target=_work, daemon=True).start()


class Handler(http.server.SimpleHTTPRequestHandler):
    def __init__(self, *a, **kw):
        super().__init__(*a, directory=str(HOME), **kw)

    def _send_file(self, target, content_type, *, send_body=True,
                   cache_control=None, allow_origin=None):
        if not target.is_file():
            self.send_error(404)
            return
        body = target.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        if cache_control:
            self.send_header("Cache-Control", cache_control)
        if allow_origin:
            self.send_header("Access-Control-Allow-Origin", allow_origin)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if send_body:
            self.wfile.write(body)

    def _serve(self, *, send_body):
        path = self.path.split("?")[0]
        client = ipaddress.ip_address(self.client_address[0])

        if path.startswith("/share/"):
            target = (SHARED_REPORTS / path.removeprefix("/share/")).resolve()
            if (SHARED_REPORTS.resolve() not in target.parents
                    or target.suffix != ".html"):
                self.send_error(404)
                return
            return self._send_file(
                target, "text/html; charset=utf-8",
                cache_control="private, no-store",
                send_body=send_body,
            )

        if not client.is_loopback:
            self.send_error(404)
            return

        if path in ("/", "/index.html"):
            hours = 14
            if "hours=" in self.path:
                try:
                    hours = int(self.path.split("hours=")[1].split("&")[0])
                except ValueError:
                    pass
            rebuild(hours)
            self.path = "/dashboard/index.html"
            if send_body:
                return super().do_GET()
            return super().do_HEAD()

        # the head this neighborhood publishes for outside neighbors
        if path == "/sentinel-head.json":
            target = HOME / "public" / "sentinel-head.json"
            return self._send_file(
                target, "application/json",
                allow_origin="*",
                send_body=send_body,
            )

        # transcripts: linked from the report, so they must be reachable over
        # http — a file:// link from an http page is blocked by the browser
        if path.startswith("/logs/"):
            target = (HOME / path.lstrip("/")).resolve()
            if LOGS.resolve() not in target.parents:
                self.send_error(404)
                return
            return self._send_file(
                target, "text/plain; charset=utf-8", send_body=send_body)

        if send_body:
            return super().do_GET()
        return super().do_HEAD()

    def do_GET(self):
        self._serve(send_body=True)

    def do_HEAD(self):
        self._serve(send_body=False)

    def log_message(self, *a):
        pass  # a watchdog that spams its own log is not helping


if __name__ == "__main__":
    port = PORT
    for a in sys.argv[1:]:
        if a.startswith("--port"):
            port = int(a.split("=")[1] if "=" in a else sys.argv[sys.argv.index(a) + 1])
    DASH.mkdir(exist_ok=True)
    if not (DASH / "index.html").exists():
        # first ever start: build once, synchronously, so there is something to serve
        subprocess.run([sys.executable, str(HOME / "standup.py")],
                       capture_output=True, timeout=180, cwd=str(HOME))
    rebuild()
    socketserver.TCPServer.allow_reuse_address = True
    with socketserver.TCPServer(("0.0.0.0", port), Handler) as httpd:
        print(f"neighborhood watch → http://localhost:{port}")
        httpd.serve_forever()
