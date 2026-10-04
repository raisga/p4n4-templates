"""
Where the ingest service copies what it stores: InfluxDB, for Grafana and
the agent's telemetry tools, and MQTT, for anything else on p4n4-net that
wants to react (Node-RED, n8n, a store screen).

SQLite stays the record. Both copies are best effort: a failed write is
logged and dropped, and never fails the Jetson's request.

InfluxDB (bucket INFLUXDB_BUCKET):
    sensor_data,device=<hostname>,sensor=<metric> value=<number>
        every number of a heartbeat (metrics.*, pipeline.*, alerts_pending),
        in the p4n4 schema, so the agent's list_sensors / get_stats /
        get_history tools see the Jetson's health with no extra code
    boutique_node,store_id,device status="healthy",up=1i
    boutique_event,store_id,camera_id,type,role[,zone_id][,garment_type][,color]
        count=1i[,dwell_seconds],event_id="…"
    boutique_alert,store_id,camera_id,alert_type,code,severity
        count=1i,title="…",alert_id="…",status="…"
    boutique_sale,store_id,shelf_id,garment_type,color count=1i,amount=<number>

MQTT (QoS 0, not retained, JSON as received), under MQTT_TOPIC_ROOT (default retail):
    retail/<store_id>/event/<type>
    retail/<store_id>/alert/<alert_type>
    retail/<store_id>/heartbeat
    retail/<store_id>/report
    retail/<store_id>/sale
"""

from __future__ import annotations

import json
import os
import socket
import struct
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import tzinfo

from contract import ALERT_TYPES, parse_time

# First level of every MQTT topic (MQTT_TOPIC_ROOT)
TOPIC_ROOT = os.environ.get("MQTT_TOPIC_ROOT", "retail").strip("/") or "retail"


def log(message: str) -> None:
    print(f"{time.strftime('%Y-%m-%dT%H:%M:%S')} {message}", file=sys.stderr, flush=True)


# ── InfluxDB line protocol ────────────────────────────────────────────────────


def _escape_tag(value: object) -> str:
    return str(value).replace("\\", "\\\\").replace(",", "\\,").replace("=", "\\=").replace(" ", "\\ ")


def _field(value: object) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return f"{value}i"
    if isinstance(value, float):
        return repr(value)
    return '"' + str(value).replace("\\", "\\\\").replace('"', '\\"') + '"'


def line(measurement: str, tags: dict, fields: dict, ns: int) -> str:
    tag_text = "".join(f",{k}={_escape_tag(v)}" for k, v in sorted(tags.items()) if v not in (None, ""))
    field_text = ",".join(f"{k}={_field(v)}" for k, v in fields.items() if v is not None)
    return f"{measurement}{tag_text} {field_text} {ns}"


def _ns(iso: str, tz: tzinfo, unique: str = "") -> int:
    """
    Nanoseconds of a timestamp. Two events with the same tags in the same
    millisecond would overwrite each other in InfluxDB, so the id's sequence
    number goes in below the millisecond: unique, and still the same point
    when the Jetson re-sends the event.
    """
    ns = int(parse_time(iso, tz).timestamp() * 1000) * 1_000_000
    digits = unique.rsplit("-", 1)[-1]
    return ns + (int(digits) % 1_000_000 if digits.isdigit() else 0)


def event_lines(doc: dict, tz: tzinfo) -> list[str]:
    attributes = doc.get("attributes") or {}
    tags = {
        "store_id": doc["store_id"],
        "camera_id": doc["camera_id"],
        "type": doc["type"],
        "role": doc["role"],
        "zone_id": doc.get("zone_id"),
        "garment_type": (attributes.get("garment_type") or "").casefold(),
        "color": (attributes.get("color") or "").casefold(),
    }
    fields = {"count": 1, "dwell_seconds": doc.get("dwell_seconds"), "event_id": doc["event_id"]}
    if isinstance(fields["dwell_seconds"], int):
        fields["dwell_seconds"] = float(fields["dwell_seconds"])
    return [line("boutique_event", tags, fields, _ns(doc["ts_start"], tz, doc["event_id"]))]


def alert_lines(doc: dict, tz: tzinfo) -> list[str]:
    tags = {
        "store_id": doc["store_id"],
        "camera_id": doc["camera_id"],
        "alert_type": doc["alert_type"],
        "code": ALERT_TYPES[doc["alert_type"]],
        "severity": doc["severity"],
    }
    fields = {
        "count": 1,
        "title": doc["title"],
        "alert_id": doc["alert_id"],
        "status": doc.get("status", "pending_review"),
        "zone_id": (doc.get("evidence") or {}).get("zone_id"),
    }
    return [line("boutique_alert", tags, fields, _ns(doc["timestamp"], tz, doc["alert_id"]))]


