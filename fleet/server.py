"""Loopback-only HTTP server for the fleet dashboard."""
import errno
import json
import os
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .model import Collector

STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")
CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".ico": "image/x-icon",
}


LOOPBACK_HOSTS = ("127.0.0.1", "localhost", "::1")


def host_header_ok(value, port):
    """Only a loopback Host may reach the payload.

    Binding to 127.0.0.1 does not stop DNS rebinding: once an attacker's
    domain resolves to 127.0.0.1, the victim's browser reaches this daemon
    and treats the response as same-origin. The daemon runs at login on a
    fixed port, so that window never closes. The payload carries session
    titles, working directories, git branches and Bash command strings, so
    the Host header must be checked.
    """
    if not value:
        return False                      # HTTP/1.1 requires one
    value = value.strip()
    if value.startswith("["):             # [::1] or [::1]:8787
        closing = value.find("]")
        if closing == -1:
            return False
        name, rest = value[1:closing], value[closing + 1:]
        port_part = rest[1:] if rest.startswith(":") else ""
        if rest and not rest.startswith(":"):
            return False
    elif value.count(":") == 1:
        name, port_part = value.split(":", 1)
    else:
        name, port_part = value, ""
    if name.lower() not in LOOPBACK_HOSTS:
        return False
    if port_part and port_part != str(port):
        return False
    return True


def make_handler(collector):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def do_GET(self):
            if not host_header_ok(
                self.headers.get("Host"), self.server.server_address[1]
            ):
                return self._error(403, "forbidden")
            path = self.path.split("?", 1)[0]
            if path == "/api/fleet":
                return self._json(collector.snapshot())
            if path == "/":
                return self._static("index.html")
            if path.startswith("/static/"):
                return self._static(path[len("/static/"):])
            self._error(404, "not found")

        def _json(self, payload):
            body = json.dumps(payload, default=str).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def _static(self, relative):
            target = os.path.normpath(os.path.join(STATIC_DIR, relative))
            # Refuse anything that escapes the static directory.
            if not target.startswith(STATIC_DIR + os.sep):
                return self._error(403, "forbidden")
            try:
                with open(target, "rb") as fh:
                    body = fh.read()
            except OSError:
                return self._error(404, "not found")
            ext = os.path.splitext(target)[1]
            self.send_response(200)
            self.send_header(
                "Content-Type", CONTENT_TYPES.get(ext, "application/octet-stream")
            )
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _error(self, code, message):
            body = message.encode()
            self.send_response(code)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass  # quiet; launchd captures real errors via stderr

    return Handler


def is_loopback(host):
    host = (host or "").strip().strip("[]").lower()
    return host in LOOPBACK_HOSTS or host.startswith("127.")


def serve(port=8787, host="127.0.0.1"):
    # Refuse to bind anywhere but loopback. The page exposes working
    # directories, git branches and prompt fragments; there is no auth, and a
    # help-text warning is not an enforcement mechanism.
    if not is_loopback(host):
        sys.stderr.write(
            f"Refusing to bind {host}: Agent Fleet is loopback-only.\n"
            f"It serves session titles, working directories and command "
            f"strings with no authentication.\n"
        )
        raise SystemExit(2)

    collector = Collector()

    # Warm the transcript cache off-thread so the first request is not slow.
    threading.Thread(target=collector.snapshot, daemon=True).start()

    try:
        httpd = ThreadingHTTPServer((host, port), make_handler(collector))
    except OSError as exc:
        if exc.errno == errno.EADDRINUSE:
            sys.stderr.write(
                f"Port {port} is already in use.\n"
                f"Either stop what is using it, or run with a different port:\n"
                f"    python3 -m fleet --port 8788\n"
            )
            raise SystemExit(1)
        raise

    sys.stderr.write(f"Agent Fleet on http://{host}:{port}\n")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        httpd.shutdown()
