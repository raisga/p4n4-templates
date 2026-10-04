"""
Scripted stand-in for Ollama, so the smoke test can drive the agent's tool loop
without downloading a model.

POST /api/chat, streaming NDJSON like Ollama:
- the last message is a tool result: replies "result: <tool content>", plus
  "system prompt: yes" when the request starts with a system message;
- tools are offered and the last user message is a JSON tool call
  ({"name": ..., "arguments": {...}}): asks for that tool call;
- otherwise: replies "no tools".
Anything else gets a small JSON answer (/api/tags lists one model).
"""

from __future__ import annotations

import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

MODEL = "fake:latest"


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.0"

    def log_message(self, format: str, *args) -> None:
        pass

    def _send(self, payload: bytes, content_type: str = "application/json") -> None:
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.end_headers()
        self.wfile.write(payload)

    def do_GET(self) -> None:
        if self.path == "/api/tags":
            self._send(json.dumps({"models": [{"name": MODEL, "model": MODEL}]}).encode())
        else:
            self._send(json.dumps({"version": "0.0.0-fake"}).encode())

    def do_POST(self) -> None:
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
        if self.path != "/api/chat":
            return self._send(b"{}")
        messages = body.get("messages", [])
        last = messages[-1] if messages else {}
        chunks: list[dict] = []
        if last.get("role") == "tool":
            system = "yes" if messages[0].get("role") == "system" else "no"
            # Two chunks, to check the agent streams them through
            chunks = [{"content": "result: "}, {"content": f"{last['content']}\nsystem prompt: {system}"}]
        elif body.get("tools") and last.get("role") == "user":
            call = json.loads(last["content"])
            chunks = [{"content": "", "tool_calls": [{"function": call}]}]
        else:
            chunks = [{"content": "no tools"}]
        lines = [
            {"model": body.get("model"), "message": {"role": "assistant", **c}, "done": False} for c in chunks
        ]
        lines.append({"model": body.get("model"), "message": {"role": "assistant", "content": ""},
                      "done": True, "done_reason": "stop"})
        self._send(b"".join(json.dumps(line).encode() + b"\n" for line in lines), "application/x-ndjson")


ThreadingHTTPServer(("0.0.0.0", 11434), Handler).serve_forever()
