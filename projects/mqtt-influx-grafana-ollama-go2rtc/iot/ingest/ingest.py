"""
ALPR ingest: the web side of the ALPR device ↔ web contract 0.1 (schemas
p4n4.alpr.*/0.1).

Each site's edge device posts the vehicles its cameras see (outbound HTTPS,
from behind the site's NAT); this service checks them against the contract,
keeps them in SQLite for the retention period, copies counts to InfluxDB and
MQTT (sinks.py), and answers the agent's questions with numbers computed
from what it stored (store.py).

    POST /api/v1/reads       one plate read, a list, or {"reads": [...]}
    POST /api/v1/heartbeat   the device's health
        X-Site-Token: <the site's token> (or Authorization: Bearer <token>);
        a token only writes its own site_id.

    GET  /api/v1/agent/summary    period | from, to
    GET  /api/v1/agent/traffic    period | from, to; direction, vehicle_type
    GET  /api/v1/agent/plate      plate; period | from, to (default: the retention period)
    GET  /api/v1/agent/frequent   period | from, to; limit
    GET  /api/v1/agent/checks     the cameras' checks C1-C4, over the last 7 days
    GET  /api/v1/agent/nodes      each device's latest heartbeat
    GET  /api/v1/sites
        Authorization: Bearer <INGEST_READ_TOKEN> for every site, or a
        site's token for that site. Every GET takes site_id; without it,
        INGEST_DEFAULT_SITE, or the only site there is.

    GET  /health

Standard library only, so it runs on the stock python image. Plates never
appear in its log.

    INGEST_PORT            Port to listen on (default 8080)
    INGEST_DB              SQLite file (default /data/ingest.db)
    INGEST_SITE_TOKENS     site_id:token pairs, comma-separated. Required:
                           with none, every write is refused
    INGEST_READ_TOKEN      Token for the GET endpoints. Empty: reads need no
                           token (trusted network only)
    INGEST_DEFAULT_SITE    Site a read means when it names none (default: the
                           only site, when there is one)
    INGEST_RETENTION_DAYS  Days plate reads are kept (default 30); older ones
                           are deleted every hour, and refused on arrival
    SITE_TZ                Site time zone, an IANA name or an offset (default
                           UTC). Days, weeks and peak hours use it, and so do
                           timestamps without an offset
    SITE_LANG              Language of the text in answers (checks, weekdays):
                           en (default) or es
    SITE_SPEED_LIMIT       km/h; with it, summaries count vehicles over it
    INFLUXDB_URL, INFLUXDB_TOKEN, INFLUXDB_ORG, INFLUXDB_BUCKET
                           Where to copy points (empty URL: don't)
    MQTT_HOST, MQTT_PORT, MQTT_USER, MQTT_PASSWORD
                           Where to publish (empty host: don't)
    MQTT_TOPIC_ROOT        First topic level (default traffic)
    MQTT_PLATES            true: MQTT messages carry plates (default: they don't)
"""

from __future__ import annotations

import hmac
import json
import os
import queue
import sqlite3
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import contract
import sinks
from store import PeriodError, Store

PORT = int(os.environ.get("INGEST_PORT", "8080"))
DB = os.environ.get("INGEST_DB", "/data/ingest.db")
TZ = contract.site_tz(os.environ.get("SITE_TZ", "UTC"))
LANG = os.environ.get("SITE_LANG", "en").strip() or "en"
READ_TOKEN = os.environ.get("INGEST_READ_TOKEN", "").strip()
DEFAULT_SITE = os.environ.get("INGEST_DEFAULT_SITE", "").strip()
RETENTION = int(os.environ.get("INGEST_RETENTION_DAYS") or 30)
SPEED_LIMIT = float(os.environ.get("SITE_SPEED_LIMIT") or 0) or None
MAX_BODY = 8 * 1024 * 1024
MAX_BATCH = 1000
PURGE_EVERY = 3600


def parse_tokens(text: str) -> dict[str, str]:
    """INGEST_SITE_TOKENS → {token: site_id}."""
    tokens = {}
    for pair in filter(None, (p.strip() for p in text.split(","))):
        site_id, _, token = pair.partition(":")
        if not token.strip() or not contract.ID.match(site_id.strip()):
            raise SystemExit(f"INGEST_SITE_TOKENS: expected site_id:token, got {pair!r}")
        tokens[token.strip()] = site_id.strip()
    return tokens


class Kind:
    """One POST endpoint: how to check, store and copy its documents."""

    def __init__(self, name, key, check, put, id_key, lines, message):
        self.name, self.key, self.check, self.put = name, key, check, put
        self.id_key, self.lines, self.message = id_key, lines, message


