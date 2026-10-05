#!/usr/bin/env python3
"""Generates the traffic dashboard.

    python3 scripts/build_dashboard.py [--lang en|es] [--uid UID] [--title TITLE]
                                      [--speed-limit KMH] [--logo PNG] [--brand TEXT]
                                      [--theme BRAND_JSON]

writes config/grafana/provisioning/dashboards/json/traffic.json (or <uid>.json
with --out). Edit this, not the JSON; Grafana reloads provisioned dashboards
on its own (up to 30 s). A project made from this template re-runs it with its
own options, e.g. a Spanish dashboard with a logo:

    python3 scripts/build_dashboard.py --lang es --uid acme --title Tráfico \\
        --speed-limit 40 --logo ../theme/logo.png --brand 'ACME'

The dashboard follows Grafana's light or dark theme, which p4n4-dashboard
sets to its own (?theme=light|dark). Panels use Grafana's named colours,
which Grafana shades for each theme. The header takes its accent colours and
brand name from the p4n4-dashboard theme (../theme/brand.json by default).

The panels read what the ingest service copies to InfluxDB (see
ingest/sinks.py): alpr_read per site, with no plates, and the edge device's
heartbeats as sensor_data (device = its hostname) plus alpr_node. Plates stay
in the ingest service, which the agent asks.
"""

from __future__ import annotations

import argparse
import base64
import html
import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
THEME = ROOT.parent / "theme/brand.json"
DS = {"type": "influxdb", "uid": "influxdb-telemetry"}

# Grafana's named colours: one shade on the dark theme, a darker one on the
# light theme, so a panel reads on both. "text" is the theme's text colour.
# Inbound is blue, outbound purple, plates read green, speed orange.
TEXT, BLUE, GREEN, ORANGE, PURPLE, RED = "text", "blue", "green", "orange", "purple", "red"

# Header accent when the theme has none: the sample theme's (light, dark)
ACCENT = ("#B45309", "#FBBF24")

