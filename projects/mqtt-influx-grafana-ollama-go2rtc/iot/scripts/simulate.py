#!/usr/bin/env python3
"""
Demo road for the ingest service (compose profile "demo"): a synthetic
two-camera ALPR site whose edge device posts contract 0.1 plate reads and
heartbeats, as a real one would.

Each day is planned up front from a seed (site id + date), so the plan is
the same on every run: on start the simulator backfills the last
SIMULATOR_BACKFILL_DAYS days, then posts the rest of today's plan as its
time comes. Restarting re-sends the same ids, which the ingest service
counts as unchanged. Everything is marked "synthetic": true, and the plates
are made up.

The plan has a story the agent can find:
  - weekdays peak inbound in the morning (07:00-09:00) and outbound in the
    evening (17:00-19:00); weekends are quieter, with a midday peak
  - the busiest vehicles are the three buses of a bus line (MTB-1101,
    MTB-1102, MTB-1103), then a delivery van (KDL-4821) on weekdays
  - cam-out-01 reads far fewer plates at night (headlight glare): check C2
  - outbound traffic is faster late at night
  - about 2% of plates are misread by one character (O for 0, B for 8, …)

    INGEST_URL                default http://ingest:8080
    INGEST_TOKEN              the site's write token (INGEST_SITE_TOKENS)
    SITE_ID                   default demo-road
    SITE_TZ                   default UTC
    SIMULATOR_INTERVAL        seconds between posts (default 15)
    SIMULATOR_BACKFILL_DAYS   days of history to post on start (default 7)
    SIMULATOR_HOST            the simulated device's hostname
    simulate.py --once        backfill and one heartbeat, then exit (tests)
"""

from __future__ import annotations

import json
import os
import random
import re
import sys
import time
import urllib.error
import urllib.request
from datetime import date, datetime, timedelta, timezone, tzinfo

INGEST_URL = os.environ.get("INGEST_URL", "http://ingest:8080").rstrip("/")
TOKEN = os.environ.get("INGEST_TOKEN", "")
SITE = os.environ.get("SITE_ID", "demo-road")
INTERVAL = float(os.environ.get("SIMULATOR_INTERVAL") or 15)
BACKFILL = int(os.environ.get("SIMULATOR_BACKFILL_DAYS") or 7)
HOST = os.environ.get("SIMULATOR_HOST") or "jetson-orin-nano-sim"
HEARTBEAT = 30

CAMERAS = {"inbound": "cam-in-01", "outbound": "cam-out-01"}
# Vehicles per site-local hour on a weekday, per direction
WEEKDAY = {
    "inbound": [4, 3, 2, 2, 4, 12, 35, 80, 90, 55, 35, 32, 35, 32, 30, 32, 35, 40, 30, 20, 15, 11, 8, 5],
    "outbound": [5, 3, 2, 2, 3, 6, 12, 25, 30, 30, 32, 33, 36, 34, 34, 40, 55, 85, 90, 50, 28, 18, 12, 8],
}
# Weekends: fewer vehicles, a midday peak, the same either way
WEEKEND = [5, 3, 2, 2, 2, 4, 8, 14, 22, 32, 40, 46, 48, 45, 40, 36, 34, 32, 28, 22, 16, 12, 9, 6]
VEHICLES = {"car": 0.72, "van": 0.09, "motorcycle": 0.08, "truck": 0.06, "bus": 0.05}
# Chance the device reads a plate, by vehicle type, in daylight
READ = {"car": 0.96, "van": 0.95, "truck": 0.9, "bus": 0.93, "motorcycle": 0.6}
NIGHT = (19, 6)
# cam-out-01 faces oncoming headlights after dark
NIGHT_READ = {"cam-in-01": 0.97, "cam-out-01": 0.62}
COLOURS = {"white": 3, "grey": 3, "black": 2.5, "silver": 2, "blue": 1, "red": 1}
# One-character misreads an OCR makes
LOOKALIKE = {"0": "O", "O": "0", "8": "B", "B": "8", "1": "I", "5": "S", "S": "5", "2": "Z", "Z": "2"}
LETTERS = "ABCDEFGHJKLMNPRSTUVWXYZ"

BUSES = ["MTB-1101", "MTB-1102", "MTB-1103"]
DELIVERY = "KDL-4821"


