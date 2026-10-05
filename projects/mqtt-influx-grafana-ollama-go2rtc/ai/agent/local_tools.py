"""
mqtt-influx-grafana-ollama-go2rtc tools for the agent (agent.py loads this file at startup).

The site's numbers come from the ingest service in ../iot (its agent API),
which computes them from the plate reads the edge device posted. The model
only picks the tool and the period; it never adds anything up. Each result
says which days it covered and whether the data is synthetic (the demo road).

agent.py's own tools (list_sensors, get_stats, get_history) still work: the
only device writing sensor_data here is the site's edge device, whose
heartbeats the ingest service stores as readings (temperatures, CPU/GPU
use, memory, FPS, vehicles in the last minute).

    INGEST_URL         the ingest service (default http://ingest:8080)
    INGEST_READ_TOKEN  its read token (iot/.env)
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.parse
import urllib.request

INGEST_URL = os.environ.get("INGEST_URL", "http://ingest:8080").rstrip("/")
READ_TOKEN = os.environ.get("INGEST_READ_TOKEN", "")

PROMPT = """\
This project counts road traffic. Two cameras watch a road, one per \
direction (inbound and outbound), and an edge device reads the plates of the \
vehicles that pass. Questions are about the road unless they name the edge \
device: never ask the user whether they mean the road or a device, call the \
tools. "What happened" over a period means get_traffic_summary for that period. \
For vehicles, directions, vehicle types, peak hours, speeds and how many \
plates were read use get_traffic_summary or get_traffic_by_hour; for one \
plate use find_plate; for regular or frequent vehicles use get_frequent_plates; \
for whether the cameras work well use get_camera_checks. Their numbers are \
final: repeat them, never add up or estimate your own. They take a period \
(today, yesterday, this_week, last_week, 7d, 30d, or a weekday such as \
tuesday or martes for its latest occurrence) and use the site's local time, \
not UTC; each result says which days it covered. When a result has \
synthetic_data true, say the data is from the demo road.

Plates: write a plate exactly as the tool returns it. A plate read is a \
reading by a camera, not proof of who drove: never guess who owns or drove a \
vehicle, or why it was there. A camera misreads a character now and then: \
when find_plate finds nothing, or finds few passages, mention its \
similar_plates. Reads older than retention_days are deleted, so "not seen" \
only covers that time.

The only device in list_sensors, get_stats and get_history is the site's \
edge device: use get_device_status for whether it is online, and those tools \
for its temperatures, CPU/GPU use, memory and FPS over time."""

PERIOD = {
    "type": "string",
    "description": "today, yesterday, this_week, last_week, this_month, last_month, 7d, 30d, or a weekday "
                   "(monday … sunday, lunes … domingo) for its latest occurrence. Default 7d (the last 7 days, today included).",
}
FROM_TO = {
    "from": {"type": "string", "description": "Start date (YYYY-MM-DD), instead of period."},
    "to": {"type": "string", "description": "End date (YYYY-MM-DD), included."},
}


def definition(name: str, description: str, properties: dict) -> dict:
    return {
        "type": "function",
        "function": {"name": name, "description": description, "parameters": {"type": "object", "properties": properties}},
    }