# Everything a person reads on the dashboard, per --lang
STRINGS = {
    "en": {
        "subtitle": "Vehicles and plates read by the site's cameras",
        "privacy": "Counts only: no plates",
        "site": "Site",
        "device": "Device",
        "vehicles": ("Vehicles", "Every vehicle the cameras saw, plate read or not."),
        "inbound": ("Inbound", "Vehicles going in."),
        "outbound": ("Outbound", "Vehicles going out."),
        "plates": ("Plates read", "Vehicles whose plate the device read."),
        "read_rate": ("Read rate", "Plates read / vehicles, over the period. Under 80% on a camera is worth checking."),
        "speed": ("Average speed", "From the vehicles whose speed the device measured."),
        "per_hour": "Vehicles per hour",
        "directions": {"inbound": "Inbound", "outbound": "Outbound", "unknown": "Unknown"},
        "types": ("Vehicle types", "Vehicles per type, over the selected period."),
        "type_names": {"car": "Car", "van": "Van", "truck": "Truck", "bus": "Bus", "motorcycle": "Motorcycle", "unknown": "Unknown"},
        "camera_rate": ("Read rate per camera", "Plates read / vehicles, per hour. A drop at night points at glare or IR."),
        "speeds": ("Speed", "Average speed per hour and direction. The line is the limit."),
        "node": "Edge device",
        "status": ("Status", "Online: a heartbeat in the last 90 s with status healthy. No heartbeat for 90 s: no signal."),
        "node_states": ("online", "degraded", "no signal"),
        "heartbeat": ("Last heartbeat", "When the edge device last reported, in the last day."),
        "temperature": "Temperature",
        "usage": ("Usage", "The edge device's CPU and GPU. On a Jetson Orin Nano they share its 8 GB of memory."),
        "processing": ("Processing", "Frames analysed per second, and vehicles in the last minute."),
        "fps": "Analysis FPS",
        "recent": "Vehicles last minute",
        "description": "Road traffic: vehicles per direction and type, plates read per camera, speeds and the edge device's health. No plates.",
    },
    "es": {
        "subtitle": "Vehículos y placas leídos por las cámaras del sitio",
        "privacy": "Solo conteos: sin placas",
        "site": "Sitio",
        "device": "Equipo",
        "vehicles": ("Vehículos", "Todos los vehículos que vieron las cámaras, con o sin placa leída."),
        "inbound": ("Entrada", "Vehículos que entran."),
        "outbound": ("Salida", "Vehículos que salen."),
        "plates": ("Placas leídas", "Vehículos cuya placa leyó el equipo."),
        "read_rate": ("Tasa de lectura", "Placas leídas / vehículos en el periodo. Menos del 80% en una cámara merece revisión."),
        "speed": ("Velocidad media", "De los vehículos cuya velocidad midió el equipo."),
        "per_hour": "Vehículos por hora",
        "directions": {"inbound": "Entrada", "outbound": "Salida", "unknown": "Desconocido"},
        "types": ("Tipos de vehículo", "Vehículos por tipo en el periodo elegido."),
        "type_names": {"car": "Auto", "van": "Furgoneta", "truck": "Camión", "bus": "Bus", "motorcycle": "Moto", "unknown": "Desconocido"},
        "camera_rate": ("Lectura por cámara", "Placas leídas / vehículos, por hora. Una caída de noche apunta a reflejos o IR."),
        "speeds": ("Velocidad", "Velocidad media por hora y sentido. La línea es el límite."),
        "node": "Equipo del sitio",
        "status": ("Estado", "En línea: latido en los últimos 90 s y estado healthy. Sin latido por 90 s: sin señal."),
        "node_states": ("en línea", "degradado", "sin señal"),
        "heartbeat": ("Último latido", "Cuándo reportó el equipo por última vez, en el último día."),
        "temperature": "Temperatura",
        "usage": ("Uso", "CPU y GPU del equipo. En un Jetson Orin Nano comparten los 8 GB de memoria."),
        "processing": ("Procesamiento", "Fotogramas analizados por segundo y vehículos en el último minuto."),
        "fps": "FPS de análisis",
        "recent": "Vehículos último minuto",
        "description": "Tráfico vial: vehículos por sentido y tipo, lectura de placas por cámara, velocidades y salud del equipo. Sin placas.",
    },
}

# ── Flux ─────────────────────────────────────────────────────────────────────

RANGE = "from(bucket: v.defaultBucket)\n  |> range(start: v.timeRangeStart, stop: v.timeRangeStop)\n"
SITE = 'r.site_id == "${site}"'
NODE = 'r.device == "${node}"'


def reads(where: str = "", field: str = "count") -> str:
    """Rows of alpr_read for the site, optionally narrowed."""
    flux = RANGE + f'  |> filter(fn: (r) => r._measurement == "alpr_read" and r._field == "{field}" and {SITE})\n'
    return flux + (f"  |> filter(fn: (r) => {where})\n" if where else "")


def hourly(flux: str, name: str) -> str:
    """The rows' sum per hour, 0 for an hour without any, as one series."""
    return (
        flux
        + "  |> group()\n"
        + "  |> aggregateWindow(every: 1h, fn: sum, createEmpty: true)\n"
        + "  |> map(fn: (r) => ({_time: r._time, _value: if exists r._value then float(v: r._value) else 0.0}))\n"
        + f'  |> rename(columns: {{_value: "{name}"}})'
    )


# Counts, with a "yes" column for the plates read among them
WITH_READ = '  |> map(fn: (r) => ({r with yes: if r.plate_read == "yes" then r._value else 0}))\n'
RATIO = '  |> reduce(identity: {n: 0, yes: 0}, fn: (r, accumulator) => ({n: accumulator.n + r._value, yes: accumulator.yes + r.yes}))\n'


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