def site_tz(value: str) -> tzinfo:
    match = re.fullmatch(r"([+-])(\d{2}):?(\d{2})", (value or "").strip())
    if match:
        sign = -1 if match.group(1) == "-" else 1
        return timezone(sign * timedelta(hours=int(match.group(2)), minutes=int(match.group(3))))
    if (value or "UTC") in ("UTC", "Z"):
        return timezone.utc
    from zoneinfo import ZoneInfo

    return ZoneInfo(value)


TZ = site_tz(os.environ.get("SITE_TZ", "UTC"))


def log(message: str) -> None:
    print(f"{time.strftime('%Y-%m-%dT%H:%M:%S')} {message}", flush=True)


def plate(rng: random.Random) -> str:
    return "".join(rng.choice(LETTERS) for _ in range(3)) + "-" + "".join(rng.choice("0123456789") for _ in range(4))


# The same commuters and occasional visitors every day
_people = random.Random(f"{SITE}/people")
COMMUTERS = [plate(_people) for _ in range(80)]
REGULARS = [plate(_people) for _ in range(400)]


def plan_day(day: date) -> dict:
    """Every read of one day, sorted by time: (epoch seconds, document)."""
    rng = random.Random(f"{SITE}/{day.isoformat()}")
    midnight = datetime(day.year, day.month, day.day, tzinfo=TZ).timestamp()
    stamp = day.strftime("%Y%m%d")
    weekday = day.weekday() < 5
    passes: list[tuple[float, str, str, str]] = []  # (time, direction, vehicle type, plate)

    def at(hour: float, spread_min: float) -> float:
        return midnight + max(0.0, min(86399.0, hour * 3600 + rng.gauss(0, spread_min * 60)))

    # The bus line: every bus both ways, every 2 hours from 06:00 (every 3 on weekends)
    for i, bus in enumerate(BUSES):
        for start in range(6, 22, 2 if weekday else 3):
            passes.append((at(start + i * 0.6, 4), "inbound", "bus", bus))
            passes.append((at(start + i * 0.6 + 0.75, 4), "outbound", "bus", bus))
    if weekday:
        for hour in (10.0, 14.0):
            passes.append((at(hour, 15), "inbound", "van", DELIVERY))
            passes.append((at(hour + 1.5, 15), "outbound", "van", DELIVERY))
        for commuter in COMMUTERS:
            if rng.random() < 0.85:
                passes.append((at(rng.gauss(8.0, 0.5), 10), "inbound", "car", commuter))
                passes.append((at(rng.gauss(17.8, 0.5), 10), "outbound", "car", commuter))

    # Everyone else, by the hour's volume
    scale = rng.uniform(0.9, 1.1)
    for direction in ("inbound", "outbound"):
        profile = WEEKDAY[direction] if weekday else WEEKEND
        for hour, volume in enumerate(profile):
            for _ in range(round(volume * scale * rng.uniform(0.85, 1.15))):
                kind = rng.choices(list(VEHICLES), weights=list(VEHICLES.values()))[0]
                who = rng.choice(REGULARS) if rng.random() < 0.25 else plate(rng)
                passes.append((midnight + hour * 3600 + rng.uniform(0, 3599), direction, kind, who))

    reads = []
    for t, direction, kind, number in passes:
        camera = CAMERAS[direction]
        hour = datetime.fromtimestamp(t, TZ).hour
        night = hour >= NIGHT[0] or hour < NIGHT[1]
        late = hour >= 22 or hour < 5
        chance = READ[kind] * (NIGHT_READ[camera] if night else 1.0)
        mean = (58 if late else 46) if direction == "outbound" else 42
        doc = {
            "schema": "p4n4.alpr.read/0.1",
            "read_id": "",
            "site_id": SITE,
            "camera_id": camera,
            "timestamp": datetime.fromtimestamp(t, TZ).isoformat(timespec="milliseconds"),
            "direction": direction,
            "lane": 1,
            "vehicle_type": kind,
            "plate": None,
            "speed_kmh": round(max(8.0, rng.gauss(mean * (0.8 if kind in ("bus", "truck") else 1.0), 7)), 1),
            "colour": rng.choices(list(COLOURS), weights=list(COLOURS.values()))[0],
            "synthetic": True,
        }
        if rng.random() < chance:
            seen = number
            if rng.random() < 0.02:
                spots = [i for i, ch in enumerate(number) if ch in LOOKALIKE]
                if spots:
                    i = rng.choice(spots)
                    seen = number[:i] + LOOKALIKE[number[i]] + number[i + 1:]
            low, high = (0.6, 0.9) if night and camera == "cam-out-01" else (0.82, 0.99)
            doc.update(plate=seen, plate_region="PA", plate_confidence=round(rng.uniform(low, high), 2))
        reads.append((t, doc))

    # Ids follow time order, as the device's counter does
    reads.sort(key=lambda x: x[0])
    for seq, (_, doc) in enumerate(reads, 1):
        doc["read_id"] = f"RD-{stamp}-{seq:06d}"
    return {"reads": reads, "vehicles": len(reads)}


