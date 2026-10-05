"""A scripted stand-in for Ollama, for the smoke test: Letta talks to it instead
of a real model, so the test needs no download and its answers are fixed.

It serves what Letta's Ollama provider uses: /api/tags and /api/show (to list
a tool-capable chat model and an embedding model), and the OpenAI-compatible
/v1/chat/completions and /v1/embeddings. Each user message picks a script of
tool calls by keyword. The fake calls them one per turn, then replies with
every tool result, so the test can check what the tools returned.

    python3 fake_llm.py            # listens on :11434
"""

from __future__ import annotations

import json
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

CHAT, EMBED, DIM = "fake-chat:latest", "fake-embed:latest", 768

# (keyword in the user's message, [(tool, arguments), ...])
SCRIPTS = [
    ("how is pump-2", [
        ("equipment_trend", {"device": "pump-2", "days": 7}),
        ("maintenance_history", {"device": "pump-2"}),
    ]),
    ("anything wrong", [("equipment_status", {})]),
    ("replaced", [("log_maintenance", {
        "device": "pump-2", "work": "Replaced the drive-end bearing and re-greased both bearings.",
        "technician": "Ann Lee"})]),
    ("standby", [("memory_insert", {
        "label": "equipment", "new_string": "- pump-3: Standby process water pump, installed this week."})]),
    ("what was done", [("archival_memory_search", {"query": "recent work on pump-2", "tags": ["pump-2"]})]),
]


def text(content: object) -> str:
    if isinstance(content, list):
        return " ".join(part.get("text", "") for part in content if isinstance(part, dict))
    return content or ""


def complete(body: dict) -> dict:
    messages = body.get("messages", [])
    user = max((i for i, m in enumerate(messages) if m.get("role") == "user"), default=-1)
    request = text(messages[user]["content"]).lower() if user >= 0 else ""
    results = [text(m.get("content")) for m in messages[user + 1:] if m.get("role") == "tool"]
    available = {t["function"]["name"] for t in body.get("tools", [])}
    script = next((steps for keyword, steps in SCRIPTS if keyword in request), [])

    step = len(results)
    if step < len(script):
        name, arguments = script[step]
        if name not in available:
            return {"role": "assistant", "content": f"FAKE: tool {name} is not attached"}
        return {"role": "assistant", "content": None, "tool_calls": [{
            "id": f"call_{int(time.time() * 1000)}_{step}", "type": "function",
            "function": {"name": name, "arguments": json.dumps(arguments)}}]}
    return {"role": "assistant", "content": "FAKE REPLY\n" + "\n---\n".join(results) if results else "FAKE REPLY"}


class Handler(BaseHTTPRequestHandler):
    def reply(self, obj: object, status: int = 200) -> None:
        body = json.dumps(obj).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        if self.path.startswith("/api/tags"):
            return self.reply({"models": [{"name": CHAT}, {"name": EMBED}]})
        if self.path.startswith("/api/version"):
            return self.reply({"version": "0.35.1"})
        self.reply({"error": "not found"}, 404)

    def do_POST(self) -> None:
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
        if self.path == "/api/show":
            if body.get("name", body.get("model")) == EMBED:
                return self.reply({"capabilities": ["embedding"],
                                   "model_info": {"general.architecture": "bert", "bert.embedding_length": DIM}})
            return self.reply({"capabilities": ["completion", "tools"],
                               "model_info": {"general.architecture": "gemma3", "gemma3.context_length": 32768}})
        # Exact paths: Letta must reach Ollama's OpenAI-compatible API at /v1
        path = self.path.split("?")[0]
        if path == "/v1/embeddings":
            inputs = body["input"] if isinstance(body["input"], list) else [body["input"]]
            return self.reply({"object": "list", "model": body.get("model"), "usage": {"prompt_tokens": 1, "total_tokens": 1},
                               "data": [{"object": "embedding", "index": i, "embedding": [0.01] * DIM} for i in range(len(inputs))]})
        if path == "/v1/chat/completions":
            message = complete(body)
            print("chat:", f"reasoning_effort={body.get('reasoning_effort')}", json.dumps(message)[:300], flush=True)
            return self.reply({"id": "fake", "object": "chat.completion", "created": int(time.time()), "model": body.get("model"),
                               "choices": [{"index": 0, "message": message,
                                            "finish_reason": "tool_calls" if message.get("tool_calls") else "stop"}],
                               "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}})
        print("unknown POST", self.path, flush=True)
        self.reply({"error": "not found"}, 404)

    def log_message(self, *args) -> None:
        pass


if __name__ == "__main__":
    ThreadingHTTPServer(("", 11434), Handler).serve_forever()
