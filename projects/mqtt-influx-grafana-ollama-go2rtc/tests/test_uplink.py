"""
Offline checks for edge/uplink/uplink.py: rows in an ALPR device's SQLite
(the table the device's service writes: reads) are forwarded to a real
ingest service in this process, and must come out as contract 0.1
documents. No Docker, no device.

    python3 tests/test_uplink.py
"""

from __future__ import annotations

import contextlib
import importlib.util
import io
import os
import sqlite3
import sys
import tempfile
import threading
from datetime import datetime, timedelta
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


# The device's table, as the device writes it (INSERT OR REPLACE, so a
# corrected row gets a new rowid)
DEVICE_SCHEMA = """
CREATE TABLE reads (read_id TEXT PRIMARY KEY, site_id TEXT NOT NULL, camera_id TEXT NOT NULL, timestamp TEXT NOT NULL,
                    direction TEXT NOT NULL, lane INTEGER, vehicle_type TEXT NOT NULL, plate TEXT, plate_confidence REAL,
                    plate_region TEXT, speed_kmh REAL, colour TEXT, image_uri TEXT, plate_crop_uri TEXT, clip_uri TEXT,
                    synthetic INTEGER NOT NULL DEFAULT 0);
"""
TZ = contract.site_tz("-05:00")
# Yesterday at 08:15 site time, without an offset, as the device writes it
NAIVE = (datetime.now(TZ) - timedelta(days=1)).replace(hour=8, minute=15, second=0, microsecond=0).strftime("%Y-%m-%dT%H:%M:%S.%f")
DAY = NAIVE[:10]


class Device:
    def __init__(self, path: Path) -> None:
        self.db = sqlite3.connect(path)
        self.db.executescript(DEVICE_SCHEMA)

    def read(self, read_id: str, plate: str | None = "ABC-1234", direction: str = "inbound", **extra) -> None:
        row = {"read_id": read_id, "site_id": "device-site", "camera_id": "cam-in-01", "timestamp": NAIVE,
               "direction": direction, "lane": 1, "vehicle_type": "car", "plate": plate,
               "plate_confidence": 0.91 if plate else None, "plate_region": "PA" if plate else None,
               "speed_kmh": 44.5, "colour": "white", "image_uri": "frames/1.jpg", "plate_crop_uri": "plates/1.jpg",
               "clip_uri": None, "synthetic": 0, **extra}
        self.db.execute(f"INSERT OR REPLACE INTO reads ({', '.join(row)}) VALUES ({', '.join('?' * len(row))})",
                        tuple(row.values()))
        self.db.commit()


work = Path(tempfile.mkdtemp())
store = Store(":memory:", TZ)
service = ingest.Service(store, {"site-token": "site-north"}, "read", influx=Off(), mqtt=Off())
server = ThreadingHTTPServer(("127.0.0.1", 0), ingest.handler(service))
threading.Thread(target=server.serve_forever, daemon=True).start()

os.environ.update({
    "DEVICE_DB": str(work / "alpr_live.db"),
    "DEVICE_API": "http://127.0.0.1:9",  # nothing there: the heartbeat says degraded
    "INGEST_URL": f"http://127.0.0.1:{server.server_port}",
    "INGEST_TOKEN": "site-token",
    "SITE_ID": "site-north",
    "NODE_NAME": "edge-test",
    "STATE_FILE": str(work / "state/uplink.json"),
})
spec = importlib.util.spec_from_file_location("uplink", ROOT / "edge/uplink/uplink.py")
uplink = importlib.util.module_from_spec(spec)
spec.loader.exec_module(uplink)

device = Device(work / "alpr_live.db")
device.read("RD-20261003-000001")
device.read("RD-20261003-000002", "KDL-4821", "outbound", camera_id="cam-out-01", vehicle_type="van")
device.read("RD-20261003-000003", None, vehicle_type="motorcycle")

print("forwarding")
state = {}
db = uplink.connect()
sent = uplink.forward(db, state)
check("three reads are accepted", sent == 3, sent)
check("the cursor advances", state.get("reads") == 3, state)
check("the cursor is saved", uplink.load() == state, uplink.load())
check("nothing is re-sent on the next poll", uplink.forward(db, state) == 0)

with store.lock:
    lo, hi, _ = store.period(None, DAY, DAY)
    summary = store.summary("site-north", lo, hi)
    plate = store.plate("site-north", "ABC-1234", lo, hi)
check("SITE_ID replaces the device's site id", summary["vehicles"] == 3, summary)
check("a vehicle without a plate is counted, not read", summary["plates_read"] == 2 and summary["read_rate"] == round(2 / 3, 3), summary)
check("naive device times are site time", plate["passages"] and plate["passages"][0]["time"] == f"{DAY}T08:15:00-05:00", plate)
with store.lock:
    doc = store.db.execute("SELECT doc FROM reads WHERE read_id = 'RD-20261003-000001'").fetchone()[0]
check("evidence and speed come through", '"plate_crop_uri":"plates/1.jpg"' in doc and '"speed_kmh":44.5' in doc, doc)

# The device corrects a read (a new rowid), e.g. after a second look at the plate
device.read("RD-20261003-000001", "ABC-1284")
uplink.forward(db, state)
with store.lock:
    fixed = store.plate("site-north", "ABC-1284", lo, hi)
check("a corrected row is sent again and updates the ingest", fixed["passages_total"] == 1, fixed)

print("refusals and outages")
device.read("RD-bad", "SECRET-1")
before = dict(state)
log = io.StringIO()
with contextlib.redirect_stdout(log):
    uplink.forward(db, state)
check("a refused row is skipped, not retried forever", state["reads"] > before["reads"], state)
check("the log names the refused row, not its plate", "RD-bad" in log.getvalue() and "SECRET" not in log.getvalue(), log.getvalue())
uplink.INGEST_URL = "http://127.0.0.1:9"
device.read("RD-20261003-000004")
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
    nodes = store.nodes("site-north")["nodes"]
node = nodes[0] if nodes else {}
check("a heartbeat reaches the ingest", node.get("hostname") == "edge-test", nodes)
check("an unreachable device API means degraded", node.get("status") == "degraded", node.get("status"))
check("memory comes from /proc/meminfo", node.get("metrics", {}).get("ram_total_mb", 0) > 0, node.get("metrics"))
check("the heartbeat reports both cameras", node.get("pipeline", {}).get("active_cameras") == 2, node.get("pipeline"))
uplink.DEVICE_API = ""
uplink.heartbeat(db)
with store.lock:
    check("without a device API, a present database is healthy", store.nodes("site-north")["nodes"][0]["status"] == "healthy")
uplink.SITE_ID = ""
seen = []
uplink.post, original = (lambda path, body: seen.append(body["site_id"])), uplink.post
uplink.heartbeat(db)
uplink.post = original
check("without SITE_ID, the heartbeat names the site of the latest read", seen == ["device-site"], seen)

db.close()
server.shutdown()
sys.exit(1 if failed else 0)
