"""
Retail-vision ingest: the web side of the AIOROS Alpha Boutique Jetson ↔ web
contract 0.1 (schemas aioros.boutique.*/0.1).

Each store's Jetson posts what it sees (outbound HTTPS, from behind the
store's NAT); this service checks it against the contract, keeps it in
SQLite, copies it to InfluxDB and MQTT (sinks.py), and answers the
agent's questions with numbers computed from what it stored (store.py).

    POST /api/v1/events      contract §2: one event, a list, or {"events": [...]}
    POST /api/v1/alerts      contract §3: one alert, a list, or {"alerts": [...]}
    POST /api/v1/reports     contract §4
    POST /api/v1/heartbeat   contract §5
    POST /api/v1/pos         point-of-sale lines (not in contract 0.1)
        X-Store-Token: <the store's token> (or Authorization: Bearer <token>);
        a token only writes its own store_id.

    GET  /api/v1/agent/summary         contract §6.1   period | from, to
    GET  /api/v1/agent/alerts          contract §6.2   period | from, to; type, severity, status, limit
    GET  /api/v1/agent/layout          contract §6.3   period | from, to
    GET  /api/v1/agent/ranking         contract §6.4   period | from, to; category, color, limit
    GET  /api/v1/agent/restock_advice  contract §6.5
    GET  /api/v1/agent/nodes           each Jetson's latest heartbeat
    GET  /api/v1/reports/latest        the newest report a Jetson sent
    POST /api/v1/alerts/<alert_id>/status  {"status": "attended" | "dismissed" | "pending_review", "by": "..."}
        Authorization: Bearer <INGEST_READ_TOKEN> for every store, or a
        store's token for that store. Every GET takes store_id; without
        it, INGEST_DEFAULT_STORE, or the only store there is.

    GET  /health

Standard library only, so it runs on the stock python image.

    INGEST_PORT          Port to listen on (default 8080)
    INGEST_DB            SQLite file (default /data/ingest.db)
    INGEST_STORE_TOKENS  store_id:token pairs, comma-separated. Required:
                         with none, every write is refused
    INGEST_READ_TOKEN    Token for the GET endpoints and alert reviews.
                         Empty: reads need no token (trusted network only)
    INGEST_DEFAULT_STORE Store a read means when it names none (default: the
                         only store, when there is one)
    STORE_TZ             Store time zone, an IANA name or an offset
                         (default UTC). Days, weeks and peak hours use it,
                         and so do timestamps without an offset
    STORE_LANG           Language of the text in answers (rules, weekdays):
                         en (default) or es
    INFLUXDB_URL, INFLUXDB_TOKEN, INFLUXDB_ORG, INFLUXDB_BUCKET
                         Where to copy points (empty URL: don't)
    MQTT_HOST, MQTT_PORT, MQTT_USER, MQTT_PASSWORD
                         Where to publish (empty host: don't)
    MQTT_TOPIC_ROOT      First topic level (default retail)
"""

from __future__ import annotations

import hmac
import json
import os
import queue
import re
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
TZ = contract.store_tz(os.environ.get("STORE_TZ", "UTC"))
LANG = os.environ.get("STORE_LANG", "en").strip() or "en"
READ_TOKEN = os.environ.get("INGEST_READ_TOKEN", "").strip()
DEFAULT_STORE = os.environ.get("INGEST_DEFAULT_STORE", "").strip()
MAX_BODY = 8 * 1024 * 1024
MAX_BATCH = 1000
REVIEW_PATH = re.compile(r"^/api/v1/alerts/(ALT-[0-9]{8}-[0-9]{6})/status$")


def parse_tokens(text: str) -> dict[str, str]:
    """INGEST_STORE_TOKENS → {token: store_id}."""
    tokens = {}
    for pair in filter(None, (p.strip() for p in text.split(","))):
        store_id, _, token = pair.partition(":")
        if not token.strip() or not contract.ID.match(store_id.strip()):
            raise SystemExit(f"INGEST_STORE_TOKENS: expected store_id:token, got {pair!r}")
        tokens[token.strip()] = store_id.strip()
    return tokens


class Kind:
    """One POST endpoint: how to check, store and copy its documents."""

    def __init__(self, name, key, check, put, id_key, lines, topic):
        self.name, self.key, self.check, self.put = name, key, check, put
        self.id_key, self.lines, self.topic = id_key, lines, topic


