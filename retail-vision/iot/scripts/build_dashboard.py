#!/usr/bin/env python3
"""Generates the store dashboard.

    python3 scripts/build_dashboard.py [--lang en|es] [--uid UID] [--title TITLE]
                                      [--currency PREFIX] [--logo PNG] [--brand TEXT]

writes config/grafana/provisioning/dashboards/json/store.json (or <uid>.json
with --out). Edit this, not the JSON; Grafana reloads provisioned dashboards
on its own (up to 30 s). A project made from this template re-runs it with its
own options, e.g. a Spanish dashboard with a logo:

    python3 scripts/build_dashboard.py --lang es --uid acme --title Boutique \\
        --currency 'B/.' --logo ../theme/logo.png --brand 'ACME'

The panels read what the ingest service copies to InfluxDB (see
ingest/sinks.py): boutique_event, boutique_alert and boutique_sale per store,
and the edge device's heartbeats as sensor_data (device = its hostname) plus
boutique_node. The counts are InfluxDB's, over the dashboard's time range; the
agent's numbers come from the ingest service's SQLite, which also knows which
alerts staff have attended.
"""

from __future__ import annotations

import argparse
import base64
import html
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DS = {"type": "influxdb", "uid": "influxdb-telemetry"}

# Dark card palette for the header; series and status colours
BG, CARD, BORDER = "#0A0C0E", "#111418", "#22272E"
IVORY, SILVER, PEWTER = "#E8EAEE", "#C8CDD6", "#8C92A0"
BLUE, ORANGE, PURPLE = "#3987E5", "#D95926", "#8F6BD8"
OK, WARN, CRIT, IDLE = "#0CA30C", "#FAB219", "#D03B3B", "#545457"
ALERT_COLOURS = {"A1": BLUE, "A2": OK, "A3": PURPLE, "A4": WARN}

