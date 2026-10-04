"""
Uplink: forwards what the store's edge device records to the ingest service,
as the AIOROS Alpha Boutique Jetson ↔ web contract 0.1 says (outbound HTTPS,
X-Store-Token, from behind the store's NAT).

The device (e.g. AIOROS Alpha Boutique's service on a Jetson) writes events,
alerts and POS lines to a SQLite file (boutique_live.db: tables events,
alerts, pos_transactions and zones) but posts nothing itself. This sidecar
reads that file, read only, and posts:

    events            → POST /api/v1/events     new or replaced rows, in batches
    alerts            → POST /api/v1/alerts
    pos_transactions  → POST /api/v1/pos
    every HEARTBEAT_INTERVAL s → POST /api/v1/heartbeat, from /proc and /sys

It remembers the last row it delivered from each table (STATE_FILE), so a
restart or an outage resumes where it stopped; the ingest service counts a
re-sent row as unchanged. A row the ingest service refuses is logged and
skipped, never retried forever.

Standard library only, so it runs on the stock python image.

    DEVICE_DB           the device's SQLite (default /data/boutique_live.db)
    DEVICE_API          the device's own HTTP API, polled to tell healthy from
                        degraded (e.g. http://alpha:8080). Empty: healthy
                        while the database is there
    INGEST_URL          the ingest service, e.g. https://stores.example.com
    INGEST_TOKEN        this store's token (X-Store-Token)
    STORE_ID            replaces the device's store_id; empty keeps it
    CAMERA_ID           replaces the camera_id likewise; empty keeps it
    NODE_NAME           this device's name in heartbeats and Grafana
    JETPACK_VERSION, POWER_MODE   reported as they are, if set
    DEVICE_CAMERAS      cameras the device processes (default 1)
    POLL_INTERVAL       seconds between reads of the SQLite (default 5)
    HEARTBEAT_INTERVAL  seconds between heartbeats (default 30, the contract's)
    STATE_FILE          default /state/uplink.json
"""

from __future__ import annotations

import glob
import json
import os
import shutil
import sqlite3
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path

DEVICE_DB = os.environ.get("DEVICE_DB", "/data/boutique_live.db")
DEVICE_API = os.environ.get("DEVICE_API", "").strip().rstrip("/")
INGEST_URL = os.environ.get("INGEST_URL", "http://ingest:8080").rstrip("/")
TOKEN = os.environ.get("INGEST_TOKEN", "")
STORE_ID = os.environ.get("STORE_ID", "").strip()
CAMERA_ID = os.environ.get("CAMERA_ID", "").strip()
NODE_NAME = os.environ.get("NODE_NAME", "").strip() or "edge-01"
CAMERAS = int(os.environ.get("DEVICE_CAMERAS") or 1)
POLL = float(os.environ.get("POLL_INTERVAL") or 5)
BEAT = float(os.environ.get("HEARTBEAT_INTERVAL") or 30)
STATE_FILE = Path(os.environ.get("STATE_FILE", "/state/uplink.json"))
BATCH = 200

EVENT = "aioros.boutique.event/0.1"
ALERT = "aioros.boutique.alert/0.1"
HEARTBEAT = "aioros.boutique.heartbeat/0.1"


def log(message: str) -> None:
    print(f"{time.strftime('%Y-%m-%dT%H:%M:%S')} {message}", flush=True)


class Unavailable(Exception):
    """The ingest service didn't take the batch; try again later."""


