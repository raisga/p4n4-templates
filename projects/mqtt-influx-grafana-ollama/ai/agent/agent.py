"""
Telemetry agent: an Ollama-compatible endpoint whose model can query InfluxDB.

p4n4-dashboard's Agent tab speaks the Ollama API, and so does this server.
POST /api/chat gets a system prompt and the InfluxDB tools below; the server
runs the tool calls the model makes and streams its answer back in Ollama's
format. Every other request goes to Ollama unchanged.

The model never writes Flux. Tools take checked arguments and build the
queries here, and the InfluxDB token stays in this container.

A project adds its own tools in local_tools.py next to this file (see
load_local_tools), so this file stays as the template ships it.

Standard library only, so it runs on the stock python image, offline.

    OLLAMA_URL       Ollama to forward to (default http://ollama:11434)
    INFLUXDB_URL     InfluxDB 2 (default http://influxdb:8086)
    INFLUXDB_TOKEN   Token with read access to INFLUXDB_BUCKET
    INFLUXDB_ORG     Organization
    INFLUXDB_BUCKET  Bucket the iot layer writes sensor_data to
    AGENT_THINK      true or false: whether thinking models think before
                     answering, for requests that don't say (empty: the
                     model's default)
    AGENT_PORT       Port to listen on (default 11434, Ollama's)
"""

from __future__ import annotations

import csv
import importlib.util
import io
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://ollama:11434").rstrip("/")
INFLUXDB_URL = os.environ.get("INFLUXDB_URL", "http://influxdb:8086").rstrip("/")
INFLUXDB_TOKEN = os.environ.get("INFLUXDB_TOKEN", "")
INFLUXDB_ORG = os.environ.get("INFLUXDB_ORG", "")
INFLUXDB_BUCKET = os.environ.get("INFLUXDB_BUCKET", "raw_telemetry")
THINK = {"true": True, "false": False}.get(os.environ.get("AGENT_THINK", "").strip().lower())
PORT = int(os.environ.get("AGENT_PORT", "11434"))

# The iot layer's schema: measurement sensor_data, tags device and sensor
MEASUREMENT = "sensor_data"
# Tool rounds per question before the model must answer with what it has
MAX_TOOL_ROUNDS = 5
# Rows a tool returns at most, so results fit a small model's context
MAX_ROWS = 100
MAX_POINTS = 48
# Device and sensor ids are MQTT topic levels
ID = re.compile(r"^[^\s\"\\#+/]{1,64}$")
DURATION = re.compile(r"^(\d{1,4})(m|h|d|w)$")
SECONDS = {"m": 60, "h": 3600, "d": 86400, "w": 604800}
MAX_RANGE = 366 * 86400

SYSTEM_PROMPT = """\
You are the assistant of a p4n4 telemetry project. Devices publish sensor \
readings over MQTT, and every reading is stored in InfluxDB with a device id \
(e.g. greenhouse-1) and a sensor id (e.g. temperature).

Answer questions about devices, sensors and readings with the tools. Never \
ask the user for device or sensor ids: call list_sensors to find them, then \
the other tools for history and statistics. Never make up values: if a tool \
returns nothing, say there is no data. Times are UTC; it is now {now}. Answer \
briefly, with units when the data has them."""

RANGE_PARAM = {
    "type": "string",
    "description": "How far back to look: a number and m, h, d or w, e.g. 15m, 1h, 24h, 7d. Default 24h.",
}
DEVICE_PARAM = {"type": "string", "description": "Device id, e.g. greenhouse-1. Omit for every device."}
SENSOR_PARAM = {"type": "string", "description": "Sensor id, e.g. temperature, as list_sensors returns it."}

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "list_sensors",
            "description": "Latest reading of every sensor on every device (or on one device), with unit and time. Use it for current values, to compare devices, and to find the device and sensor ids the other tools take.",
            "parameters": {"type": "object", "properties": {"range": RANGE_PARAM, "device": DEVICE_PARAM}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_stats",
            "description": "Minimum, maximum, mean, number of readings, first and last value of one sensor over a time range, per device.",
            "parameters": {
                "type": "object",
                "properties": {"sensor": SENSOR_PARAM, "device": DEVICE_PARAM, "range": RANGE_PARAM},
                "required": ["sensor"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_history",
            "description": "One sensor's values over a time range, averaged into evenly spaced points, per device. Use it for trends and changes over time.",
            "parameters": {
                "type": "object",
                "properties": {
                    "sensor": SENSOR_PARAM,
                    "device": DEVICE_PARAM,
                    "range": RANGE_PARAM,
                    "points": {"type": "integer", "description": f"Number of points, 2 to {MAX_POINTS}. Default 12."},
                },
                "required": ["sensor"],
            },
        },
    },
]


