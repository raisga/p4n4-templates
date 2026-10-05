"""Ollama gateway: passes Letta's requests through to Ollama, and turns thinking
off when AGENT_THINK=false.

Letta talks to Ollama's OpenAI-compatible API, and has no setting to stop a
thinking model such as gemma4 from reasoning before every step. On a CPU that
roughly doubles the time an answer takes. With AGENT_THINK=false this gateway
adds "reasoning_effort": "none" to every /v1/chat/completions request, which
Ollama honours. Everything else, streaming included, passes through
unchanged.

    UPSTREAM      Ollama's URL (default http://ollama:11434)
    AGENT_THINK   true: leave requests alone; false (default): no thinking

Standard library only (python:3.13-slim).
"""

from __future__ import annotations

import http.client
import json
import os
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

UPSTREAM = urllib.parse.urlsplit(os.environ.get("UPSTREAM", "http://ollama:11434"))
THINK = os.environ.get("AGENT_THINK", "false").strip().lower() in ("1", "true", "yes")
# Hop-by-hop headers, and the ones this gateway recomputes
SKIP = {"connection", "keep-alive", "transfer-encoding", "te", "trailer", "upgrade", "content-length", "host"}


class Gateway(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def forward(self) -> None:
        body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
        if not THINK and self.command == "POST" and self.path.split("?")[0].endswith("/chat/completions"):
            try:
                request = json.loads(body)
                request["reasoning_effort"] = "none"
                body = json.dumps(request).encode()
            except ValueError:
                pass
        headers = {k: v for k, v in self.headers.items() if k.lower() not in SKIP}
        upstream = http.client.HTTPConnection(UPSTREAM.hostname, UPSTREAM.port or 80, timeout=900)
        try:
            upstream.request(self.command, self.path, body=body or None, headers=headers)
            response = upstream.getresponse()
            self.send_response(response.status, response.reason)
            for key, value in response.getheaders():
                if key.lower() not in SKIP:
                    self.send_header(key, value)
            # Stream the body as it comes (chunked), so streamed replies stay streamed
            self.send_header("Transfer-Encoding", "chunked")
            self.end_headers()
            while chunk := response.read1(65536):
                self.wfile.write(b"%x\r\n%s\r\n" % (len(chunk), chunk))
                self.wfile.flush()
            self.wfile.write(b"0\r\n\r\n")
        except OSError as exc:
            self.send_error(502, f"Ollama unreachable: {exc}")
        finally:
            upstream.close()

    do_GET = do_POST = do_DELETE = forward

    def log_message(self, *args) -> None:
        pass


if __name__ == "__main__":
    print(f"gateway: {'thinking allowed' if THINK else 'thinking off'}, to {UPSTREAM.geturl()}", flush=True)
    ThreadingHTTPServer(("", 11434), Gateway).serve_forever()
