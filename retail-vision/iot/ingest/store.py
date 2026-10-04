"""
SQLite store of the ingest service, and the deterministic queries behind the
agent API (contract §6).

Every number the agent says comes from here: counts, sums and rules over
the stored events, alerts and sales, never from the model. The rules are
written out next to each query so the answers can be traced.

Times are stored twice: the document's own ISO string, and epoch seconds
for range queries. "Today", weekdays and peak hours are in the store's time
zone (STORE_TZ).
"""

from __future__ import annotations

import json
import sqlite3
import statistics
import threading
import time
from collections import Counter, defaultdict
from datetime import datetime, timedelta, tzinfo

from contract import ALERT_STATUSES, ALERT_TYPES, parse_time

SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
    store_id TEXT NOT NULL,
    event_id TEXT NOT NULL,
    camera_id TEXT NOT NULL,
    type TEXT NOT NULL,
    t_start REAL NOT NULL,
    t_end REAL NOT NULL,
    track_id TEXT NOT NULL,
    role TEXT NOT NULL,
    zone_id TEXT,
    dwell_seconds REAL,
    garment_type TEXT,
    color TEXT,
    synthetic INTEGER NOT NULL,
    doc TEXT NOT NULL,
    received REAL NOT NULL,
    PRIMARY KEY (store_id, event_id)
);
CREATE INDEX IF NOT EXISTS events_time ON events (store_id, t_start);

CREATE TABLE IF NOT EXISTS zones (
    store_id TEXT NOT NULL,
    zone_id TEXT NOT NULL,
    brand TEXT,
    category TEXT,
    price_tier TEXT,
    avg_price REAL,
    updated REAL NOT NULL,
    PRIMARY KEY (store_id, zone_id)
);

CREATE TABLE IF NOT EXISTS alerts (
    store_id TEXT NOT NULL,
    alert_id TEXT NOT NULL,
    camera_id TEXT NOT NULL,
    alert_type TEXT NOT NULL,
    severity TEXT NOT NULL,
    t REAL NOT NULL,
    track_id TEXT,
    zone_id TEXT,
    title TEXT NOT NULL,
    status TEXT NOT NULL,
    -- set by a person (POST /api/v1/alerts/<id>/status); wins over status
    review_status TEXT,
    reviewed_by TEXT,
    reviewed_at REAL,
    synthetic INTEGER NOT NULL,
    doc TEXT NOT NULL,
    received REAL NOT NULL,
    PRIMARY KEY (store_id, alert_id)
);
CREATE INDEX IF NOT EXISTS alerts_time ON alerts (store_id, t);

CREATE TABLE IF NOT EXISTS sales (
    store_id TEXT NOT NULL,
    tx_id TEXT NOT NULL,
    t REAL NOT NULL,
    shelf_id TEXT NOT NULL,
    brand TEXT,
    garment_type TEXT NOT NULL,
    color TEXT NOT NULL,
    amount REAL NOT NULL,
    currency TEXT,
    synthetic INTEGER NOT NULL,
    doc TEXT NOT NULL,
    received REAL NOT NULL,
    PRIMARY KEY (store_id, tx_id)
);
CREATE INDEX IF NOT EXISTS sales_time ON sales (store_id, t);

CREATE TABLE IF NOT EXISTS reports (
    store_id TEXT NOT NULL,
    report_id TEXT NOT NULL,
    period_type TEXT,
    start_date TEXT,
    end_date TEXT,
    synthetic INTEGER NOT NULL,
    doc TEXT NOT NULL,
    received REAL NOT NULL,
    PRIMARY KEY (store_id, report_id)
);

