# retail-vision

> Camera analytics for a clothing store, with live video in p4n4-dashboard. An edge device in the store follows customers on camera and posts what happens. An ingest service stores it and answers questions with fixed numbers. Grafana shows the store, the Agent tab explains it, and the **Video tab** plays the store camera and the device's tracked view. Works with no hardware: a demo store and synthetic video stand in for them.

```
  edge/ (in the store)
  camera ─────────────┐                     ┌──► p4n4-dashboard Video tab (MJPEG)
  device's live view ─┴──► video :1984 (go2rtc)
  device SQLite (./data) ──► uplink ══HTTPS, X-Store-Token══╗   (profile "device")
                                                            ▼
  iot/                                ┌──► iot/data/ingest/ingest.db (SQLite: the record)
  simulator (profile "demo") ──► ingest :8090 ──┼──► influxdb ──► grafana :3000
  till (POS) ─────────────────────┘    ▲       └──► mqtt  retail/<store>/…
                                       │ agent API (deterministic numbers)
  ai/  p4n4-dashboard Agent tab ──► agent :11434 ──┬──► ollama (gemma4:e2b)
                                                   └──► influxdb (the device's health)
```

The device side follows the **AIOROS Alpha Boutique Jetson ↔ web contract 0.1**: schemas `aioros.boutique.{event,alert,report,heartbeat}/0.1`. Any device that posts those documents, or writes the same SQLite tables for the uplink to forward, works with it. A Jetson Orin Nano running AIOROS Alpha Boutique is the one it was built for.

## Quick start

```bash
cp -r retail-vision my-store && cd my-store
for layer in iot ai edge; do cp $layer/.env.example $layer/.env; done
p4n4 up        # iot, then ai, then edge
```

Without the CLI, start the layers in that order: `(cd iot && docker compose up -d) && (cd ai && docker compose up -d) && (cd edge && docker compose up -d)`. ai and edge join the `p4n4-net` network that iot creates.

| | |
|---|---|
| Grafana | <http://localhost:3000>, `admin` / `adminpassword`. Home: **Store** |
| Video | <http://localhost:1984>: go2rtc's page; streams at `/api/stream.mjpeg?src=floor` and `?src=overlay` |
| Ingest | <http://localhost:8090/health>; the API needs the tokens in `iot/.env` |
| Agent | <http://localhost:11434>, Ollama's API (p4n4-dashboard's Agent tab). The model is pulled on first start, about 4.6 GB |

The demo store (`COMPOSE_PROFILES=demo` in `iot/.env`) posts a week of history, then a live day from 10:00 to 20:00 store time. It's seeded, so every run produces the same story:
- `shelf-2` (Polos) is the busiest shelf.
- `shelf-3` (Dresses) draws interest but rarely sells.
- `shelf-5` (Suits) is high-value with little traffic.
- The evening is the peak.

Everything it posts is marked `"synthetic": true`, and the agent says so.

## Layout