def log(message: str) -> None:
    print(message, file=sys.stderr, flush=True)


class ToolError(Exception):
    """A problem the model can fix by calling the tool differently."""


# ------------------------------------------------------------------------------
# InfluxDB
# ------------------------------------------------------------------------------


def flux_string(value: str) -> str:
    return json.dumps(value)  # Flux string literals share JSON's escapes


def parse_range(value: object) -> int:
    """A range argument (e.g. 24h) in seconds."""
    text = str(value or "24h").strip().lower().lstrip("-")
    match = DURATION.match(text)
    if not match or int(match[1]) == 0:
        raise ToolError(f"range must look like 15m, 1h, 24h or 7d, not {value!r}")
    return min(int(match[1]) * SECONDS[match[2]], MAX_RANGE)


def parse_id(name: str, value: object, required: bool = False) -> str | None:
    if value in (None, ""):
        if required:
            raise ToolError(f"{name} is required")
        return None
    if not isinstance(value, str) or not ID.match(value):
        raise ToolError(f"{name} {value!r} is not a valid id")
    return value


def readings(seconds: int, field: str, sensor: str | None = None, device: str | None = None) -> str:
    """Flux selecting one field of sensor_data in the last `seconds`."""
    flux = (
        f"from(bucket: {flux_string(INFLUXDB_BUCKET)})\n"
        f"  |> range(start: -{seconds}s)\n"
        f"  |> filter(fn: (r) => r._measurement == {flux_string(MEASUREMENT)} and r._field == {flux_string(field)})\n"
    )
    if sensor:
        flux += f"  |> filter(fn: (r) => r.sensor == {flux_string(sensor)})\n"
    if device:
        flux += f"  |> filter(fn: (r) => r.device == {flux_string(device)})\n"
    return flux