def request(path: str, body: object) -> dict:
    data = json.dumps(body, ensure_ascii=False).encode()
    req = urllib.request.Request(
        f"{INGEST_URL}{path}", data=data, method="POST",
        headers={"Content-Type": "application/json", "X-Site-Token": TOKEN},
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        detail = e.read().decode(errors="replace")[:300]
        raise RuntimeError(f"POST {path}: HTTP {e.code} {detail}") from None


def send(plan: dict, lo: float, hi: float) -> int:
    """Post every read in the plan timed in [lo, hi); returns how many."""
    reads = [doc for t, doc in plan["reads"] if lo <= t < hi]
    for i in range(0, len(reads), 500):
        result = request("/api/v1/reads", reads[i : i + 500])
        if result.get("rejected"):
            log(f"/api/v1/reads: rejected {result['rejected'][:3]}")
    return len(reads)


def heartbeat(plan: dict, now: float, started: float) -> None:
    rng = random.Random(int(now // HEARTBEAT))
    recent = sum(1 for t, _ in plan["reads"] if now - 60 <= t <= now)
    busy = min(1.0, recent / 6)
    request("/api/v1/heartbeat", {
        "schema": "p4n4.alpr.heartbeat/0.1",
        "site_id": SITE,
        "timestamp": datetime.fromtimestamp(now, TZ).isoformat(timespec="seconds"),
        "device": {"hostname": HOST, "jetpack_version": "6.2", "power_mode": "15W", "uptime_seconds": int(now - started) + 86400},
        "metrics": {
            "cpu_utilization_pct": round(22 + 25 * busy + rng.uniform(-4, 4), 1),
            "gpu_utilization_pct": round(30 + 45 * busy + rng.uniform(-5, 5), 1),
            "ram_used_mb": round(3900 + 700 * busy + rng.uniform(-60, 60)),
            "ram_total_mb": 7620,
            "temperature_cpu_c": round(47 + 8 * busy + rng.uniform(-1, 1), 1),
            "temperature_gpu_c": round(48 + 10 * busy + rng.uniform(-1, 1), 1),
            "disk_free_gb": round(402.6 - (now - started) / 86400 * 0.3, 1),
            "disk_total_gb": 512.0,
        },
        "pipeline": {
            "active_cameras": len(CAMERAS),
            "processing_fps": round(15.0 + rng.uniform(-0.6, 0.2), 2),
            "queue_latency_ms": round(25 + 30 * busy + rng.uniform(0, 10)),
            "vehicles_last_minute": recent,
        },
        "status": "healthy",
    })


def main() -> None:
    if not TOKEN:
        sys.exit("INGEST_TOKEN is empty: set it to this site's token from INGEST_SITE_TOKENS")
    for _ in range(60):
        try:
            with urllib.request.urlopen(f"{INGEST_URL}/health", timeout=5):
                break
        except OSError:
            time.sleep(2)
    else:
        sys.exit(f"no ingest service at {INGEST_URL}")

    started = now = time.time()
    today = datetime.fromtimestamp(now, TZ).date()
    for back in range(BACKFILL, 0, -1):
        day = today - timedelta(days=back)
        plan = plan_day(day)
        sent = send(plan, 0, now)
        log(f"backfilled {day} ({sent} vehicles)")
    plan = plan_day(today)
    sent = send(plan, 0, now)
    if "--once" in sys.argv[1:]:
        heartbeat(plan, now, started)
        log(f"today so far: {sent} vehicles, and a heartbeat; done (--once)")
        return
    log(f"today so far: {sent} vehicles; then live every {INTERVAL:g} s")

    last, beat = now, 0.0
    while True:
        time.sleep(INTERVAL)
        now = time.time()
        day = datetime.fromtimestamp(now, TZ).date()
        try:
            if day != today:
                send(plan, last, now)  # the end of yesterday
                today, plan = day, plan_day(day)
            sent = send(plan, last, now)
            if sent:
                log(f"posted {sent} vehicles")
            if now - beat >= HEARTBEAT:
                heartbeat(plan, now, started)
                beat = now
            last = now
        except (RuntimeError, OSError) as e:
            log(f"will retry: {e}")


if __name__ == "__main__":
    main()