| | |
|---|---|
| `iot/` | `ingest` (`iot/ingest/`), `mqtt`, `influxdb`, `grafana`, and the demo `simulator`. Creates `p4n4-net` |
| `ai/` | `agent` (the Ollama API with tools; `agent.py` is [`mqtt-influx-grafana-ollama`](../mqtt-influx-grafana-ollama)'s, unchanged) and `ollama`. The store tools are in `ai/agent/local_tools.py` |
| `edge/` | `video` (go2rtc) and `uplink` (profile `device`). Runs in the store |
| `theme/` | A sample white-label brand (`retail-vision`) with the Video tab enabled. Replace it with your client's |

Tokens must agree between layers: `STORE_TOKEN` in `iot/.env` and `edge/.env`, and `INGEST_READ_TOKEN` in `iot/.env` and `ai/.env`. Generate real ones with `openssl rand -hex 24`.

## Video

`edge/config/go2rtc/video.sh` writes go2rtc's config from two variables in `edge/.env`. Each stream becomes a camera in the dashboard:

| Stream | Source | Without it |
|---|---|---|
| `floor` | `CAMERA_URL`: `rtsp://user:pass@ip:554/stream`, or an `http://` MJPEG stream. H.264 is transcoded to MJPEG; a camera substream (640×360) keeps that cheap | `edge/clips/floor.mp4` on a loop, else a test pattern |
| `overlay` | `OVERLAY_URL`: the device's annotated view as MJPEG, e.g. AIOROS Alpha Boutique's live mode at `http://<jetson>:8777/mjpeg` | `edge/clips/overlay.mp4` on a loop, else the test pattern with a moving tracked-person box |

**Demo footage.** Drop recorded clips into `edge/clips/` as `floor.mp4` and `overlay.mp4` (any length; audio is dropped) to show a real-looking store before cameras are connected. They loop at real speed, and `.gitignore` keeps them out of git. Check the footage's licence before showing it, and burn in any credit it requires: the dashboard's camera caption covers the bottom of each tile, so keep it clear of that.

`.p4n4.json` lists both as `dashboard.cameras`. p4n4-dashboard reads them through p4n4-api and shows them in the **Video** tab on whatever host it's connected to:

```json
"cameras": [
  {"id": "floor", "name": "Sales floor", "port": 1984, "path": "/api/stream.mjpeg?src=floor"},
  {"id": "overlay", "name": "Sales floor (tracked)", "port": 1984, "path": "/api/stream.mjpeg?src=overlay"}
]
```

A camera is `{id, name}` plus either `port` and `path` on the connected host, or an absolute `url`. Once a user edits the cameras in Settings → Video, their list wins. To add a camera, add a stream to `video.sh` and an entry here.

**Where video goes.** The streams stay on the store network. The contract sends ids, attributes and URIs, never images. Run the edge layer in the store and open the Video tab from the store's network. Don't publish port 1984 to the internet.

## The ingest service

`iot/ingest/` is standard library only, on the stock `python:3.13-slim` image.

| Endpoint | |
|---|---|
| `POST /api/v1/events`, `/alerts`, `/reports`, `/heartbeat` | Contract §2–§5: one document, a list, or `{"events": [...]}` (up to 1000). Alerts that accuse anyone (`robo`, `ladrón`, `theft`, …) are refused |
| `POST /api/v1/pos` | Point-of-sale lines, one garment each (not in contract 0.1) |
| `GET /api/v1/agent/summary`, `/alerts`, `/layout`, `/ranking`, `/restock_advice` | Contract §6: visitors, peak hour, conversion, shelves' interest against sales, garment ranking, rule-based advice (R1–R4), each with its numbers |
| `GET /api/v1/agent/nodes` | Each device's latest heartbeat, and whether it's online |
| `POST /api/v1/alerts/<id>/status` | Staff marking an alert `attended` or `dismissed` |

- **Auth.** Writes need the store's token (`X-Store-Token`), and a token writes only its own `store_id`. Reads need `INGEST_READ_TOKEN`, or a store's token for that store.
- **Periods.** The agent endpoints take `today`, `yesterday`, `this_week`, `last_week`, `<N>d`, a weekday (`tuesday`, `martes`), or `from`/`to`. They use `STORE_TZ`.
- **Language.** `STORE_LANG` (`en` or `es`) sets the text people read: rules, weekdays and the demo store's names. Codes stay the contract's (`caliente`/`frio`, `alta`/`media`).
- **Copies.** InfluxDB gets `boutique_event`, `boutique_alert`, `boutique_sale` and `boutique_node` per store. Heartbeat numbers go in as `sensor_data,device=<hostname>,sensor=<metric>`. MQTT gets everything under `MQTT_TOPIC_ROOT` (default `retail`). Both copies are best effort: SQLite is the record.

## Dashboard and agent

`iot/scripts/build_dashboard.py` generates `store.json`. Edit the script, not the JSON. For a Spanish, branded version:

```bash
python3 iot/scripts/build_dashboard.py --lang es --uid my-store --title Boutique \
  --currency 'B/.' --logo theme/logo.png --brand 'ACME'
```

Then set `grafana_path` in `.p4n4.json` and `GRAFANA_LANGUAGE=es-ES` in `iot/.env` to match.

The agent's store tools are `get_store_summary`, `get_alerts`, `get_shelves`, `get_garment_ranking`, `get_advice` and `get_device_status`. They call the ingest service, so the model picks a tool and a period but never computes a number. The template's own tools (`list_sensors`, `get_stats`, `get_history`) see the device's health.

## Edge device

Point the uplink at the device's database, with the `device` profile in `edge/.env`:

```bash
COMPOSE_PROFILES=device
DEVICE_DATA=/path/to/the/device/data     # where it writes boutique_live.db
INGEST_URL=https://stores.example.com    # or http://ingest:8080 on the same host
STORE_ID=my-store
STORE_TOKEN=<the store's token>
```

The uplink forwards new or replaced rows of `events`, `alerts` and `pos_transactions`, with each shelf's planogram from `zones`. It also sends a heartbeat every 30 s from `/proc` and `/sys` (CPU/GPU, temperatures, memory, disk). It keeps a cursor per table and retries with backoff. Rows the ingest service refuses are logged and skipped. `DEVICE_API` (the device's own HTTP API) tells healthy from degraded.

On a machine of its own, run `docker network create p4n4-net` once before `docker compose up -d` in `edge/`.

## Testing

```bash
python3 tests/test_ingest.py        # contract rules, periods, upserts, queries, both languages (no Docker)
python3 tests/test_local_tools.py   # the agent's store tools against an in-process ingest (no Docker)
python3 tests/test_uplink.py        # the uplink against a device database (no Docker)
./tests/smoke.sh                    # iot and edge end to end, isolated (Docker)
```

The smoke test posts two seeded days with `simulate.py --once`, then checks:
- the agent API and its access rules
- the InfluxDB and MQTT copies
- every Grafana panel
- that both cameras in `.p4n4.json` serve JPEG frames

## Security

- **Tokens.** The `.env.example` tokens are public; replace them before anything leaves the machine. The InfluxDB token is the stack default `p4n4-stack-token`.
- **Devices outside the network** post through an HTTPS reverse proxy in front of port 8090.
- **Video.** Port 1984 serves every camera to anyone who can reach it. go2rtc's API refuses `exec:` sources, and its config isn't writable, but it accepts new stream URLs. Keep it on the store network.
- **Agent.** Anyone who can reach port 11434 can ask about the store and use the Ollama API.
- **HTML panels.** `GF_PANELS_DISABLE_SANITIZE_HTML=true` lets the header render. Keep Grafana logins to Viewers.
