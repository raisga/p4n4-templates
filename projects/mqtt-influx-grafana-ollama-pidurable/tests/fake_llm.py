"""Scripted stand-in for Ollama's OpenAI-compatible API (/v1/chat/completions).

The tests point the agent at it so investigations are fast and deterministic. Its
next step depends only on the conversation it is sent, never on earlier requests,
so it answers a resumed conversation after a crash exactly as before:

    recent_inference → query_influx → system1_cause (when offered) → report_finding → "Done."

GET /stats returns {"requests": n}: the crash test checks a repeated alert costs no
model call. Standard library only; runs in python:3.13-slim.
"""

from __future__ import annotations

import json
import re
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

REQUESTS = 0


def next_step(messages: list[dict], offered: set[str]) -> dict:
    """The assistant message to answer with: tool calls, or the final text."""
    called = {}
    for m in messages:
        for call in m.get("tool_calls") or []:
            called[call["id"]] = call["function"]["name"]
    done = {called.get(m.get("tool_call_id")) for m in messages if m.get("role") == "tool"}
    prompt = " ".join(str(m.get("content")) for m in messages if m.get("role") == "user")
    found = re.search(r"device ([a-z0-9_-]+)", prompt)
    device = found.group(1) if found else "unknown"
    plan = [
        ("recent_inference", {"device": device, "limit": 5}),
        ("query_influx", {"device": device, "measurement": "sensor_data", "sensor": "vibration",
                          "minutes": 30, "every": "1m", "fn": "max"}),
        ("system1_cause", {}),
        ("report_finding", {"cause": "bearing wear", "confidence": 0.8,
                            "summary": f"{device}: vibration rose well above its baseline before the alert."}),
    ]
    for name, args in plan:
        if name in offered and name not in done:
            return {"tool": name, "arguments": args}
    return {"text": "Done."}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_args):
        pass

    def send(self, status: int, body: bytes, content_type: str = "application/json") -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == "/stats":
            self.send(200, json.dumps({"requests": REQUESTS}).encode())
        elif self.path in ("/", "/api/version"):
            self.send(200, b'{"version": "fake"}')
        else:
            self.send(404, b"{}")

    def do_POST(self):
        global REQUESTS
        if not self.path.endswith("/chat/completions"):
            return self.send(404, b"{}")
        request = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))))
        REQUESTS += 1
        offered = {t["function"]["name"] for t in request.get("tools") or []}
        step = next_step(request.get("messages", []), offered)
        model = request.get("model", "fake")
        created = int(time.time())
        if "tool" in step:
            delta = {"role": "assistant", "content": None, "tool_calls": [{
                "index": 0, "id": f"call_{REQUESTS}", "type": "function",
                "function": {"name": step["tool"], "arguments": json.dumps(step["arguments"])},
            }]}
            finish = "tool_calls"
        else:
            delta = {"role": "assistant", "content": step["text"]}
            finish = "stop"
        usage = {"prompt_tokens": 100, "completion_tokens": 10, "total_tokens": 110}
        if not request.get("stream"):
            message = {k: v for k, v in delta.items() if k != "tool_calls"}
            if "tool_calls" in delta:
                message["tool_calls"] = [{k: v for k, v in c.items() if k != "index"} for c in delta["tool_calls"]]
            body = {"id": f"fake-{REQUESTS}", "object": "chat.completion", "created": created, "model": model,
                    "choices": [{"index": 0, "message": message, "finish_reason": finish}], "usage": usage}
            return self.send(200, json.dumps(body).encode())
        chunks = [
            {"choices": [{"index": 0, "delta": delta, "finish_reason": None}]},
            {"choices": [{"index": 0, "delta": {}, "finish_reason": finish}]},
            {"choices": [], "usage": usage},
        ]
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.end_headers()
        for chunk in chunks:
            chunk.update({"id": f"fake-{REQUESTS}", "object": "chat.completion.chunk", "created": created, "model": model})
            self.wfile.write(f"data: {json.dumps(chunk)}\n\n".encode())
        self.wfile.write(b"data: [DONE]\n\n")


if __name__ == "__main__":
    ThreadingHTTPServer(("0.0.0.0", 11434), Handler).serve_forever()