# The header is HTML in a text panel (GF_PANELS_DISABLE_SANITIZE_HTML). It
# inherits Grafana's text colour; only the accent changes with the theme, which
# Grafana names on <body> (theme-dark, theme-light).
HEADER_CSS = """<style>
.rv-head{--rv-accent:@DARK@;--rv-line:rgba(128,128,128,.25);--rv-line:color-mix(in srgb,currentColor 14%,transparent);
  position:relative;overflow:hidden;display:flex;justify-content:space-between;align-items:center;gap:24px;
  height:100%;padding:0 20px 0 26px;box-sizing:border-box;border:1px solid var(--rv-line);border-radius:6px;
  background:linear-gradient(100deg,color-mix(in srgb,var(--rv-accent) 14%,transparent),transparent 60%)}
.theme-light .rv-head{--rv-accent:@LIGHT@}
.rv-head::before{content:"";position:absolute;inset:0 auto 0 0;width:4px;background:var(--rv-accent)}
.rv-id{display:flex;align-items:center;gap:20px;min-width:0}
.rv-brand{font-size:19px;font-weight:800;letter-spacing:-.02em;white-space:nowrap}
.rv-brand img{height:32px;display:block}
.rv-sep{align-self:stretch;width:1px;margin:18px 0;background:var(--rv-line)}
.rv-eyebrow{font-size:11px;font-weight:600;letter-spacing:.09em;text-transform:uppercase;color:var(--rv-accent)}
.rv-title{font-size:21px;font-weight:600;letter-spacing:-.01em;line-height:1.3}
.rv-sub{font-size:12.5px;opacity:.7;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.rv-chips{display:flex;gap:8px;flex-shrink:0}
.rv-chip{display:inline-flex;align-items:center;gap:7px;padding:5px 12px;border:1px solid var(--rv-line);border-radius:999px;font-size:12px;white-space:nowrap}
.rv-chip span{opacity:.7}
.rv-chip b{font-weight:500;font-family:'JetBrains Mono',ui-monospace,monospace;font-size:11.5px}
.rv-chip svg{opacity:.7}
@media (max-width:1000px){.rv-sub,.rv-privacy{display:none}}
</style>"""

SHIELD = ('<svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" '
          'stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">'
          '<path d="M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z"/></svg>')


def accents(theme: dict) -> tuple[str, str]:
    """The theme's (light, dark) accent colours; either falls back to the other."""
    colors = theme.get("colors", {})
    light, dark = (colors.get(mode, {}).get("accent") for mode in ("light", "dark"))
    light, dark = (c if isinstance(c, str) and re.fullmatch(r"#[0-9A-Fa-f]{3,8}", c) else None for c in (light, dark))
    return (light or dark or ACCENT[0], dark or light or ACCENT[1])


