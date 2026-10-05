# mqtt-influx-grafana-ollama-go2rtc

> Road traffic counting with automatic licence plate recognition (ALPR), with live video in p4n4-dashboard. An edge device at the roadside reads the plates of the vehicles two cameras see and posts them. An ingest service keeps the plates for a retention period and answers questions with fixed numbers. Grafana shows the traffic without a single plate, the Agent tab explains it and looks plates up, and the **Video tab** plays the road camera and the device's plate view. Works with no hardware: a demo road and synthetic video stand in for them.

```
  edge/ (at the site)
  road camera ────────┐                     ┌──► p4n4-dashboard Video tab (MJPEG)
  device's plate view ┴──► video :1984 (go2rtc)
  device SQLite (./data) ──► uplink ══HTTPS, X-Site-Token══╗   (profile "device")
                                                           ▼
  iot/                                    ┌──► iot/data/ingest/ingest.db (SQLite: plates, RETENTION_DAYS)
  simulator (profile "demo") ──► ingest :8090 ──┼──► influxdb ──► grafana :3000   (counts, no plates)
                                       ▲        └──► mqtt  traffic/<site>/…       (no plates)
                                       │ agent API (fixed numbers, plate lookups)
  ai/  p4n4-dashboard Agent tab ──► agent :11434 ──┬──► ollama (gemma4:e2b)
                                                   └──► influxdb (the device's health)
```

The device side follows the **ALPR device ↔ web contract 0.1**: schemas `p4n4.alpr.read/0.1` (one vehicle passing a camera, with its plate if the device read it) and `p4n4.alpr.heartbeat/0.1`. Any device that posts those documents, or writes the same SQLite table for the uplink to forward, works with it. `iot/ingest/contract.py` has the rules.

Template 0.3.0 replaced the retail store analytics of 0.2.0 (`p4n4.retail.*`) with ALPR. Projects made from 0.2.0 keep their copy; there's no upgrade path between the two.

The [road traffic use case](../../docs/use-cases/road-traffic.md) walks through a deployment: the demo, the device at the roadside, p4n4-api and a branded dashboard.

## Quick start

```bash
cp -r mqtt-influx-grafana-ollama-go2rtc my-road && cd my-road
for layer in iot ai edge; do cp $layer/.env.example $layer/.env; done
p4n4 up        # iot, then ai, then edge
```

Without the CLI, start the layers in that order: `(cd iot && docker compose up -d) && (cd ai && docker compose up -d) && (cd edge && docker compose up -d)`. ai and edge join the `p4n4-net` network that iot creates.

| | |
|---|---|
| Grafana | <http://localhost:3000>, `admin` / `adminpassword`. Home: **Traffic** |
| Video | <http://localhost:1984>: go2rtc's page; streams at `/api/stream.mjpeg?src=road` and `?src=plates` |
| Ingest | <http://localhost:8090/health>; the API needs the tokens in `iot/.env` |
| Agent | <http://localhost:11434>, Ollama's API (p4n4-dashboard's Agent tab). The model is pulled on first start, about 4.6 GB |

The demo road (`COMPOSE_PROFILES=demo` in `iot/.env`) posts a week of history, then the rest of today as it happens. It's seeded, so every run tells the same story:
- Weekdays peak inbound from 07:00 to 09:00 and outbound from 17:00 to 19:00. Weekends are quieter, with a midday peak.
- The most frequent plates are three buses (`MTB-1101`, `MTB-1102`, `MTB-1103`), then a delivery van (`KDL-4821`) on weekdays.
- `cam-out-01` faces oncoming headlights and reads far fewer plates at night: check C2 fires for it.
- Outbound traffic is faster late at night.
- About 2% of plates are misread by one character, as a real ALPR does now and then.

Everything it posts is marked `"synthetic": true`, the plates are made up, and the agent says the data is from the demo.

## Layout