class Service:
    def __init__(self, store: Store, tokens: dict[str, str], read_token: str = "", default_store: str = "",
                 influx: sinks.Influx | None = None, mqtt: sinks.Mqtt | None = None) -> None:
        self.store, self.tokens, self.read_token = store, tokens, read_token
        self.default_store = default_store
        self.influx = influx or sinks.Influx()
        self.mqtt = mqtt or sinks.Mqtt()
        self.tz = store.tz
        self.outbox: queue.Queue = queue.Queue(maxsize=10_000)
        threading.Thread(target=self._drain, daemon=True).start()
        self.kinds = {
            "/api/v1/events": Kind(
                "event", "events", contract.check_event, store.put_event, "event_id",
                sinks.event_lines, lambda d: sinks.topic(d["store_id"], "event", d["type"]),
            ),
            "/api/v1/alerts": Kind(
                "alert", "alerts", contract.check_alert, store.put_alert, "alert_id",
                sinks.alert_lines, lambda d: sinks.topic(d["store_id"], "alert", d["alert_type"]),
            ),
            "/api/v1/reports": Kind(
                "report", "reports", contract.check_report, store.put_report, "report_id",
                lambda d, tz: [], lambda d: sinks.topic(d["store_id"], "report"),
            ),
            "/api/v1/heartbeat": Kind(
                "heartbeat", "heartbeats", contract.check_heartbeat, store.put_heartbeat, "timestamp",
                sinks.heartbeat_lines, lambda d: sinks.topic(d["store_id"], "heartbeat"),
            ),
            "/api/v1/pos": Kind(
                "sale", "sales", contract.check_sale, store.put_sale, "tx_id",
                sinks.sale_lines, lambda d: sinks.topic(d["store_id"], "sale"),
            ),
        }

    def _drain(self) -> None:
        """Copies to InfluxDB and MQTT off the request thread, so a slow sink never delays a Jetson."""
        while True:
            lines, messages = self.outbox.get()
            self.influx.write(lines)
            self.mqtt.publish(messages)

    def flush(self, timeout: float = 5.0) -> None:
        """Wait for the outbox to empty (tests)."""
        deadline = time.monotonic() + timeout
        while not self.outbox.empty() and time.monotonic() < deadline:
            time.sleep(0.01)

    # ── auth ──────────────────────────────────────────────────────────────────

    def _match(self, token: str) -> str | None:
        """Store the token writes to, or None. Compared in constant time."""
        found = None
        for known, store_id in self.tokens.items():
            if hmac.compare_digest(known.encode(), token.encode()):
                found = store_id
        return found

    def writer(self, headers) -> str | None:
        return self._match(_token(headers)) if self.tokens else None

    def reader(self, headers) -> tuple[bool, str | None]:
        """(allowed, the one store the token is limited to, or None for all)."""
        token = _token(headers)
        if not self.read_token:
            return True, None
        if token and hmac.compare_digest(self.read_token.encode(), token.encode()):
            return True, None
        store_id = self._match(token) if token else None
        return (store_id is not None), store_id

    # ── writes ────────────────────────────────────────────────────────────────

    def ingest(self, path: str, body: object, store_id: str) -> tuple[int, dict]:
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
            if not error and doc["store_id"] != store_id:
                error = f"this token writes store {store_id!r} only"
            if error:
                ref = doc.get(kind.id_key) if isinstance(doc, dict) else None
                rejected.append({"index": index, kind.id_key: ref, "error": error})
                continue
            try:
                outcome = kind.put(doc)
            except (sqlite3.Error, ValueError) as e:
                rejected.append({"index": index, kind.id_key: doc.get(kind.id_key), "error": f"not stored: {e}"})
                continue
            counts[outcome] += 1
            if outcome != "unchanged":
                lines += kind.lines(doc, self.tz)
                messages.append((kind.topic(doc), doc))
        if lines or messages:
            try:
                self.outbox.put_nowait((lines, messages))
            except queue.Full:
                sinks.log(f"outbox full: {len(lines)} points and {len(messages)} messages dropped")
        accepted = sum(counts.values())
        if rejected:
            sinks.log(f"{kind.key} from {store_id}: {accepted} accepted, {len(rejected)} rejected: {rejected[0]['error']}")
        status = 400 if rejected and not accepted else 200
        return status, {"accepted": accepted, **counts, "rejected": rejected}

    # ── reads ─────────────────────────────────────────────────────────────────

    def pick_store(self, params: dict, limit: str | None) -> str:
        wanted = (params.get("store_id") or "").strip()
        if limit:
            if wanted and wanted != limit:
                raise PermissionError(f"this token reads store {limit!r} only")
            return limit
        if wanted or self.default_store:
            return wanted or self.default_store
        known = sorted(set(self.store.stores()) | set(self.tokens.values()))
        if len(known) == 1:
            return known[0]
        raise PeriodError(f"pass store_id, one of: {', '.join(known) or '(no stores yet)'}")

    def query(self, path: str, params: dict, limit: str | None) -> dict | None:
        s = self.store
        if path == "/api/v1/stores":
            stores = s.stores()
            return {"stores": [x for x in stores if not limit or x == limit]}
        if path == "/api/v1/agent/nodes":
            return s.nodes(limit or params.get("store_id") or None)
        store = self.pick_store(params, limit)
        if path == "/api/v1/reports/latest":
            return s.latest_report(store) or {"store_id": store, "report": None}
        if path == "/api/v1/agent/restock_advice":
            return s.restock_advice(store)
        lo, hi, period = s.period(params.get("period"), params.get("from"), params.get("to"))
        number = lambda key, default: max(1, min(100, int(params.get(key) or default)))  # noqa: E731
        if path == "/api/v1/agent/summary":
            result = s.summary(store, lo, hi)
        elif path == "/api/v1/agent/alerts":
            result = s.alerts(store, lo, hi, params.get("type"), params.get("severity"), params.get("status"),
                              number("limit", 20))
        elif path == "/api/v1/agent/layout":
            result = s.layout(store, lo, hi)
        elif path == "/api/v1/agent/ranking":
            result = s.ranking(store, lo, hi, params.get("category"), params.get("color"), number("limit", 5))
        else:
            return None
        return {"period": period, **result}