class Service:
    def __init__(self, store: Store, tokens: dict[str, str], read_token: str = "", default_site: str = "",
                 influx: sinks.Influx | None = None, mqtt: sinks.Mqtt | None = None) -> None:
        self.store, self.tokens, self.read_token = store, tokens, read_token
        self.default_site = default_site
        self.influx = influx or sinks.Influx()
        self.mqtt = mqtt or sinks.Mqtt()
        self.tz = store.tz
        self.outbox: queue.Queue = queue.Queue(maxsize=10_000)
        threading.Thread(target=self._drain, daemon=True).start()
        self.kinds = {
            "/api/v1/reads": Kind(
                "read", "reads", contract.check_read, store.put_read, "read_id", sinks.read_lines,
                lambda d: (sinks.topic(d["site_id"], "read", d["direction"]), sinks.read_message(d)),
            ),
            "/api/v1/heartbeat": Kind(
                "heartbeat", "heartbeats", contract.check_heartbeat, store.put_heartbeat, "timestamp",
                sinks.heartbeat_lines, lambda d: (sinks.topic(d["site_id"], "heartbeat"), d),
            ),
        }

    def _drain(self) -> None:
        """Copies to InfluxDB and MQTT off the request thread, so a slow sink never delays a device."""
        while True:
            lines, messages = self.outbox.get()
            self.influx.write(lines)
            self.mqtt.publish(messages)

    def flush(self, timeout: float = 5.0) -> None:
        """Wait for the outbox to empty (tests)."""
        deadline = time.monotonic() + timeout
        while not self.outbox.empty() and time.monotonic() < deadline:
            time.sleep(0.01)

    def purge(self) -> None:
        """Delete reads past the retention period, every PURGE_EVERY seconds."""
        while True:
            deleted = self.store.purge()
            if deleted:
                sinks.log(f"retention: deleted {deleted} reads older than {self.store.retention_days} days")
            time.sleep(PURGE_EVERY)

    # ── auth ──────────────────────────────────────────────────────────────────

    def _match(self, token: str) -> str | None:
        """Site the token writes to, or None. Compared in constant time."""
        found = None
        for known, site_id in self.tokens.items():
            if hmac.compare_digest(known.encode(), token.encode()):
                found = site_id
        return found

    def writer(self, headers) -> str | None:
        return self._match(_token(headers)) if self.tokens else None

    def reader(self, headers) -> tuple[bool, str | None]:
        """(allowed, the one site the token is limited to, or None for all)."""
        token = _token(headers)
        if not self.read_token:
            return True, None
        if token and hmac.compare_digest(self.read_token.encode(), token.encode()):
            return True, None
        site_id = self._match(token) if token else None
        return (site_id is not None), site_id

    # ── writes ────────────────────────────────────────────────────────────────

    def ingest(self, path: str, body: object, site_id: str) -> tuple[int, dict]:
        kind = self.kinds[path]
        if isinstance(body, dict) and isinstance(body.get(kind.key), list):
            docs = body[kind.key]
        elif isinstance(body, list):
            docs = body
        else:
            docs = [body]
        if len(docs) > MAX_BATCH:
            return 413, {"error": f"at most {MAX_BATCH} {kind.key} per request"}

        counts = {"created": 0, "updated": 0, "unchanged": 0}
        rejected, lines, messages = [], [], []
        for index, doc in enumerate(docs):
            error = kind.check(doc, self.tz)
            if not error and doc["site_id"] != site_id:
                error = f"this token writes site {site_id!r} only"
            if error:
                ref = doc.get(kind.id_key) if isinstance(doc, dict) else None
                rejected.append({"index": index, kind.id_key: ref if isinstance(ref, str) else None, "error": error})
                continue
            try:
                outcome = kind.put(doc)
            except (sqlite3.Error, ValueError) as e:
                rejected.append({"index": index, kind.id_key: doc.get(kind.id_key), "error": f"not stored: {e}"})
                continue
            counts[outcome] += 1
            if outcome != "unchanged":
                lines += kind.lines(doc, self.tz)
                messages.append(kind.message(doc))
        if lines or messages:
            try:
                self.outbox.put_nowait((lines, messages))
            except queue.Full:
                sinks.log(f"outbox full: {len(lines)} points and {len(messages)} messages dropped")
        accepted = sum(counts.values())
        if rejected:
            sinks.log(f"{kind.key} from {site_id}: {accepted} accepted, {len(rejected)} rejected: {rejected[0]['error']}")
        status = 400 if rejected and not accepted else 200
        return status, {"accepted": accepted, **counts, "rejected": rejected}

    # ── reads ─────────────────────────────────────────────────────────────────

    def pick_site(self, params: dict, limit: str | None) -> str:
        wanted = (params.get("site_id") or "").strip()
        if limit:
            if wanted and wanted != limit:
                raise PermissionError(f"this token reads site {limit!r} only")
            return limit
        if wanted or self.default_site:
            return wanted or self.default_site
        known = sorted(set(self.store.sites()) | set(self.tokens.values()))
        if len(known) == 1:
            return known[0]
        raise PeriodError(f"pass site_id, one of: {', '.join(known) or '(no sites yet)'}")

    def query(self, path: str, params: dict, limit: str | None) -> dict | None:
        s = self.store
        if path == "/api/v1/sites":
            return {"sites": [x for x in s.sites() if not limit or x == limit]}
        if path == "/api/v1/agent/nodes":
            return s.nodes(limit or params.get("site_id") or None)
        site = self.pick_site(params, limit)
        if path == "/api/v1/agent/checks":
            return s.checks(site)
        default = f"{min(366, s.retention_days)}d" if path == "/api/v1/agent/plate" else None
        lo, hi, period = s.period(params.get("period") or default, params.get("from"), params.get("to"))
        if path == "/api/v1/agent/summary":
            result = s.summary(site, lo, hi)
        elif path == "/api/v1/agent/traffic":
            result = s.traffic(site, lo, hi, params.get("direction") or None, params.get("vehicle_type") or None)
        elif path == "/api/v1/agent/plate":
            result = s.plate(site, params.get("plate", ""), lo, hi)
        elif path == "/api/v1/agent/frequent":
            result = s.frequent(site, lo, hi, max(1, min(50, int(params.get("limit") or 10))))
        else:
            return None
        return {"period": period, **result}