-- The latest heartbeat of each Jetson; the history goes to InfluxDB
CREATE TABLE IF NOT EXISTS nodes (
    store_id TEXT NOT NULL,
    hostname TEXT NOT NULL,
    t REAL NOT NULL,
    status TEXT NOT NULL,
    doc TEXT NOT NULL,
    received REAL NOT NULL,
    PRIMARY KEY (store_id, hostname)
);
"""

# Status names people use for alert reviews, in Spanish and English
STATUS_NAMES = {
    **{s: s for s in ALERT_STATUSES},
    "pending": "pending_review", "pendiente": "pending_review", "pendientes": "pending_review",
    "atendida": "attended", "atendidas": "attended",
    "descartada": "dismissed", "descartadas": "dismissed",
}
# Garment interactions, as the Jetson's own reports count them (db.py get_summary)
INTERACTIONS = ("garment_touched", "garment_taken")
# Text people read in the agent API's answers, in the store's language
# (STORE_LANG). Codes (caliente/frio/normal, alta/media/baja, the diagnoses)
# are the contract's and stay as they are.
TEXT = {
    "en": {
        "weekdays": ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"),
        "r1": ("{garment}, {colour} ({shelf}): {units} units sold in 7 days.", "Check stock and restock the floor."),
        "r2": ("{shelf} ({id}) is high-value and sells, but gets little traffic.",
               "Move it near the entrance flow or to the centre of the store."),
        "r3": ("{shelf} ({id}) draws interest but rarely sells.", "Check the sizes on hand, price labels and display."),
        "r4": ("{count} high-value opportunity alerts (A2) at {shelf} in 7 days.", "Assign a salesperson to that shelf"),
        "r4_pending": ("{count} high-value opportunity alerts (A2) are still pending review.",
                       "Review them and reinforce floor coverage"),
        "peak": " at the peak hour ({hour}).",
        "none": ("No rule fired.", "Keep monitoring weekly."),
        "note": "No inventory feed: \"restock\" is based on sales, not on stock.",
    },
    "es": {
        "weekdays": ("lunes", "martes", "miércoles", "jueves", "viernes", "sábado", "domingo"),
        "r1": ("{garment}, color {colour} ({shelf}): {units} unidades vendidas en 7 días.",
               "Revisar existencias y reponer en sala."),
        "r2": ("{shelf} ({id}) es de alto valor y vende, pero tiene poco tráfico.",
               "Reubicar cerca del flujo de entrada o en la zona central."),
        "r3": ("{shelf} ({id}) atrae interés pero convierte poco.", "Revisar tallas disponibles, precio visible y exhibición."),
        "r4": ("{count} alertas de oportunidad (A2) en {shelf} en 7 días.", "Asignar un vendedor a ese estante"),
        "r4_pending": ("{count} alertas de oportunidad (A2) siguen pendientes de revisión.",
                       "Revisarlas y reforzar la atención en sala"),
        "peak": " en la hora pico ({hour}).",
        "none": ("Ninguna regla se activó.", "Seguir el monitoreo semanal."),
        "note": "Sin inventario conectado: «reponer» se basa en las ventas, no en las existencias.",
    },
}
# Periods take a weekday in either language
WEEKDAY_NAMES = {
    **{name: i for text in TEXT.values() for i, name in enumerate(text["weekdays"])},
    "miercoles": 2,
    "sabado": 5,
}
PERIODS = ("today", "yesterday", "this_week", "last_week", "this_month", "last_month", "<N>d (e.g. 7d, 30d)", "a weekday (tuesday, martes)")
# A Jetson that hasn't sent a heartbeat in this long counts as offline (3 missed beats)
NODE_TIMEOUT = 90
# A visit longer than this is a track id reused by the camera, not a visit
MAX_VISIT = 4 * 3600


class PeriodError(ValueError):
    pass


def canonical(doc: dict) -> str:
    return json.dumps(doc, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def singular(word: str) -> str:
    """camisas → camisa, polos → polo, pantalones → pantalon: enough for category filters."""
    word = word.strip().casefold()
    if word.endswith("ones"):
        return word[:-2]
    if word.endswith("es") and len(word) > 4 and word[-3] not in "aeiou":
        return word[:-2]
    if word.endswith("s") and len(word) > 3:
        return word[:-1]
    return word


class Store:
    def __init__(self, path: str, tz: tzinfo, lang: str = "en") -> None:
        self.tz = tz
        if lang not in TEXT:
            raise ValueError(f"STORE_LANG must be one of {', '.join(TEXT)}")
        self.text = TEXT[lang]
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
        Jetson re-sends a row when it retries a batch, or when the model's
        description of an event arrives after the event: the newest wins.
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

    def put_event(self, doc: dict) -> str:
        attributes = doc.get("attributes") or {}
        planogram = doc.get("planogram") or {}
        with self.lock:
            if doc.get("zone_id") and planogram:
                # The planogram travels with the event, as on the Jetson
                self.db.execute(
                    "INSERT OR REPLACE INTO zones VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (
                        doc["store_id"],
                        doc["zone_id"],
                        planogram.get("brand"),
                        planogram.get("category"),
                        planogram.get("price_tier"),
                        planogram.get("avg_price"),
                        time.time(),
                    ),
                )
            return self._upsert(
                "events",
                {"store_id": doc["store_id"], "event_id": doc["event_id"]},
                {
                    "camera_id": doc["camera_id"],
                    "type": doc["type"],
                    "t_start": parse_time(doc["ts_start"], self.tz).timestamp(),
                    "t_end": parse_time(doc["ts_end"], self.tz).timestamp(),
                    "track_id": doc["track_id"],
                    "role": doc["role"],
                    "zone_id": doc.get("zone_id"),
                    "dwell_seconds": doc.get("dwell_seconds"),
                    "garment_type": (attributes.get("garment_type") or "").strip().casefold() or None,
                    "color": (attributes.get("color") or "").strip().casefold() or None,
                    "synthetic": int(doc["synthetic"]),
                },
                doc,
            )

    def put_alert(self, doc: dict) -> str:
        evidence = doc.get("evidence") or {}
        with self.lock:
            return self._upsert(
                "alerts",
                {"store_id": doc["store_id"], "alert_id": doc["alert_id"]},
                {
                    "camera_id": doc["camera_id"],
                    "alert_type": doc["alert_type"],
                    "severity": doc["severity"],
                    "t": parse_time(doc["timestamp"], self.tz).timestamp(),
                    "track_id": doc.get("track_id"),
                    "zone_id": evidence.get("zone_id"),
                    "title": doc["title"],
                    "status": doc.get("status", "pending_review"),
                    "synthetic": int(doc["synthetic"]),
                },
                doc,
            )

    def put_sale(self, doc: dict) -> str:
        with self.lock:
            return self._upsert(
                "sales",
                {"store_id": doc["store_id"], "tx_id": doc["tx_id"]},
                {
                    "t": parse_time(doc["timestamp"], self.tz).timestamp(),
                    "shelf_id": doc["shelf_id"],
                    "brand": doc.get("brand"),
                    "garment_type": doc["garment_type"].strip().casefold(),
                    "color": doc["color"].strip().casefold(),
                    "amount": float(doc["amount"]),
                    "currency": doc.get("currency"),
                    "synthetic": int(doc.get("synthetic", False)),
                },
                doc,
            )

    def put_report(self, doc: dict) -> str:
        period = doc.get("period") or {}
        with self.lock:
            return self._upsert(
                "reports",
                {"store_id": doc["store_id"], "report_id": doc["report_id"]},
                {
                    "period_type": period.get("type"),
                    "start_date": period.get("start_date"),
                    "end_date": period.get("end_date"),
                    "synthetic": int(doc["synthetic"]),
                },
                doc,
            )

    def put_heartbeat(self, doc: dict) -> str:
        with self.lock:
            return self._upsert(
                "nodes",
                {"store_id": doc["store_id"], "hostname": doc["device"]["hostname"]},
                {"t": parse_time(doc["timestamp"], self.tz).timestamp(), "status": doc["status"]},
                doc,
            )

    def review_alert(self, store_id: str, alert_id: str, status: str, by: str | None) -> dict | None:
        if status not in ALERT_STATUSES:
            raise ValueError(f"status must be one of {', '.join(ALERT_STATUSES)}")
        with self.lock:
            cursor = self.db.execute(
                "UPDATE alerts SET review_status = ?, reviewed_by = ?, reviewed_at = ? WHERE store_id = ? AND alert_id = ?",
                (status, by, time.time(), store_id, alert_id),
            )
            if not cursor.rowcount:
                return None
        return self.alert(store_id, alert_id)

    # ── reads ─────────────────────────────────────────────────────────────────

    def stores(self) -> list[str]:
        rows = self.db.execute(
            "SELECT store_id FROM events UNION SELECT store_id FROM alerts UNION SELECT store_id FROM nodes "
            "UNION SELECT store_id FROM sales ORDER BY store_id"
        )
        return [r[0] for r in rows]

    def local(self, t: float) -> datetime:
        return datetime.fromtimestamp(t, self.tz)

    def iso(self, t: float | None) -> str | None:
        return None if t is None else self.local(t).isoformat(timespec="seconds")

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

    def _events(self, store: str, lo: float, hi: float) -> list[sqlite3.Row]:
        return self.db.execute(
            "SELECT * FROM events WHERE store_id = ? AND t_start >= ? AND t_start < ? ORDER BY t_start",
            (store, lo, hi),
        ).fetchall()

    def _alerts(self, store: str, lo: float, hi: float) -> list[sqlite3.Row]:
        return self.db.execute(
            "SELECT * FROM alerts WHERE store_id = ? AND t >= ? AND t < ? ORDER BY t DESC",
            (store, lo, hi),
        ).fetchall()

    def _sales(self, store: str, lo: float, hi: float) -> list[sqlite3.Row]:
        return self.db.execute(
            "SELECT * FROM sales WHERE store_id = ? AND t >= ? AND t < ? ORDER BY t",
            (store, lo, hi),
        ).fetchall()

    def _zones(self, store: str) -> dict[str, sqlite3.Row]:
        return {r["zone_id"]: r for r in self.db.execute("SELECT * FROM zones WHERE store_id = ?", (store,))}

    @staticmethod
    def _shelf_name(zone_id: str, zone: sqlite3.Row | None) -> str:
        if zone is None:
            return zone_id
        parts = [p for p in (zone["category"], zone["brand"]) if p]
        return " · ".join(parts) or zone_id

    @staticmethod
    def _status(row: sqlite3.Row) -> str:
        return row["review_status"] or row["status"]

    def _synthetic(self, *groups: list[sqlite3.Row]) -> bool:
        return any(r["synthetic"] for rows in groups for r in rows)

    def summary(self, store: str, lo: float, hi: float) -> dict:
        """
        Traffic, interest, fitting rooms, sales and alerts over a period.
          visitors           customer_entry events of customers (staff excluded)
          garment_interactions  garment_touched + garment_taken, as on the Jetson
          conversion_rate    sales transactions / visitors (POS lines are transactions)
          avg_visit_minutes  entry to exit of the same track on the same camera
          peak_hour          the store-local hour with the most entries
        """
        events, alerts, sales = self._events(store, lo, hi), self._alerts(store, lo, hi), self._sales(store, lo, hi)
        customers = [e for e in events if e["role"] != "staff"]
        types = Counter(e["type"] for e in customers)
        visitors = types["customer_entry"]

        entries: dict[tuple, float] = {}
        visits = []
        for e in customers:
            key = (e["camera_id"], e["track_id"])
            if e["type"] == "customer_entry":
                entries[key] = e["t_start"]
            elif e["type"] == "customer_exit" and key in entries:
                length = e["t_start"] - entries.pop(key)
                if 0 < length <= MAX_VISIT:
                    visits.append(length)

        hours = Counter(self.local(e["t_start"]).hour for e in customers if e["type"] == "customer_entry")
        peak = None
        if hours:
            hour, count = max(sorted(hours.items()), key=lambda kv: kv[1])
            peak = {"hour": f"{hour:02d}:00 - {(hour + 1) % 24:02d}:00", "entries": count}

        shelves = Counter(e["zone_id"] for e in customers if e["type"] in INTERACTIONS and e["zone_id"])
        zones = self._zones(store)
        top = None
        if shelves:
            zone_id, count = max(sorted(shelves.items()), key=lambda kv: kv[1])
            top = {"shelf_id": zone_id, "name": self._shelf_name(zone_id, zones.get(zone_id)), "interactions": count}

        revenue = sum(s["amount"] for s in sales)
        currencies = Counter(s["currency"] for s in sales if s["currency"])
        return {
            "store_id": store,
            "visitors": visitors,
            "exits": types["customer_exit"],
            "garment_interactions": sum(types[t] for t in INTERACTIONS),
            "garments_taken": types["garment_taken"],
            "fitting_room_visits": types["fitting_room_entered"],
            "checkout_visits": types["checkout_visited"],
            "sales_transactions": len(sales),
            "revenue": round(revenue, 2),
            "currency": currencies.most_common(1)[0][0] if currencies else None,
            "conversion_rate": round(len(sales) / visitors, 3) if visitors else None,
            "avg_visit_minutes": round(statistics.mean(visits) / 60, 1) if visits else None,
            "peak_hour": peak,
            "top_shelf": top,
            "alerts": self._alert_counts(alerts),
            "events_by_type": dict(sorted(types.items())),
            "synthetic_data": self._synthetic(events, alerts, sales),
        }

    def _alert_counts(self, alerts: list[sqlite3.Row]) -> dict:
        by_type = Counter(f"{ALERT_TYPES[a['alert_type']]} {a['alert_type']}" for a in alerts)
        by_status = Counter(self._status(a) for a in alerts)
        return {"total": len(alerts), "by_type": dict(sorted(by_type.items())), "by_status": dict(sorted(by_status.items()))}

    def alert(self, store: str, alert_id: str) -> dict | None:
        row = self.db.execute("SELECT * FROM alerts WHERE store_id = ? AND alert_id = ?", (store, alert_id)).fetchone()
        return self._alert_item(row) if row else None

    def _alert_item(self, a: sqlite3.Row) -> dict:
        doc = json.loads(a["doc"])
        evidence = doc.get("evidence") or {}
        item = {
            "alert_id": a["alert_id"],
            "code": ALERT_TYPES[a["alert_type"]],
            "alert_type": a["alert_type"],
            "severity": a["severity"],
            "time": self.iso(a["t"]),
            "weekday": self.text["weekdays"][self.local(a["t"]).weekday()],
            "camera_id": a["camera_id"],
            "track_id": a["track_id"],
            "zone_id": a["zone_id"],
            "title": a["title"],
            "description": doc.get("description"),
            "recommended_action": doc.get("recommended_action"),
            "status": self._status(a),
            "reviewed_by": a["reviewed_by"],
            "reviewed_at": self.iso(a["reviewed_at"]),
            "keyframe_image_uri": evidence.get("keyframe_image_uri"),
            "clip_uri": evidence.get("clip_uri"),
            "boxes_uri": evidence.get("boxes_uri"),
            "synthetic": bool(a["synthetic"]),
        }
        return {k: v for k, v in item.items() if v is not None}

    def alerts(self, store: str, lo: float, hi: float, alert_type: str | None = None, severity: str | None = None,
               status: str | None = None, limit: int = 20) -> dict:
        """
        Alerts over a period, newest first, with counts per type, status,
        type × status and store-local day. alert_type takes the contract's name
        or A1–A4; status also takes Spanish (atendidas, descartadas, pendientes).
        """
        if status:
            wanted = STATUS_NAMES.get(status.strip().casefold())
            if wanted is None:
                raise PeriodError(f"status must be one of {', '.join(ALERT_STATUSES)}")
            status = wanted
        codes = {code.casefold(): name for name, code in ALERT_TYPES.items()}
        if alert_type:
            alert_type = codes.get(alert_type.strip().casefold(), alert_type.strip())
            if alert_type not in ALERT_TYPES:
                raise PeriodError(f"type must be A1-A4 or one of {', '.join(ALERT_TYPES)}")
        rows = [
            a
            for a in self._alerts(store, lo, hi)
            if (not alert_type or a["alert_type"] == alert_type)
            and (not severity or a["severity"] == severity)
            and (not status or self._status(a) == status)
        ]
        days: dict[str, Counter] = defaultdict(Counter)
        crossed: dict[str, Counter] = defaultdict(Counter)
        for a in rows:
            crossed[f"{ALERT_TYPES[a['alert_type']]} {a['alert_type']}"][self._status(a)] += 1
            day = self.local(a["t"])
            key = f"{day.date().isoformat()} {self.text['weekdays'][day.weekday()]}"
            days[key]["total"] += 1
            days[key][self._status(a)] += 1
        return {
            "store_id": store,
            **self._alert_counts(rows),
            "by_type_and_status": {k: dict(sorted(v.items())) for k, v in sorted(crossed.items())},
            "by_day": [{"day": k, **dict(v)} for k, v in sorted(days.items())],
            "items": [self._alert_item(a) for a in rows[: max(0, limit)]],
            "items_shown": min(len(rows), max(0, limit)),
            "synthetic_data": self._synthetic(rows),
        }

    def layout(self, store: str, lo: float, hi: float) -> dict:
        """
        Every shelf's interest (camera) against its sales (POS).
          interactions   garment_touched + garment_taken on the shelf
          conversion     units sold / interactions
          status         caliente: interactions >= 1.5 × the median shelf's;
                         frio: <= 0.5 × the median; normal otherwise
          diagnosis      interes_sin_venta: at least the median interest, but
                         converting at under half the store's rate;
                         alto_valor_poco_trafico: a price_tier "alto" shelf that is frio
        """
        events, sales = self._events(store, lo, hi), self._sales(store, lo, hi)
        zones = self._zones(store)
        shelves: dict[str, dict] = defaultdict(lambda: {"interactions": 0, "taken": 0, "dwell": [], "units_sold": 0, "revenue": 0.0})
        for e in events:
            if not e["zone_id"] or e["role"] == "staff":
                continue
            if e["type"] in INTERACTIONS:
                shelves[e["zone_id"]]["interactions"] += 1
                if e["type"] == "garment_taken":
                    shelves[e["zone_id"]]["taken"] += 1
            elif e["type"] == "shelf_dwell" and e["dwell_seconds"] is not None:
                shelves[e["zone_id"]]["dwell"].append(e["dwell_seconds"])
        for s in sales:
            shelves[s["shelf_id"]]["units_sold"] += 1
            shelves[s["shelf_id"]]["revenue"] += s["amount"]
        # Shelves the planogram knows but nobody touched are the coldest of all
        for zone_id in zones:
            if zones[zone_id]["category"] or zones[zone_id]["brand"]:
                shelves[zone_id]

        if not shelves:
            return {"store_id": store, "shelves": [], "synthetic_data": False}
        median = statistics.median(s["interactions"] for s in shelves.values())
        total_i = sum(s["interactions"] for s in shelves.values())
        total_u = sum(s["units_sold"] for s in shelves.values())
        store_rate = total_u / total_i if total_i else 0.0

        rows = []
        for zone_id, s in shelves.items():
            zone = zones.get(zone_id)
            rate = s["units_sold"] / s["interactions"] if s["interactions"] else None
            status = "normal"
            if median and s["interactions"] >= 1.5 * median:
                status = "caliente"
            elif s["interactions"] <= 0.5 * median:
                status = "frio"
            diagnosis = []
            if s["interactions"] and s["interactions"] >= median and store_rate and (rate or 0) < 0.5 * store_rate:
                diagnosis.append("interes_sin_venta")
            if zone is not None and zone["price_tier"] == "alto" and status == "frio":
                diagnosis.append("alto_valor_poco_trafico")
            row = {
                "shelf_id": zone_id,
                "name": self._shelf_name(zone_id, zone),
                "brand": zone["brand"] if zone else None,
                "category": zone["category"] if zone else None,
                "price_tier": zone["price_tier"] if zone else None,
                "interactions": s["interactions"],
                "garments_taken": s["taken"],
                "avg_dwell_seconds": round(statistics.mean(s["dwell"]), 1) if s["dwell"] else None,
                "units_sold": s["units_sold"],
                "revenue": round(s["revenue"], 2),
                "conversion": round(rate, 3) if rate is not None else None,
                "status": status,
                "diagnosis": diagnosis,
            }
            rows.append({k: v for k, v in row.items() if v is not None})
        rows.sort(key=lambda r: (-r["interactions"], r["shelf_id"]))

        def pick(key, reverse=False, where=lambda r: True):
            found = sorted((r for r in rows if where(r)), key=lambda r: (key(r), r["shelf_id"]), reverse=reverse)
            return found[0]["shelf_id"] if found else None

        return {
            "store_id": store,
            "median_interactions": median,
            "store_conversion": round(store_rate, 3),
            "most_interest": pick(lambda r: r["interactions"], reverse=True),
            "best_seller": pick(lambda r: r["revenue"], reverse=True),
            "lowest_sales": pick(lambda r: r["revenue"]),
            "interest_without_sales": pick(
                lambda r: r.get("conversion", 0) - r["interactions"] / 1e6,
                where=lambda r: "interes_sin_venta" in r["diagnosis"],
            ),
            "shelves": rows,
            "synthetic_data": self._synthetic(events, sales),
        }

    def ranking(self, store: str, lo: float, hi: float, category: str | None = None, color: str | None = None,
                limit: int = 5) -> dict:
        """
        Garments by type and colour: interactions seen by the camera (the
        model's garment_type and color on touched/taken events) and units sold
        (POS). Sorted by units sold, then interactions.
        """
        events, sales = self._events(store, lo, hi), self._sales(store, lo, hi)
        want_type = singular(category) if category else None
        want_color = color.strip().casefold() if color else None
        table: dict[tuple, dict] = defaultdict(lambda: {"interactions": 0, "units_sold": 0, "revenue": 0.0})

        def keep(garment: str | None, colour: str | None) -> bool:
            if not garment or not colour:
                return False
            return (not want_type or singular(garment) == want_type) and (not want_color or colour == want_color)

        for e in events:
            if e["type"] in INTERACTIONS and e["role"] != "staff" and keep(e["garment_type"], e["color"]):
                table[(e["garment_type"], e["color"])]["interactions"] += 1
        for s in sales:
            if keep(s["garment_type"], s["color"]):
                row = table[(s["garment_type"], s["color"])]
                row["units_sold"] += 1
                row["revenue"] += s["amount"]
        rows = [
            {"garment_type": g, "color": c, **v, "revenue": round(v["revenue"], 2)}
            for (g, c), v in table.items()
        ]
        rows.sort(key=lambda r: (-r["units_sold"], -r["interactions"], r["garment_type"], r["color"]))
        return {
            "store_id": store,
            "filter": {k: v for k, v in (("garment_type", want_type), ("color", want_color)) if v},
            "total_interactions": sum(r["interactions"] for r in rows),
            "total_units_sold": sum(r["units_sold"] for r in rows),
            "ranking": rows[: max(1, limit)],
            "synthetic_data": self._synthetic(events, sales),
        }

    def restock_advice(self, store: str, now: float | None = None) -> dict:
        """
        Explainable suggestions over the last 7 days, each with the rule that
        fired and its numbers. There is no inventory feed yet, so "reponer"
        means sales are high enough that stock should be checked.
          R1 reponer      the 3 best-selling garment/colours from "alto" shelves,
                          if they sold >= 3 units (alta from 6)
          R2 reubicar     an "alto" shelf is frio but still sells
          R3 exhibicion   interes_sin_venta on a shelf
          R4 personal     >= 3 A2 alerts on one shelf, or A2 alerts still pending
        """
        t = self.text
        lo, hi, period = self.period("last_7d", now=now)
        layout = self.layout(store, lo, hi)
        zones = self._zones(store)
        advice = []

        sales = self._sales(store, lo, hi)
        sold: dict[tuple, dict] = defaultdict(lambda: {"units": 0, "revenue": 0.0, "shelves": Counter()})
        for s in sales:
            zone = zones.get(s["shelf_id"])
            if zone is not None and zone["price_tier"] == "alto":
                item = sold[(s["garment_type"], s["color"])]
                item["units"] += 1
                item["revenue"] += s["amount"]
                item["shelves"][s["shelf_id"]] += 1
        best = sorted(sold.items(), key=lambda kv: (-kv[1]["units"], kv[0]))
        for (garment, colour), item in best[:3]:
            if item["units"] >= 3:
                shelf = item["shelves"].most_common(1)[0][0]
                advice.append({
                    "priority": "alta" if item["units"] >= 6 else "media",
                    "rule": "R1 reponer",
                    "issue": t["r1"][0].format(garment=garment, colour=colour, shelf=self._shelf_name(shelf, zones.get(shelf)),
                                               units=item["units"]),
                    "action": t["r1"][1],
                    "evidence": {"garment_type": garment, "color": colour, "shelf_id": shelf,
                                 "units_sold": item["units"], "revenue": round(item["revenue"], 2)},
                })

        for shelf in layout["shelves"]:
            if shelf.get("price_tier") == "alto" and shelf["status"] == "frio" and shelf["units_sold"] > 0:
                advice.append({
                    "priority": "alta",
                    "rule": "R2 reubicar",
                    "issue": t["r2"][0].format(shelf=shelf["name"], id=shelf["shelf_id"]),
                    "action": t["r2"][1],
                    "evidence": {k: shelf.get(k) for k in ("shelf_id", "interactions", "units_sold", "revenue")},
                })
            if "interes_sin_venta" in shelf["diagnosis"]:
                advice.append({
                    "priority": "media",
                    "rule": "R3 exhibicion",
                    "issue": t["r3"][0].format(shelf=shelf["name"], id=shelf["shelf_id"]),
                    "action": t["r3"][1],
                    "evidence": {k: shelf.get(k) for k in ("shelf_id", "interactions", "units_sold", "conversion")},
                })

        a2 = [a for a in self._alerts(store, lo, hi) if a["alert_type"] == "high_value_opportunity"]
        per_shelf = Counter(a["zone_id"] for a in a2 if a["zone_id"])
        pending = sum(1 for a in a2 if self._status(a) == "pending_review")
        peak = self.summary(store, lo, hi)["peak_hour"]
        at_peak = t["peak"].format(hour=peak["hour"]) if peak else "."
        for zone_id, count in sorted(per_shelf.items(), key=lambda kv: (-kv[1], kv[0])):
            if count >= 3:
                advice.append({
                    "priority": "media",
                    "rule": "R4 personal",
                    "issue": t["r4"][0].format(count=count, shelf=self._shelf_name(zone_id, zones.get(zone_id))),
                    "action": t["r4"][1] + at_peak,
                    "evidence": {"shelf_id": zone_id, "a2_alerts": count},
                })
        if pending:
            advice.append({
                "priority": "media",
                "rule": "R4 personal",
                "issue": t["r4_pending"][0].format(count=pending),
                "action": t["r4_pending"][1] + at_peak,
                "evidence": {"a2_pending": pending},
            })

        order = {"alta": 0, "media": 1, "baja": 2}
        advice.sort(key=lambda a: order[a["priority"]])
        return {
            "store_id": store,
            "period": period,
            "advice": advice or [{"priority": "baja", "rule": "-", "issue": t["none"][0], "action": t["none"][1]}],
            "note": t["note"],
            "synthetic_data": layout["synthetic_data"],
        }

    def nodes(self, store: str | None, now: float | None = None) -> dict:
        """The latest heartbeat of every Jetson, and whether it is still reporting."""
        now = now if now is not None else time.time()
        query, args = "SELECT * FROM nodes", ()
        if store:
            query, args = query + " WHERE store_id = ?", (store,)
        rows = []
        for r in self.db.execute(query + " ORDER BY store_id, hostname", args):
            doc = json.loads(r["doc"])
            age = now - r["received"]
            rows.append({
                "store_id": r["store_id"],
                "hostname": r["hostname"],
                "online": age <= NODE_TIMEOUT,
                "status": r["status"] if age <= NODE_TIMEOUT else "offline",
                "last_heartbeat": self.iso(r["t"]),
                "seconds_since": round(age),
                "device": doc.get("device"),
                "metrics": doc.get("metrics"),
                "pipeline": doc.get("pipeline"),
                "alerts_pending": doc.get("alerts_pending"),
            })
        return {"nodes": rows}

    def latest_report(self, store: str) -> dict | None:
        row = self.db.execute(
            "SELECT doc FROM reports WHERE store_id = ? ORDER BY end_date DESC, received DESC LIMIT 1", (store,)
        ).fetchone()
        return json.loads(row["doc"]) if row else None