def heartbeat_lines(doc: dict, tz: tzinfo) -> list[str]:
    ns = _ns(doc["timestamp"], tz)
    host = doc["device"]["hostname"]
    numbers = {**doc["metrics"], **doc["pipeline"]}
    if isinstance(doc.get("alerts_pending"), (int, float)) and not isinstance(doc.get("alerts_pending"), bool):
        numbers["alerts_pending"] = doc["alerts_pending"]
    lines = [
        line("sensor_data", {"device": host, "sensor": key}, {"value": float(value)}, ns)
        for key, value in sorted(numbers.items())
    ]
    lines.append(line(
        "boutique_node",
        {"store_id": doc["store_id"], "device": host},
        {"status": doc["status"], "up": 1 if doc["status"] == "healthy" else 0},
        ns,
    ))
    return lines


def sale_lines(doc: dict, tz: tzinfo) -> list[str]:
    tags = {
        "store_id": doc["store_id"],
        "shelf_id": doc["shelf_id"],
        "garment_type": doc["garment_type"].casefold(),
        "color": doc["color"].casefold(),
    }
    return [line("boutique_sale", tags, {"count": 1, "amount": float(doc["amount"])}, _ns(doc["timestamp"], tz, doc["tx_id"]))]


class Influx:
    def __init__(self) -> None:
        self.url = os.environ.get("INFLUXDB_URL", "").rstrip("/")
        self.token = os.environ.get("INFLUXDB_TOKEN", "")
        self.org = os.environ.get("INFLUXDB_ORG", "")
        self.bucket = os.environ.get("INFLUXDB_BUCKET", "raw_telemetry")

    @property
    def enabled(self) -> bool:
        return bool(self.url and self.token)

    def write(self, lines: list[str]) -> None:
        if not self.enabled or not lines:
            return
        query = urllib.parse.urlencode({"org": self.org, "bucket": self.bucket, "precision": "ns"})
        request = urllib.request.Request(
            f"{self.url}/api/v2/write?{query}",
            data="\n".join(lines).encode(),
            headers={"Authorization": f"Token {self.token}", "Content-Type": "text/plain; charset=utf-8"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=10):
                pass
        except (urllib.error.URLError, OSError) as e:
            detail = e.read().decode(errors="replace")[:200] if isinstance(e, urllib.error.HTTPError) else e
            log(f"influxdb: {len(lines)} points dropped: {detail}")


# ── MQTT 3.1.1, publish only, QoS 0 ───────────────────────────────────────────


def _string(value: str) -> bytes:
    data = value.encode()
    return struct.pack("!H", len(data)) + data


def _packet(kind: int, body: bytes) -> bytes:
    length, encoded = len(body), b""
    while True:
        byte, length = length % 128, length // 128
        encoded += bytes([byte | (0x80 if length else 0)])
        if not length:
            return bytes([kind]) + encoded + body


class Mqtt:
    """
    Enough of MQTT to publish: one connection per batch, so a broker restart
    costs nothing to recover from. Batches are small (a Jetson posts every
    few seconds), so the connection overhead doesn't matter.
    """

    def __init__(self) -> None:
        self.host = os.environ.get("MQTT_HOST", "")
        self.port = int(os.environ.get("MQTT_PORT") or 1883)
        self.user = os.environ.get("MQTT_USER", "")
        self.password = os.environ.get("MQTT_PASSWORD", "")

    @property
    def enabled(self) -> bool:
        return bool(self.host)

    def publish(self, messages: list[tuple[str, dict]]) -> None:
        if not self.enabled or not messages:
            return
        flags, payload = 0x02, _string(f"retail-ingest-{os.getpid()}-{time.monotonic_ns() % 1_000_000}")
        if self.user:
            flags |= 0x80
            payload += _string(self.user)
            if self.password:
                flags |= 0x40
                payload += _string(self.password)
        connect = _string("MQTT") + bytes([4, flags]) + struct.pack("!H", 30) + payload
        try:
            with socket.create_connection((self.host, self.port), timeout=5) as sock:
                sock.sendall(_packet(0x10, connect))
                ack = sock.recv(4)
                if len(ack) < 4 or ack[0] != 0x20 or ack[3] != 0:
                    raise OSError(f"connection refused (CONNACK {ack.hex() or 'none'})")
                for topic, doc in messages:
                    body = json.dumps(doc, ensure_ascii=False, separators=(",", ":")).encode()
                    sock.sendall(_packet(0x30, _string(topic) + body))
                sock.sendall(_packet(0xE0, b""))
        except OSError as e:
            log(f"mqtt: {len(messages)} messages dropped: {e}")


def topic(store_id: str, *parts: str) -> str:
    return "/".join((TOPIC_ROOT, store_id, *parts))
