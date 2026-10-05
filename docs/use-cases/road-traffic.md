# Use case: road traffic counting with ALPR

A business park has one access road. Its operator wants to know how the road is used, to
plan the shuttle bus, deliveries and the car park: how many vehicles come and go, when,
of what kind, how fast, and how many are regulars rather than visitors. Two cameras on the
road, one per direction, and an edge device that reads licence plates (automatic licence
plate recognition, ALPR) count every vehicle. The operator wants three things:

- The facilities team sees the road live and the day's numbers on a tablet, in an app with
  the park's name. No ports, URLs or p4n4 branding.
- They ask in plain language ("when is the road busiest?", "when did KDL-4821 come in
  today?") and get the same numbers Grafana shows. The model never adds anything up itself.
- Plates are kept for 30 days, in one place, and never reach the dashboards, the message
  broker or a log.

The integrator (you) runs the deployment from the same app. This guide builds that with
the [`mqtt-influx-grafana-ollama-go2rtc`](../../projects/mqtt-influx-grafana-ollama-go2rtc)
template, [p4n4-api](https://github.com/raisga/p4n4-docs/blob/main/reference/api.md) and a
white-labelled [p4n4-dashboard](https://github.com/raisga/p4n4-docs/blob/main/reference/dashboard.md).
It starts with the demo road, so you can show the client the whole thing before any
hardware is installed.

```
 at the roadside                                   server (on site, or any host the device reaches)
 ┌──────────────────────────────────────┐          ┌──────────────────────────────────────────────────────┐
 │ cameras ──RTSP──┐                    │          │ iot/  ingest :8090 ──► ingest.db (plates, 30 days)
 │ ALPR device ─MJPEG┴► video :1984 ────┼── Video ─┤        │   ├──► influxdb ──► grafana :3000 ── Grafana tab
 │   (reads plates)                     │   tab    │        │   │    (counts, no plates)
 │   └─► alpr_live.db ──► uplink ───────┼─ HTTPS ──┤        │   └──► mqtt  traffic/<site>/… (no plates)
 │                      X-Site-Token    │          │        │ agent API (fixed numbers, plate lookups)
 └──────────────────────────────────────┘          │ ai/   agent :11434 ──► ollama (gemma4:e2b)
                                                   │ p4n4-api :8000 ── project, status, assistant ── dashboard
                                                   └──────────────────────────────────────── ("roadwatch" brand)
```

| Piece | Repo | Role here |
|---|---|---|
| `mqtt-influx-grafana-ollama-go2rtc` template | `p4n4-templates` | Three layers: `iot` (ingest service, InfluxDB, MQTT, Grafana *Traffic* dashboard), `ai` (agent with the traffic tools, Ollama), `edge` (go2rtc video, uplink) |
| ALPR device ↔ web contract 0.1 | the template (`iot/ingest/contract.py`) | What the device posts: `p4n4.alpr.read/0.1` per vehicle, `p4n4.alpr.heartbeat/0.1` |
| `.p4n4.json` `dashboard` block | `p4n4-lib` (validated), the template (declared) | Grafana page, tabs, theme, and the two cameras for the Video tab |
| p4n4-api | `p4n4-api` | Serves the manifest, service status and host metrics, and relays the assistant behind sign-in |
| `theme/` (sample: `roadwatch`) | the project | The white-label theme, with the Video and Agent tabs |
| p4n4-dashboard | `p4n4-dashboard` | The app the facilities team, their manager and you use |

---

## 0. Before the cameras go up

A plate is personal data in many places. Before you point a camera at a road, check with
the client what the law where it stands asks for: usually a stated purpose, signs at the
road, and a limit on how long reads are kept. This guide keeps reads for 30 days; set
`RETENTION_DAYS` to what applies. Agree, too, on who may look plates up: in this setup,
everyone with the Assistant tab can.

## 1. Create the project and run the demo road

Copy the template and create each layer's `.env`:

```bash
cp -r tools/templates/projects/mqtt-influx-grafana-ollama-go2rtc ~/projects/parkside
cd ~/projects/parkside
for layer in iot ai edge; do cp $layer/.env.example $layer/.env; done
```

Rename the project in `.p4n4.json` (`"project": "parkside"`). Then edit the `.env` files.
Tokens must agree between layers:

| Variable | Layer(s) | Set it to |
|---|---|---|
| `SITE_ID` | `iot`, `edge` | The site's id, e.g. `parkside-access` |
| `SITE_TOKEN` | `iot`, `edge` | The site's write token: `openssl rand -hex 24` |
| `INGEST_READ_TOKEN` | `iot`, `ai` | The read token for the agent API, plate lookups included: `openssl rand -hex 24` |
| `SITE_TZ` | `iot`; `TZ` in `edge` | The site's time zone, e.g. `America/Panama`. "Today", weekdays and peak hours follow it |
| `SITE_SPEED_LIMIT` | `iot` | The road's limit in km/h, if the device measures speed; empty if it doesn't |
| `RETENTION_DAYS` | `iot` | How long plates are kept: 30 here |
| `INFLUXDB_PASSWORD`, `GRAFANA_PASSWORD` | `iot` | Your own secrets |
| `DATA_UID` / `DATA_GID` | `iot` | `id -u` / `id -g`, so `iot/data/ingest/ingest.db` is yours |
| `GRAFANA_ANONYMOUS` | `iot` | `true`, so the team sees the Traffic dashboard without a Grafana login (Viewer role). It shows no plates |
| `GRAFANA_ALLOW_EMBEDDING` | `iot` | `true` if the team uses the dashboard in a browser |
| `COMPOSE_PROFILES` | `iot` | Keep `demo` until the device posts; then clear it |

Start the layers in order (ai and edge join the `p4n4-net` network that iot creates) and
check them:

```bash
p4n4 up          # iot, then ai, then edge
p4n4 validate    # .p4n4.json, dashboard settings and theme, each layer's compose file and .env
```

Without the CLI: `(cd iot && docker compose up -d) && (cd ai && docker compose up -d) && (cd edge && docker compose up -d)`.

On first start Ollama downloads `gemma4:e2b` (about 4.6 GB). `p4n4 logs ai` (or
`docker compose logs -f ollama` in `ai/`) prints `model gemma4:e2b is ready` when it's done.

The demo road posts a week of history, then the rest of today as it happens. It's seeded,
so it tells the same story on every run:

- Weekdays peak inbound from 07:00 to 09:00 and outbound from 17:00 to 19:00. Weekends
  are quieter, with a midday peak.
- The most frequent plates are three shuttle buses (`MTB-1101`, `MTB-1102`, `MTB-1103`),
  then a delivery van (`KDL-4821`) on weekdays.
- `cam-out-01` faces oncoming headlights and reads far fewer plates at night.
- Outbound traffic is faster late at night.
- About 2% of plates are misread by one character.

Open Grafana at `http://<host>:3000`: the **Traffic** dashboard is the home page. Ask the
ingest service directly to see the numbers the assistant will quote:

```bash
curl -s -H "Authorization: Bearer $INGEST_READ_TOKEN" \
  "http://<host>:8090/api/v1/agent/checks" | jq '.checks'
```

Everything the demo posts is marked `"synthetic": true`, its plates are made up, and the
assistant says the data is from the demo.

### Demo video

Without cameras, the `video` service plays a test pattern, and a moving vehicle box with
a made-up plate on the `plates` stream. To show the client something closer to a road,
drop two clips into `edge/clips/` as `road.mp4` and `plates.mp4` and run
`docker compose up -d` in `edge/`. They loop at real speed and git ignores them. Footage of
a real road shows real plates, so use footage you may show, and check its licence.

## 2. Install the device at the roadside

The ALPR service on the device writes each vehicle to a `reads` table in `alpr_live.db`
(the template [README](../../projects/mqtt-influx-grafana-ollama-go2rtc/README.md#edge-device)
has the columns). The edge layer runs next to it, on the device itself or on a machine
that can read that file, and does two things:

- **uplink** forwards new rows to the ingest service, and sends a heartbeat (CPU/GPU,
  temperatures, memory, disk, vehicles in the last minute) every 30 s. It keeps a cursor
  and retries with backoff, so the road keeps being counted through a network cut.
- **video** (go2rtc) serves the road camera and the device's plate view as MJPEG, on the
  site's network only.

Copy the project's `edge/` directory to the device, then set `edge/.env` there:

```bash
COMPOSE_PROFILES=device
DEVICE_DATA=/path/to/the/device/data        # where it writes alpr_live.db
DEVICE_API=http://<device>:8080             # the ALPR service's own API: tells healthy from degraded
INGEST_URL=https://<server>                 # an HTTPS reverse proxy in front of port 8090: plates travel here
SITE_ID=parkside-access
SITE_TOKEN=<the site's token>
NODE_NAME=gate-road-01
CAMERA_URL=rtsp://user:password@<camera>:554/stream2    # a 640×360 substream keeps the transcode cheap
OVERLAY_URL=http://<device>:8777/mjpeg      # the ALPR service's annotated view
```

```bash
docker network create p4n4-net    # once, on a machine without the iot layer
docker compose up -d
```

`DEVICE_API` and the camera URLs are read from inside containers, so use the device's
LAN address or hostname, not `localhost`.

On the server, clear `COMPOSE_PROFILES` in `iot/.env` and run
`docker compose up -d --remove-orphans` in `iot/` to stop the demo road. The device's
edge layer replaces the server's: run `docker compose down` in the server's `edge/`, then
remove `edge` from `layers` in the server's `.p4n4.json`, so `p4n4 up` and
`p4n4 validate` leave it out.

The device's `video` service now runs on the device, not on the host the dashboard
connects to. Point the cameras in `.p4n4.json` at it with absolute URLs:

```json
"cameras": [
  {"id": "road", "name": "Access road", "url": "http://<device>:1984/api/stream.mjpeg?src=road"},
  {"id": "plates", "name": "Access road (plates)", "url": "http://<device>:1984/api/stream.mjpeg?src=plates"}
]
```

If every layer runs on one machine, keep the template's `port` and `path` entries instead:
they resolve against whatever host the dashboard connects to.

The device keeps its own copy of every read, and often a picture of each plate. Purge
those on the device to the same retention.

Any device that posts the contract's documents to `/api/v1/reads` and `/api/v1/heartbeat`
works too, without the uplink. `iot/ingest/contract.py` has the rules.

## 3. Serve the project with p4n4-api

```bash
cd api
uv venv && uv pip install -e ../lib -e .
export P4N4_PROJECT_DIR=~/projects/parkside
uv run p4n4-api users add integrator --role admin     # you: the admin view
uv run p4n4-api users add manager --role operator     # the facilities manager: the power view
uv run p4n4-api users add facilities --role normie    # the team: the normie view
uv run p4n4-api
```

The Assistant tab goes through p4n4-api (`/api/v1/agents/chat`), which forwards to the
template's agent at `P4N4_API_OLLAMA_URL` (default `http://localhost:11434`). The agent
adds the traffic tools on the way, so the dashboard needs no configuration for them.

The agent can look plates up, and port 11434 has no sign-in of its own. Keep it on the
loopback interface so only p4n4-api reaches it:

```yaml
# ai/docker-compose.override.yml
services:
  agent:
    ports: !override ["127.0.0.1:11434:11434"]
```

## 4. Build the white-label dashboard

The template ships `roadwatch`, a sample brand: dark by default, with the Video tab.
Replace it with the park's brand in `theme/brand.json`:

```jsonc
{
  "id": "roadwatch",                          // unique per client; "p4n4" is reserved
  "appName": "Roadwatch",
  "wordmark": { "text": "road", "suffix": ".watch" },
  "platform": "road",                         // road-iot, road-ai, road-edge
  "tabs": ["services", "edge", "agent", "grafana", "video"],
  "defaults": {
    "themeMode": "dark",
    "powerTabs": "services,edge,agent,grafana,video",   // the manager
    "normieTabs": "video,agent,grafana",                 // the team: Home, then these
    "agentBackend": "ollama",
    "ollamaModel": "gemma4:e2b"                          // offered as the assistant on first sign-in
  },
  "native": { "displayName": "Roadwatch", "applicationId": "com.example.roadwatch", … }
}
```

Leave `agent` out of `normieTabs` if the team shouldn't look plates up.

The Traffic dashboard's header takes its accent colours and brand name from the same file,
so regenerate it after changing the theme, with the road's speed limit:

```bash
python3 iot/scripts/build_dashboard.py --brand 'Parkside' --speed-limit 30
```

Install the theme into p4n4-dashboard and build:

```bash
cd dashboard
dart run tool/brand.dart check ~/projects/parkside              # schema, IDs and WCAG AA contrast
dart run tool/brand.dart install ~/projects/parkside --apply    # copies theme/ to brands/roadwatch/ (gitignored)
flutter build apk --split-per-abi                               # a tablet for the facilities team
dart run tool/brand.dart apply p4n4                             # back to the default before committing
```

Or serve the web build from the server, as the `p4n4-dashboard` container
(`make image THEME=~/projects/parkside`), so the team just opens a browser.

### In Spanish

Set `SITE_LANG=es` and `GRAFANA_LANGUAGE=es-ES` in `iot/.env`, regenerate the dashboard
in Spanish and point `.p4n4.json` at it, and add `"locale": "es"` to the theme's
`defaults`:

```bash
python3 iot/scripts/build_dashboard.py --lang es --uid parkside --title Tráfico --speed-limit 30
```

```json
"grafana_path": "/d/parkside/trafico"
```

`SITE_LANG` sets the text people read (the checks and weekdays). Codes stay as they are
(`inbound`, `car`, `C2 night`), and the assistant understands weekdays in both languages
(`tuesday`, `martes`).

## 5. Use it

**Facilities team (normie view).** Home shows one health card and the device's readings.
Then:

- **Video** plays the road and the device's plate view, one at a time or in a grid.
- **Assistant** answers questions about the road. Each one maps to a traffic tool, which
  calls the ingest service's agent API:

  | Ask | Tool | The demo road answers |
  |---|---|---|
  | "How was traffic today?" | `get_traffic_summary` | Vehicles per direction and type, read rate, peak hour, speeds and how many went over the limit |
  | "When is the road busiest?" | `get_traffic_by_hour` | Inbound peaks at 07:00–09:00 and outbound at 17:00–19:00 on weekdays |
  | "Which vehicles come most often?" | `get_frequent_plates` | The three buses, then the delivery van |
  | "When did KDL-4821 come in today?" | `find_plate` | Its passages, newest first, and any plate one character away |
  | "Are the cameras working well?" | `get_camera_checks` | C2 for `cam-out-01`: it reads far fewer plates at night |
  | "Is the device online?" | `get_device_status` | The latest heartbeat, and whether it's recent |

  The model picks the tool, the period and the plate. The numbers are the ingest
  service's, so they match Grafana's. It writes plates exactly as the tool returns them,
  and never guesses who drove.
- **Grafana** opens the Traffic dashboard in kiosk mode: vehicles per hour and direction,
  vehicle types, each camera's read rate, speeds against the limit, and the device's
  health. It holds no plates. It's embedded on the web build, Android, iOS and macOS; on
  Linux and Windows the tab opens it in the browser.

**Manager (power view).** The same tabs, plus Services and Edge, the connection settings
and the choice of assistant model for everyone.

**Integrator (admin view).** Everything, plus Clients: each site is a connection profile,
and **Connect** switches the dashboard to it. Views sets which tabs the power and normie
views show, for every device of that deployment.

### Camera checks

The ingest service checks each camera over the last 7 days. Each check is a reason to go
and look, with the numbers that fired it:

| Check | Fires when | Usually means |
|---|---|---|
| C1 read rate | Plates read for under 80% of vehicles | A dirty lens, bad focus or angle, weak IR |
| C2 night | The night read rate (19:00–06:00) is under 0.8 × the day's | Headlight glare, IR or shutter speed at night |
| C3 silent | No vehicle for an hour, at an hour that usually has 10 or more | The camera or the device is down |
| C4 confidence | Over 20% of plates read with confidence under 0.7 | Plates too small in the image, or out of focus |

## Checks

These were run against the template on Linux:

- [x] `uv run scripts/validate.py` in p4n4-templates passes, including the `dashboard` block (tabs, `grafana_path`, cameras) and the `roadwatch` theme
- [x] `tests/test_ingest.py`, `tests/test_local_tools.py` and `tests/test_uplink.py` pass
- [x] `tests/smoke.sh` passes: the agent API, plate lookups and their access rules, retention, the InfluxDB and MQTT copies without plates, an ingest log without plates, every Grafana panel, and JPEG frames from both cameras in `.p4n4.json`
- [ ] The Assistant tab with `gemma4:e2b` answering the questions above
- [ ] The dashboard (`roadwatch` build) on a tablet, with Video, Assistant and Grafana in the normie view
- [ ] A Jetson running an ALPR service, with the edge layer on it and the iot and ai layers on a server
- [ ] Real cameras over RTSP, by day and at night

## Limitations and next steps

- **One site, two directions.** Journey times between two sites (matching plates at A
  and B) aren't computed. The ingest service takes several sites (`EXTRA_SITE_TOKENS`),
  so a "journey" endpoint over two sites' reads would be the next step.
- **Plate lookups are all or nothing.** Anyone with the Assistant tab can look plates up.
  p4n4-api has no per-tool permissions yet; leave `agent` out of the normie view if only
  the manager should.
- **No alerts.** Nothing tells the team when a check fires or a camera goes quiet. A
  subscriber on `traffic/<site>/heartbeat`, or a scheduled call to `/api/v1/agent/checks`,
  could send them to a phone.
- **Video is LAN only.** Port 1984 serves every camera, the plate view included, to anyone
  who can reach it. Keep it on the site's network.
- **Speeds are the device's.** The ingest service trusts `speed_kmh` as posted; the
  device needs calibrating for it to mean anything.
- **Services doesn't know the template's services.** The dashboard's service catalog is
  fixed: Grafana, InfluxDB, MQTT and Ollama show live status, but the ingest service,
  `video` and `uplink` aren't listed, and catalog services the template doesn't run
  (Node-RED, Letta, n8n, EI Runner) show *unknown*. The manifest-driven catalog the
  [greenhouse use case](greenhouse-telemetry.md#limitations-and-next-steps) asks for would fix both.
- **The model is small.** `gemma4:e2b` sometimes asks which period you mean instead of
  calling a tool; asking again usually works. `AGENT_THINK=true` picks tools more reliably
  but answers several times slower. The numbers are right either way: they never come
  from the model.
