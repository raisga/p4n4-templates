"""
SQLite store of the ingest service, and the deterministic queries behind the
agent API.

Every number the agent says comes from here: counts, rates and checks over
the stored plate reads, never from the model. The rules are written out next
to each query so the answers can be traced.

This is the only place plates are kept. Reads older than the retention
period (INGEST_RETENTION_DAYS) are deleted by purge(), which the ingest
service runs every hour.

Times are stored twice: the document's own ISO string, and epoch seconds
for range queries. "Today", weekdays and peak hours are in the site's time
zone (SITE_TZ).
"""

from __future__ import annotations

import json
import math
import sqlite3
import statistics
import threading
import time
from collections import Counter, defaultdict
from datetime import datetime, timedelta, tzinfo

from contract import DIRECTIONS, VEHICLE_TYPES, parse_time, plate_key

SCHEMA = """
CREATE TABLE IF NOT EXISTS reads (
    site_id TEXT NOT NULL,
    read_id TEXT NOT NULL,
    camera_id TEXT NOT NULL,
    t REAL NOT NULL,
    direction TEXT NOT NULL,
    lane INTEGER,
    vehicle_type TEXT NOT NULL,
    -- as the device read it, and as matched (contract.plate_key); NULL: not read
    plate TEXT,
    plate_key TEXT,
    plate_confidence REAL,
    speed_kmh REAL,
    synthetic INTEGER NOT NULL,
    doc TEXT NOT NULL,
    received REAL NOT NULL,
    PRIMARY KEY (site_id, read_id)
);
CREATE INDEX IF NOT EXISTS reads_time ON reads (site_id, t);
CREATE INDEX IF NOT EXISTS reads_plate ON reads (site_id, plate_key, t);

-- The latest heartbeat of each edge device; the history goes to InfluxDB
CREATE TABLE IF NOT EXISTS nodes (
    site_id TEXT NOT NULL,
    hostname TEXT NOT NULL,
    t REAL NOT NULL,
    status TEXT NOT NULL,
    doc TEXT NOT NULL,
    received REAL NOT NULL,
    PRIMARY KEY (site_id, hostname)
);
"""

# Text people read in the agent API's answers, in the site's language
# (SITE_LANG). Codes (inbound, car, high, C1-C4) stay as they are.
TEXT = {
    "en": {
        "weekdays": ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"),
        "c1": ("{camera}: plates read for {rate}% of vehicles in 7 days, under {min}%.",
               "Clean the lens, and check the focus, the angle and the IR illumination."),
        "c2": ("{camera}: plates read for {night}% of vehicles at night against {day}% by day.",
               "Check the IR illumination and the shutter speed at night."),
        "c3": ("{camera}: no vehicle in the last {minutes} min, where this hour usually has {usual}.",
               "Check that the camera and the edge device are running."),
        "c4": ("{camera}: {share}% of plates read with confidence under {confidence}.",
               "Check the focus and how large plates are in the image."),
        "none": ("No check fired.", "Keep monitoring weekly."),
    },
    "es": {
        "weekdays": ("lunes", "martes", "miércoles", "jueves", "viernes", "sábado", "domingo"),
        "c1": ("{camera}: se leyó la placa del {rate}% de los vehículos en 7 días, menos del {min}%.",
               "Limpiar el lente y revisar el enfoque, el ángulo y la iluminación IR."),
        "c2": ("{camera}: lee la placa del {night}% de los vehículos de noche y del {day}% de día.",
               "Revisar la iluminación IR y la velocidad de obturación de noche."),
        "c3": ("{camera}: ningún vehículo en los últimos {minutes} min, cuando a esta hora suele haber {usual}.",
               "Revisar que la cámara y el equipo estén funcionando."),
        "c4": ("{camera}: el {share}% de las placas se leyó con confianza menor a {confidence}.",
               "Revisar el enfoque y el tamaño de las placas en la imagen."),
        "none": ("Ningún chequeo se activó.", "Seguir el monitoreo semanal."),
    },
}
# Periods take a weekday in either language
WEEKDAY_NAMES = {
    **{name: i for text in TEXT.values() for i, name in enumerate(text["weekdays"])},
    "miercoles": 2,
    "sabado": 5,
}
PERIODS = ("today", "yesterday", "this_week", "last_week", "this_month", "last_month", "<N>d (e.g. 7d, 30d)", "a weekday (tuesday, martes)")
# A device that hasn't sent a heartbeat in this long counts as offline (3 missed beats)
NODE_TIMEOUT = 90
# Night, in site-local hours: from NIGHT[0]:00 to NIGHT[1]:00
NIGHT = (19, 6)
# The checks' thresholds (see checks())
READ_RATE_MIN = 0.8
NIGHT_RATIO = 0.8
LOW_CONFIDENCE = 0.7
LOW_CONFIDENCE_SHARE = 0.2
SILENT_MINUTES = 60
SILENT_USUAL = 10
MIN_SAMPLE = 50
MAX_PASSAGES = 50