def query(flux: str) -> list[dict[str, str]]:
    """Run a Flux query; rows as dicts of CSV columns."""
    body = json.dumps({"query": flux, "type": "flux", "dialect": {"header": True, "annotations": []}})
    req = urllib.request.Request(
        f"{INFLUXDB_URL}/api/v2/query?org={urllib.parse.quote(INFLUXDB_ORG)}",
        data=body.encode(),
        headers={
            "Authorization": f"Token {INFLUXDB_TOKEN}",
            "Content-Type": "application/json",
            "Accept": "application/csv",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as res:
            text = res.read().decode()
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode(errors="replace")
        raise RuntimeError(f"InfluxDB answered {exc.code}: {detail}") from exc
    rows, header = [], None
    # Tables are separated by blank lines, each with its own header
    for row in csv.reader(io.StringIO(text)):
        if not any(row):
            header = None
        elif header is None:
            header = row
        else:
            rows.append(dict(zip(header, row)))
    return rows


def iso(value: str) -> str:
    """InfluxDB's RFC 3339 time, to the second."""
    return value[:19] + "Z" if value else value


def number(value: str) -> float | int | str:
    try:
        num = float(value)
    except ValueError:
        return value
    return int(num) if num.is_integer() else round(num, 3)


def capped(items: list) -> dict:
    result: dict = {"rows": items[:MAX_ROWS]}
    if len(items) > MAX_ROWS:
        result["truncated"] = f"showing {MAX_ROWS} of {len(items)}; narrow the device or sensor"
    return result


def list_sensors(args: dict) -> dict:
    seconds = parse_range(args.get("range"))
    device = parse_id("device", args.get("device"))
    latest = query(readings(seconds, "value", device=device) + "  |> last()")
    units = {
        (r["device"], r["sensor"]): r["_value"]
        for r in query(readings(seconds, "unit", device=device) + "  |> last()")
    }
    items = [
        {
            "device": r["device"],
            "sensor": r["sensor"],
            "latest": number(r["_value"]),
            **({"unit": units[(r["device"], r["sensor"])]} if (r["device"], r["sensor"]) in units else {}),
            "time": iso(r["_time"]),
        }
        for r in sorted(latest, key=lambda r: (r["device"], r["sensor"]))
    ]
    return capped(items)


def get_stats(args: dict) -> dict:
    seconds = parse_range(args.get("range"))
    sensor = parse_id("sensor", args.get("sensor"), required=True)
    device = parse_id("device", args.get("device"))
    data = readings(seconds, "value", sensor, device)
    stats = ("min", "max", "mean", "count", "first", "last")
    flux = (
        f"data = {data}\n"
        "union(tables: [\n"
        + ",\n".join(
            f'  data |> {fn}() |> toFloat() |> set(key: "stat", value: "{fn}")'
            if fn == "count"
            else f'  data |> {fn}() |> set(key: "stat", value: "{fn}")'
            for fn in stats
        )
        + '\n])\n  |> keep(columns: ["device", "sensor", "stat", "_value"])'
    )
    by_device: dict[str, dict] = {}
    for r in query(flux):
        entry = by_device.setdefault(r["device"], {"device": r["device"], "sensor": r["sensor"]})
        entry[r["stat"]] = number(r["_value"])
    items = [
        {**{k: e[k] for k in ("device", "sensor")}, **{s: e[s] for s in stats if s in e}}
        for _, e in sorted(by_device.items())
    ]
    return {**capped(items), "range": f"last {args.get('range') or '24h'}"}


def get_history(args: dict) -> dict:
    seconds = parse_range(args.get("range"))
    sensor = parse_id("sensor", args.get("sensor"), required=True)
    device = parse_id("device", args.get("device"))
    try:
        points = int(args.get("points") or 12)
    except (TypeError, ValueError):
        raise ToolError("points must be a whole number") from None
    points = max(2, min(points, MAX_POINTS))
    every = max(seconds // points, 1)
    flux = readings(seconds, "value", sensor, device) + (
        f"  |> aggregateWindow(every: {every}s, fn: mean, createEmpty: false)\n"
        '  |> keep(columns: ["device", "sensor", "_time", "_value"])'
    )
    series: dict[str, list] = {}
    for r in query(flux):
        series.setdefault(r["device"], []).append([iso(r["_time"]), number(r["_value"])])
    items = [
        {"device": d, "sensor": sensor, "points": pts[-MAX_POINTS:]} for d, pts in sorted(series.items())
    ]
    return {**capped(items), "point_spacing_seconds": every}


TOOL_FUNCTIONS = {"list_sensors": list_sensors, "get_stats": get_stats, "get_history": get_history}
# Appended to SYSTEM_PROMPT by local_tools.py's PROMPT
LOCAL_PROMPT = ""


def load_local_tools() -> None:
    """
    Add a project's own tools from local_tools.py next to this file, if any.

    The module defines tools(agent): it gets this module, for query(),
    readings(), parse_id(), parse_range(), flux_string(), number(), iso(),
    capped(), ToolError and the INFLUXDB_* settings, and returns a list of
    (Ollama tool definition, function) pairs. A function takes the call's
    arguments (a dict) and returns a JSON-serializable dict; raising ToolError
    tells the model what to fix. An optional PROMPT string is appended to the
    system prompt, e.g. to say when to use the tools.
    """
    global LOCAL_PROMPT
    path = Path(__file__).with_name("local_tools.py")
    if not path.exists():
        return
    spec = importlib.util.spec_from_file_location("local_tools", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    names = []
    for definition, function in module.tools(sys.modules[__name__]):
        name = definition["function"]["name"]
        if name in TOOL_FUNCTIONS:
            raise SystemExit(f"local_tools.py: tool {name!r} is already defined")
        TOOLS.append(definition)
        TOOL_FUNCTIONS[name] = function
        names.append(name)
    LOCAL_PROMPT = getattr(module, "PROMPT", "").strip()
    log(f"local tools from {path.name}: {', '.join(names) or 'none'}")


def run_tool(call: dict) -> tuple[str, str]:
    """Run one Ollama tool call; returns (tool name, JSON result for the model)."""
    fn = call.get("function") or {}
    name, args = fn.get("name", ""), fn.get("arguments") or {}
    if isinstance(args, str):
        try:
            args = json.loads(args)
        except json.JSONDecodeError:
            args = {}
    started = time.monotonic()
    try:
        if name not in TOOL_FUNCTIONS:
            raise ToolError(f"unknown tool {name!r}; use one of {', '.join(TOOL_FUNCTIONS)}")
        result = TOOL_FUNCTIONS[name](args if isinstance(args, dict) else {})
    except ToolError as exc:
        result = {"error": str(exc)}
    except Exception as exc:  # InfluxDB down, bad token, …: tell the model, keep serving
        log(f"tool {name} failed: {exc}")
        result = {"error": f"the query failed: {exc}"}
    log(f"tool {name} {json.dumps(args, ensure_ascii=False)} -> {len(result.get('rows', []))} rows "
        f"in {time.monotonic() - started:.2f}s")
    # Unescaped, so the model reads "°C" and "Señal", not "\u00b0C", and doesn't copy escapes into its answer
    return name, json.dumps(result, ensure_ascii=False)


# ------------------------------------------------------------------------------
# Ollama
# ------------------------------------------------------------------------------


class OllamaError(Exception):
    pass


def ollama_chat(request: dict):
    """POST /api/chat with streaming; yields each NDJSON chunk."""
    req = urllib.request.Request(
        f"{OLLAMA_URL}/api/chat",
        data=json.dumps({**request, "stream": True}).encode(),
        headers={"Content-Type": "application/json"},
    )
    try:
        res = urllib.request.urlopen(req, timeout=600)
    except urllib.error.HTTPError as exc:
        body = exc.read().decode(errors="replace")
        try:
            message = json.loads(body).get("error", body)
        except json.JSONDecodeError:
            message = body
        raise OllamaError(message) from exc
    except urllib.error.URLError as exc:
        raise OllamaError(f"Ollama is unreachable at {OLLAMA_URL}: {exc.reason}") from exc
    with res:
        for line in res:
            if line.strip():
                chunk = json.loads(line)
                if "error" in chunk:
                    raise OllamaError(chunk["error"])
                yield chunk


def with_system_prompt(messages: list[dict]) -> list[dict]:
    if any(m.get("role") == "system" for m in messages):
        return list(messages)
    now = datetime.now(UTC).strftime("%Y-%m-%d %H:%M UTC")
    prompt = SYSTEM_PROMPT.format(now=now) + (f"\n\n{LOCAL_PROMPT}" if LOCAL_PROMPT else "")
    return [{"role": "system", "content": prompt}, *messages]


def agent_chat(body: dict, emit) -> dict:
    """
    Answer a chat request, running tool calls until the model replies in text.
    `emit` receives the chunks to stream; returns the final chunk with the
    whole reply as its message, for non-streaming requests.
    """
    messages = with_system_prompt(body.get("messages") or [])
    request = {k: v for k, v in body.items() if k not in ("messages", "tools", "stream")}
    if THINK is not None and "think" not in request:
        request["think"] = THINK
    tools = TOOLS
    reply = ""
    for round_ in range(MAX_TOOL_ROUNDS + 1):
        # Out of rounds: no tools, so the model answers with what it has
        offer = tools if round_ < MAX_TOOL_ROUNDS else None
        calls, text, last = [], "", {}
        try:
            for chunk in ollama_chat({**request, "messages": messages, **({"tools": offer} if offer else {})}):
                message = chunk.get("message") or {}
                calls += message.get("tool_calls") or []
                text += message.get("content") or ""
                if not chunk.get("done") and (message.get("content") or message.get("thinking")):
                    emit(chunk)
                last = chunk
        except OllamaError as exc:
            if offer and "does not support tools" in str(exc):
                log(f"{body.get('model')} does not support tools; chatting without them")
                tools = None
                continue
            if "think" in request and "does not support thinking" in str(exc):
                request.pop("think")
                continue
            raise
        reply += text
        if not calls:
            final = {**last, "message": {**(last.get("message") or {}), "role": "assistant"}}
            emit(final)
            return {**final, "message": {"role": "assistant", "content": reply}}
        messages.append({"role": "assistant", "content": text, "tool_calls": calls})
        for call in calls:
            name, result = run_tool(call)
            messages.append({"role": "tool", "tool_name": name, "content": result})
        if text.strip():
            emit({**last, "done": False, "message": {"role": "assistant", "content": "\n\n"}})
            reply += "\n\n"
    raise OllamaError("no reply")  # unreachable: the last round has no tools


# ------------------------------------------------------------------------------
# HTTP
# ------------------------------------------------------------------------------


class Handler(BaseHTTPRequestHandler):
    # HTTP/1.0: each response ends by closing the connection, so streamed
    # responses need neither a length nor chunked encoding
    protocol_version = "HTTP/1.0"
    server_version = "p4n4-agent"

    def log_message(self, format: str, *args) -> None:
        pass  # tool calls are logged instead

    def _body(self) -> bytes:
        length = int(self.headers.get("Content-Length") or 0)
        return self.rfile.read(length) if length else b""

    def do_POST(self) -> None:
        if self.path.split("?")[0] == "/api/chat":
            self._chat()
        else:
            self._forward()

    def _forward(self) -> None:
        """Pass the request to Ollama and stream its response back."""
        req = urllib.request.Request(
            OLLAMA_URL + self.path,
            data=self._body() or None,
            method=self.command,
            headers={k: v for k, v in self.headers.items() if k.lower() in ("content-type", "accept")},
        )
        try:
            res = urllib.request.urlopen(req, timeout=600)
        except urllib.error.HTTPError as exc:
            res = exc
        except urllib.error.URLError as exc:
            return self._json(502, {"error": f"Ollama is unreachable at {OLLAMA_URL}: {exc.reason}"})
        with res:
            self.send_response(res.status)
            for key in ("Content-Type", "Content-Length"):
                if res.headers.get(key):
                    self.send_header(key, res.headers[key])
            self.end_headers()
            if self.command == "HEAD":
                return
            try:
                while chunk := res.read1(65536):
                    self.wfile.write(chunk)
                    self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError):
                pass

    do_GET = do_DELETE = do_HEAD = _forward

    def _chat(self) -> None:
        try:
            body = json.loads(self._body() or b"{}")
        except json.JSONDecodeError:
            return self._json(400, {"error": "invalid JSON"})
        if body.get("stream", True):
            self.send_response(200)
            self.send_header("Content-Type", "application/x-ndjson")
            self.end_headers()

            def emit(chunk: dict) -> None:
                self.wfile.write(json.dumps(chunk).encode() + b"\n")
                self.wfile.flush()

            try:
                agent_chat(body, emit)
            except OllamaError as exc:
                emit({"error": str(exc)})
            except (BrokenPipeError, ConnectionResetError):
                pass  # the user pressed stop
        else:
            try:
                self._json(200, agent_chat(body, lambda chunk: None))
            except OllamaError as exc:
                self._json(500, {"error": str(exc)})

    def _json(self, status: int, data: dict) -> None:
        payload = json.dumps(data).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


def main() -> None:
    if not INFLUXDB_TOKEN:
        log("INFLUXDB_TOKEN is empty: tools will fail until it is set")
    load_local_tools()
    server = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    server.daemon_threads = True
    log(f"p4n4 agent on :{PORT}: Ollama {OLLAMA_URL}, InfluxDB {INFLUXDB_URL} bucket {INFLUXDB_BUCKET}")
    server.serve_forever()


if __name__ == "__main__":
    main()
