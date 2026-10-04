"""
Drive the agent's tool loop against tests/fake_ollama.py and the smoke test's
InfluxDB. smoke.sh pipes this into the agent container:

    docker compose exec -T agent python - < tests/check_agent.py

Each chat sends a JSON tool call as the user message; the fake model makes
that call and replies with the tool's result, so the checks below see exactly
what the real model would get from InfluxDB.
"""

from __future__ import annotations

import json
import sys
import urllib.request

URL = "http://localhost:11434"
failed = False


def chat(name: str, arguments: dict, stream: bool = True) -> tuple[str, list[dict]]:
    body = {
        "model": "fake:latest",
        "stream": stream,
        "messages": [{"role": "user", "content": json.dumps({"name": name, "arguments": arguments})}],
    }
    req = urllib.request.Request(f"{URL}/api/chat", json.dumps(body).encode(), {"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=60) as res:
        lines = [json.loads(line) for line in res if line.strip()]
    return "".join((c.get("message") or {}).get("content", "") for c in lines), lines


def result(text: str) -> dict:
    """The tool result the fake model echoed back."""
    return json.loads(text.split("result: ", 1)[1].rsplit("\nsystem prompt:", 1)[0])


def check(label: str, ok: bool, detail: object = "") -> None:
    global failed
    print(f"  {'ok  ' if ok else 'FAIL'} {label}" + ("" if ok else f": {detail}"))
    failed |= not ok


text, lines = chat("list_sensors", {"range": "1h"})
rows = result(text)["rows"]
temperature = next((r for r in rows if r["device"] == "smoke-01" and r["sensor"] == "temperature"), {})
check("list_sensors returns the latest reading with its unit", temperature.get("latest") == 21.5
      and temperature.get("unit") == "C", rows)
check("chat replies stream through, ending with one done chunk",
      len(lines) >= 3 and lines[-1].get("done") is True and not any(c.get("done") for c in lines[:-1]), lines)
check("the agent adds its system prompt", text.endswith("system prompt: yes"), text)

stats = result(chat("get_stats", {"sensor": "temperature", "device": "smoke-01", "range": "1h"})[0])["rows"]
check("get_stats returns min/max/mean/count", stats and stats[0].get("min") == 21.5
      and stats[0].get("max") == 21.5 and stats[0].get("count") == 1, stats)

history = result(chat("get_history", {"sensor": "humidity", "range": "1h", "points": 6})[0])["rows"]
check("get_history returns points", history and history[0]["points"]
      and history[0]["points"][-1][1] == 48, history)

# Tool results reach the model as text: non-ASCII must stay readable, not \u-escaped
text, _ = chat("café", {})
check("tool results keep non-ASCII text unescaped", "'café'" in text and "\\u00e9" not in text, text)

bad = result(chat("get_stats", {"sensor": 'x") |> drop(columns: ["_value"]', "range": "1h"})[0])
check("ids that aren't MQTT topic levels are rejected", "not a valid id" in bad.get("error", ""), bad)
bad = result(chat("list_sensors", {"range": "forever"})[0])
check("bad ranges are rejected", "range must look like" in bad.get("error", ""), bad)

text, lines = chat("list_sensors", {"range": "1h"}, stream=False)
check("non-streaming chat returns one reply", len(lines) == 1 and "smoke-01" in text, lines)

# smoke.sh adds this tool in ai/agent/local_tools.py
devices = result(chat("count_devices", {"range": "1h"})[0])
check("local_tools.py adds tools", "smoke-01" in devices.get("devices", []), devices)

with urllib.request.urlopen(f"{URL}/api/tags", timeout=10) as res:
    models = json.load(res)["models"]
check("other requests pass through to Ollama", models[0]["name"] == "fake:latest", models)

sys.exit(1 if failed else 0)