# Everything a person reads on the dashboard, per --lang
STRINGS = {
    "en": {
        "subtitle": "Customers, garments and alerts seen by the store's camera · no face recognition",
        "store": "Store",
        "device": "Device",
        "visitors": ("Visitors", "Customers who came in (customer_entry events; staff don't count)."),
        "interactions": ("Interactions", "Garments touched or taken, as the edge device counts them."),
        "fitting": ("Fitting room", "Fitting-room entries."),
        "sales": ("Sales", "Point-of-sale lines (POST /api/v1/pos)."),
        "revenue": ("Revenue", "Total sales over the period."),
        "alerts": ("Alerts", "Alerts A1–A4 raised. Each one is a request for a person to look."),
        "traffic": "Visitors per hour",
        "funnel": ("Funnel", "From the door to the till, over the selected period."),
        "stages": ("Entries", "Interactions", "Took a garment", "Fitting room", "Till", "Sales"),
        "shelves": ("Shelves: interest and sales",
                    "Garment interactions (camera) against units sold (till). "
                    "High interest with few sales suggests checking sizes, price or display."),
        "units_sold": "Units sold",
        "by_type": ("Alerts by type", "Every alert is a request for a person to review; the system judges no one's intent."),
        "codes": {"A1": "A1 · Arrival / browsing", "A2": "A2 · High-value opportunity", "A3": "A3 · Fitting room",
                  "A4": "A4 · Loss risk — review"},
        "severities": {"low": "low", "medium": "medium", "high": "high"},
        "recent": ("Recent alerts", "The latest 100 in the period. Whether someone attended them is in the agent (ingest), not here."),
        "columns": {"_time": "Time", "code": "Type", "severity": "Severity", "title": "Alert", "zone_id": "Zone",
                    "camera_id": "Camera", "alert_id": "Id"},
        "node": ("Edge device", "Online: a heartbeat in the last 90 s with status healthy. No heartbeat for 90 s: no signal."),
        "node_states": ("online", "degraded", "no signal"),
        "temperature": "Temperature",
        "usage": ("Usage", "The edge device's CPU and GPU. On a Jetson Orin Nano they share its 8 GB of memory."),
        "processing": ("Processing", "Frames analysed per second and people tracked."),
        "fps": "Analysis FPS",
        "tracks": "People on camera",
        "description": "Store traffic, shelf interest, sales, alerts A1–A4 and the edge device's health.",
    },
    "es": {
        "subtitle": "Clientes, prendas y alertas vistos por la cámara de la tienda · sin reconocimiento facial",
        "store": "Tienda",
        "device": "Equipo",
        "visitors": ("Visitantes", "Clientes que entraron (eventos customer_entry; el personal no cuenta)."),
        "interactions": ("Interacciones", "Prendas tocadas o tomadas, como las cuenta el equipo de la tienda."),
        "fitting": ("Probador", "Entradas al probador."),
        "sales": ("Ventas", "Líneas de venta del punto de venta (POST /api/v1/pos)."),
        "revenue": ("Ingresos", "Suma de las ventas del periodo."),
        "alerts": ("Alertas", "Alertas A1–A4 emitidas. Cada una es una solicitud de revisión para una persona."),
        "traffic": "Visitantes por hora",
        "funnel": ("Embudo", "De la entrada a la venta, en el periodo elegido."),
        "stages": ("Entradas", "Interacciones", "Tomó una prenda", "Probador", "Caja", "Ventas"),
        "shelves": ("Estantes: interés y ventas",
                    "Interacciones con prendas (cámara) frente a unidades vendidas (punto de venta). "
                    "Mucho interés con pocas ventas sugiere revisar tallas, precio o exhibición."),
        "units_sold": "Unidades vendidas",
        "by_type": ("Alertas por tipo", "Toda alerta es una solicitud de revisión para una persona; el sistema no juzga intenciones."),
        "codes": {"A1": "A1 · Llegada / qué busca", "A2": "A2 · Oportunidad de alto valor", "A3": "A3 · Probador",
                  "A4": "A4 · Riesgo de pérdida — revisar"},
        "severities": {"low": "baja", "medium": "media", "high": "alta"},
        "recent": ("Alertas recientes", "Las 100 más recientes del periodo. Si las atendió alguien lo sabe el agente (ingest), no esta tabla."),
        "columns": {"_time": "Hora", "code": "Tipo", "severity": "Severidad", "title": "Alerta", "zone_id": "Zona",
                    "camera_id": "Cámara", "alert_id": "Id"},
        "node": ("Equipo de la tienda", "En línea: latido en los últimos 90 s y estado healthy. Sin latido por 90 s: sin señal."),
        "node_states": ("en línea", "degradado", "sin señal"),
        "temperature": "Temperatura",
        "usage": ("Uso", "CPU y GPU del equipo. En un Jetson Orin Nano comparten los 8 GB de memoria."),
        "processing": ("Procesamiento", "Fotogramas analizados por segundo y personas seguidas."),
        "fps": "FPS de análisis",
        "tracks": "Personas en cámara",
        "description": "Tráfico de la tienda, interés por estante, ventas, alertas A1–A4 y salud del equipo.",
    },
}

# ── Flux ─────────────────────────────────────────────────────────────────────

RANGE = "from(bucket: v.defaultBucket)\n  |> range(start: v.timeRangeStart, stop: v.timeRangeStop)\n"
STORE = 'r.store_id == "${store}"'
NODE = 'r.device == "${node}"'
CUSTOMER = 'r.role != "staff"'
SALES = RANGE + f'  |> filter(fn: (r) => r._measurement == "boutique_sale" and {STORE})\n'
ALERT_ROWS = RANGE + f'  |> filter(fn: (r) => r._measurement == "boutique_alert" and {STORE})\n'


def events(types: str) -> str:
    """Rows of boutique_event whose type matches the regex."""
    return (
        RANGE
        + f'  |> filter(fn: (r) => r._measurement == "boutique_event" and r._field == "count" and {STORE})\n'
        + f"  |> filter(fn: (r) => r.type =~ /^({types})$/ and {CUSTOMER})\n"
    )


