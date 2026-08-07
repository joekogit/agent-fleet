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


def make_handler(collector):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def do_GET(self):
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


def serve(port=8787, host="127.0.0.1"):
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
