"""
retail-vision tools for the agent (agent.py loads this file at startup).

The store's numbers come from the ingest service in ../iot (its agent API,
contract §6), which computes them from the events, alerts and sales the
edge device and the till posted. The model only picks the tool and the period;
it never adds anything up, as the contract asks ("cifras 100 %
deterministas"). Each result says which days it covered and whether the
data is synthetic (the demo store).

agent.py's own tools (list_sensors, get_stats, get_history) still work: the
only device writing sensor_data here is the store's edge device, whose
heartbeats the ingest service stores as readings (temperatures, CPU/GPU
use, memory, FPS, people on camera).

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
This project is a clothing store. A camera and an edge device in \
the store follow customers and see when they touch, take or try on garments; \
the till reports sales. Questions are about the store unless they name the \
edge device: never ask the user whether they mean the store or a device, call the \
tools. "What happened" over a period means get_store_summary for that period. \
For anything about the store (visitors, peak hours, \
shelves, garments and colours, fitting rooms, sales, conversion, alerts, what \
to restock or move) use the store tools: get_store_summary, get_alerts, \
get_shelves, get_garment_ranking, get_advice. Their numbers are final: repeat \
them, never add up or estimate your own. They take a period (today, \
yesterday, this_week, last_week, 7d, 30d, or a weekday such as tuesday or \
martes for its latest occurrence) and use the store's local time, not UTC; each result says \
which days it covered. When a result has synthetic_data true, say the data \
is from the demo store.

Alerts: A1 a customer arriving or looking at a garment (arrival, llegada), A2 \
a high-value opportunity without staff nearby (opportunity, oportunidad), A3 \
fitting-room assistance (probador), A4 loss risk to review ("riesgo de \
pérdida — revisar"). get_alerts counts every alert of the period per type, \
per status and per type and status (by_type_and_status): read how many \
opportunity alerts were attended there. Every alert is a request for a person \
to look, never a conclusion: never call anyone a thief or speak of theft \
(robo, ladrón); say "loss risk — review" ("riesgo de pérdida — revisar"). The \
system recognises no faces.

The only device in list_sensors, get_stats and get_history is the store's \
edge device: use get_device_status for whether it is online, and those tools for \
its temperatures, CPU/GPU use, memory and FPS over time."""

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
    "get_store_summary",
    "The store over a period: visitors, garment interactions, fitting-room and checkout visits, sales "
    "transactions, revenue and currency, conversion rate (sales / visitors), average visit length, peak hour, "
    "the shelf with most interest, and alerts per type (A1–A4) and status (pending_review, attended, dismissed).",
    {"period": PERIOD, **FROM_TO},
)
ALERTS = definition(
    "get_alerts",
    "Alerts over a period: counts per type (A1-A4), per status (attended, dismissed, pending_review), per type "
    "and status, and per day (with its weekday), and the latest ones with time, shelf, title, description, "
    "recommended action and status. Use it for how many alerts of a kind there were, how many were attended, "
    "and what happened.",
    {
        "period": PERIOD,
        **FROM_TO,
        "type": {"type": "string", "description": "A1 llegada, A2 oportunidad (high value), A3 probador, "
                                                  "A4 riesgo de pérdida. Omit for all."},
        "limit": {"type": "integer", "description": "How many alerts to list, 1 to 20. Default 5; the counts cover all."},
    },
)
SHELVES = definition(
    "get_shelves",
    "Every shelf over a period: interactions (camera), garments taken, average dwell, units sold and revenue "
    "(till), conversion, and status: caliente (busy), frio (quiet) or normal. diagnosis lists "
    "interes_sin_venta (interest but few sales) and alto_valor_poco_trafico (high value, little traffic). Also "
    "names the shelf with most interest, the best seller, the lowest sales and the one with interest but no sales.",
    {"period": PERIOD, **FROM_TO},
)
RANKING = definition(
    "get_garment_ranking",
    "Garments by type and colour over a period: interactions seen by the camera and units sold, sorted by units "
    "sold. Use it for which garments or colours sell or draw interest, e.g. whether white shirts are selling.",
    {
        "period": PERIOD,
        **FROM_TO,
        "category": {"type": "string", "description": "Garment type, e.g. camisa, polo, vestido, jeans, traje. Omit for all."},
        "color": {"type": "string", "description": "Colour in Spanish, e.g. blanco, negro, azul marino. Omit for all."},
        "limit": {"type": "integer", "description": "Rows, 1 to 20. Default 5."},
    },
)
ADVICE = definition(
    "get_advice",
    "Rule-based suggestions over the last 7 days, each with its rule and numbers: R1 what to restock (best "
    "sellers from high-value shelves), R2 which shelf to move, R3 which display to review, R4 where staff is "
    "needed and when. Use it for what to restock, move or change.",
    {},
)
NODES = definition(
    "get_device_status",
    "The store's edge device: online or not, seconds since its last heartbeat, and its latest temperatures, "
    "CPU/GPU use, memory, disk, cameras, FPS and alerts pending review.",
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

    def get_store_summary(args: dict) -> dict:
        return get("/api/v1/agent/summary", period(args))

    def get_alerts(args: dict) -> dict:
        result = get("/api/v1/agent/alerts", {
            **period(args), "type": args.get("type"), "limit": limit(args, 5),
        })
        # What a person would read; the rest (clip URIs, camera) stays in the ingest API
        keep = ("alert_id", "code", "time", "weekday", "zone_id", "title", "description", "recommended_action", "status")
        result["items"] = [{k: a[k] for k in keep if k in a} for a in result.get("items", [])]
        return result

    def get_shelves(args: dict) -> dict:
        return get("/api/v1/agent/layout", period(args))

    def get_garment_ranking(args: dict) -> dict:
        return get("/api/v1/agent/ranking", {
            **period(args), "category": args.get("category"), "color": args.get("color"), "limit": limit(args, 5),
        })

    def get_advice(args: dict) -> dict:
        return get("/api/v1/agent/restock_advice", {})

    def get_device_status(args: dict) -> dict:
        return get("/api/v1/agent/nodes", {})

    return [
        (SUMMARY, get_store_summary),
        (ALERTS, get_alerts),
        (SHELVES, get_shelves),
        (RANKING, get_garment_ranking),
        (ADVICE, get_advice),
        (NODES, get_device_status),
    ]