| | |
|---|---|
| `iot/` | `ingest` (`iot/ingest/`), `mqtt`, `influxdb`, `grafana`, and the demo `simulator`. Creates `p4n4-net` |
| `ai/` | `agent` (the Ollama API with tools; `agent.py` is [`mqtt-influx-grafana-ollama`](../mqtt-influx-grafana-ollama)'s, unchanged) and `ollama`. The traffic tools are in `ai/agent/local_tools.py` |
| `edge/` | `video` (go2rtc) and `uplink` (profile `device`). Runs at the site |
| `theme/` | A sample white-label brand (`roadwatch`) with the Video tab enabled. Replace it with your client's |

Tokens must agree between layers: `SITE_TOKEN` in `iot/.env` and `edge/.env`, and `INGEST_READ_TOKEN` in `iot/.env` and `ai/.env`. Generate real ones with `openssl rand -hex 24`.

## Plates and privacy

A plate is personal data in many places. Check the law where the cameras are (signage, a lawful purpose, how long you may keep reads) before you point one at a road.

- **One place.** Plates are stored only in the ingest service's SQLite (`iot/data/ingest/ingest.db`). InfluxDB, Grafana and the MQTT topics get counts per camera, direction and vehicle type, never a plate. MQTT messages leave the plate out too, unless `MQTT_PLATES=true`.
- **Retention.** The ingest service deletes reads older than `RETENTION_DAYS` (default 30) every hour, and refuses older ones on arrival. Plate lookups cover only what's still kept, and every answer says how long that is (`retention_days`).
- **Who can look.** Plate lookups need `INGEST_READ_TOKEN` (or the site's own token). The agent has it, so anyone who can chat with the agent can look plates up: in p4n4-dashboard, that's every signed-in account with the Agent tab.
- **Logs.** Neither the ingest service nor the uplink logs a plate. A refused read is logged by its id.
- **The device** keeps its own copy (`alpr_live.db`) and its evidence images. Purge those on the device too.
- **What the agent says.** A plate read is a camera's reading, not proof of who drove. The agent is told never to guess who owns or drove a vehicle, and to mention similar plates when a lookup finds little.

## Video

`edge/config/go2rtc/video.sh` writes go2rtc's config from two variables in `edge/.env`. Each stream becomes a camera in the dashboard:

| Stream | Source | Without it |
|---|---|---|
| `road` | `CAMERA_URL`: `rtsp://user:pass@ip:554/stream`, or an `http://` MJPEG stream. H.264 is transcoded to MJPEG; a camera substream (640×360) keeps that cheap | `edge/clips/road.mp4` on a loop, else a test pattern |
| `plates` | `OVERLAY_URL`: the device's annotated view as MJPEG, with the vehicles it tracks and the plates it reads | `edge/clips/plates.mp4` on a loop, else the test pattern with a moving vehicle box and a made-up plate |

**Demo footage.** Drop recorded clips into `edge/clips/` as `road.mp4` and `plates.mp4` (any length; audio is dropped) to show a real-looking road before cameras are connected. They loop at real speed, and `.gitignore` keeps them out of git. Footage of a real road shows real plates: use footage you may show, and check its licence. The dashboard's camera caption covers the bottom of each tile, so keep any credit clear of that.

`.p4n4.json` lists both as `dashboard.cameras`. p4n4-dashboard reads them through p4n4-api and shows them in the **Video** tab on whatever host it's connected to:

```json
"cameras": [
  {"id": "road", "name": "Road", "port": 1984, "path": "/api/stream.mjpeg?src=road"},
  {"id": "plates", "name": "Road (plates)", "port": 1984, "path": "/api/stream.mjpeg?src=plates"}
]
```

A camera is `{id, name}` plus either `port` and `path` on the connected host, or an absolute `url`. Once a user edits the cameras in Settings → Video, their list wins. To add a camera, add a stream to `video.sh` and an entry here.

**Where video goes.** The streams stay on the site's network. The contract sends plates and attributes, and evidence only as URIs on the device, never images. Don't publish port 1984 to the internet: the plate view shows plates.

## The ingest service

`iot/ingest/` is standard library only, on the stock `python:3.13-slim` image.

| Endpoint | |
|---|---|
| `POST /api/v1/reads` | One read, a list, or `{"reads": [...]}` (up to 1000) |
| `POST /api/v1/heartbeat` | The device's health, every 30 s |
| `GET /api/v1/agent/summary` | Vehicles per direction and type, plates read (read rate), distinct and repeat plates, peak hour, busiest day, speeds (average, 85th percentile, over `SITE_SPEED_LIMIT`), each camera's read rate |
| `GET /api/v1/agent/traffic` | Vehicles per hour of the day (total and per-day average) and per day; `direction`, `vehicle_type` |
| `GET /api/v1/agent/plate` | One plate's passages, newest first, and plates one character away (likely misreads); `plate`; default period: everything kept |
| `GET /api/v1/agent/frequent` | The plates seen most often, with days seen and directions; `limit` |
| `GET /api/v1/agent/checks` | Each camera over the last 7 days: C1 read rate under 80%, C2 night read rate under 0.8 × the day's, C3 silent for an hour at a usually busy hour, C4 over 20% of plates read with confidence under 0.7 |
| `GET /api/v1/agent/nodes` | Each device's latest heartbeat, and whether it's online |

- **Auth.** Writes need the site's token (`X-Site-Token`), and a token writes only its own `site_id`. Reads need `INGEST_READ_TOKEN`, or a site's token for that site.
- **Plates.** Matched without case, spaces, dashes or dots (`abc 1234` is `ABC-1234`), and kept as the device read them.
- **Periods.** The agent endpoints take `today`, `yesterday`, `this_week`, `last_week`, `<N>d`, a weekday (`tuesday`, `martes`), or `from`/`to`. They use `SITE_TZ`.
- **Language.** `SITE_LANG` (`en` or `es`) sets the text people read: checks and weekdays. Codes stay as they are (`inbound`, `car`, `C2 night`).
- **Copies.** InfluxDB gets `alpr_read` (a count per read, tagged by site, camera, direction, vehicle type and whether the plate was read, with speed and confidence) and `alpr_node`. Heartbeat numbers go in as `sensor_data,device=<hostname>,sensor=<metric>`. MQTT gets `traffic/<site>/read/<direction>` and `traffic/<site>/heartbeat` under `MQTT_TOPIC_ROOT`. Both copies are best effort: SQLite is the record.

## Dashboard and agent

`iot/scripts/build_dashboard.py` generates `traffic.json`. Edit the script, not the JSON. For a Spanish, branded version:

```bash
python3 iot/scripts/build_dashboard.py --lang es --uid my-road --title Tráfico \
  --speed-limit 40 --logo theme/logo.png --brand 'ACME'
```

Then set `grafana_path` in `.p4n4.json` and `GRAFANA_LANGUAGE=es-ES` in `iot/.env` to match, and keep `--speed-limit` and `SITE_SPEED_LIMIT` the same.

The dashboard works in Grafana's light and dark themes. p4n4-dashboard's Grafana tab opens it in the app's own theme and reloads it when the theme changes. The header takes its accent colours and brand name from `theme/brand.json` (`--theme` for another file, `--brand ''` for no name), so re-run the script after changing the theme.

The agent's traffic tools are `get_traffic_summary`, `get_traffic_by_hour`, `find_plate`, `get_frequent_plates`, `get_camera_checks` and `get_device_status`. They call the ingest service, so the model picks a tool, a period and maybe a plate, but never computes a number. The template's own tools (`list_sensors`, `get_stats`, `get_history`) see the device's health.

## Edge device

The device writes each vehicle to a `reads` table in `alpr_live.db`:

```sql
CREATE TABLE reads (read_id TEXT PRIMARY KEY, site_id TEXT NOT NULL, camera_id TEXT NOT NULL, timestamp TEXT NOT NULL,
                    direction TEXT NOT NULL, lane INTEGER, vehicle_type TEXT NOT NULL, plate TEXT, plate_confidence REAL,
                    plate_region TEXT, speed_kmh REAL, colour TEXT, image_uri TEXT, plate_crop_uri TEXT, clip_uri TEXT,
                    synthetic INTEGER NOT NULL DEFAULT 0);
```

Point the uplink at it, with the `device` profile in `edge/.env`:

```bash
COMPOSE_PROFILES=device
DEVICE_DATA=/path/to/the/device/data     # where it writes alpr_live.db
INGEST_URL=https://traffic.example.com   # or http://ingest:8080 on the same host
SITE_ID=my-road
SITE_TOKEN=<the site's token>
```

The uplink forwards new or replaced rows, and sends a heartbeat every 30 s from `/proc` and `/sys` (CPU/GPU, temperatures, memory, disk, vehicles in the last minute). It keeps a cursor and retries with backoff. Rows the ingest service refuses are logged by id and skipped. `DEVICE_API` (the device's own HTTP API) tells healthy from degraded. Plates travel in these requests: use HTTPS off the site's network.

On a machine of its own, run `docker network create p4n4-net` once before `docker compose up -d` in `edge/`.

## Testing

```bash
python3 tests/test_ingest.py        # contract rules, retention, periods, checks, no plates in the copies (no Docker)
python3 tests/test_local_tools.py   # the agent's traffic tools against an in-process ingest (no Docker)
python3 tests/test_uplink.py        # the uplink against a device database (no Docker)
./tests/smoke.sh                    # iot and edge end to end, isolated (Docker)
```

The smoke test posts two seeded days with `simulate.py --once`, then checks:
- the agent API, plate lookups and their access rules
- retention: an old read is refused
- the InfluxDB and MQTT copies, and that neither holds a plate, nor does the ingest log
- every Grafana panel
- that both cameras in `.p4n4.json` serve JPEG frames

## Security

- **Tokens.** The `.env.example` tokens are public; replace them before anything leaves the machine. The read token can look up plates: treat it like the plates. The InfluxDB token is the stack default `p4n4-stack-token`.
- **Devices outside the network** post through an HTTPS reverse proxy in front of port 8090.
- **Video.** Port 1984 serves every camera, plate view included, to anyone who can reach it. go2rtc's API refuses `exec:` sources, and its config isn't writable, but it accepts new stream URLs. Keep it on the site's network.
- **Agent.** Anyone who can reach port 11434 can ask about the road, look plates up and use the Ollama API. Keep it on the loopback interface (`ports: !override ["127.0.0.1:11434:11434"]` under `agent` in `ai/docker-compose.override.yml`) so only p4n4-api, which requires sign-in, reaches it.
- **MQTT.** Mosquitto allows anonymous clients. The topics carry no plates; with `MQTT_PLATES=true` the messages do, so set up the broker's authentication first.
- **HTML panels.** `GF_PANELS_DISABLE_SANITIZE_HTML=true` lets the header render. Keep Grafana logins to Viewers.
