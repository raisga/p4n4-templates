"""
Offline checks for edge/uplink/uplink.py: rows in an edge device's SQLite
(the tables AIOROS Alpha Boutique's service writes: events, alerts,
pos_transactions, zones) are forwarded to a real ingest service in this
process, and must come out as contract 0.1 documents. No Docker, no device.

    python3 tests/test_uplink.py
"""

from __future__ import annotations

import importlib.util
import json
import os
import sqlite3
import sys
import tempfile
import threading
from http.server import ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.dont_write_bytecode = True
sys.path[:0] = [str(ROOT / "iot/ingest")]
import contract  # noqa: E402
import ingest  # noqa: E402
from store import Store  # noqa: E402

failed = False


def check(label: str, ok: bool, detail: object = "") -> None:
    global failed
    print(f"  {'ok  ' if ok else 'FAIL'} {label}" + ("" if ok else f": {str(detail)[:300]}"))
    failed |= not ok


class Off:
    enabled = False
    url = host = ""

    def write(self, lines):
        pass

    def publish(self, messages):
        pass


# The device's tables, as the device writes them (INSERT OR REPLACE, so a
# re-recorded row gets a new rowid)
DEVICE_SCHEMA = """
CREATE TABLE zones (zone_id TEXT NOT NULL, store_id TEXT NOT NULL, camera_id TEXT NOT NULL, zone_type TEXT NOT NULL,
                    description TEXT, planogram_json TEXT, PRIMARY KEY (store_id, zone_id));
CREATE TABLE events (event_id TEXT PRIMARY KEY, store_id TEXT NOT NULL, camera_id TEXT NOT NULL, event_type TEXT NOT NULL,
                     ts_start TEXT NOT NULL, ts_end TEXT NOT NULL, track_id TEXT NOT NULL, role TEXT NOT NULL, zone_id TEXT,
                     dwell_seconds REAL, attributes_json TEXT, clip_uri TEXT, boxes_uri TEXT,
                     synthetic INTEGER NOT NULL DEFAULT 1, created_at TEXT NOT NULL);
CREATE TABLE alerts (alert_id TEXT PRIMARY KEY, store_id TEXT NOT NULL, camera_id TEXT NOT NULL, alert_type TEXT NOT NULL,
                     severity TEXT NOT NULL, timestamp TEXT NOT NULL, track_id TEXT NOT NULL, title TEXT NOT NULL,
                     description TEXT NOT NULL, recommended_action TEXT NOT NULL, evidence_json TEXT,
                     review_required INTEGER NOT NULL DEFAULT 1, status TEXT NOT NULL DEFAULT 'pending_review',
                     synthetic INTEGER NOT NULL DEFAULT 1, created_at TEXT NOT NULL);
CREATE TABLE pos_transactions (tx_id TEXT PRIMARY KEY, store_id TEXT NOT NULL, timestamp TEXT NOT NULL,
                               currency TEXT NOT NULL DEFAULT 'USD', shelf_id TEXT NOT NULL, brand TEXT NOT NULL,
                               garment_type TEXT NOT NULL, color TEXT NOT NULL, amount REAL NOT NULL,
                               payment_method TEXT NOT NULL, synthetic INTEGER NOT NULL DEFAULT 1);
"""
NAIVE = "2026-10-03T18:15:00.250000"  # the device's local time, no offset


class Device:
    def __init__(self, path: Path) -> None:
        self.db = sqlite3.connect(path)
        self.db.executescript(DEVICE_SCHEMA)

    def zone(self, zone_id: str, planogram: dict) -> None:
        self.db.execute("INSERT OR IGNORE INTO zones VALUES (?, 'device-store', 'cam-1', 'SHELF', '', ?)",
                        (zone_id, json.dumps(planogram)))
        self.db.commit()

    def event(self, event_id: str, kind: str, zone_id: str | None = None, attributes: dict | None = None) -> None:
        self.db.execute(
            "INSERT OR REPLACE INTO events (event_id, store_id, camera_id, event_type, ts_start, ts_end, track_id, role,"
            " zone_id, attributes_json, synthetic, created_at) VALUES (?, 'device-store', 'cam-1', ?, ?, ?, 'P001',"
            " 'customer', ?, ?, 0, ?)",
            (event_id, kind, NAIVE, NAIVE, zone_id, json.dumps(attributes) if attributes else None, NAIVE),
        )
        self.db.commit()

    def alert(self, alert_id: str) -> None:
        evidence = {"zone_id": "shelf-2", "dwell_seconds": 92.4, "items_interacted": 1, "price_tier": "alto",
                    "keyframe_image_uri": "alerts/a.jpg", "clip_uri": "clips/a.mp4", "boxes_uri": "clips/a.json"}
        self.db.execute(
            "INSERT OR REPLACE INTO alerts VALUES (?, 'device-store', 'cam-1', 'high_value_opportunity', 'medium', ?,"
            " 'P001', 'Customer interested in polos', 'Took a white polo.', 'Attend.', ?, 1, 'pending_review', 0, ?)",
            (alert_id, NAIVE, json.dumps(evidence), NAIVE),
        )
        self.db.commit()

    def sale(self, tx_id: str) -> None:
        self.db.execute("INSERT INTO pos_transactions VALUES (?, 'device-store', ?, 'USD', 'shelf-2', 'Coastline',"
                        " 'polo', 'white', 119.9, 'card', 0)", (tx_id, NAIVE))
        self.db.commit()