SUMMARY = definition(
    "get_traffic_summary",
    "The road over a period: vehicles in total, per direction and per vehicle type, how many plates were read "
    "(read rate), distinct and repeat plates, peak hour, busiest day, speeds (average, 85th percentile, over "
    "the limit) and each camera's vehicles and read rate.",
    {"period": PERIOD, **FROM_TO},
)
BY_HOUR = definition(
    "get_traffic_by_hour",
    "Vehicles per hour of the day (total over the period and average per day, per direction) and per day. "
    "Use it for when the road is busy or quiet, rush hours, or comparing days.",
    {
        "period": PERIOD,
        **FROM_TO,
        "direction": {"type": "string", "description": "inbound or outbound. Omit for both."},
        "vehicle_type": {"type": "string", "description": "car, motorcycle, van, truck or bus. Omit for all."},
    },
)
PLATE = definition(
    "find_plate",
    "Every passage of one plate: times, directions, cameras and vehicle type, newest first, plus similar plates "
    "(one character away) in case a camera misread it. Use it for when a vehicle passed or how often.",
    {
        "plate": {"type": "string", "description": "The plate, e.g. ABC-1234 (spaces and dashes don't matter)."},
        "period": {**PERIOD, "description": PERIOD["description"].replace("Default 7d (the last 7 days, today included).",
                                                                          "Default: every read still kept.")},
        **FROM_TO,
    },
)
FREQUENT = definition(
    "get_frequent_plates",
    "The plates seen most often in a period, with passages, days seen and directions, and how many plates came "
    "back more than once. Use it for regular vehicles, commuters, buses or deliveries.",
    {"period": PERIOD, **FROM_TO, "limit": {"type": "integer", "description": "Plates, 1 to 20. Default 10."}},
)
CHECKS = definition(
    "get_camera_checks",
    "Checks of each camera over the last 7 days, each with its numbers: C1 few plates read, C2 few plates read "
    "at night compared with the day, C3 no vehicles for an hour when there usually are, C4 many plates read with "
    "low confidence. Use it for whether the cameras work well or need attention.",
    {},
)
NODES = definition(
    "get_device_status",
    "The site's edge device: online or not, seconds since its last heartbeat, and its latest temperatures, "
    "CPU/GPU use, memory, disk, cameras and FPS.",
    {},
)


def tools(agent):
    def get(path: str, params: dict) -> dict:
        query = urllib.parse.urlencode({k: v for k, v in params.items() if v not in (None, "")})
        request = urllib.request.Request(
            f"{INGEST_URL}{path}" + (f"?{query}" if query else ""),
            headers={"Authorization": f"Bearer {READ_TOKEN}"} if READ_TOKEN else {},
        )
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                return json.load(response)
        except urllib.error.HTTPError as e:
            try:
                message = json.load(e).get("error", "")
            except ValueError:
                message = ""
            if e.code == 400:
                raise agent.ToolError(message or "bad arguments") from None
            raise RuntimeError(f"ingest service: HTTP {e.code} {message}") from None

    def period(args: dict) -> dict:
        return {"period": args.get("period"), "from": args.get("from"), "to": args.get("to")}

    def limit(args: dict, default: int) -> int:
        try:
            return max(1, min(20, int(args.get("limit") or default)))
        except (TypeError, ValueError):
            raise agent.ToolError("limit must be a whole number") from None

    def get_traffic_summary(args: dict) -> dict:
        return get("/api/v1/agent/summary", period(args))

    def get_traffic_by_hour(args: dict) -> dict:
        result = get("/api/v1/agent/traffic", {
            **period(args), "direction": args.get("direction"), "vehicle_type": args.get("vehicle_type"),
        })
        # Quiet hours say nothing a small model needs; the totals still count them
        result["by_hour"] = [h for h in result.get("by_hour", []) if h["vehicles"]]
        return result

    def find_plate(args: dict) -> dict:
        if not args.get("plate"):
            raise agent.ToolError("plate is required, e.g. ABC-1234")
        result = get("/api/v1/agent/plate", {**period(args), "plate": args["plate"]})
        result["passages"] = result.get("passages", [])[:20]
        result["passages_shown"] = len(result["passages"])
        return result

    def get_frequent_plates(args: dict) -> dict:
        return get("/api/v1/agent/frequent", {**period(args), "limit": limit(args, 10)})

    def get_camera_checks(args: dict) -> dict:
        return get("/api/v1/agent/checks", {})

    def get_device_status(args: dict) -> dict:
        return get("/api/v1/agent/nodes", {})

    return [
        (SUMMARY, get_traffic_summary),
        (BY_HOUR, get_traffic_by_hour),
        (PLATE, find_plate),
        (FREQUENT, get_frequent_plates),
        (CHECKS, get_camera_checks),
        (NODES, get_device_status),
    ]
