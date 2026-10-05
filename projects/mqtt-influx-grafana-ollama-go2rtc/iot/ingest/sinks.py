"""
Where the ingest service copies what it stores: InfluxDB, for Grafana and
the agent's telemetry tools, and MQTT, for anything else on p4n4-net that
wants to react (Node-RED, n8n, a roadside sign).

SQLite stays the record, and the only place plates are kept: neither copy
carries a plate (MQTT does with MQTT_PLATES=true). Both copies are best
effort: a failed write is logged and dropped, and never fails the device's
request.

InfluxDB (bucket INFLUXDB_BUCKET):
    alpr_read,site_id,camera_id,direction,vehicle_type,plate_read=yes|no
        count=1i[,speed_kmh][,confidence],read_id="…"
    sensor_data,device=<hostname>,sensor=<metric> value=<number>
        every number of a heartbeat (metrics.*, pipeline.*), in the p4n4
        schema, so the agent's list_sensors / get_stats / get_history tools
        see the device's health with no extra code
    alpr_node,site_id,device status="healthy",up=1i

MQTT (QoS 0, not retained, JSON), under MQTT_TOPIC_ROOT (default traffic):
    traffic/<site_id>/read/<direction>    the read without plate, plate_region
                                          and evidence, plus plate_read: true|false
    traffic/<site_id>/heartbeat
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

from contract import parse_time

# First level of every MQTT topic (MQTT_TOPIC_ROOT)
TOPIC_ROOT = os.environ.get("MQTT_TOPIC_ROOT", "traffic").strip("/") or "traffic"
# Whether MQTT messages carry plates (MQTT_PLATES=true); they never do in topics
MQTT_PLATES = os.environ.get("MQTT_PLATES", "").strip().lower() == "true"
# What a read loses on its way to MQTT, unless MQTT_PLATES
PRIVATE = ("plate", "plate_region", "evidence")


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
    Nanoseconds of a timestamp. Two reads with the same tags in the same
    millisecond would overwrite each other in InfluxDB, so the id's sequence
    number goes in below the millisecond: unique, and still the same point
    when the device re-sends the read.
    """
    ns = int(parse_time(iso, tz).timestamp() * 1000) * 1_000_000
    digits = unique.rsplit("-", 1)[-1]
    return ns + (int(digits) % 1_000_000 if digits.isdigit() else 0)


def read_lines(doc: dict, tz: tzinfo) -> list[str]:
    tags = {
        "site_id": doc["site_id"],
        "camera_id": doc["camera_id"],
        "direction": doc["direction"],
        "vehicle_type": doc["vehicle_type"],
        "plate_read": "yes" if doc.get("plate") else "no",
    }
    fields = {
        "count": 1,
        "speed_kmh": float(doc["speed_kmh"]) if doc.get("speed_kmh") is not None else None,
        "confidence": float(doc["plate_confidence"]) if doc.get("plate_confidence") is not None else None,
        "read_id": doc["read_id"],
    }
    return [line("alpr_read", tags, fields, _ns(doc["timestamp"], tz, doc["read_id"]))]


def heartbeat_lines(doc: dict, tz: tzinfo) -> list[str]:
    ns = _ns(doc["timestamp"], tz)
    host = doc["device"]["hostname"]
    numbers = {**doc["metrics"], **doc["pipeline"]}
    lines = [
        line("sensor_data", {"device": host, "sensor": key}, {"value": float(value)}, ns)
        for key, value in sorted(numbers.items())
    ]
    lines.append(line(
        "alpr_node",
        {"site_id": doc["site_id"], "device": host},
        {"status": doc["status"], "up": 1 if doc["status"] == "healthy" else 0},
        ns,
    ))
    return lines


def read_message(doc: dict) -> dict:
    """A read as MQTT gets it: without the plate, unless MQTT_PLATES."""
    if MQTT_PLATES:
        return doc
    return {**{k: v for k, v in doc.items() if k not in PRIVATE and k != "plate_confidence"},
            "plate_read": bool(doc.get("plate"))}


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
    costs nothing to recover from. Batches are small (a device posts every
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
        flags, payload = 0x02, _string(f"alpr-ingest-{os.getpid()}-{time.monotonic_ns() % 1_000_000}")
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


def topic(site_id: str, *parts: str) -> str:
    return "/".join((TOPIC_ROOT, site_id, *parts))