class PeriodError(ValueError):
    pass


def canonical(doc: dict) -> str:
    return json.dumps(doc, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def one_edit(a: str, b: str) -> bool:
    """Whether b is a with one character changed, added, removed or two neighbours swapped: a likely misread."""
    if a == b or abs(len(a) - len(b)) > 1:
        return False
    if len(a) == len(b):
        diff = [i for i in range(len(a)) if a[i] != b[i]]
        return len(diff) == 1 or (len(diff) == 2 and diff[1] == diff[0] + 1 and a[diff[0]] == b[diff[1]] and a[diff[1]] == b[diff[0]])
    short, long = (a, b) if len(a) < len(b) else (b, a)
    i = next((i for i in range(len(short)) if short[i] != long[i]), len(short))
    return short[i:] == long[i + 1:]


def percentile(values: list[float], share: float) -> float:
    """The nearest-rank percentile (85th: the speed 85% of vehicles stay under)."""
    ordered = sorted(values)
    return ordered[max(0, math.ceil(share * len(ordered)) - 1)]


class Store:
    def __init__(self, path: str, tz: tzinfo, lang: str = "en", retention_days: int = 30,
                 speed_limit: float | None = None) -> None:
        self.tz = tz
        if lang not in TEXT:
            raise ValueError(f"SITE_LANG must be one of {', '.join(TEXT)}")
        self.text = TEXT[lang]
        self.retention_days = retention_days
        self.speed_limit = speed_limit
        # Writers take it themselves; the HTTP server takes it around every read
        self.lock = threading.RLock()
        self.db = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        if path != ":memory:":
            self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript(SCHEMA)

    # ── writes ────────────────────────────────────────────────────────────────

    def _upsert(self, table: str, keys: dict, values: dict, doc: dict) -> str:
        """
        Insert or replace one row; returns created, updated or unchanged. A
        device re-sends a row when it retries a batch, or when it corrects a
        read: the newest wins.
        """
        text = canonical(doc)
        where = " AND ".join(f"{k} = ?" for k in keys)
        row = self.db.execute(f"SELECT doc FROM {table} WHERE {where}", tuple(keys.values())).fetchone()
        if row and row["doc"] == text:
            return "unchanged"
        columns = {**keys, **values, "doc": text, "received": time.time()}
        if row:
            sets = ", ".join(f"{k} = ?" for k in columns)
            self.db.execute(f"UPDATE {table} SET {sets} WHERE {where}", (*columns.values(), *keys.values()))
            return "updated"
        names = ", ".join(columns)
        marks = ", ".join("?" for _ in columns)
        self.db.execute(f"INSERT INTO {table} ({names}) VALUES ({marks})", tuple(columns.values()))
        return "created"

    def put_read(self, doc: dict) -> str:
        t = parse_time(doc["timestamp"], self.tz).timestamp()
        if t < time.time() - self.retention_days * 86400:
            raise ValueError(f"older than the retention period ({self.retention_days} days)")
        plate = doc.get("plate")
        with self.lock:
            return self._upsert(
                "reads",
                {"site_id": doc["site_id"], "read_id": doc["read_id"]},
                {
                    "camera_id": doc["camera_id"],
                    "t": t,
                    "direction": doc["direction"],
                    "lane": doc.get("lane"),
                    "vehicle_type": doc["vehicle_type"],
                    "plate": plate,
                    "plate_key": plate_key(plate) if plate else None,
                    "plate_confidence": doc.get("plate_confidence"),
                    "speed_kmh": doc.get("speed_kmh"),
                    "synthetic": int(doc["synthetic"]),
                },
                doc,
            )

    def put_heartbeat(self, doc: dict) -> str:
        with self.lock:
            return self._upsert(
                "nodes",
                {"site_id": doc["site_id"], "hostname": doc["device"]["hostname"]},
                {"t": parse_time(doc["timestamp"], self.tz).timestamp(), "status": doc["status"]},
                doc,
            )

    def purge(self, now: float | None = None) -> int:
        """Delete the reads older than the retention period; returns how many went."""
        cutoff = (now if now is not None else time.time()) - self.retention_days * 86400
        with self.lock:
            return self.db.execute("DELETE FROM reads WHERE t < ?", (cutoff,)).rowcount

    # ── reads ─────────────────────────────────────────────────────────────────

    def sites(self) -> list[str]:
        rows = self.db.execute("SELECT site_id FROM reads UNION SELECT site_id FROM nodes ORDER BY site_id")
        return [r[0] for r in rows]

    def local(self, t: float) -> datetime:
        return datetime.fromtimestamp(t, self.tz)

    def iso(self, t: float | None) -> str | None:
        return None if t is None else self.local(t).isoformat(timespec="seconds")

    def day_name(self, t: float) -> str:
        day = self.local(t)
        return f"{day.date().isoformat()} {self.text['weekdays'][day.weekday()]}"

    def period(self, period: str | None = None, start: str | None = None, end: str | None = None,
               now: float | None = None) -> tuple[float, float, dict]:
        """
        (start, end, description) of a period: a name (today, yesterday,
        this_week, last_week, this_month, last_month), <N>d for the last N
        days, a weekday for its latest occurrence (today included), or from/to
        dates or date-times. Weeks start on Monday. Default: the last 7 days.
        """
        today = self.local(now if now is not None else time.time()).replace(hour=0, minute=0, second=0, microsecond=0)
        end_t = now if now is not None else time.time()
        name = (period or "").strip().casefold()
        if start or end:
            try:
                lo = self._bound(start, today - timedelta(days=6), False)
                hi = self._bound(end, None, True) if end else end_t
            except ValueError as e:
                raise PeriodError(f"from/to: {e}") from None
            name = "custom"
        elif name in ("", "last_7d", "7d"):
            name, lo, hi = "last_7d", (today - timedelta(days=6)).timestamp(), end_t
        elif name == "today":
            lo, hi = today.timestamp(), end_t
        elif name == "yesterday":
            lo, hi = (today - timedelta(days=1)).timestamp(), today.timestamp()
        elif name == "this_week":
            lo, hi = (today - timedelta(days=today.weekday())).timestamp(), end_t
        elif name == "last_week":
            monday = today - timedelta(days=today.weekday())
            lo, hi = (monday - timedelta(days=7)).timestamp(), monday.timestamp()
        elif name == "this_month":
            lo, hi = today.replace(day=1).timestamp(), end_t
        elif name == "last_month":
            first = today.replace(day=1)
            lo, hi = (first - timedelta(days=1)).replace(day=1).timestamp(), first.timestamp()
        elif name in WEEKDAY_NAMES:
            day = today - timedelta(days=(today.weekday() - WEEKDAY_NAMES[name]) % 7)
            lo, hi = day.timestamp(), min(end_t, (day + timedelta(days=1)).timestamp())
        elif name.endswith("d") and name[:-1].isdigit() and 0 < int(name[:-1]) <= 366:
            lo, hi = (today - timedelta(days=int(name[:-1]) - 1)).timestamp(), end_t
        else:
            raise PeriodError(f"unknown period {period!r}; use one of: {', '.join(PERIODS)}")
        if hi <= lo:
            raise PeriodError("the period ends before it starts")
        return lo, hi, {"name": name, "from": self.iso(lo), "to": self.iso(hi)}

    def _bound(self, value: str | None, default: datetime | None, upper: bool) -> float:
        if not value:
            return default.timestamp()
        if len(value) == 10:
            day = datetime.fromisoformat(value).replace(tzinfo=self.tz)
            # A date as "to" includes the whole day
            return (day + timedelta(days=1)).timestamp() if upper else day.timestamp()
        return parse_time(value, self.tz).timestamp()

    def _reads(self, site: str, lo: float, hi: float) -> list[sqlite3.Row]:
        return self.db.execute(
            "SELECT * FROM reads WHERE site_id = ? AND t >= ? AND t < ? ORDER BY t", (site, lo, hi)
        ).fetchall()

    @staticmethod
    def _synthetic(rows: list[sqlite3.Row]) -> bool:
        return any(r["synthetic"] for r in rows)

    @staticmethod
    def _rate(read: int, total: int) -> float | None:
        return round(read / total, 3) if total else None

    def _night(self, t: float) -> bool:
        hour = self.local(t).hour
        return hour >= NIGHT[0] or hour < NIGHT[1]

    def summary(self, site: str, lo: float, hi: float) -> dict:
        """
        Traffic over a period.
          vehicles         every read, plate or not
          read_rate        reads with a plate / vehicles
          distinct_plates  different plates (contract.plate_key)
          repeat_plates    plates seen more than once
          peak_hour        the site-local hour with the most vehicles
          busiest_day      the site-local day with the most vehicles
          speed            from reads with speed_kmh: mean, 85th percentile,
                           max, and over the limit (SITE_SPEED_LIMIT) if set
        """
        rows = self._reads(site, lo, hi)
        plates = Counter(r["plate_key"] for r in rows if r["plate_key"])
        read = sum(plates.values())
        hours = Counter(self.local(r["t"]).hour for r in rows)
        days = Counter(self.day_name(r["t"]) for r in rows)
        peak = busiest = None
        if hours:
            hour, count = max(sorted(hours.items()), key=lambda kv: kv[1])
            peak = {"hour": f"{hour:02d}:00 - {(hour + 1) % 24:02d}:00", "vehicles": count}
        if len(days) > 1:
            day, count = max(sorted(days.items()), key=lambda kv: kv[1])
            busiest = {"day": day, "vehicles": count}
        cameras: dict[str, list] = defaultdict(lambda: [0, 0])
        for r in rows:
            cameras[r["camera_id"]][0] += 1
            cameras[r["camera_id"]][1] += bool(r["plate_key"])
        return {
            "site_id": site,
            "vehicles": len(rows),
            "by_direction": {d: n for d in DIRECTIONS if (n := sum(1 for r in rows if r["direction"] == d))},
            "by_vehicle_type": dict(Counter(r["vehicle_type"] for r in rows).most_common()),
            "plates_read": read,
            "read_rate": self._rate(read, len(rows)),
            "distinct_plates": len(plates),
            "repeat_plates": sum(1 for n in plates.values() if n > 1),
            "peak_hour": peak,
            "busiest_day": busiest,
            "speed": self._speed([r["speed_kmh"] for r in rows if r["speed_kmh"] is not None]),
            "by_camera": {c: {"vehicles": v, "read_rate": self._rate(p, v)} for c, (v, p) in sorted(cameras.items())},
            "retention_days": self.retention_days,
            "synthetic_data": self._synthetic(rows),
        }

    def _speed(self, speeds: list[float]) -> dict | None:
        if not speeds:
            return None
        result = {
            "readings": len(speeds),
            "avg_kmh": round(statistics.mean(speeds), 1),
            "p85_kmh": round(percentile(speeds, 0.85), 1),
            "max_kmh": round(max(speeds), 1),
        }
        if self.speed_limit:
            over = sum(1 for s in speeds if s > self.speed_limit)
            result.update(limit_kmh=self.speed_limit, over_limit=over, over_limit_share=round(over / len(speeds), 3))
        return result

    def traffic(self, site: str, lo: float, hi: float, direction: str | None = None,
                vehicle_type: str | None = None) -> dict:
        """
        Vehicles per site-local hour of the day (summed over the period, and
        averaged per day) and per day, optionally for one direction or
        vehicle type.
        """
        if direction and direction not in DIRECTIONS:
            raise PeriodError(f"direction must be one of {', '.join(DIRECTIONS)}")
        if vehicle_type and vehicle_type not in VEHICLE_TYPES:
            raise PeriodError(f"vehicle_type must be one of {', '.join(VEHICLE_TYPES)}")
        rows = [
            r for r in self._reads(site, lo, hi)
            if (not direction or r["direction"] == direction) and (not vehicle_type or r["vehicle_type"] == vehicle_type)
        ]
        days = max(1, math.ceil((hi - lo) / 86400 - 1e-9))
        hours: dict[int, Counter] = defaultdict(Counter)
        per_day: dict[str, Counter] = defaultdict(Counter)
        for r in rows:
            hours[self.local(r["t"]).hour][r["direction"]] += 1
            per_day[self.day_name(r["t"])][r["direction"]] += 1
        by_hour = []
        for hour in range(24):
            counts = hours.get(hour, Counter())
            total = sum(counts.values())
            by_hour.append({"hour": f"{hour:02d}:00", "vehicles": total, "avg_per_day": round(total / days, 1),
                            **{d: counts[d] for d in DIRECTIONS if counts[d]}})
        return {
            "site_id": site,
            "filter": {k: v for k, v in (("direction", direction), ("vehicle_type", vehicle_type)) if v},
            "vehicles": len(rows),
            "days": days,
            "by_hour": by_hour,
            "by_day": [{"day": k, "vehicles": sum(v.values()), **dict(v)} for k, v in sorted(per_day.items())],
            "synthetic_data": self._synthetic(rows),
        }

    def plate(self, site: str, plate: str, lo: float, hi: float) -> dict:
        """
        Every passage of one plate, newest first (at most MAX_PASSAGES), and
        plates one character away that were seen in the period: an ALPR
        misreads a character now and then, so a vehicle "not seen" may be
        under one of those.
        """
        key = plate_key(plate or "")
        if not key or len(key) > 16 or not key.isalnum():
            raise PeriodError("plate must be letters and digits, e.g. ABC-1234")
        rows = self.db.execute(
            "SELECT * FROM reads WHERE site_id = ? AND plate_key = ? AND t >= ? AND t < ? ORDER BY t DESC",
            (site, key, lo, hi),
        ).fetchall()
        passages = []
        for r in rows[:MAX_PASSAGES]:
            item = {
                "time": self.iso(r["t"]),
                "weekday": self.text["weekdays"][self.local(r["t"]).weekday()],
                "direction": r["direction"],
                "camera_id": r["camera_id"],
                "vehicle_type": r["vehicle_type"],
                "plate_as_read": r["plate"],
                "confidence": r["plate_confidence"],
                "speed_kmh": r["speed_kmh"],
            }
            passages.append({k: v for k, v in item.items() if v is not None})
        others = Counter(
            r[0] for r in self.db.execute(
                "SELECT plate_key FROM reads WHERE site_id = ? AND plate_key IS NOT NULL AND t >= ? AND t < ?",
                (site, lo, hi),
            )
        )
        similar = sorted(((k, n) for k, n in others.items() if one_edit(key, k)), key=lambda kv: (-kv[1], kv[0]))
        return {
            "site_id": site,
            "plate": key,
            "passages_total": len(rows),
            "first_seen": self.iso(rows[-1]["t"]) if rows else None,
            "last_seen": self.iso(rows[0]["t"]) if rows else None,
            "by_direction": dict(Counter(r["direction"] for r in rows)),
            "passages": passages,
            "passages_shown": len(passages),
            "similar_plates": [{"plate": k, "passages": n} for k, n in similar[:5]],
            "retention_days": self.retention_days,
            "synthetic_data": self._synthetic(rows),
        }

    def frequent(self, site: str, lo: float, hi: float, limit: int = 10) -> dict:
        """
        The plates seen most often in a period, with how many passages, on
        how many days, and in which direction. Only plates seen more than once.
        """
        rows = [r for r in self._reads(site, lo, hi) if r["plate_key"]]
        table: dict[str, dict] = defaultdict(lambda: {"passages": 0, "days": set(), "first": None, "last": None,
                                                      "directions": Counter(), "types": Counter()})
        for r in rows:
            item = table[r["plate_key"]]
            item["passages"] += 1
            item["days"].add(self.local(r["t"]).date())
            item["first"] = item["first"] or r["t"]
            item["last"] = r["t"]
            item["directions"][r["direction"]] += 1
            item["types"][r["vehicle_type"]] += 1
        repeat = sorted(((k, v) for k, v in table.items() if v["passages"] > 1),
                        key=lambda kv: (-kv[1]["passages"], kv[0]))
        return {
            "site_id": site,
            "distinct_plates": len(table),
            "repeat_plates": len(repeat),
            "reads_by_repeat_plates": sum(v["passages"] for _, v in repeat),
            "plates_read": len(rows),
            "plates": [
                {
                    "plate": k,
                    "passages": v["passages"],
                    "days_seen": len(v["days"]),
                    "first_seen": self.iso(v["first"]),
                    "last_seen": self.iso(v["last"]),
                    "by_direction": dict(v["directions"]),
                    "vehicle_type": v["types"].most_common(1)[0][0],
                }
                for k, v in repeat[: max(1, limit)]
            ],
            "synthetic_data": self._synthetic(rows),
        }

    def checks(self, site: str, now: float | None = None) -> dict:
        """
        Explainable checks of each camera over the last 7 days, each with the
        numbers that fired it. They need MIN_SAMPLE vehicles to judge.
          C1 read rate    plates read for under READ_RATE_MIN of vehicles
          C2 night        the night read rate (NIGHT hours) is under
                          NIGHT_RATIO × the day's
          C3 silent       no vehicle in the last SILENT_MINUTES, where the same
                          hour averaged SILENT_USUAL or more on the 7 days before
          C4 confidence   over LOW_CONFIDENCE_SHARE of plates read with
                          confidence under LOW_CONFIDENCE
        """
        t = self.text
        now = now if now is not None else time.time()
        lo, hi, period = self.period("7d", now=now)
        rows = self._reads(site, lo, hi)
        today = self.local(now).replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
        hour = self.local(now).hour
        cameras: dict[str, dict] = defaultdict(lambda: {"vehicles": 0, "read": 0, "night": [0, 0], "day": [0, 0],
                                                        "low": 0, "last": 0.0, "same_hour": 0})
        for r in rows:
            c = cameras[r["camera_id"]]
            read = bool(r["plate_key"])
            c["vehicles"] += 1
            c["read"] += read
            c["night" if self._night(r["t"]) else "day"][0] += 1
            c["night" if self._night(r["t"]) else "day"][1] += read
            c["low"] += read and (r["plate_confidence"] or 0) < LOW_CONFIDENCE
            c["last"] = max(c["last"], r["t"])
            c["same_hour"] += r["t"] < today and self.local(r["t"]).hour == hour

        found = []
        for camera, c in sorted(cameras.items()):
            rate = c["read"] / c["vehicles"]
            if c["vehicles"] >= MIN_SAMPLE and rate < READ_RATE_MIN:
                found.append({
                    "priority": "high", "check": "C1 read rate",
                    "issue": t["c1"][0].format(camera=camera, rate=round(100 * rate), min=round(100 * READ_RATE_MIN)),
                    "action": t["c1"][1],
                    "evidence": {"camera_id": camera, "vehicles": c["vehicles"], "plates_read": c["read"], "read_rate": round(rate, 3)},
                })
            (night_n, night_r), (day_n, day_r) = c["night"], c["day"]
            if night_n >= MIN_SAMPLE and day_n >= MIN_SAMPLE and night_r / night_n < NIGHT_RATIO * day_r / day_n:
                found.append({
                    "priority": "medium", "check": "C2 night",
                    "issue": t["c2"][0].format(camera=camera, night=round(100 * night_r / night_n), day=round(100 * day_r / day_n)),
                    "action": t["c2"][1],
                    "evidence": {"camera_id": camera, "night_vehicles": night_n, "night_read_rate": round(night_r / night_n, 3),
                                 "day_vehicles": day_n, "day_read_rate": round(day_r / day_n, 3),
                                 "night_hours": f"{NIGHT[0]:02d}:00 - {NIGHT[1]:02d}:00"},
                })
            usual = c["same_hour"] / 6  # the 6 full days before today
            if now - c["last"] >= SILENT_MINUTES * 60 and usual >= SILENT_USUAL:
                found.append({
                    "priority": "high", "check": "C3 silent",
                    "issue": t["c3"][0].format(camera=camera, minutes=SILENT_MINUTES, usual=round(usual)),
                    "action": t["c3"][1],
                    "evidence": {"camera_id": camera, "last_vehicle": self.iso(c["last"]), "usual_this_hour": round(usual, 1)},
                })
            if c["read"] >= MIN_SAMPLE and c["low"] / c["read"] > LOW_CONFIDENCE_SHARE:
                found.append({
                    "priority": "medium", "check": "C4 confidence",
                    "issue": t["c4"][0].format(camera=camera, share=round(100 * c["low"] / c["read"]), confidence=LOW_CONFIDENCE),
                    "action": t["c4"][1],
                    "evidence": {"camera_id": camera, "plates_read": c["read"], "low_confidence": c["low"]},
                })
        found.sort(key=lambda x: (x["priority"] != "high", x["check"]))
        return {
            "site_id": site,
            "period": period,
            "checks": found or [{"priority": "low", "check": "-", "issue": t["none"][0], "action": t["none"][1]}],
            "cameras": {k: {"vehicles": v["vehicles"], "read_rate": self._rate(v["read"], v["vehicles"]),
                            "last_vehicle": self.iso(v["last"])} for k, v in sorted(cameras.items())},
            "synthetic_data": self._synthetic(rows),
        }

    def nodes(self, site: str | None, now: float | None = None) -> dict:
        """The latest heartbeat of every edge device, and whether it is still reporting."""
        now = now if now is not None else time.time()
        query, args = "SELECT * FROM nodes", ()
        if site:
            query, args = query + " WHERE site_id = ?", (site,)
        rows = []
        for r in self.db.execute(query + " ORDER BY site_id, hostname", args):
            doc = json.loads(r["doc"])
            age = now - r["received"]
            rows.append({
                "site_id": r["site_id"],
                "hostname": r["hostname"],
                "online": age <= NODE_TIMEOUT,
                "status": r["status"] if age <= NODE_TIMEOUT else "offline",
                "last_heartbeat": self.iso(r["t"]),
                "seconds_since": round(age),
                "device": doc.get("device"),
                "metrics": doc.get("metrics"),
                "pipeline": doc.get("pipeline"),
            })
        return {"nodes": rows}
