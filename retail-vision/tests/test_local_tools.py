"""
Offline checks for ai/agent/local_tools.py: the store tools run against a
real ingest service (iot/ingest) in this process, loaded with the demo
simulator's last week. No Docker, no model.

    python3 tests/test_local_tools.py
    STORE_LANG=es python3 tests/test_local_tools.py   # the Spanish demo store
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


# The demo store's shelves: [1] is the busiest, [2] draws interest but rarely
# sells, [4] is high value with little traffic (simulate.SHELF_PLAN)
SHELF = [shelf[0] for shelf in simulate.SHELVES]
SHIRT, WHITE = simulate.SHELVES[0][5][0], simulate.COLORS[0][0]

# The ingest service with the demo store's last 7 days and today so far
store = Store(":memory:", simulate.TZ, simulate.LANG)
service = ingest.Service(store, {"demo-token": simulate.STORE}, "read-token", influx=Off(), mqtt=Off())
now = datetime.now(simulate.TZ)
for back in range(7, -1, -1):
    plan = simulate.plan_day(now.date() - timedelta(days=back))
    pick = lambda items: [doc for t, doc in items if t <= now.timestamp()]  # noqa: E731
    for path, docs in (("/api/v1/events", pick(plan["events"])), ("/api/v1/alerts", pick(plan["alerts"])),
                       ("/api/v1/pos", pick(plan["sales"]))):
        status, result = service.ingest(path, docs, simulate.STORE)
        assert status == 200 and not result["rejected"], result
    for t, alert_id, status in plan["reviews"]:
        if t <= now.timestamp():
            store.review_alert(simulate.STORE, alert_id, status, "test")
service.ingest("/api/v1/heartbeat", {
    "schema": "aioros.boutique.heartbeat/0.1", "store_id": simulate.STORE, "timestamp": now.isoformat(),
    "device": {"hostname": "jetson-test"}, "metrics": {"temperature_gpu_c": 51.5}, "pipeline": {"active_cameras": 1},
    "alerts_pending": 2, "status": "healthy",
}, simulate.STORE)

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
check("six store tools", sorted(TOOLS) == sorted(
    ["get_store_summary", "get_alerts", "get_shelves", "get_garment_ranking", "get_advice", "get_device_status"]))
check("no clash with agent.py's tools", not set(TOOLS) & {"list_sensors", "get_stats", "get_history"})
check("the prompt forbids accusatory words", "robo" in local_tools.PROMPT and "riesgo de pérdida" in local_tools.PROMPT)

print("tools against the ingest service")
r = call("get_store_summary", {"period": "last_week"})
check("summary: says which days it covered", r["period"]["name"] == "last_week", r.get("period"))
# The whole backfilled week (last_week may hold one day of it, on a Monday)
r = call("get_store_summary", {"period": "7d"})
check("summary: the last 7 days have visitors and a peak hour", r.get("visitors", 0) > 0 and r.get("peak_hour"), r)
check("summary: marks demo data", r["synthetic_data"] is True, r)
check("summary: peak hour is the demo's evening", r["peak_hour"]["hour"] in ("18:00 - 19:00", "17:00 - 18:00", "19:00 - 20:00"), r["peak_hour"])

r = call("get_alerts", {"period": "7d", "type": "A2", "limit": 3})
check("alerts: A2 only", set(r["by_type"]) == {"A2 high_value_opportunity"}, r.get("by_type"))
check("alerts: lists at most the limit", len(r["items"]) <= 3 and r["total"] >= len(r["items"]), r)
check("alerts: items are trimmed for the model", r["items"] and "clip_uri" not in r["items"][0] and "code" in r["items"][0], r["items"][:1])
check("alerts: per-day counts carry the weekday", all(" " in d["day"] for d in r["by_day"]), r["by_day"])
r = call("get_alerts", {"period": "tuesday"})
crossed = r.get("by_type_and_status", {})
check("alerts: type × status counts cover every alert",
      sum(sum(v.values()) for v in crossed.values()) == r["total"] and "A2 high_value_opportunity" in crossed, crossed)
check("alerts: the model can't narrow by status", "status" not in DEFINITIONS["get_alerts"]["function"]["parameters"]["properties"])

r = call("get_shelves", {"period": "7d"})
check("shelves: the demo's busiest shelf", r.get("most_interest") == SHELF[1], r.get("most_interest"))
check("shelves: the shelf with interest but few sales", r.get("interest_without_sales") == SHELF[2], r.get("interest_without_sales"))

r = call("get_garment_ranking", {"period": "7d", "category": SHIRT + "s", "color": WHITE.upper()})
check("ranking: white shirts, by plural and any case", [(x["garment_type"], x["color"]) for x in r["ranking"]] == [(SHIRT, WHITE)], r)

r = call("get_advice", {})
rules = {a["rule"] for a in r["advice"]}
check("advice: moves the high-value, quiet shelf (R2)", any(a["rule"] == "R2 reubicar" and SHELF[4] in a["issue"] for a in r["advice"]), rules)
check("advice: reviews the display that doesn't sell (R3)", any(a["rule"] == "R3 exhibicion" and SHELF[2] in a["issue"] for a in r["advice"]), rules)

r = call("get_device_status", {})
check("device: online with its metrics", r["nodes"] and r["nodes"][0]["online"] and r["nodes"][0]["metrics"]["temperature_gpu_c"] == 51.5, r)

print("errors the model can fix")
r = call("get_store_summary", {"period": "fortnight"})
check("an unknown period is a tool error naming the choices", "error" in r and "last_week" in r["error"], r)
r = call("get_alerts", {"type": "A9"})
check("an unknown alert type is a tool error", "error" in r and "A1-A4" in r["error"], r)
r = call("get_alerts", {"limit": "many"})
check("a bad limit is a tool error", "error" in r, r)

os.environ["INGEST_READ_TOKEN"] = "wrong"
local_tools.READ_TOKEN = "wrong"
r = call("get_store_summary", {})
check("a wrong read token reports a failure, not data", "error" in r and "401" in r["error"], r)

server.shutdown()
sys.exit(1 if failed else 0)
