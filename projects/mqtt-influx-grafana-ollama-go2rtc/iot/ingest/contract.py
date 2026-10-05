"""
The ALPR device ↔ web contract, version 0.1, as checks on the JSON the
ingest service accepts.

Standard library only: each check returns the reason a document is invalid,
or None. A reason never repeats the document's plate, so a refused read can
be logged without one.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone, tzinfo

READ = "p4n4.alpr.read/0.1"
HEARTBEAT = "p4n4.alpr.heartbeat/0.1"

DIRECTIONS = ("inbound", "outbound", "unknown")
VEHICLE_TYPES = ("car", "motorcycle", "van", "truck", "bus", "unknown")
HEALTH = ("healthy", "degraded", "error")

READ_ID = re.compile(r"^RD-[0-9]{8}-[0-9]{6}$")
# Site, camera and host ids end up in MQTT topics and InfluxDB tags
ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,63}$")
# As the device read it: letters (any script), digits, spaces, dashes and dots
PLATE = re.compile(r"^[^\W_](?:(?:[^\W_]|[ .-]){0,14}[^\W_])?$")
REGION = re.compile(r"^[A-Za-z0-9-]{1,16}$")
EVIDENCE = ("image_uri", "plate_crop_uri", "clip_uri")


def plate_key(plate: str) -> str:
    """The plate as matched: upper case, no spaces, dashes or dots ("abc-1234" → "ABC1234")."""
    return re.sub(r"[\s.-]", "", plate).upper()


def parse_time(value: object, tz: tzinfo = timezone.utc) -> datetime:
    """An ISO 8601 date-time; one without an offset is in the site's time zone."""
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


def check_read(doc: object, tz: tzinfo = timezone.utc) -> str | None:
    """
    One vehicle passing a camera. plate is null when the device counted the
    vehicle but couldn't read its plate; then plate_confidence is left out.
    """
    if not isinstance(doc, dict):
        return "a read must be a JSON object"
    if doc.get("schema") != READ:
        return f"schema must be {READ}"
    error = _first(
        _id(doc, "site_id"),
        _id(doc, "camera_id"),
        _time(doc, "timestamp", tz),
        _number(doc, "speed_kmh", 0, 400),
    )
    if error:
        return error
    if not READ_ID.match(str(doc.get("read_id", ""))):
        return "read_id must look like RD-20261004-000123"
    if doc.get("direction") not in DIRECTIONS:
        return f"direction must be one of {', '.join(DIRECTIONS)}"
    if doc.get("vehicle_type") not in VEHICLE_TYPES:
        return f"vehicle_type must be one of {', '.join(VEHICLE_TYPES)}"
    lane = doc.get("lane")
    if lane is not None and (isinstance(lane, bool) or not isinstance(lane, int) or not 1 <= lane <= 16):
        return "lane must be a whole number from 1 to 16"
    plate = doc.get("plate")
    if plate is not None:
        if not isinstance(plate, str) or not PLATE.match(plate) or not plate_key(plate):
            return "plate must be 1-16 letters or digits, with spaces, dashes or dots between them, or null"
        if doc.get("plate_confidence") is None:
            return "a read with a plate needs plate_confidence"
    elif doc.get("plate_confidence") is not None:
        return "plate_confidence without a plate"
    if _number(doc, "plate_confidence", 0, 1):
        return "plate_confidence must be between 0 and 1"
    region = doc.get("plate_region")
    if region is not None and (not isinstance(region, str) or not REGION.match(region)):
        return "plate_region must be 1-16 letters, digits or '-', e.g. PA or US-CA"
    colour = doc.get("colour")
    if colour is not None and (not isinstance(colour, str) or len(colour) > 32):
        return "colour must be a short text"
    evidence = doc.get("evidence")
    if evidence is not None:
        if not isinstance(evidence, dict) or set(evidence) - set(EVIDENCE):
            return f"evidence must be an object with {', '.join(EVIDENCE)}"
        if any(not isinstance(v, str) for v in evidence.values()):
            return "evidence URIs must be text"
    if not isinstance(doc.get("synthetic"), bool):
        return "synthetic must be true or false"
    return None


def check_heartbeat(doc: object, tz: tzinfo = timezone.utc) -> str | None:
    if not isinstance(doc, dict):
        return "a heartbeat must be a JSON object"
    if doc.get("schema") != HEARTBEAT:
        return f"schema must be {HEARTBEAT}"
    error = _first(_id(doc, "site_id"), _time(doc, "timestamp", tz))
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


def site_tz(value: str) -> tzinfo:
    """
    SITE_TZ: an IANA name (America/Panama), or a fixed offset (-05:00) for
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