def post(path: str, body: object) -> dict:
    request = urllib.request.Request(
        f"{INGEST_URL}{path}",
        data=json.dumps(body, ensure_ascii=False).encode(),
        headers={"Content-Type": "application/json", "X-Store-Token": TOKEN},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return json.load(response)
    except urllib.error.HTTPError as e:
        text = e.read().decode(errors="replace")[:300]
        if e.code == 400:
            # Every document refused: log them and move on
            try:
                return json.loads(text)
            except ValueError:
                return {"accepted": 0, "rejected": [{"error": text}]}
        raise Unavailable(f"HTTP {e.code} {text}") from None
    except (urllib.error.URLError, OSError) as e:
        raise Unavailable(str(e)) from None


# ── rows → contract documents ─────────────────────────────────────────────────


def loads(text: str | None) -> dict | None:
    try:
        value = json.loads(text) if text else None
    except ValueError:
        return None
    return value if isinstance(value, dict) else None


def ids(doc: dict) -> dict:
    if STORE_ID:
        doc["store_id"] = STORE_ID
    if CAMERA_ID and "camera_id" in doc:
        doc["camera_id"] = CAMERA_ID
    return doc


def event_doc(row: sqlite3.Row, planograms: dict) -> dict:
    doc = {
        "schema": EVENT,
        "store_id": row["store_id"],
        "camera_id": row["camera_id"],
        "event_id": row["event_id"],
        "type": row["event_type"],
        "ts_start": row["ts_start"],
        "ts_end": row["ts_end"],
        "track_id": row["track_id"],
        "role": row["role"],
        "synthetic": bool(row["synthetic"]),
    }
    if row["zone_id"]:
        doc["zone_id"] = row["zone_id"]
        planogram = planograms.get((row["store_id"], row["zone_id"]))
        if planogram:
            doc["planogram"] = planogram
    if row["dwell_seconds"] is not None:
        doc["dwell_seconds"] = row["dwell_seconds"]
    attributes = loads(row["attributes_json"])
    if attributes:
        doc["attributes"] = attributes
    if row["clip_uri"] or row["boxes_uri"]:
        doc["clip"] = {k: v for k, v in (("video_uri", row["clip_uri"]), ("boxes_uri", row["boxes_uri"])) if v}
    return ids(doc)


def alert_doc(row: sqlite3.Row) -> dict:
    doc = {
        "schema": ALERT,
        "alert_id": row["alert_id"],
        "store_id": row["store_id"],
        "camera_id": row["camera_id"],
        "alert_type": row["alert_type"],
        "severity": row["severity"],
        "timestamp": row["timestamp"],
        "track_id": row["track_id"],
        "title": row["title"],
        "description": row["description"],
        "recommended_action": row["recommended_action"],
        "review_required": bool(row["review_required"]),
        "status": row["status"],
        "synthetic": bool(row["synthetic"]),
    }
    evidence = loads(row["evidence_json"])
    if evidence:
        doc["evidence"] = evidence
    return ids(doc)


def sale_doc(row: sqlite3.Row) -> dict:
    doc = {k: row[k] for k in ("tx_id", "store_id", "timestamp", "currency", "shelf_id", "brand", "garment_type",
                               "color", "amount", "payment_method")}
    doc["synthetic"] = bool(row["synthetic"])
    return ids(doc)


TABLES = [
    # table, endpoint, id column
    ("events", "/api/v1/events", "event_id"),
    ("alerts", "/api/v1/alerts", "alert_id"),
    ("pos_transactions", "/api/v1/pos", "tx_id"),
]


def connect() -> sqlite3.Connection | None:
    if not Path(DEVICE_DB).exists():
        return None
    db = sqlite3.connect(f"file:{DEVICE_DB}?mode=ro", uri=True, timeout=10)
    db.row_factory = sqlite3.Row
    return db


def planograms(db: sqlite3.Connection) -> dict:
    found = {}
    for row in db.execute("SELECT store_id, zone_id, planogram_json FROM zones WHERE planogram_json IS NOT NULL"):
        planogram = loads(row["planogram_json"])
        if planogram:
            found[(row["store_id"], row["zone_id"])] = planogram
    return found


def forward(db: sqlite3.Connection, state: dict) -> int:
    """Post every row past the cursor of each table; returns how many went."""
    sent = 0
    zones = planograms(db)
    for table, path, key in TABLES:
        try:
            rows = db.execute(
                f"SELECT rowid AS _rowid, * FROM {table} WHERE rowid > ? ORDER BY rowid LIMIT ?",
                (state.get(table, 0), BATCH),
            ).fetchall()
        except sqlite3.OperationalError:
            continue  # the device hasn't created the table yet
        while rows:
            if table == "events":
                docs = [event_doc(r, zones) for r in rows]
            elif table == "alerts":
                docs = [alert_doc(r) for r in rows]
            else:
                docs = [sale_doc(r) for r in rows]
            result = post(path, docs)
            for item in result.get("rejected", []):
                log(f"{table}: {item.get(key) or item.get('index')} refused: {item.get('error')}")
            state[table] = rows[-1]["_rowid"]
            save(state)
            sent += result.get("accepted", 0)
            rows = db.execute(
                f"SELECT rowid AS _rowid, * FROM {table} WHERE rowid > ? ORDER BY rowid LIMIT ?",
                (state[table], BATCH),
            ).fetchall()
    return sent


# ── heartbeat ─────────────────────────────────────────────────────────────────


def read(path: str) -> str | None:
    try:
        return Path(path).read_text().strip()
    except OSError:
        return None


_cpu_last: tuple[int, int] | None = None


def cpu_percent() -> float | None:
    """Host CPU use since the last heartbeat, from /proc/stat (not namespaced)."""
    global _cpu_last
    line = (read("/proc/stat") or "").splitlines()[:1]
    if not line:
        return None
    numbers = [int(x) for x in line[0].split()[1:]]
    idle, total = numbers[3] + (numbers[4] if len(numbers) > 4 else 0), sum(numbers)
    last, _cpu_last = _cpu_last, (idle, total)
    if not last or total == last[1]:
        return None
    return round(100 * (1 - (idle - last[0]) / (total - last[1])), 1)


def gpu_percent() -> float | None:
    """Jetson GPU load, 0-1000 in sysfs (JetPack 5 and 6 paths)."""
    for path in ["/sys/devices/platform/gpu.0/load", *glob.glob("/sys/devices/platform/bus@0/*.gpu/load"),
                 *glob.glob("/sys/devices/platform/*.gpu/load")]:
        value = read(path)
        if value and value.isdigit():
            return round(int(value) / 10, 1)
    return None


def temperatures() -> dict:
    found = {}
    for zone in sorted(glob.glob("/sys/class/thermal/thermal_zone*")):
        kind, value = (read(f"{zone}/type") or "").lower(), read(f"{zone}/temp")
        if not value or not value.lstrip("-").isdigit():
            continue
        for name in ("cpu", "gpu"):
            if kind.startswith(name) and f"temperature_{name}_c" not in found:
                found[f"temperature_{name}_c"] = round(int(value) / 1000, 1)
    return found


def memory() -> dict:
    info = {}
    for line in (read("/proc/meminfo") or "").splitlines():
        name, _, rest = line.partition(":")
        if name in ("MemTotal", "MemAvailable"):
            info[name] = int(rest.split()[0]) // 1024
    if len(info) < 2:
        return {}
    return {"ram_used_mb": info["MemTotal"] - info["MemAvailable"], "ram_total_mb": info["MemTotal"]}


def device_healthy() -> bool:
    if not DEVICE_API:
        return True
    try:
        with urllib.request.urlopen(DEVICE_API, timeout=5) as response:
            return response.status == 200
    except (urllib.error.URLError, OSError):
        return False


def heartbeat(db: sqlite3.Connection | None) -> None:
    """A heartbeat for STORE_ID, or the store the device's latest event names; none before either is known."""
    store = STORE_ID
    if not store and db is not None:
        try:
            row = db.execute("SELECT store_id FROM events ORDER BY rowid DESC LIMIT 1").fetchone()
            store = row["store_id"] if row else ""
        except sqlite3.OperationalError:
            pass
    if not store:
        return
    metrics = {"cpu_utilization_pct": cpu_percent(), "gpu_utilization_pct": gpu_percent(), **memory(), **temperatures()}
    disk = shutil.disk_usage(Path(DEVICE_DB).parent)
    metrics.update(disk_free_gb=round(disk.free / 1e9, 1), disk_total_gb=round(disk.total / 1e9, 1))
    device = {"hostname": NODE_NAME}
    uptime = read("/proc/uptime")
    if uptime:
        device["uptime_seconds"] = int(float(uptime.split()[0]))
    for key, env in (("jetpack_version", "JETPACK_VERSION"), ("power_mode", "POWER_MODE")):
        if os.environ.get(env):
            device[key] = os.environ[env]
    pending = 0
    if db is not None:
        try:
            pending = db.execute("SELECT COUNT(*) FROM alerts WHERE status = 'pending_review'").fetchone()[0]
        except sqlite3.OperationalError:
            pass
    status = "error" if db is None else "healthy" if device_healthy() else "degraded"
    post("/api/v1/heartbeat", {
        "schema": HEARTBEAT,
        "store_id": store,
        "timestamp": datetime.now().astimezone().isoformat(timespec="seconds"),
        "device": device,
        "metrics": {k: v for k, v in metrics.items() if v is not None},
        "pipeline": {"active_cameras": CAMERAS},
        "alerts_pending": pending,
        "status": status,
    })


# ── main loop ─────────────────────────────────────────────────────────────────


def load() -> dict:
    try:
        return json.loads(STATE_FILE.read_text())
    except (OSError, ValueError):
        return {}


def save(state: dict) -> None:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = STATE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(state))
    tmp.replace(STATE_FILE)


def main() -> None:
    if not TOKEN:
        sys.exit("INGEST_TOKEN is empty: set it to this store's token (STORE_TOKEN in iot/.env)")
    state = load()
    log(f"uplink {DEVICE_DB} → {INGEST_URL} as {STORE_ID or 'the device’s store_id'}, from {state or 'the start'}")
    cpu_percent()
    last_beat, backoff, waiting = 0.0, POLL, False
    while True:
        db = None
        try:
            db = connect()
            if db is None and not waiting:
                log(f"waiting for {DEVICE_DB}")
                waiting = True
            if db is not None:
                waiting = False
                sent = forward(db, state)
                if sent:
                    log(f"forwarded {sent} rows")
            if time.monotonic() - last_beat >= BEAT:
                heartbeat(db)
                last_beat = time.monotonic()
            backoff = POLL
        except (Unavailable, sqlite3.Error) as e:
            log(f"will retry in {backoff:.0f} s: {e}")
            backoff = min(300.0, backoff * 2)
        finally:
            if db is not None:
                db.close()
        time.sleep(backoff)


if __name__ == "__main__":
    main()
