"""
Offline checks for ai/agent/local_tools.py: the traffic tools run against a
real ingest service (iot/ingest) in this process, loaded with the demo
simulator's last week. No Docker, no model.

    python3 tests/test_local_tools.py
"""

from __future__ import annotations

import importlib.util
import json
import os
import sys
import threading
from datetime import datetime, timedelta
from http.server import ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.dont_write_bytecode = True
sys.path[:0] = [str(ROOT / "iot/ingest"), str(ROOT / "iot/scripts"), str(ROOT / "ai/agent")]
import agent  # noqa: E402
import contract  # noqa: E402
import ingest  # noqa: E402
import simulate  # noqa: E402
from store import Store  # noqa: E402

failed = False


def check(label: str, ok: bool, detail: object = "") -> None:
    global failed
    print(f"  {'ok  ' if ok else 'FAIL'} {label}" + ("" if ok else f": {str(detail)[:300]}"))
    failed |= not ok


class Off:
    """A sink that is switched off."""

    enabled = False
    url = host = ""

    def write(self, lines):
        pass

    def publish(self, messages):
        pass


# The ingest service with the demo road's last 7 days and today so far
store = Store(":memory:", simulate.TZ, speed_limit=50)
service = ingest.Service(store, {"demo-token": simulate.SITE}, "read-token", influx=Off(), mqtt=Off())
now = datetime.now(simulate.TZ)
for back in range(7, -1, -1):
    plan = simulate.plan_day(now.date() - timedelta(days=back))
    docs = [doc for t, doc in plan["reads"] if t <= now.timestamp()]
    for i in range(0, len(docs), 1000):
        status, result = service.ingest("/api/v1/reads", docs[i : i + 1000], simulate.SITE)
        assert status == 200 and not result["rejected"], result
service.ingest("/api/v1/heartbeat", {
    "schema": contract.HEARTBEAT, "site_id": simulate.SITE, "timestamp": now.isoformat(),
    "device": {"hostname": "jetson-test"}, "metrics": {"temperature_gpu_c": 51.5}, "pipeline": {"active_cameras": 2},
    "status": "healthy",
}, simulate.SITE)

server = ThreadingHTTPServer(("127.0.0.1", 0), ingest.handler(service))
threading.Thread(target=server.serve_forever, daemon=True).start()
os.environ["INGEST_URL"] = f"http://127.0.0.1:{server.server_port}"
os.environ["INGEST_READ_TOKEN"] = "read-token"

spec = importlib.util.spec_from_file_location("local_tools", ROOT / "ai/agent/local_tools.py")
local_tools = importlib.util.module_from_spec(spec)
spec.loader.exec_module(local_tools)
DEFINITIONS = {d["function"]["name"]: d for d, _ in local_tools.tools(agent)}
TOOLS = {d["function"]["name"]: fn for d, fn in local_tools.tools(agent)}


def call(name: str, args: dict) -> dict:
    """Through agent.run_tool, as a model's tool call would go."""
    agent.TOOL_FUNCTIONS[name] = TOOLS[name]
    _, result = agent.run_tool({"function": {"name": name, "arguments": args}})
    return json.loads(result)


print("definitions")
check("six traffic tools", sorted(TOOLS) == sorted(
    ["get_traffic_summary", "get_traffic_by_hour", "find_plate", "get_frequent_plates", "get_camera_checks",
     "get_device_status"]))
check("no clash with agent.py's tools", not set(TOOLS) & {"list_sensors", "get_stats", "get_history"})
check("the prompt forbids guessing who drove", "never guess who owns or drove" in local_tools.PROMPT)
check("find_plate defaults to every read kept", "every read still kept" in json.dumps(DEFINITIONS["find_plate"]))

print("tools against the ingest service")
r = call("get_traffic_summary", {"period": "last_week"})
check("summary: says which days it covered", r["period"]["name"] == "last_week", r.get("period"))
r = call("get_traffic_summary", {"period": "7d"})
check("summary: the last 7 days have vehicles in both directions",
      r.get("vehicles", 0) > 0 and set(r.get("by_direction", {})) == {"inbound", "outbound"}, r)
check("summary: marks demo data", r["synthetic_data"] is True, r)
check("summary: cars are the most common vehicle", next(iter(r["by_vehicle_type"])) == "car", r["by_vehicle_type"])
check("summary: speeds over the limit are counted", r["speed"]["limit_kmh"] == 50 and r["speed"]["over_limit"] > 0, r["speed"])

r = call("get_traffic_by_hour", {"period": "7d", "direction": "inbound"})
hours = {h["hour"]: h["vehicles"] for h in r["by_hour"]}
check("by hour: inbound peaks in the morning", max(hours, key=hours.get) in ("07:00", "08:00"), hours)
check("by hour: empty hours are left out", all(v > 0 for v in hours.values()), hours)
r = call("get_traffic_by_hour", {"period": "7d", "direction": "outbound"})
hours = {h["hour"]: h["vehicles"] for h in r["by_hour"]}
check("by hour: outbound peaks in the evening", max(hours, key=hours.get) in ("17:00", "18:00"), hours)

r = call("get_frequent_plates", {"period": "7d", "limit": 4})
top = [p["plate"] for p in r["plates"]]
check("frequent: the buses come first", set(top[:3]) == {"MTB1101", "MTB1102", "MTB1103"}, top)
check("frequent: then the delivery van", top[3] == "KDL4821", top)

r = call("find_plate", {"plate": "kdl 4821"})
check("plate: found however it's written", r["plate"] == "KDL4821" and r["passages_total"] > 0, r)
check("plate: at most 20 passages for the model", r["passages_shown"] <= 20, r["passages_shown"])
check("plate: default period is every read kept", r["period"]["name"] == "30d", r["period"])
check("plate: each passage says when, which way and which camera",
      all({"time", "direction", "camera_id"} <= set(p) for p in r["passages"]), r["passages"][:2])

r = call("get_camera_checks", {})
fired = {(c["check"], c.get("evidence", {}).get("camera_id")) for c in r["checks"]}
check("checks: the outbound camera struggles at night (C2)", ("C2 night", "cam-out-01") in fired, r["checks"])
check("checks: the inbound camera is fine at night", ("C2 night", "cam-in-01") not in fired, fired)

r = call("get_device_status", {})
check("device: online with its metrics", r["nodes"] and r["nodes"][0]["online"] and r["nodes"][0]["metrics"]["temperature_gpu_c"] == 51.5, r)

print("errors the model can fix")
r = call("get_traffic_summary", {"period": "fortnight"})
check("an unknown period is a tool error naming the choices", "error" in r and "last_week" in r["error"], r)
r = call("get_traffic_by_hour", {"direction": "north"})
check("an unknown direction is a tool error", "error" in r and "inbound" in r["error"], r)
r = call("find_plate", {})
check("a missing plate is a tool error", "error" in r and "plate" in r["error"], r)
r = call("get_frequent_plates", {"limit": "many"})
check("a bad limit is a tool error", "error" in r, r)

os.environ["INGEST_READ_TOKEN"] = "wrong"
local_tools.READ_TOKEN = "wrong"
r = call("get_traffic_summary", {})
check("a wrong read token reports a failure, not data", "error" in r and "401" in r["error"], r)

server.shutdown()
sys.exit(1 if failed else 0)