class Builder:
    def __init__(self, args: argparse.Namespace) -> None:
        self.s = STRINGS[args.lang]
        self.args = args
        self.theme = json.loads(Path(args.theme).read_text()) if args.theme else {}
        self.brand = args.brand if args.brand is not None else self.theme.get("appName", "")

    @staticmethod
    def target(query: str) -> dict:
        return {"refId": "A", "datasource": DS, "query": query}

    def header(self) -> dict:
        light, dark = accents(self.theme)
        css = HEADER_CSS.replace("@LIGHT@", light).replace("@DARK@", dark)
        brand = ""
        if self.args.logo:
            logo = base64.b64encode(Path(self.args.logo).read_bytes()).decode()
            alt = html.escape(self.brand or self.args.title)
            brand = f'<div class="rv-brand"><img src="data:image/png;base64,{logo}" alt="{alt}"></div>\n    <span class="rv-sep"></span>'
        elif self.brand:
            brand = f'<div class="rv-brand">{html.escape(self.brand)}</div>\n    <span class="rv-sep"></span>'
        content = f"""{css}
<div class="rv-head">
  <div class="rv-id">
    {brand}
    <div style="min-width:0">
      <div class="rv-eyebrow">{html.escape(self.args.title)}</div>
      <div class="rv-title">${{site}}</div>
      <div class="rv-sub">{html.escape(self.s["subtitle"])}</div>
    </div>
  </div>
  <div class="rv-chips">
    <div class="rv-chip rv-privacy">{SHIELD}<span>{html.escape(self.s["privacy"])}</span></div>
    <div class="rv-chip"><span>{html.escape(self.s["device"])}</span><b>${{node}}</b></div>
  </div>
</div>"""
        return {
            "type": "text",
            "title": "",
            "transparent": True,
            "gridPos": {"x": 0, "y": 0, "w": 24, "h": 3},
            "options": {"mode": "html", "content": content},
        }

    def kpi(self, key: str, query: str, x: int, color: str, *, unit: str = "none", decimals: int = 0,
            sparkline: bool = True) -> dict:
        """The period's total, over a sparkline of it per hour; or one value the query computes."""
        title, description = self.s[key]
        return {
            "type": "stat",
            "title": title,
            "description": description,
            "datasource": DS,
            "gridPos": {"x": x, "y": 3, "w": 4, "h": 4},
            "targets": [self.target(hourly(query, title) if sparkline else query)],
            "fieldConfig": {
                "defaults": {"unit": unit, "decimals": decimals, "noValue": "—" if not sparkline else "0", "min": 0,
                             "color": {"mode": "fixed", "fixedColor": color}},
                "overrides": [],
            },
            "options": {
                "reduceOptions": {"calcs": ["sum" if sparkline else "lastNotNull"], "fields": "", "values": False},
                "colorMode": "none",
                "graphMode": "area" if sparkline else "none",
                "textMode": "value",
                "justifyMode": "auto",
                "wideLayout": True,
                "showPercentChange": False,
            },
        }

    def kpis(self) -> list[dict]:
        rate = (
            reads() + WITH_READ + "  |> group()\n" + RATIO
            + "  |> map(fn: (r) => ({_value: if r.n > 0 then float(v: r.yes) / float(v: r.n) else 0.0}))"
        )
        speed = reads(field="speed_kmh") + "  |> group()\n  |> mean()\n  |> keep(columns: [\"_value\"])"
        return [
            self.kpi("vehicles", reads(), 0, TEXT),
            self.kpi("inbound", reads('r.direction == "inbound"'), 4, BLUE),
            self.kpi("outbound", reads('r.direction == "outbound"'), 8, PURPLE),
            self.kpi("plates", reads('r.plate_read == "yes"'), 12, GREEN),
            self.kpi("read_rate", rate, 16, GREEN, unit="percentunit", decimals=1, sparkline=False),
            self.kpi("speed", speed, 20, ORANGE, unit="velocitykmh", decimals=1, sparkline=False),
        ]

    def per_hour(self, y: int) -> dict:
        names = self.s["directions"]
        flux = (
            reads('r.direction == "inbound" or r.direction == "outbound"')
            + '  |> group(columns: ["direction"])\n'
            + "  |> aggregateWindow(every: 1h, fn: sum, createEmpty: true)\n"
            + "  |> map(fn: (r) => ({_time: r._time, direction: r.direction, _value: if exists r._value then float(v: r._value) else 0.0}))\n"
            + '  |> group(columns: ["direction"])'
        )
        return {
            "type": "timeseries",
            "title": self.s["per_hour"],
            "datasource": DS,
            "gridPos": {"x": 0, "y": y, "w": 14, "h": 8},
            "targets": [self.target(flux)],
            "fieldConfig": {
                "defaults": {
                    "custom": {"drawStyle": "bars", "fillOpacity": 85, "lineWidth": 0, "barAlignment": -1,
                               "barWidthFactor": 0.8, "axisBorderShow": False, "stacking": {"mode": "normal", "group": "A"}},
                    "decimals": 0,
                    "min": 0,
                },
                "overrides": [
                    {"matcher": {"id": "byRegexp", "options": f".*{d}.*"},
                     "properties": [{"id": "displayName", "value": names[d]},
                                    {"id": "color", "value": {"mode": "fixed", "fixedColor": colour}}]}
                    for d, colour in (("inbound", BLUE), ("outbound", PURPLE))
                ],
            },
            "options": {"legend": {"showLegend": True, "displayMode": "list", "placement": "bottom"}, "tooltip": {"mode": "multi"}},
        }

    def types(self, y: int) -> dict:
        names = self.s["type_names"]
        label = "".join(f'if r.vehicle_type == "{k}" then "{v}" else ' for k, v in names.items()) + "r.vehicle_type"
        flux = (
            reads()
            + '  |> group(columns: ["vehicle_type"])\n  |> sum()\n  |> group()\n'
            + '  |> sort(columns: ["_value"], desc: true)\n'
            + f"  |> map(fn: (r) => ({{type: {label}, _value: r._value}}))"
        )
        title, description = self.s["types"]
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
                "valueMode": "text",
                "namePlacement": "left",
                "text": {"valueSize": 18},
            },
        }

    def camera_rate(self, y: int) -> dict:
        flux = (
            reads() + WITH_READ
            + '  |> group(columns: ["camera_id"])\n'
            + "  |> window(every: 1h)\n"
            + RATIO
            + '  |> duplicate(column: "_stop", as: "_time")\n'
            + "  |> window(every: inf)\n"
            + "  |> map(fn: (r) => ({_time: r._time, camera_id: r.camera_id, _value: if r.n > 0 then float(v: r.yes) / float(v: r.n) else 0.0}))\n"
            + '  |> group(columns: ["camera_id"])'
        )
        title, description = self.s["camera_rate"]
        return {
            "type": "timeseries",
            "title": title,
            "description": description,
            "datasource": DS,
            "gridPos": {"x": 0, "y": y, "w": 14, "h": 8},
            "targets": [self.target(flux)],
            "fieldConfig": {
                "defaults": {
                    "unit": "percentunit", "min": 0, "max": 1,
                    "displayName": "${__field.labels.camera_id}",
                    "color": {"mode": "palette-classic"},
                    "custom": {"lineWidth": 2, "fillOpacity": 0, "lineInterpolation": "smooth", "showPoints": "never",
                               "axisBorderShow": False, "thresholdsStyle": {"mode": "dashed"}},
                    "thresholds": {"mode": "absolute", "steps": [{"color": "transparent", "value": None}, {"color": ORANGE, "value": 0.8}]},
                },
                "overrides": [],
            },
            "options": {"legend": {"showLegend": True, "displayMode": "list", "placement": "bottom"}, "tooltip": {"mode": "multi"}},
        }

    def speeds(self, y: int) -> dict:
        names = self.s["directions"]
        flux = (
            reads('r.direction == "inbound" or r.direction == "outbound"', field="speed_kmh")
            + '  |> group(columns: ["direction"])\n'
            + "  |> aggregateWindow(every: 1h, fn: mean, createEmpty: false)\n"
            + "  |> map(fn: (r) => ({_time: r._time, direction: r.direction, _value: r._value}))\n"
            + '  |> group(columns: ["direction"])'
        )
        limit = self.args.speed_limit
        title, description = self.s["speeds"]
        return {
            "type": "timeseries",
            "title": title,
            "description": description if limit else description.rsplit(". ", 1)[0] + ".",
            "datasource": DS,
            "gridPos": {"x": 14, "y": y, "w": 10, "h": 8},
            "targets": [self.target(flux)],
            "fieldConfig": {
                "defaults": {
                    "unit": "velocitykmh", "min": 0, "decimals": 0,
                    "custom": {"lineWidth": 2, "fillOpacity": 10, "lineInterpolation": "smooth", "showPoints": "never",
                               "axisBorderShow": False, "thresholdsStyle": {"mode": "dashed" if limit else "off"}},
                    "thresholds": {"mode": "absolute", "steps": [{"color": "transparent", "value": None},
                                                                 *([{"color": RED, "value": limit}] if limit else [])]},
                },
                "overrides": [
                    {"matcher": {"id": "byRegexp", "options": f".*{d}.*"},
                     "properties": [{"id": "displayName", "value": names[d]},
                                    {"id": "color", "value": {"mode": "fixed", "fixedColor": colour}}]}
                    for d, colour in (("inbound", BLUE), ("outbound", PURPLE))
                ],
            },
            "options": {"legend": {"showLegend": True, "displayMode": "list", "placement": "bottom"}, "tooltip": {"mode": "multi"}},
        }

    @staticmethod
    def row(title: str, y: int) -> dict:
        return {"type": "row", "title": title, "collapsed": False, "gridPos": {"x": 0, "y": y, "w": 24, "h": 1}, "panels": []}

    def node_status(self, y: int) -> dict:
        flux = (
            "from(bucket: v.defaultBucket)\n  |> range(start: -1d)\n"
            + f'  |> filter(fn: (r) => r._measurement == "alpr_node" and r._field == "up" and {NODE})\n'
            + "  |> last()\n"
            + "  |> map(fn: (r) => ({_time: r._time, _value: if uint(v: now()) - uint(v: r._time) > uint(v: 90000000000) then 2 else 1 - r._value}))"
        )
        title, description = self.s["status"]
        online, degraded, lost = self.s["node_states"]
        return {
            "type": "stat",
            "title": title,
            "description": description,
            "datasource": DS,
            "gridPos": {"x": 0, "y": y, "w": 6, "h": 4},
            "targets": [self.target(flux)],
            "fieldConfig": {
                "defaults": {
                    "noValue": lost,
                    "mappings": [{"type": "value", "options": {
                        "0": {"text": online, "color": GREEN, "index": 0},
                        "1": {"text": degraded, "color": ORANGE, "index": 1},
                        "2": {"text": lost, "color": RED, "index": 2},
                    }}],
                    "color": {"mode": "thresholds"},
                    "thresholds": {"mode": "absolute", "steps": [{"color": RED, "value": None}]},
                },
                "overrides": [],
            },
            "options": {
                "reduceOptions": {"calcs": ["lastNotNull"], "fields": "", "values": False},
                "colorMode": "background",
                "graphMode": "none",
                "textMode": "value",
                "justifyMode": "center",
                "text": {"valueSize": 26},
            },
        }

    def heartbeat(self, y: int) -> dict:
        flux = (
            "from(bucket: v.defaultBucket)\n  |> range(start: -1d)\n"
            + f'  |> filter(fn: (r) => r._measurement == "alpr_node" and r._field == "up" and {NODE})\n'
            + "  |> last()\n"
            + "  |> map(fn: (r) => ({_time: r._time, _value: int(v: r._time) / 1000000}))"
        )
        title, description = self.s["heartbeat"]
        return {
            "type": "stat",
            "title": title,
            "description": description,
            "datasource": DS,
            "gridPos": {"x": 0, "y": y, "w": 6, "h": 4},
            "targets": [self.target(flux)],
            "fieldConfig": {
                "defaults": {"unit": "dateTimeFromNow", "noValue": "—", "color": {"mode": "fixed", "fixedColor": TEXT}},
                "overrides": [],
            },
            "options": {
                "reduceOptions": {"calcs": ["lastNotNull"], "fields": "", "values": False},
                "colorMode": "none",
                "graphMode": "none",
                "textMode": "value",
                "justifyMode": "center",
                "text": {"valueSize": 22},
            },
        }

    @staticmethod
    def health_panel(title: str, flux: str, x: int, y: int, unit: str, names: dict[str, tuple[str, str]],
                     description: str = "", **limits: float) -> dict:
        return {
            "type": "timeseries",
            "title": title,
            "description": description,
            "datasource": DS,
            "gridPos": {"x": x, "y": y, "w": 6, "h": 8},
            "targets": [Builder.target(flux)],
            "fieldConfig": {
                "defaults": {
                    "unit": unit,
                    **limits,
                    "custom": {"lineWidth": 2, "fillOpacity": 18, "gradientMode": "opacity", "lineInterpolation": "smooth",
                               "showPoints": "never", "axisBorderShow": False},
                },
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
        panels = [self.header(), *self.kpis(), self.per_hour(y), self.types(y)]
        y += 8
        panels += [self.camera_rate(y), self.speeds(y)]
        y += 8
        panels += [self.row(f'{s["node"]} · ${{node}}', y), self.node_status(y + 1), self.heartbeat(y + 5)]
        y += 1
        panels += [
            self.health_panel(s["temperature"], health("temperature_cpu_c", "temperature_gpu_c"), 6, y, "celsius",
                              {"temperature_cpu_c": ("CPU", BLUE), "temperature_gpu_c": ("GPU", ORANGE)}),
            self.health_panel(s["usage"][0], health("cpu_utilization_pct", "gpu_utilization_pct"), 12, y, "percent",
                              {"cpu_utilization_pct": ("CPU", BLUE), "gpu_utilization_pct": ("GPU", ORANGE)}, s["usage"][1],
                              min=0, max=100),
            self.health_panel(s["processing"][0], health("processing_fps", "vehicles_last_minute"), 18, y, "none",
                              {"processing_fps": (s["fps"], GREEN), "vehicles_last_minute": (s["recent"], PURPLE)},
                              s["processing"][1], min=0),
        ]
        for i, panel in enumerate(panels, 1):
            panel["id"] = i
        sites = (
            'import "influxdata/influxdb/schema"\n'
            'schema.tagValues(bucket: v.defaultBucket, tag: "site_id", '
            'predicate: (r) => r._measurement == "alpr_read" or r._measurement == "alpr_node", start: -30d)'
        )
        nodes = (
            'import "influxdata/influxdb/schema"\n'
            'schema.tagValues(bucket: v.defaultBucket, tag: "device", '
            'predicate: (r) => r._measurement == "alpr_node" and r.site_id == "${site}", start: -30d)'
        )
        return {
            "uid": self.args.uid,
            "title": self.args.title,
            "description": s["description"],
            "tags": ["traffic", "alpr", "video"],
            "editable": False,
            "graphTooltip": 1,
            "refresh": "30s",
            "schemaVersion": 41,
            "time": {"from": "now/d", "to": "now"},
            "timepicker": {},
            "timezone": "browser",
            "templating": {"list": [self.variable("site", s["site"], sites), self.variable("node", s["device"], nodes)]},
            "annotations": {"list": []},
            "links": [],
            "panels": panels,
        }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--lang", choices=sorted(STRINGS), default="en")
    parser.add_argument("--uid", default="p4n4-traffic", help="Grafana dashboard uid (.p4n4.json grafana_path is /d/<uid>/…)")
    parser.add_argument("--title", default="Traffic")
    parser.add_argument("--speed-limit", type=float, default=50, help="km/h, drawn on the speed panel (SITE_SPEED_LIMIT); 0 for none")
    parser.add_argument("--logo", help="PNG shown in the header (e.g. ../theme/logo.png)")
    parser.add_argument("--brand", help="brand name: the logo's alt text, or shown in the header without a logo "
                        "(default the theme's appName; '' for none)")
    parser.add_argument("--theme", default=str(THEME) if THEME.is_file() else None,
                        help="p4n4-dashboard theme (brand.json) for the header's accent colours and brand name "
                        "(default ../theme/brand.json)")
    parser.add_argument("--out", help="output file (default config/grafana/provisioning/dashboards/json/traffic.json)")
    args = parser.parse_args()
    out = Path(args.out) if args.out else ROOT / "config/grafana/provisioning/dashboards/json/traffic.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(Builder(args).dashboard(), indent=2, ensure_ascii=False) + "\n")
    print(f"wrote {out}")


if __name__ == "__main__":
    main()