def _token(headers) -> str:
    token = (headers.get("X-Store-Token") or "").strip()
    auth = (headers.get("Authorization") or "").strip()
    if not token and auth[:7].lower() == "bearer ":
        token = auth[7:].strip()
    return token


def handler(service: Service):
    class Handler(BaseHTTPRequestHandler):
        server_version = "retail-ingest/0.1"
        protocol_version = "HTTP/1.1"

        def log_message(self, format: str, *args) -> None:
            pass

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
                    "stores": service.store.stores(),
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

            review = REVIEW_PATH.match(path)
            if review:
                allowed, limit = service.reader(self.headers)
                if not allowed:
                    return self._send(401, {"error": "Authorization: Bearer <read token> required"})
                if not isinstance(body, dict):
                    return self._send(400, {"error": 'expected {"status": "...", "by": "..."}'})
                try:
                    store = service.pick_store(body, limit)
                    alert = service.store.review_alert(store, review.group(1), str(body.get("status")),
                                                       str(body["by"])[:64] if body.get("by") else None)
                except PermissionError as e:
                    return self._send(403, {"error": str(e)})
                except (PeriodError, ValueError) as e:
                    return self._send(400, {"error": str(e)})
                if alert is None:
                    return self._send(404, {"error": f"no alert {review.group(1)} in store {store}"})
                return self._send(200, alert)

            if path not in service.kinds:
                return self._send(404, {"error": f"no endpoint {path}"})
            if not service.tokens:
                return self._send(503, {"error": "no store tokens configured (INGEST_STORE_TOKENS)"})
            store_id = service.writer(self.headers)
            if store_id is None:
                return self._send(401, {"error": "X-Store-Token: <store token> required"})
            status, result = service.ingest(path, body, store_id)
            self._send(status, result)

    return Handler


def main() -> None:
    tokens = parse_tokens(os.environ.get("INGEST_STORE_TOKENS", ""))
    Path(DB).parent.mkdir(parents=True, exist_ok=True)
    service = Service(Store(DB, TZ, LANG), tokens, READ_TOKEN, DEFAULT_STORE)
    sinks.log(f"ingest on :{PORT}, {DB}, stores with a token: {', '.join(sorted(set(tokens.values()))) or 'none'}")
    if not tokens:
        sinks.log("WARNING: INGEST_STORE_TOKENS is empty, so every write is refused")
    if not READ_TOKEN:
        sinks.log("WARNING: INGEST_READ_TOKEN is empty, so anyone who reaches this port can read every store")
    sinks.log(f"influxdb: {service.influx.url or 'off'}, mqtt: {service.mqtt.host or 'off'}")
    ThreadingHTTPServer(("0.0.0.0", PORT), handler(service)).serve_forever()


if __name__ == "__main__":
    main()