def total(flux: str, name: str) -> str:
    return flux + f'  |> group()\n  |> sum()\n  |> rename(columns: {{_value: "{name}"}})'


def health(*sensors: str) -> str:
    return (
        RANGE
        + f'  |> filter(fn: (r) => r._measurement == "sensor_data" and r._field == "value" and {NODE})\n'
        + f"  |> filter(fn: (r) => r.sensor =~ /^({'|'.join(sensors)})$/)\n"
        + "  |> aggregateWindow(every: v.windowPeriod, fn: mean, createEmpty: false)\n"
        + "  |> map(fn: (r) => ({_time: r._time, _value: r._value, sensor: r.sensor}))\n"
        + '  |> group(columns: ["sensor"])'
    )


# ── panels ───────────────────────────────────────────────────────────────────


class Builder:
    def __init__(self, args: argparse.Namespace) -> None:
        self.s = STRINGS[args.lang]
        self.args = args

    @staticmethod
    def target(query: str) -> dict:
        return {"refId": "A", "datasource": DS, "query": query}

    def header(self) -> dict:
        title = html.escape(self.args.title)
        brand = ""
        if self.args.logo:
            logo = base64.b64encode(Path(self.args.logo).read_bytes()).decode()
            alt = html.escape(self.args.brand or self.args.title)
            brand = f'<img src="data:image/png;base64,{logo}" alt="{alt}" style="height:30px;display:block">'
        elif self.args.brand:
            brand = f'<div style="color:{IVORY};font-size:20px;font-weight:800;letter-spacing:-.4px">{html.escape(self.args.brand)}</div>'
        divider = f"border-left:1px solid {BORDER};padding-left:18px" if brand else ""
        content = f"""<div style="display:flex;justify-content:space-between;align-items:center;gap:24px;height:100%;padding:12px 20px;box-sizing:border-box;border:1px solid {BORDER};border-radius:14px;background:radial-gradient(900px 130px at 12% 0%,rgba(226,232,240,.08),transparent 70%),{CARD};font-family:Inter,system-ui,sans-serif">
  <div style="display:flex;align-items:center;gap:18px">
    {brand}
    <div style="{divider}">
      <div style="color:{IVORY};font-size:19px;font-weight:640;letter-spacing:-.3px">{title} · ${{store}}</div>
      <div style="color:{PEWTER};font-size:12px;margin-top:2px">{html.escape(self.s["subtitle"])}</div>
    </div>
  </div>
  <div style="display:flex;align-items:center;gap:8px;font-size:11.5px;color:{SILVER};background:{BG};border:1px solid {BORDER};padding:7px 13px;border-radius:20px;white-space:nowrap;font-family:'JetBrains Mono',monospace">
    <span style="width:7px;height:7px;border-radius:50%;background:{OK};box-shadow:0 0 8px {OK}"></span>${{node}}
  </div>
</div>"""
        return {
            "type": "text",
            "title": "",
            "transparent": True,
            "gridPos": {"x": 0, "y": 0, "w": 24, "h": 3},
            "options": {"mode": "html", "content": content},
        }

    def kpi(self, key: str, query: str, x: int, *, unit: str = "none", decimals: int = 0, color: str = IVORY) -> dict:
        title, description = self.s[key]
        return {
            "type": "stat",
            "title": title,
            "description": description,
            "datasource": DS,
            "gridPos": {"x": x, "y": 3, "w": 4, "h": 4},
            "targets": [self.target(total(query, title))],
            "fieldConfig": {
                "defaults": {"unit": unit, "decimals": decimals, "noValue": "0",
                             "color": {"mode": "fixed", "fixedColor": color}},
                "overrides": [],
            },
            "options": {
                "reduceOptions": {"calcs": ["lastNotNull"], "fields": "", "values": False},
                "colorMode": "value",
                "graphMode": "none",
                "textMode": "value",
                "justifyMode": "center",
            },
        }

    def kpis(self) -> list[dict]:
        currency = f"currency:{self.args.currency}" if self.args.currency else "none"
        return [
            self.kpi("visitors", events("customer_entry"), 0),
            self.kpi("interactions", events("garment_touched|garment_taken"), 4),
            self.kpi("fitting", events("fitting_room_entered"), 8),
            self.kpi("sales", SALES + '  |> filter(fn: (r) => r._field == "count")\n', 12, color=OK),
            self.kpi("revenue", SALES + '  |> filter(fn: (r) => r._field == "amount")\n', 16, unit=currency, decimals=2, color=OK),
            self.kpi("alerts", ALERT_ROWS + '  |> filter(fn: (r) => r._field == "count")\n', 20, color=WARN),
        ]

    def traffic(self, y: int) -> dict:
        flux = (
            events("customer_entry")
            + "  |> group()\n"
            + "  |> aggregateWindow(every: 1h, fn: sum, createEmpty: true)\n"
            + "  |> fill(value: 0)\n"
            + f'  |> rename(columns: {{_value: "{self.s["visitors"][0]}"}})'
        )
        return {
            "type": "timeseries",
            "title": self.s["traffic"],
            "datasource": DS,
            "gridPos": {"x": 0, "y": y, "w": 14, "h": 8},
            "targets": [self.target(flux)],
            "fieldConfig": {
                "defaults": {
                    "color": {"mode": "fixed", "fixedColor": BLUE},
                    "custom": {"drawStyle": "bars", "fillOpacity": 70, "lineWidth": 0, "barAlignment": -1},
                    "decimals": 0,
                    "min": 0,
                },
                "overrides": [],
            },
            "options": {"legend": {"showLegend": False}, "tooltip": {"mode": "single"}},
        }

    def funnel(self, y: int) -> dict:
        names = self.s["stages"]
        types = ("customer_entry", "garment_touched|garment_taken", "garment_taken", "fitting_room_entered", "checkout_visited")
        parts = [
            events(t) + f'  |> group()\n  |> sum()\n  |> map(fn: (r) => ({{stage: "{names[i]}", order: {i}, _value: r._value}}))'
            for i, t in enumerate(types)
        ]
        parts.append(
            SALES
            + '  |> filter(fn: (r) => r._field == "count")\n  |> group()\n  |> sum()\n'
            + f'  |> map(fn: (r) => ({{stage: "{names[-1]}", order: {len(types)}, _value: r._value}}))'
        )
        flux = (
            "\n".join(f"s{i} = {p}" for i, p in enumerate(parts))
            + f"\nunion(tables: [{', '.join(f's{i}' for i in range(len(parts)))}])\n"
            + '  |> group()\n  |> sort(columns: ["order"])\n  |> keep(columns: ["stage", "_value"])'
        )
        title, description = self.s["funnel"]
        return {
            "type": "bargauge",
            "title": title,
            "description": description,
            "datasource": DS,
            "gridPos": {"x": 14, "y": y, "w": 10, "h": 8},
            "targets": [self.target(flux)],
            "fieldConfig": {"defaults": {"color": {"mode": "fixed", "fixedColor": BLUE}, "min": 0, "decimals": 0}, "overrides": []},
            "options": {
                "orientation": "horizontal",
                "displayMode": "basic",
                "showUnfilled": True,
                "reduceOptions": {"calcs": [], "fields": "/^_value$/", "values": True},
                "valueMode": "color",
                "namePlacement": "left",
            },
        }

    def shelves(self, y: int) -> dict:
        interest, sold = self.s["interactions"][0], self.s["units_sold"]
        flux = (
            "interest = "
            + events("garment_touched|garment_taken")
            + "  |> filter(fn: (r) => exists r.zone_id)\n"
            + '  |> group(columns: ["zone_id"])\n  |> sum()\n'
            + f'  |> map(fn: (r) => ({{shelf: r.zone_id, measure: "{interest}", _value: r._value}}))\n'
            + "sales = "
            + SALES
            + '  |> filter(fn: (r) => r._field == "count")\n'
            + '  |> group(columns: ["shelf_id"])\n  |> sum()\n'
            + f'  |> map(fn: (r) => ({{shelf: r.shelf_id, measure: "{sold}", _value: r._value}}))\n'
            + "union(tables: [interest, sales])\n"
            + "  |> group()\n"
            + '  |> pivot(rowKey: ["shelf"], columnKey: ["measure"], valueColumn: "_value")\n'
            + '  |> sort(columns: ["shelf"])'
        )
        title, description = self.s["shelves"]
        return {
            "type": "barchart",
            "title": title,
            "description": description,
            "datasource": DS,
            "gridPos": {"x": 0, "y": y, "w": 14, "h": 9},
            "targets": [self.target(flux)],
            "fieldConfig": {
                "defaults": {"decimals": 0, "min": 0, "custom": {"fillOpacity": 80, "lineWidth": 0}},
                "overrides": [
                    {"matcher": {"id": "byName", "options": interest},
                     "properties": [{"id": "color", "value": {"mode": "fixed", "fixedColor": BLUE}}]},
                    {"matcher": {"id": "byName", "options": sold},
                     "properties": [{"id": "color", "value": {"mode": "fixed", "fixedColor": OK}}]},
                ],
            },
            "options": {
                "xField": "shelf",
                "orientation": "auto",
                "groupWidth": 0.7,
                "barWidth": 0.9,
                "showValue": "auto",
                "legend": {"showLegend": True, "displayMode": "list", "placement": "bottom"},
                "tooltip": {"mode": "multi"},
            },
        }

    def alerts_by_type(self, y: int) -> dict:
        flux = (
            ALERT_ROWS
            + '  |> filter(fn: (r) => r._field == "count")\n'
            + '  |> group(columns: ["code"])\n  |> sum()\n'
            + "  |> group()\n"
            + '  |> sort(columns: ["code"])\n'
            + '  |> map(fn: (r) => ({row: "alerts", code: r.code, _value: r._value}))\n'
            + '  |> pivot(rowKey: ["row"], columnKey: ["code"], valueColumn: "_value")\n'
            + '  |> drop(columns: ["row"])'
        )
        overrides = [
            {"matcher": {"id": "byName", "options": code},
             "properties": [{"id": "displayName", "value": label},
                            {"id": "color", "value": {"mode": "fixed", "fixedColor": ALERT_COLOURS[code]}}]}
            for code, label in self.s["codes"].items()
        ]
        title, description = self.s["by_type"]
        return {
            "type": "bargauge",
            "title": title,
            "description": description,
            "datasource": DS,
            "gridPos": {"x": 14, "y": y, "w": 10, "h": 9},
            "targets": [self.target(flux)],
            "fieldConfig": {"defaults": {"min": 0, "decimals": 0}, "overrides": overrides},
            "options": {
                "orientation": "horizontal",
                "displayMode": "basic",
                "showUnfilled": True,
                "reduceOptions": {"calcs": ["lastNotNull"], "fields": "", "values": False},
                "valueMode": "color",
                "namePlacement": "top",
            },
        }

    def alert_table(self, y: int) -> dict:
        order = ["_time", "code", "severity", "title", "zone_id", "camera_id", "alert_id"]
        flux = (
            ALERT_ROWS
            + '  |> filter(fn: (r) => r._field == "title" or r._field == "alert_id" or r._field == "zone_id")\n'
            + '  |> pivot(rowKey: ["_time"], columnKey: ["_field"], valueColumn: "_value")\n'
            + "  |> group()\n"
            + '  |> sort(columns: ["_time"], desc: true)\n'
            + "  |> limit(n: 100)\n"
            + '  |> map(fn: (r) => ({r with zone_id: if exists r.zone_id then r.zone_id else ""}))\n'
            + f"  |> keep(columns: {json.dumps(order)})"
        )
        codes = {code: {"text": label, "color": ALERT_COLOURS[code], "index": i}
                 for i, (code, label) in enumerate(self.s["codes"].items())}
        severities = {key: {"text": self.s["severities"][key], "color": colour, "index": i}
                      for i, (key, colour) in enumerate((("low", PEWTER), ("medium", WARN), ("high", CRIT)))}
        names = self.s["columns"]

        def column(name: str, *props: dict) -> dict:
            return {"matcher": {"id": "byName", "options": name}, "properties": [{"id": "displayName", "value": names[name]}, *props]}

        title, description = self.s["recent"]
        return {
            "type": "table",
            "title": title,
            "description": description,
            "datasource": DS,
            "gridPos": {"x": 0, "y": y, "w": 24, "h": 9},
            "targets": [self.target(flux)],
            "fieldConfig": {
                "defaults": {"custom": {"align": "left", "cellOptions": {"type": "auto"}}},
                "overrides": [
                    column("_time", {"id": "unit", "value": "dateTimeAsLocalNoDateIfToday"}, {"id": "custom.width", "value": 150}),
                    column("code", {"id": "mappings", "value": [{"type": "value", "options": codes}]},
                           {"id": "custom.cellOptions", "value": {"type": "color-text"}}, {"id": "custom.width", "value": 290}),
                    column("severity", {"id": "mappings", "value": [{"type": "value", "options": severities}]},
                           {"id": "custom.cellOptions", "value": {"type": "color-text"}}, {"id": "custom.width", "value": 110}),
                    column("title"),
                    column("zone_id", {"id": "custom.width", "value": 140}),
                    column("camera_id", {"id": "custom.width", "value": 140}),
                    column("alert_id", {"id": "custom.width", "value": 200}),
                ],
            },
            "options": {"showHeader": True, "cellHeight": "sm", "footer": {"show": False}},
            "transformations": [{"id": "organize", "options": {"indexByName": {name: i for i, name in enumerate(order)}}}],
        }

    def node_status(self, y: int) -> dict:
        flux = (
            "from(bucket: v.defaultBucket)\n  |> range(start: -1d)\n"
            + f'  |> filter(fn: (r) => r._measurement == "boutique_node" and r._field == "up" and {NODE})\n'
            + "  |> last()\n"
            + "  |> map(fn: (r) => ({_time: r._time, _value: if uint(v: now()) - uint(v: r._time) > uint(v: 90000000000) then 2 else 1 - r._value}))"
        )
        title, description = self.s["node"]
        online, degraded, lost = self.s["node_states"]
        return {
            "type": "stat",
            "title": title,
            "description": description,
            "datasource": DS,
            "gridPos": {"x": 0, "y": y, "w": 6, "h": 8},
            "targets": [self.target(flux)],
            "fieldConfig": {
                "defaults": {
                    "noValue": lost,
                    "mappings": [{"type": "value", "options": {
                        "0": {"text": online, "color": OK, "index": 0},
                        "1": {"text": degraded, "color": WARN, "index": 1},
                        "2": {"text": lost, "color": CRIT, "index": 2},
                    }}],
                    "color": {"mode": "thresholds"},
                    "thresholds": {"mode": "absolute", "steps": [{"color": IDLE, "value": None}]},
                },
                "overrides": [],
            },
            "options": {
                "reduceOptions": {"calcs": ["lastNotNull"], "fields": "", "values": False},
                "colorMode": "background",
                "graphMode": "none",
                "textMode": "value",
                "justifyMode": "center",
            },
        }

    @staticmethod
    def health_panel(title: str, flux: str, x: int, y: int, unit: str, names: dict[str, tuple[str, str]],
                     description: str = "") -> dict:
        return {
            "type": "timeseries",
            "title": title,
            "description": description,
            "datasource": DS,
            "gridPos": {"x": x, "y": y, "w": 6, "h": 8},
            "targets": [Builder.target(flux)],
            "fieldConfig": {
                "defaults": {"unit": unit, "custom": {"lineWidth": 2, "fillOpacity": 8, "showPoints": "never"}},
                "overrides": [
                    {"matcher": {"id": "byRegexp", "options": f".*{sensor}.*"},
                     "properties": [{"id": "displayName", "value": label},
                                    {"id": "color", "value": {"mode": "fixed", "fixedColor": colour}}]}
                    for sensor, (label, colour) in names.items()
                ],
            },
            "options": {"legend": {"showLegend": True, "displayMode": "list", "placement": "bottom"}, "tooltip": {"mode": "multi"}},
        }

    @staticmethod
    def variable(name: str, label: str, flux: str) -> dict:
        return {
            "name": name,
            "label": label,
            "type": "query",
            "datasource": DS,
            "query": flux,
            "definition": flux,
            "refresh": 2,
            "sort": 1,
            "includeAll": False,
            "multi": False,
            "current": {},
            "options": [],
        }

    def dashboard(self) -> dict:
        s = self.s
        y = 7
        panels = [self.header(), *self.kpis(), self.traffic(y), self.funnel(y)]
        y += 8
        panels += [self.shelves(y), self.alerts_by_type(y)]
        y += 9
        panels.append(self.alert_table(y))
        y += 9
        panels += [
            self.node_status(y),
            self.health_panel(s["temperature"], health("temperature_cpu_c", "temperature_gpu_c"), 6, y, "celsius",
                              {"temperature_cpu_c": ("CPU", BLUE), "temperature_gpu_c": ("GPU", ORANGE)}),
            self.health_panel(s["usage"][0], health("cpu_utilization_pct", "gpu_utilization_pct"), 12, y, "percent",
                              {"cpu_utilization_pct": ("CPU", BLUE), "gpu_utilization_pct": ("GPU", ORANGE)}, s["usage"][1]),
            self.health_panel(s["processing"][0], health("processing_fps", "active_tracks"), 18, y, "none",
                              {"processing_fps": (s["fps"], OK), "active_tracks": (s["tracks"], PURPLE)}, s["processing"][1]),
        ]
        for i, panel in enumerate(panels, 1):
            panel["id"] = i
        stores = (
            'import "influxdata/influxdb/schema"\n'
            'schema.tagValues(bucket: v.defaultBucket, tag: "store_id", '
            'predicate: (r) => r._measurement == "boutique_event" or r._measurement == "boutique_node", start: -30d)'
        )
        nodes = (
            'import "influxdata/influxdb/schema"\n'
            'schema.tagValues(bucket: v.defaultBucket, tag: "device", '
            'predicate: (r) => r._measurement == "boutique_node" and r.store_id == "${store}", start: -30d)'
        )
        return {
            "uid": self.args.uid,
            "title": self.args.title,
            "description": s["description"],
            "tags": ["retail", "store", "vision"],
            "editable": False,
            "graphTooltip": 1,
            "refresh": "30s",
            "schemaVersion": 41,
            "time": {"from": "now/d", "to": "now"},
            "timepicker": {},
            "timezone": "browser",
            "templating": {"list": [self.variable("store", s["store"], stores), self.variable("node", s["device"], nodes)]},
            "annotations": {"list": []},
            "links": [],
            "panels": panels,
        }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--lang", choices=sorted(STRINGS), default="en")
    parser.add_argument("--uid", default="retail-vision", help="Grafana dashboard uid (.p4n4.json grafana_path is /d/<uid>/…)")
    parser.add_argument("--title", default="Store")
    parser.add_argument("--currency", default="$", help="revenue prefix, e.g. $, €, B/.; empty for none")
    parser.add_argument("--logo", help="PNG shown in the header (e.g. ../theme/logo.png)")
    parser.add_argument("--brand", help="brand name: the logo's alt text, or shown in the header without a logo")
    parser.add_argument("--out", help="output file (default config/grafana/provisioning/dashboards/json/store.json)")
    args = parser.parse_args()
    out = Path(args.out) if args.out else ROOT / "config/grafana/provisioning/dashboards/json/store.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(Builder(args).dashboard(), indent=2, ensure_ascii=False) + "\n")
    print(f"wrote {out}")


if __name__ == "__main__":
    main()
