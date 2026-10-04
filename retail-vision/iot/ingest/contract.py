"""
The Jetson ↔ web contract, version 0.1 (AIOROS-Alpha-Benito,
docs/alpha_vnext/boutique/CONTRATO_API_JETSON_WEB.md), as checks on the
JSON the ingest service accepts.

Standard library only: each check returns the reason a document is invalid,
or None. The rules follow the contract's JSON Schema and the Jetson's own
validators (src/retail_edge_ai/alpha/boutique/contracts.py), including the
vocabulary rule: an alert never accuses anyone.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone, tzinfo

EVENT = "aioros.boutique.event/0.1"
ALERT = "aioros.boutique.alert/0.1"
REPORT = "aioros.boutique.report/0.1"
HEARTBEAT = "aioros.boutique.heartbeat/0.1"

# The contract's enum, plus garment_tried_on and garment_carried, which the
# Jetson's engine also emits (contracts.py BoutiqueEventType)
EVENT_TYPES = (
    "customer_entry",
    "customer_exit",
    "shelf_dwell",
    "garment_touched",
    "garment_taken",
    "garment_returned",
    "garment_tried_on",
    "garment_carried",
    "fitting_room_entered",
    "fitting_room_exited",
    "checkout_visited",
    "staff_interaction",
)
ROLES = ("customer", "staff", "unresolved")
PRICE_TIERS = ("alto", "medio", "accesible")
ATTRIBUTE_SOURCES = ("vlm", "planogram_default", "rule")

# A1–A4, in the contract's order
ALERT_TYPES = {
    "customer_arrival": "A1",
    "high_value_opportunity": "A2",
    "fitting_room_assistance": "A3",
    "loss_risk_review": "A4",
}
SEVERITIES = ("low", "medium", "high")
# pending_review is the Jetson's; the others are set by a person, through
# POST /api/v1/alerts/<alert_id>/status
ALERT_STATUSES = ("pending_review", "attended", "dismissed")

HEALTH = ("healthy", "degraded", "error")

EVENT_ID = re.compile(r"^EVT-[0-9]{8}-[0-9]{6}$")
ALERT_ID = re.compile(r"^ALT-[0-9]{8}-[0-9]{6}$")
# Store, camera, zone and track ids end up in MQTT topics and InfluxDB tags
ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,63}$")

# Whole words only, as on the Jetson: "probó" and "probador" must not match "robo"
ACCUSATORY = re.compile(r"\b(ladr[oó]n\w*|sospechos\w*|robo|robos|robando|robar|hurto\w*|thief|thieves|theft|steal\w*|shoplift\w*)\b")


def parse_time(value: object, tz: tzinfo = timezone.utc) -> datetime:
    """An ISO 8601 date-time; one without an offset is in the store's time zone."""
    if not isinstance(value, str) or not value:
        raise ValueError("expected an ISO 8601 date-time")
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00") if value.endswith("Z") else value)
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=tz)


def _id(doc: dict, key: str, required: bool = True) -> str | None:
    value = doc.get(key)
    if value is None:
        return f"missing {key}" if required else None
    if not isinstance(value, str) or not ID.match(value):
        return f"{key} must be 1-64 letters, digits, '.', '_', ':' or '-'"
    return None


def _number(doc: dict, key: str, low: float | None = None, high: float | None = None) -> str | None:
    value = doc.get(key)
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return f"{key} must be a number"
    if (low is not None and value < low) or (high is not None and value > high):
        return f"{key} out of range"
    return None


def _time(doc: dict, key: str, tz: tzinfo) -> str | None:
    if key not in doc:
        return f"missing {key}"
    try:
        parse_time(doc[key], tz)
    except (TypeError, ValueError):
        return f"{key} must be an ISO 8601 date-time"
    return None


def _first(*errors: str | None) -> str | None:
    return next((e for e in errors if e), None)


def check_event(doc: object, tz: tzinfo = timezone.utc) -> str | None:
    if not isinstance(doc, dict):
        return "an event must be a JSON object"
    if doc.get("schema") != EVENT:
        return f"schema must be {EVENT}"
    error = _first(
        _id(doc, "store_id"),
        _id(doc, "camera_id"),
        _id(doc, "track_id"),
        _id(doc, "zone_id", required=False),
        _time(doc, "ts_start", tz),
        _time(doc, "ts_end", tz),
        _number(doc, "dwell_seconds", 0),
    )
    if error:
        return error
    if not EVENT_ID.match(str(doc.get("event_id", ""))):
        return "event_id must look like EVT-20260919-000123"
    if doc.get("type") not in EVENT_TYPES:
        return f"type must be one of {', '.join(EVENT_TYPES)}"
    if doc.get("role") not in ROLES:
        return f"role must be one of {', '.join(ROLES)}"
    if not isinstance(doc.get("synthetic"), bool):
        return "synthetic must be true or false"
    if parse_time(doc["ts_end"], tz) < parse_time(doc["ts_start"], tz):
        return "ts_end is before ts_start"
    for key in ("planogram", "attributes", "clip"):
        if key in doc and doc[key] is not None and not isinstance(doc[key], dict):
            return f"{key} must be an object"
    planogram = doc.get("planogram") or {}
    if planogram.get("price_tier") not in (None, *PRICE_TIERS):
        return f"planogram.price_tier must be one of {', '.join(PRICE_TIERS)}"
    if _number(planogram, "avg_price", 0):
        return "planogram.avg_price must be a number >= 0"
    attributes = doc.get("attributes") or {}
    if _number(attributes, "confidence", 0, 1):
        return "attributes.confidence must be between 0 and 1"
    if attributes.get("source") not in (None, *ATTRIBUTE_SOURCES):
        return f"attributes.source must be one of {', '.join(ATTRIBUTE_SOURCES)}"
    return None


