#!/usr/bin/env python3
"""
Demo store for the ingest service (compose profile "demo"): a synthetic
clothing store whose edge device posts contract 0.1 events, alerts and
heartbeats, and whose till posts sales, as the real ones would.

Each day is planned up front from a seed (store id + date), so the plan is
the same on every run: on start the simulator backfills the last
SIMULATOR_BACKFILL_DAYS days, then posts the rest of today's plan as its
time comes. Restarting re-sends the same ids, which the ingest service
counts as unchanged. Everything is marked "synthetic": true.

The plan has a story the agent can find: shelf-2 (Polos) is the busiest
shelf, shelf-3 (Dresses) draws interest but rarely sells, shelf-5 (Suits) is
high value with little traffic, and the evening is the peak. Brands are
made up.

    INGEST_URL                default http://ingest:8080
    INGEST_TOKEN              the store's write token (INGEST_STORE_TOKENS)
    INGEST_READ_TOKEN         for marking alerts attended, as staff would
    STORE_ID                  default demo-store
    STORE_TZ                  default UTC
    STORE_LANG                en (default) or es: shelf, zone and garment names, alert texts
    SIMULATOR_CURRENCY        till currency (default USD)
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
READ_TOKEN = os.environ.get("INGEST_READ_TOKEN", "")
STORE = os.environ.get("STORE_ID", "demo-store")
LANG = os.environ.get("STORE_LANG", "en").strip() or "en"
CURRENCY = os.environ.get("SIMULATOR_CURRENCY") or "USD"
INTERVAL = float(os.environ.get("SIMULATOR_INTERVAL") or 15)
BACKFILL = int(os.environ.get("SIMULATOR_BACKFILL_DAYS") or 7)
HOST = os.environ.get("SIMULATOR_HOST") or "jetson-orin-nano-sim"
HEARTBEAT = 30

# What people read, per STORE_LANG: shelves (id, category, garment types),
# colours, zones, payment methods and alert texts (title, description, action)
TEXT = {
    "en": {
        "camera": "cam-floor-01",
        "shelves": [("shelf-1", "Shirts", ["shirt"]), ("shelf-2", "Polos", ["polo"]), ("shelf-3", "Dresses", ["dress"]),
                    ("shelf-4", "Jeans", ["jeans"]), ("shelf-5", "Suits", ["suit"]), ("shelf-6", "Accessories", ["belt", "scarf"])],
        "colors": ["white", "black", "navy", "beige", "grey", "red"],
        "zones": {"entry": "entrance", "fitting": "fitting-room-1", "till": "till"},
        "payments": ["card", "card", "cash"],
        "a1": ("Customer looking at {category}", "{category} ({brand}): looking at a {colour} {garment}.",
               "Greet them and offer sizes and alternatives."),
        "a2": ("Customer interested in {brand} {category}",
               "{shelf} ({category}, {brand}): took a {colour} {garment}, {dwell} s without staff nearby.",
               "Attend now and offer the fitting room."),
        "a3": ("Customer in the fitting room with high-value items", "Went into the fitting room with {n} item(s).",
               "Assist at the fitting room: offer another size or colour."),
        "a4": ("Loss risk — review", "{shelf}: a {colour} {garment} went out of view after being taken. It may be a camera error.",
               "Approach politely and offer help. This is not an accusation."),
    },
    "es": {
        "camera": "cam-salon-01",
        "shelves": [("estante-1", "Camisas", ["camisa"]), ("estante-2", "Polos", ["polo"]), ("estante-3", "Vestidos", ["vestido"]),
                    ("estante-4", "Jeans", ["jeans"]), ("estante-5", "Trajes", ["traje"]),
                    ("estante-6", "Accesorios", ["cinturón", "bufanda"])],
        "colors": ["blanco", "negro", "azul marino", "beige", "gris", "rojo"],
        "zones": {"entry": "entrada", "fitting": "probador-1", "till": "caja"},
        "payments": ["tarjeta", "tarjeta", "efectivo"],
        "a1": ("Cliente mirando {category}", "{category} ({brand}): mira un {garment} {colour}.",
               "Acercarse, saludar y ofrecer talla y alternativas."),
        "a2": ("Cliente interesado en {category} {brand}",
               "{shelf} ({category} {brand}): tomó un {garment} {colour}, {dwell} s sin atención.",
               "Atender de inmediato y ofrecer probador."),
        "a3": ("Cliente en el probador con prendas de alto valor", "Entró al probador con {n} prenda(s).",
               "Asistir en el probador: ofrecer otra talla o color."),
        "a4": ("Riesgo de pérdida — revisar", "{shelf}: un {garment} {colour} dejó de verse tras tomarlo. Puede ser un error de la cámara.",
               "Acercarse con cortesía y ofrecer ayuda. No es una acusación."),
    },
}
if LANG not in TEXT:
    sys.exit(f"STORE_LANG must be one of {', '.join(TEXT)}")
T = TEXT[LANG]
CAMERA = T["camera"]
# brand, price tier, average price, share of attention, chance a garment taken there is bought
SHELF_PLAN = [
    ("Atelier", "alto", 145.0, 1.3, 0.30),
    ("Coastline", "alto", 120.0, 1.8, 0.35),
    ("Linea", "medio", 70.0, 1.4, 0.04),
    ("Denim Co.", "medio", 85.0, 1.0, 0.30),
    ("Atelier", "alto", 450.0, 0.3, 0.35),
    ("Accent", "accesible", 30.0, 0.8, 0.40),
]
# shelf id, category, brand, price tier, average price, garment types, attention, buy chance
SHELVES = [(sid, cat, *plan[:3], garments, *plan[3:]) for (sid, cat, garments), plan in zip(T["shelves"], SHELF_PLAN, strict=True)]
COLORS = list(zip(T["colors"], [3, 3, 2, 1.5, 1, 0.7], strict=True))
# Store-local opening hours and each hour's share of visitors
HOURS = {10: 0.5, 11: 0.6, 12: 0.9, 13: 1.0, 14: 0.7, 15: 0.6, 16: 0.7, 17: 1.0, 18: 1.5, 19: 1.1}
VISITORS = {0: 55, 1: 50, 2: 55, 3: 60, 4: 75, 5: 95, 6: 70}  # Monday..Sunday


def store_tz(value: str) -> tzinfo:
    match = re.fullmatch(r"([+-])(\d{2}):?(\d{2})", (value or "").strip())
    if match:
        sign = -1 if match.group(1) == "-" else 1
        return timezone(sign * timedelta(hours=int(match.group(2)), minutes=int(match.group(3))))
    if (value or "UTC") in ("UTC", "Z"):
        return timezone.utc
    from zoneinfo import ZoneInfo

    return ZoneInfo(value)


TZ = store_tz(os.environ.get("STORE_TZ", "UTC"))


def log(message: str) -> None:
    print(f"{time.strftime('%Y-%m-%dT%H:%M:%S')} {message}", flush=True)


def plan_day(day: date) -> dict:
    """
    Every event, alert, sale and staff review of one day, sorted by time.
    Items are (epoch seconds, document); reviews are (epoch, alert_id, status).
    """
    rng = random.Random(f"{STORE}/{day.isoformat()}")
    midnight = datetime(day.year, day.month, day.day, tzinfo=TZ)
    stamp = day.strftime("%Y%m%d")
    events, alerts, sales, reviews = [], [], [], []
    total = round(VISITORS[day.weekday()] * rng.uniform(0.85, 1.15))
    hours = rng.choices(list(HOURS), weights=list(HOURS.values()), k=total)
    starts = sorted(midnight.timestamp() + h * 3600 + rng.uniform(0, 3300) for h in hours)

    def iso(t: float) -> str:
        return datetime.fromtimestamp(t, TZ).isoformat(timespec="milliseconds")

    def event(t: float, kind: str, track: str, end: float | None = None, **extra) -> None:
        doc = {
            "schema": "aioros.boutique.event/0.1",
            "store_id": STORE,
            "camera_id": CAMERA,
            "event_id": "",
            "type": kind,
            "ts_start": iso(t),
            "ts_end": iso(end or t),
            "track_id": track,
            "role": "customer",
            "synthetic": True,
            **extra,
        }
        events.append((t, doc))

    def alert(t: float, kind: str, severity: str, track: str, title: str, description: str, action: str,
              evidence: dict) -> None:
        doc = {
            "schema": "aioros.boutique.alert/0.1",
            "alert_id": "",
            "store_id": STORE,
            "camera_id": CAMERA,
            "alert_type": kind,
            "severity": severity,
            "timestamp": iso(t),
            "track_id": track,
            "title": title,
            "description": description,
            "recommended_action": action,
            "evidence": evidence,
            "review_required": True,
            "status": "pending_review",
            "synthetic": True,
        }
        alerts.append((t, doc))
        # Staff look at most alerts within a few minutes; some stay pending
        roll = rng.random()
        if roll < 0.9:
            reviews.append([t + rng.uniform(90, 900), doc, "attended" if roll < 0.75 else "dismissed"])

    for n, t in enumerate(starts, 1):
        track = f"P{n:03d}"
        event(t, "customer_entry", track, zone_id=T["zones"]["entry"])
        t += rng.uniform(15, 60)
        taken = []
        for i in range(rng.choices([0, 1, 2, 3], weights=[2, 4, 3, 1])[0]):
            shelf = rng.choices(SHELVES, weights=[s[6] for s in SHELVES])[0]
            shelf_id, category, brand, tier, price, garments, _, buy = shelf
            garment = rng.choice(garments)
            color = rng.choices([c for c, _ in COLORS], weights=[w for _, w in COLORS])[0]
            dwell = rng.uniform(12, 150 if tier == "alto" else 90)
            planogram = {"shelf_id": shelf_id, "brand": brand, "category": category, "price_tier": tier, "avg_price": price}
            attributes = {"garment_type": garment, "color": color, "confidence": round(rng.uniform(0.62, 0.97), 2), "source": "vlm"}
            event(t, "shelf_dwell", track, t + dwell, zone_id=shelf_id, dwell_seconds=round(dwell, 1), planogram=planogram)
            event(t + dwell * 0.3, "garment_touched", track, t + dwell * 0.5, zone_id=shelf_id, planogram=planogram,
                  attributes=attributes)
            if i == 0:
                title, text, action = T["a1"]
                alert(t + dwell * 0.5, "customer_arrival", "low", track, title.format(category=category.lower()),
                      text.format(category=category, brand=brand, garment=garment, colour=color), action,
                      {"zone_id": shelf_id, "dwell_seconds": round(dwell * 0.5, 1), "items_interacted": 1, "price_tier": tier})
            if rng.random() < 0.45:
                event(t + dwell * 0.6, "garment_taken", track, t + dwell * 0.8, zone_id=shelf_id, planogram=planogram,
                      attributes=attributes)
                taken.append((t + dwell * 0.8, shelf, garment, color))
                if tier == "alto" and dwell >= 60 and rng.random() < 0.6:
                    title, text, action = T["a2"]
                    alert(t + dwell, "high_value_opportunity", "medium", track,
                          title.format(category=category.lower(), brand=brand),
                          text.format(shelf=shelf_id, category=category, brand=brand, garment=garment, colour=color,
                                      dwell=round(dwell)),
                          action,
                          {"zone_id": shelf_id, "dwell_seconds": round(dwell, 1), "items_interacted": 1, "price_tier": tier})
            t += dwell + rng.uniform(20, 90)

        if taken and rng.random() < 0.5:
            stay = rng.uniform(150, 540)
            fitting = T["zones"]["fitting"]
            event(t, "fitting_room_entered", track, zone_id=fitting)
            if any(s[3] == "alto" for _, s, _, _ in taken):
                title, text, action = T["a3"]
                alert(t + 5, "fitting_room_assistance", "medium", track, title, text.format(n=len(taken)), action,
                      {"zone_id": fitting, "dwell_seconds": 0, "items_interacted": len(taken), "price_tier": "alto"})
            event(t + stay, "fitting_room_exited", track, zone_id=fitting)
            t += stay + rng.uniform(10, 40)

        bought = [x for x in taken if rng.random() < x[1][7]]
        if bought:
            event(t, "checkout_visited", track, t + 60, zone_id=T["zones"]["till"])
            for k, (_, shelf, garment, color) in enumerate(bought):
                sales.append((t + 60 + k, {
                    "tx_id": f"TX-{stamp}-{n:03d}{k}",
                    "store_id": STORE,
                    "timestamp": iso(t + 60 + k),
                    "currency": CURRENCY,
                    "shelf_id": shelf[0],
                    "brand": shelf[2],
                    "garment_type": garment,
                    "color": color,
                    "amount": round(shelf[4] * rng.uniform(0.8, 1.25), 2),
                    "payment_method": rng.choice(T["payments"]),
                    "synthetic": True,
                }))
            t += 90
        elif taken and rng.random() < 0.03:
            when, shelf, garment, color = taken[-1]
            title, text, action = T["a4"]
            alert(when + 20, "loss_risk_review", "medium", track, title,
                  text.format(shelf=shelf[0], garment=garment, colour=color), action,
                  {"zone_id": shelf[0], "dwell_seconds": 0, "items_interacted": 1, "price_tier": shelf[3]})
        event(t + rng.uniform(10, 40), "customer_exit", track, zone_id=T["zones"]["entry"])

    # Ids follow time order, as the Jetson's counters do
    for prefix, key, items in (("EVT", "event_id", events), ("ALT", "alert_id", alerts)):
        items.sort(key=lambda x: x[0])
        for seq, (_, doc) in enumerate(items, 1):
            doc[key] = f"{prefix}-{stamp}-{seq:06d}"
    reviews = [(t, doc["alert_id"], status) for t, doc, status in sorted(reviews, key=lambda r: r[0])]
    sales.sort(key=lambda x: x[0])
    return {"events": events, "alerts": alerts, "sales": sales, "reviews": reviews, "visitors": total}


def request(method: str, path: str, body: object, token: str) -> dict:
    data = json.dumps(body, ensure_ascii=False).encode()
    req = urllib.request.Request(
        f"{INGEST_URL}{path}", data=data, method=method,
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {token}"},
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        detail = e.read().decode(errors="replace")[:300]
        raise RuntimeError(f"{method} {path}: HTTP {e.code} {detail}") from None


def post(path: str, docs: list[dict]) -> None:
    for i in range(0, len(docs), 500):
        result = request("POST", path, docs[i : i + 500], TOKEN)
        if result.get("rejected"):
            log(f"{path}: rejected {result['rejected'][:3]}")


def review(alert_id: str, status: str) -> None:
    if not READ_TOKEN:
        return
    try:
        request("POST", f"/api/v1/alerts/{alert_id}/status", {"store_id": STORE, "status": status, "by": "vendedor-demo"}, READ_TOKEN)
    except RuntimeError as e:
        log(str(e))


def send(plan: dict, lo: float, hi: float) -> int:
    """Post everything in the plan timed in [lo, hi); returns the number of documents."""
    pick = lambda items: [doc for t, doc in items if lo <= t < hi]  # noqa: E731
    events, alerts, sales = pick(plan["events"]), pick(plan["alerts"]), pick(plan["sales"])
    post("/api/v1/events", events)
    post("/api/v1/alerts", alerts)
    post("/api/v1/pos", sales)
    for t, alert_id, status in plan["reviews"]:
        if lo <= t < hi:
            review(alert_id, status)
    return len(events) + len(alerts) + len(sales)


def heartbeat(plan: dict, now: float, started: float) -> None:
    rng = random.Random(int(now // HEARTBEAT))
    inside = sum(
        1
        for t, doc in plan["events"]
        if doc["type"] == "customer_entry" and t <= now
    ) - sum(1 for t, doc in plan["events"] if doc["type"] == "customer_exit" and t <= now)
    busy = min(1.0, max(0, inside) / 6)
    pending = sum(1 for t, doc in plan["alerts"] if t <= now) - sum(1 for t, _, _ in plan["reviews"] if t <= now)
    request("POST", "/api/v1/heartbeat", {
        "schema": "aioros.boutique.heartbeat/0.1",
        "store_id": STORE,
        "timestamp": datetime.fromtimestamp(now, TZ).isoformat(timespec="seconds"),
        "device": {"hostname": HOST, "jetpack_version": "6.2", "power_mode": "15W", "uptime_seconds": int(now - started) + 86400},
        "metrics": {
            "cpu_utilization_pct": round(18 + 30 * busy + rng.uniform(-4, 4), 1),
            "gpu_utilization_pct": round(12 + 60 * busy + rng.uniform(-5, 5), 1),
            "ram_used_mb": round(4300 + 900 * busy + rng.uniform(-60, 60)),
            "ram_total_mb": 7620,
            "temperature_cpu_c": round(46 + 9 * busy + rng.uniform(-1, 1), 1),
            "temperature_gpu_c": round(47 + 11 * busy + rng.uniform(-1, 1), 1),
            "disk_free_gb": round(348.2 - (now - started) / 86400 * 0.8, 1),
            "disk_total_gb": 512.0,
        },
        "pipeline": {
            "active_cameras": 1,
            "processing_fps": round(6.0 + rng.uniform(-0.3, 0.1), 2),
            "queue_latency_ms": round(30 + 40 * busy + rng.uniform(0, 15)),
            "active_tracks": max(0, inside),
        },
        "alerts_pending": max(0, pending),
        "status": "healthy",
    }, TOKEN)


def main() -> None:
    if not TOKEN:
        sys.exit("INGEST_TOKEN is empty: set it to this store's token from INGEST_STORE_TOKENS")
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
        log(f"backfilled {day} ({plan['visitors']} visitors, {sent} documents)")
    plan = plan_day(today)
    sent = send(plan, 0, now)
    if "--once" in sys.argv[1:]:
        heartbeat(plan, now, started)
        log(f"today so far: {sent} documents, and a heartbeat; done (--once)")
        return
    log(f"today so far: {sent} documents; then live every {INTERVAL:g} s")

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
                log(f"posted {sent} documents")
            if now - beat >= HEARTBEAT:
                heartbeat(plan, now, started)
                beat = now
            last = now
        except (RuntimeError, OSError) as e:
            log(f"will retry: {e}")


if __name__ == "__main__":
    main()