work = Path(tempfile.mkdtemp())
TZ = contract.store_tz("-05:00")
store = Store(":memory:", TZ)
service = ingest.Service(store, {"store-token": "store-north"}, "read", influx=Off(), mqtt=Off())
server = ThreadingHTTPServer(("127.0.0.1", 0), ingest.handler(service))
threading.Thread(target=server.serve_forever, daemon=True).start()

os.environ.update({
    "DEVICE_DB": str(work / "boutique_live.db"),
    "DEVICE_API": "http://127.0.0.1:9",  # nothing there: the heartbeat says degraded
    "INGEST_URL": f"http://127.0.0.1:{server.server_port}",
    "INGEST_TOKEN": "store-token",
    "STORE_ID": "store-north",
    "NODE_NAME": "edge-test",
    "STATE_FILE": str(work / "state/uplink.json"),
})
spec = importlib.util.spec_from_file_location("uplink", ROOT / "edge/uplink/uplink.py")
uplink = importlib.util.module_from_spec(spec)
spec.loader.exec_module(uplink)

device = Device(work / "boutique_live.db")
polo = {"shelf_id": "shelf-2", "brand": "Coastline", "category": "Polos", "price_tier": "alto", "avg_price": 120.0}
device.zone("shelf-2", polo)
device.event("EVT-20261003-000001", "customer_entry")
device.event("EVT-20261003-000002", "garment_taken", "shelf-2", {"garment_type": "polo", "color": "white"})
device.alert("ALT-20261003-000001")
device.sale("TX-1")

print("forwarding")
state = {}
db = uplink.connect()
sent = uplink.forward(db, state)
check("events, alert and sale are accepted", sent == 4, sent)
check("the cursor advances per table", state.get("events") == 2 and state.get("alerts") == 1 and state.get("pos_transactions") == 1, state)
check("the cursor is saved", uplink.load() == state, uplink.load())
check("nothing is re-sent on the next poll", uplink.forward(db, state) == 0)

with store.lock:
    lo, hi, _ = store.period(None, "2026-10-03", "2026-10-03")
    summary = store.summary("store-north", lo, hi)
    shelves = store.layout("store-north", lo, hi)["shelves"]
    alert = store.alert("store-north", "ALT-20261003-000001")
check("STORE_ID replaces the device's store id", summary["visitors"] == 1 and summary["sales_transactions"] == 1, summary)
check("the planogram travels with the event", shelves and shelves[0]["name"] == "Polos · Coastline", shelves)
check("naive device times are store time", alert["time"] == "2026-10-03T18:15:00-05:00", alert)
check("evidence comes through", alert.get("clip_uri") == "clips/a.mp4" and alert.get("zone_id") == "shelf-2", alert)

# The device replaces an event (a new rowid), e.g. once the model has described it
device.event("EVT-20261003-000002", "garment_taken", "shelf-2", {"garment_type": "polo", "color": "navy"})
uplink.forward(db, state)
with store.lock:
    ranking = store.ranking("store-north", lo, hi)["ranking"]
check("a replaced row is sent again and updates the ingest", any(r["color"] == "navy" for r in ranking), ranking)

print("refusals and outages")
device.event("EVT-bad", "garment_touched")
before = dict(state)
uplink.forward(db, state)
check("a refused row is skipped, not retried forever", state["events"] > before["events"], state)
uplink.INGEST_URL = "http://127.0.0.1:9"
device.event("EVT-20261003-000003", "customer_exit")
held = dict(state)
try:
    uplink.forward(db, state)
    check("an unreachable ingest raises Unavailable", False)
except uplink.Unavailable:
    check("an unreachable ingest raises Unavailable", True)
check("the cursor stays put while the ingest is down", state == held, state)
uplink.INGEST_URL = f"http://127.0.0.1:{server.server_port}"
check("and the row goes once it's back", uplink.forward(db, state) == 1)

print("heartbeat")
uplink.heartbeat(db)
with store.lock:
    nodes = store.nodes("store-north")["nodes"]
node = nodes[0] if nodes else {}
check("a heartbeat reaches the ingest", node.get("hostname") == "edge-test", nodes)
check("an unreachable device API means degraded", node.get("status") == "degraded", node.get("status"))
check("memory comes from /proc/meminfo", node.get("metrics", {}).get("ram_total_mb", 0) > 0, node.get("metrics"))
check("pending alerts come from the device's database", node.get("alerts_pending") == 1, node.get("alerts_pending"))
uplink.DEVICE_API = ""
uplink.heartbeat(db)
with store.lock:
    check("without a device API, a present database is healthy", store.nodes("store-north")["nodes"][0]["status"] == "healthy")
uplink.STORE_ID = ""
seen = []
uplink.post, original = (lambda path, body: seen.append(body["store_id"])), uplink.post
uplink.heartbeat(db)
uplink.post = original
check("without STORE_ID, the heartbeat names the store of the latest event", seen == ["device-store"], seen)

db.close()
server.shutdown()
sys.exit(1 if failed else 0)
