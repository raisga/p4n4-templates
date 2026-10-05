"""
Uplink: forwards what the site's ALPR device records to the ingest service,
as the ALPR device ↔ web contract 0.1 says (outbound HTTPS, X-Site-Token,
from behind the site's NAT).

The device (e.g. an ALPR service on a Jetson) writes each vehicle it sees to
a SQLite file (alpr_live.db: table reads) but posts nothing itself. This
sidecar reads that file, read only, and posts:

    reads                      → POST /api/v1/reads      new or replaced rows, in batches
    every HEARTBEAT_INTERVAL s → POST /api/v1/heartbeat  from /proc and /sys

It remembers the last row it delivered (STATE_FILE), so a restart or an
outage resumes where it stopped; the ingest service counts a re-sent row as
unchanged. A row the ingest service refuses is logged by id and skipped,
never retried forever. Plates never appear in its log.

Standard library only, so it runs on the stock python image.

    DEVICE_DB           the device's SQLite (default /data/alpr_live.db)
    DEVICE_API          the device's own HTTP API, polled to tell healthy from
                        degraded (e.g. http://alpr:8080). Empty: healthy
                        while the database is there
    INGEST_URL          the ingest service, e.g. https://traffic.example.com
    INGEST_TOKEN        this site's token (X-Site-Token)
    SITE_ID             replaces the device's site_id; empty keeps it
    NODE_NAME           this device's name in heartbeats and Grafana
    JETPACK_VERSION, POWER_MODE   reported as they are, if set
    DEVICE_CAMERAS      cameras the device processes (default 2)
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

DEVICE_DB = os.environ.get("DEVICE_DB", "/data/alpr_live.db")
DEVICE_API = os.environ.get("DEVICE_API", "").strip().rstrip("/")
INGEST_URL = os.environ.get("INGEST_URL", "http://ingest:8080").rstrip("/")
TOKEN = os.environ.get("INGEST_TOKEN", "")
SITE_ID = os.environ.get("SITE_ID", "").strip()
NODE_NAME = os.environ.get("NODE_NAME", "").strip() or "edge-01"
CAMERAS = int(os.environ.get("DEVICE_CAMERAS") or 2)
POLL = float(os.environ.get("POLL_INTERVAL") or 5)
BEAT = float(os.environ.get("HEARTBEAT_INTERVAL") or 30)
STATE_FILE = Path(os.environ.get("STATE_FILE", "/state/uplink.json"))
BATCH = 200

READ = "p4n4.alpr.read/0.1"
HEARTBEAT = "p4n4.alpr.heartbeat/0.1"


def log(message: str) -> None:
    print(f"{time.strftime('%Y-%m-%dT%H:%M:%S')} {message}", flush=True)


class Unavailable(Exception):
    """The ingest service didn't take the batch; try again later."""


def post(path: str, body: object) -> dict:
    request = urllib.request.Request(
        f"{INGEST_URL}{path}",
        data=json.dumps(body, ensure_ascii=False).encode(),
        headers={"Content-Type": "application/json", "X-Site-Token": TOKEN},
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


def read_doc(row: sqlite3.Row) -> dict:
    doc = {
        "schema": READ,
        "read_id": row["read_id"],
        "site_id": SITE_ID or row["site_id"],
        "camera_id": row["camera_id"],
        "timestamp": row["timestamp"],
        "direction": row["direction"],
        "vehicle_type": row["vehicle_type"],
        "plate": row["plate"] or None,
        "synthetic": bool(row["synthetic"]),
    }
    if doc["plate"]:
        doc["plate_confidence"] = row["plate_confidence"]
        if row["plate_region"]:
            doc["plate_region"] = row["plate_region"]
    for key in ("lane", "speed_kmh", "colour"):
        if row[key] is not None:
            doc[key] = row[key]
    evidence = {k: row[k] for k in ("image_uri", "plate_crop_uri", "clip_uri") if row[k]}
    if evidence:
        doc["evidence"] = evidence
    return doc


def connect() -> sqlite3.Connection | None:
    if not Path(DEVICE_DB).exists():
        return None
    db = sqlite3.connect(f"file:{DEVICE_DB}?mode=ro", uri=True, timeout=10)
    db.row_factory = sqlite3.Row
    return db


def forward(db: sqlite3.Connection, state: dict) -> int:
    """Post every row past the cursor; returns how many went."""
    sent = 0
    query = "SELECT rowid AS _rowid, * FROM reads WHERE rowid > ? ORDER BY rowid LIMIT ?"
    try:
        rows = db.execute(query, (state.get("reads", 0), BATCH)).fetchall()
    except sqlite3.OperationalError:
        return 0  # the device hasn't created the table yet
    while rows:
        result = post("/api/v1/reads", [read_doc(r) for r in rows])
        for item in result.get("rejected", []):
            log(f"reads: {item.get('read_id') or item.get('index')} refused: {item.get('error')}")
        state["reads"] = rows[-1]["_rowid"]
        save(state)
        sent += result.get("accepted", 0)
        rows = db.execute(query, (state["reads"], BATCH)).fetchall()
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
    """A heartbeat for SITE_ID, or the site the device's latest read names; none before either is known."""
    site = SITE_ID
    if not site and db is not None:
        try:
            row = db.execute("SELECT site_id FROM reads ORDER BY rowid DESC LIMIT 1").fetchone()
            site = row["site_id"] if row else ""
        except sqlite3.OperationalError:
            pass
    if not site:
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
    pipeline: dict = {"active_cameras": CAMERAS}
    if db is not None:
        try:
            since = time.time() - 60
            pipeline["vehicles_last_minute"] = sum(
                1 for (ts,) in db.execute("SELECT timestamp FROM reads ORDER BY rowid DESC LIMIT 500")
                if _epoch(ts) >= since
            )
        except sqlite3.OperationalError:
            pass
    status = "error" if db is None else "healthy" if device_healthy() else "degraded"
    post("/api/v1/heartbeat", {
        "schema": HEARTBEAT,
        "site_id": site,
        "timestamp": datetime.now().astimezone().isoformat(timespec="seconds"),
        "device": device,
        "metrics": {k: v for k, v in metrics.items() if v is not None},
        "pipeline": pipeline,
        "status": status,
    })


def _epoch(ts: str) -> float:
    """A device timestamp in epoch seconds; one without an offset is this machine's local time."""
    try:
        parsed = datetime.fromisoformat(ts.replace("Z", "+00:00") if ts.endswith("Z") else ts)
    except (TypeError, ValueError):
        return 0.0
    return (parsed if parsed.tzinfo else parsed.astimezone()).timestamp()


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
        sys.exit("INGEST_TOKEN is empty: set it to this site's token (SITE_TOKEN in iot/.env)")
    state = load()
    log(f"uplink {DEVICE_DB} → {INGEST_URL} as {SITE_ID or 'the device’s site_id'}, from {state or 'the start'}")
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