def check_alert(doc: object, tz: tzinfo = timezone.utc) -> str | None:
    if not isinstance(doc, dict):
        return "an alert must be a JSON object"
    if doc.get("schema") != ALERT:
        return f"schema must be {ALERT}"
    error = _first(
        _id(doc, "store_id"),
        _id(doc, "camera_id"),
        _id(doc, "track_id", required=False),
        _time(doc, "timestamp", tz),
    )
    if error:
        return error
    if not ALERT_ID.match(str(doc.get("alert_id", ""))):
        return "alert_id must look like ALT-20260919-000042"
    if doc.get("alert_type") not in ALERT_TYPES:
        return f"alert_type must be one of {', '.join(ALERT_TYPES)}"
    if doc.get("severity") not in SEVERITIES:
        return f"severity must be one of {', '.join(SEVERITIES)}"
    if doc.get("status", "pending_review") not in ALERT_STATUSES:
        return f"status must be one of {', '.join(ALERT_STATUSES)}"
    for key in ("title", "description"):
        if not isinstance(doc.get(key), str) or not doc[key].strip():
            return f"missing {key}"
    if not isinstance(doc.get("review_required"), bool) or not isinstance(doc.get("synthetic"), bool):
        return "review_required and synthetic must be true or false"
    if "evidence" in doc and doc["evidence"] is not None and not isinstance(doc["evidence"], dict):
        return "evidence must be an object"
    words = " ".join(str(doc.get(k) or "") for k in ("title", "description", "recommended_action")).lower()
    hit = ACCUSATORY.search(words)
    if hit:
        return f"accusatory term {hit.group(1)!r}: alerts are review requests ('riesgo de pérdida — revisar')"
    return None


def check_report(doc: object, tz: tzinfo = timezone.utc) -> str | None:
    if not isinstance(doc, dict):
        return "a report must be a JSON object"
    if doc.get("schema") != REPORT:
        return f"schema must be {REPORT}"
    error = _id(doc, "store_id")
    if error:
        return error
    if not isinstance(doc.get("report_id"), str) or not ID.match(doc["report_id"]):
        return "report_id must be 1-64 letters, digits, '.', '_', ':' or '-'"
    for key in ("period", "traffic", "conversion_funnel"):
        if not isinstance(doc.get(key), dict):
            return f"{key} must be an object"
    for key in ("shelves_performance", "garments_popularity", "layout_recommendations"):
        if not isinstance(doc.get(key), list):
            return f"{key} must be a list"
    if not isinstance(doc.get("synthetic"), bool):
        return "synthetic must be true or false"
    return None


def check_heartbeat(doc: object, tz: tzinfo = timezone.utc) -> str | None:
    if not isinstance(doc, dict):
        return "a heartbeat must be a JSON object"
    if doc.get("schema") != HEARTBEAT:
        return f"schema must be {HEARTBEAT}"
    error = _first(_id(doc, "store_id"), _time(doc, "timestamp", tz))
    if error:
        return error
    for key in ("device", "metrics", "pipeline"):
        if not isinstance(doc.get(key), dict):
            return f"{key} must be an object"
    if _id(doc["device"], "hostname"):
        return "device.hostname must be 1-64 letters, digits, '.', '_', ':' or '-'"
    for section in ("metrics", "pipeline"):
        for key, value in doc[section].items():
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                return f"{section}.{key} must be a number"
    if doc.get("status") not in HEALTH:
        return f"status must be one of {', '.join(HEALTH)}"
    return None


def check_sale(doc: object, tz: tzinfo = timezone.utc) -> str | None:
    """
    A point-of-sale line. Not part of contract 0.1, which only says sales come
    from the POS: this is how they reach the ingest service, one garment per line.
    """
    if not isinstance(doc, dict):
        return "a sale must be a JSON object"
    error = _first(
        _id(doc, "tx_id"),
        _id(doc, "store_id"),
        _id(doc, "shelf_id"),
        _time(doc, "timestamp", tz),
        _number(doc, "amount", 0),
    )
    if error:
        return error
    if "amount" not in doc:
        return "missing amount"
    for key in ("garment_type", "color"):
        if not isinstance(doc.get(key), str) or not doc[key].strip():
            return f"missing {key}"
    if not isinstance(doc.get("synthetic", False), bool):
        return "synthetic must be true or false"
    return None


def store_tz(value: str) -> tzinfo:
    """
    STORE_TZ: an IANA name (America/Panama), or a fixed offset (-05:00) for
    images without time-zone data.
    """
    value = (value or "").strip()
    match = re.fullmatch(r"([+-])(\d{2}):?(\d{2})", value)
    if match:
        sign = -1 if match.group(1) == "-" else 1
        return timezone(sign * timedelta(hours=int(match.group(2)), minutes=int(match.group(3))))
    if value in ("", "UTC", "Z"):
        return timezone.utc
    from zoneinfo import ZoneInfo

    return ZoneInfo(value)