def _token(headers) -> str:
    token = (headers.get("X-Site-Token") or "").strip()
    auth = (headers.get("Authorization") or "").strip()
    if not token and auth[:7].lower() == "bearer ":
        token = auth[7:].strip()
    return token


def handler(service: Service):
    class Handler(BaseHTTPRequestHandler):
        server_version = "alpr-ingest/0.1"
        protocol_version = "HTTP/1.1"

        def log_message(self, format: str, *args) -> None:
            pass  # request lines carry plates (GET /api/v1/agent/plate?plate=…)

        def _send(self, status: int, data: dict) -> None:
            body = json.dumps(data, ensure_ascii=False).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _body(self) -> object:
            length = int(self.headers.get("Content-Length") or 0)
            if length > MAX_BODY:
                raise ValueError(f"body over {MAX_BODY // 1024 // 1024} MB")
            return json.loads(self.rfile.read(length) or b"null")

        def do_GET(self) -> None:
            url = urllib.parse.urlsplit(self.path)
            path = url.path.rstrip("/") or "/"
            if path == "/health":
                return self._send(200, {
                    "status": "ok",
                    "sites": service.store.sites(),
                    "retention_days": service.store.retention_days,
                    "influxdb": service.influx.enabled,
                    "mqtt": service.mqtt.enabled,
                })
            allowed, limit = service.reader(self.headers)
            if not allowed:
                return self._send(401, {"error": "Authorization: Bearer <read token> required"})
            params = {k: v[-1] for k, v in urllib.parse.parse_qs(url.query).items()}
            try:
                with service.store.lock:
                    result = service.query(path, params, limit)
            except PermissionError as e:
                return self._send(403, {"error": str(e)})
            except (PeriodError, ValueError) as e:
                return self._send(400, {"error": str(e)})
            if result is None:
                return self._send(404, {"error": f"no endpoint {path}"})
            self._send(200, result)

        def do_POST(self) -> None:
            path = urllib.parse.urlsplit(self.path).path.rstrip("/")
            try:
                body = self._body()
            except (ValueError, UnicodeDecodeError) as e:
                return self._send(400, {"error": f"invalid JSON: {e}"})
            if path not in service.kinds:
                return self._send(404, {"error": f"no endpoint {path}"})
            if not service.tokens:
                return self._send(503, {"error": "no site tokens configured (INGEST_SITE_TOKENS)"})
            site_id = service.writer(self.headers)
            if site_id is None:
                return self._send(401, {"error": "X-Site-Token: <site token> required"})
            status, result = service.ingest(path, body, site_id)
            self._send(status, result)

    return Handler


def main() -> None:
    tokens = parse_tokens(os.environ.get("INGEST_SITE_TOKENS", ""))
    Path(DB).parent.mkdir(parents=True, exist_ok=True)
    service = Service(Store(DB, TZ, LANG, RETENTION, SPEED_LIMIT), tokens, READ_TOKEN, DEFAULT_SITE)
    threading.Thread(target=service.purge, daemon=True).start()
    sinks.log(f"ingest on :{PORT}, {DB}, plates kept {RETENTION} days, "
              f"sites with a token: {', '.join(sorted(set(tokens.values()))) or 'none'}")
    if not tokens:
        sinks.log("WARNING: INGEST_SITE_TOKENS is empty, so every write is refused")
    if not READ_TOKEN:
        sinks.log("WARNING: INGEST_READ_TOKEN is empty, so anyone who reaches this port can look up plates")
    sinks.log(f"influxdb: {service.influx.url or 'off'}, mqtt: {service.mqtt.host or 'off'}"
              + (" (with plates)" if sinks.MQTT_PLATES else ""))
    ThreadingHTTPServer(("0.0.0.0", PORT), handler(service)).serve_forever()


if __name__ == "__main__":
    main()
