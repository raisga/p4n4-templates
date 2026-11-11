"""
The HTTP API, read-only: GET /health, /api/v1/probe and /api/v1/dtc.

/health answers 200 while the bridge runs, whether or not an adapter or a car is
there (an ignition that's off isn't a fault), and 503 once its loop has died.
There's no endpoint to clear codes: that waits for the harness's approvals
(design §7.3), and the guard would block it anyway.
"""

from __future__ import annotations

import json
import logging
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import __version__, guard
from .bridge import Bridge

log = logging.getLogger(__name__)


def make_server(bridge: Bridge, loop: threading.Thread, publisher, host: str = "0.0.0.0",
                port: int = 8090) -> ThreadingHTTPServer:

    class Handler(BaseHTTPRequestHandler):
        server_version = f"p4n4-obd2/{__version__}"

        def _send(self, code: int, body: dict) -> None:
            data = json.dumps(body).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            path = self.path.split("?", 1)[0].rstrip("/") or "/"
            if path == "/health":
                status = bridge.status()
                body = {
                    "status": "ok" if loop.is_alive() else "stopped",
                    "version": __version__,
                    "mqtt": publisher.connected,
                    "adapter": status.get("adapter") is not None,
                    "vehicle": bool(status.get("vehicle")),
                    "last_reading_age_s": bridge.last_reading_age(),
                    "blocked_writes": guard.blocked(),
                }
                self._send(200 if loop.is_alive() else 503, body)
            elif path == "/api/v1/probe":
                self._send(200, bridge.probe())
            elif path == "/api/v1/dtc":
                dtc = bridge.dtc()
                if dtc is None:
                    self._send(404, {"error": {"code": "not_read", "message": "no trouble codes read yet"}})
                else:
                    self._send(200, dtc)
            else:
                self._send(404, {"error": {"code": "not_found", "message": f"no route {path}"}})

        def do_POST(self):
            self._send(405, {"error": {"code": "read_only", "message": "obd2 is read-only"}})

        do_PUT = do_PATCH = do_DELETE = do_POST

        def log_message(self, fmt, *args):
            log.debug("http: " + fmt, *args)

    return ThreadingHTTPServer((host, port), Handler)
