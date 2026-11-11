# mqtt-influx-grafana-obd2

> A dashcam, the car's OBD-II stats and an offline map on one phone, for everyday drivers. A rooted Android phone runs this stack and p4n4-dashboard side by side. The app records the road with the back camera and the cabin with the front one, relays a Bluetooth **ELM327** adapter to the **obd2** service, and publishes the phone's GPS fixes over **MQTT**. **Telegraf** stores the car's readings, its trouble codes and the route in **InfluxDB**, **Grafana**'s Drive dashboard shows them, and **nginx** serves an offline map to the app's Map tab. No car is needed to try it: a simulated car and a demo route around Monaco stand in.

| | |
|---|---|
| Layers | `iot` + `extras` |
| Services | Mosquitto · Telegraf · InfluxDB · Grafana · obd2 · nginx (map) (+ demo GPS and car simulators) |
| Version | 0.1.0 |

```
  p4n4-dashboard (on the phone)        the stack (Docker, on the same phone)

  GPS ───── sensors/car/gps ─────────► mqtt :1883 ──► telegraf ──┬──► influxdb ──► grafana :3000 ──┐
                                       ▲                         └──► iot/data/archive/            │
                                       │ sensors/car/<reading>,                                    │
                                       │ alerts/car/dtc, status/car/online                         │
  OBD relay :35100 ────── TCP ───────► obd2                                                        │
    ▲ Bluetooth                                                                                    │
  ELM327, in the car's OBD-II port                                                                 │
  Grafana tab ◄────────────────────────────────────────────────────────────────────────────────────┘
  Map tab ◄─────────────────────────── tiles :8088 ◄── extras/data/map/ ◄── scripts/fetch-map.sh
  Video tab ◄── back (dashcam) and front (copilot) cameras: recorded in the app, not the stack
```

Everything runs on the phone, and the app reaches the stack on `127.0.0.1`. Once the images and the map are on the phone, nothing needs the internet but the basemap under Grafana's route panel.

---

## Quick start

On any machine with Docker; a PC is fine for a first look:

```bash
cp -r mqtt-influx-grafana-obd2 my-car && cd my-car
for layer in iot extras; do cp $layer/.env.example $layer/.env; done
p4n4 up        # iot, then extras
```

Without the CLI: `(cd iot && docker compose up -d) && (cd extras && docker compose up -d)`. extras joins the `p4n4-net` network that iot creates. The first start builds the obd2 and simulator images from `extras/obd2/`.

| | |
|---|---|
| Grafana | <http://localhost:3000>, `admin` / `adminpassword`. Home: **Drive** |
| Map | <http://localhost:8088/style.json> once `scripts/fetch-map.sh` has written a map (see [Offline map](#offline-map)); a 404 until then |
| obd2 | <http://127.0.0.1:8090/health>, `/api/v1/probe` and `/api/v1/dtc`, all read-only |
| InfluxDB | <http://localhost:8086>, `admin` / `adminpassword` |
| MQTT | `localhost:1883` (the app's GPS fixes), WebSocket on 9001 |

The `demo` profile is on in both `.env.example` files:
- **obd2-sim** (extras) is a simulated car behind an ELM327 adapter, which obd2 reads like a real one. Its `warmup` scenario starts cold (the coolant climbs from 18 to 91 °C in five minutes), drives between 5 and 75 km/h, and sets trouble code P0171 (System Too Lean): pending after a minute, then stored, with the check-engine light, after two. Every ten minutes it starts over, cold and with the code cleared.
- **gps-sim** (iot) laps Monaco's streets the way the Grand Prix circuit runs (3.3 km) as device `car`. It publishes a fix a second, in the app's format, and slows down for the corners.

The two don't know about each other: the car's speed isn't the route's.

To turn the demo off, clear `COMPOSE_PROFILES` in both `.env` files, then run `docker compose up -d --remove-orphans` in `iot/` and in `extras/`.

On Linux and Termux, set `ARCHIVE_UID` / `ARCHIVE_GID` in `iot/.env` to the output of `id -u` / `id -g`. Telegraf writes the archive as that user.

### Where things live

```
iot/config/mosquitto/                broker and device logins (auth.sh)
iot/config/telegraf/telegraf.conf    inputs, outputs and archive settings
iot/config/telegraf/parse_*.star     topic + payload → sensor_data, dtc, vehicle_status
iot/config/grafana/provisioning/     InfluxDB datasource (uid influxdb-telemetry), Drive and Telemetry dashboards
iot/scripts/gps-sim.sh               demo GPS route
iot/data/archive/                    file-system archive (gitignored)
extras/obd2/                         obd2 and its simulator, from p4n4-extras (GPL-2.0)
extras/obd2/simulator/scenarios/     simulated drives (mounted: add one, no rebuild)
extras/config/tiles/default.conf     map server: CORS, range requests, __ORIGIN__ in style.json
extras/data/map/                     the offline map (gitignored)
scripts/fetch-map.sh                 downloads an area's offline map
theme/                               p4n4-dashboard white-label theme (sample: p4n4-drive)
tests/smoke.sh                       end-to-end test
```

---

## On the phone

The template was made for a rooted Redmi 7 (arm64) that runs Docker in Termux next to the app. Any rooted 64-bit Android phone whose kernel can run Docker should do.

### Docker in Termux

Docker needs root and a kernel with namespaces, cgroups and overlayfs, plus bridge, veth and netfilter support for Compose's networks. Many stock kernels lack some of these; moby's [`check-config.sh`](https://github.com/moby/moby/blob/master/contrib/check-config.sh) lists what yours has. In [Termux](https://termux.dev), `dockerd` is in the root repository:

```bash
pkg install root-repo
pkg install dockerd docker-compose tsu      # docker-compose brings docker-cli
sudo dockerd &                              # as root: tsu's sudo
sudo docker info | grep Architecture        # aarch64: InfluxDB 2 has no 32-bit image
termux-wake-lock                            # keep Termux running with the screen off
```

With `termux-services` installed, `sv-enable dockerd` starts the daemon (as root) whenever Termux starts. Run `docker` as root too, unless your user can open its socket, and exempt Termux and p4n4-dashboard from battery optimisation, so Android doesn't stop them on the road. Termux guides often start `dockerd` with `--iptables=false` or `--bridge=none`; with `--bridge=none`, read [If the bridge network doesn't work](#if-the-bridge-network-doesnt-work).

Copy the project to the phone and start it as in [Quick start](#quick-start), with `ARCHIVE_UID` / `ARCHIVE_GID` set to Termux's own user (`id -u` / `id -g` in a Termux shell, not as root). The first start builds the obd2 images on the phone, which needs the internet. If the builds can't reach it (without iptables, containers get no NAT), build them on the host network, from `extras/`:

```bash
docker build --network host -t p4n4-obd2:0.1.0 obd2/service
docker build --network host -t p4n4-obd2-sim:0.1.0 obd2/simulator
```

### Memory and storage

`docker stats` on a PC (x86-64), a few minutes into the demo:

| Container | RAM |
|---|---|
| grafana | 138 MiB |
| influxdb | 109 MiB |
| telegraf | 62 MiB |
| obd2 | 36 MiB |
| tiles | 26 MiB: an nginx worker per CPU, 32 there and 8 on a Redmi 7 |
| mqtt | 21 MiB |
| obd2-sim, gps-sim (demo) | 20 MiB, 2 MiB |
| **All** | **about 410 MiB** |

Docker itself, Android and the app come on top of that: it's tight on a 2 GB phone, and the 3 and 4 GB Redmi 7 have more room. p4n4-api isn't needed on the phone (see [p4n4-dashboard](#p4n4-dashboard)): leave it out.

The images take about 2.8 GB (`docker images` on a PC) and the Monaco map 13 MB. Driving adds about 15 MB an hour to the archive (9 MB of raw messages, 6 MB of line protocol), which keeps 30 rotated files of each: a month, at the default daily rotation. The demo writes as much while it runs, about 360 MB a day: on the phone, turn it off once you've seen it, or lower `rotation_max_archives` in `iot/config/telegraf/telegraf.conf`.

### Keep the ports on the phone

As in the other templates, MQTT (1883, 9001), InfluxDB (8086), Grafana (3000) and the map (8088) are published on every interface, obd2's API on loopback only. A phone joins other people's Wi-Fi and shares hotspots, and the app needs nothing but loopback. A layer's `docker-compose.override.yml` is gitignored, and Compose reads it automatically:

```yaml
# iot/docker-compose.override.yml
services:
  mqtt:
    ports: !override ["127.0.0.1:1883:1884", "127.0.0.1:9001:9001"]
  influxdb:
    ports: !override ["127.0.0.1:8086:8086"]
  grafana:
    ports: !override ["127.0.0.1:3000:3000"]
```

```yaml
# extras/docker-compose.override.yml
services:
  tiles:
    ports: !override ["127.0.0.1:8088:8088"]
```

With Grafana on loopback, `GRAFANA_ANONYMOUS=true` in `iot/.env` opens the app's Grafana tab without a login.

### If the bridge network doesn't work

The stack uses Docker's bridge networking: the services find each other by name on `p4n4-net`, and obd2 reaches the app's relay at `host.docker.internal`, the phone's address on Docker's default bridge (`host-gateway`). That fails when:
- dockerd runs with `--bridge=none`: `host-gateway` has no address then, unless `--host-gateway-ip` gives it one;
- the relay listens on 127.0.0.1 only;
- the phone's firewall drops what comes from the bridge.

Then run obd2 alone on the host network. It's been tested with Docker Desktop, not yet on a phone:

```yaml
# extras/docker-compose.override.yml
services:
  obd2:
    network_mode: host
    networks: !reset []
    ports: !reset []
    extra_hosts: !reset []
```

```bash
# extras/.env
OBD_DEVICE=socket://127.0.0.1:35100
MQTT_HOST=127.0.0.1    # the broker's published port 1883: its device listener
MQTT_USER=             # iot/.env's MQTT_USER and MQTT_PASSWORD, if set
MQTT_PASSWORD=
```

obd2's API then listens on every interface at 8090 (`OBD2_BIND` and `OBD2_PORT` no longer apply), and the simulated car is out of its reach: this is for the real one.

If the bridge doesn't work at all, put every service on the host network (also tested with Docker Desktop). The services then reach each other on 127.0.0.1, `p4n4-net` isn't created, and each one listens on its own port: `MAP_PORT`, `OBD2_BIND` and `OBD2_PORT` no longer apply.

```yaml
# iot/docker-compose.override.yml
services:
  mqtt:
    network_mode: host
    networks: !reset []
    ports: !reset []
  influxdb:
    network_mode: host
    networks: !reset []
    ports: !reset []
  telegraf:
    network_mode: host
    networks: !reset []
    extra_hosts: ["mqtt:127.0.0.1", "influxdb:127.0.0.1"]
  grafana:
    network_mode: host
    networks: !reset []
    ports: !reset []
    extra_hosts: ["influxdb:127.0.0.1"]
  gps-sim:
    network_mode: host
    networks: !reset []
    extra_hosts: ["mqtt:127.0.0.1"]
```

```yaml
# extras/docker-compose.override.yml
services:
  obd2:
    network_mode: host
    networks: !reset []
    ports: !reset []
    extra_hosts: !reset []
  obd2-sim:
    network_mode: host
    networks: !reset []
  tiles:
    network_mode: host
    networks: !reset []
    ports: !reset []
```

In `extras/.env`, set `MQTT_HOST=127.0.0.1`, and `OBD_DEVICE=socket://127.0.0.1:35100` for the relay or `socket://127.0.0.1:35000` for the simulator.

**Mind the broker.** On the host network, port 1883 is Mosquitto's anonymous listener, the one meant for containers only, and it's open on every interface; the device login is on 1884. Keep the phone off networks you don't trust, or drop outside traffic to the stack as root: `iptables -I INPUT -p tcp --dport 1883 ! -i lo -j DROP`, and the same for 1884, 9001, 8086, 3000, 8088, 8090 and 35000. The rules are gone after a reboot.

---

## In the car

1. Plug the ELM327 adapter into the car's OBD-II port, under the dashboard near the steering column, and pair it with the phone in Android's Bluetooth settings. The PIN is usually `1234` or `0000`.
2. In p4n4-dashboard, turn the OBD relay on and pick the adapter. The app bridges it to TCP port 35100 on the phone. The relay is off in this theme (`obdRelay`) until there's an adapter to relay.
3. In `extras/.env`, point obd2 at the relay and turn the simulated car off:
   ```bash
   OBD_DEVICE=socket://host.docker.internal:35100
   COMPOSE_PROFILES=
   ```
   Clear `COMPOSE_PROFILES` in `iot/.env` too: the app publishes the phone's GPS fixes (`gpsPublish`, on in this theme) in place of gps-sim.
4. `p4n4 up`, or `docker compose up -d --remove-orphans` in `iot/`, then in `extras/`.

With the ignition on, obd2 logs the car (`docker logs p4n4-obd2`), for example:

```
vehicle connected: ISO 15765-4 (CAN 11/500) via ELM327 v1.5; 11 of 11 measurements supported
```

The Drive dashboard's **Vehicle** stat then says *Connected*. *Ignition off* means the adapter answers but the car doesn't. *No adapter* means obd2 can't reach it (`no adapter at socket://host.docker.internal:35100` in its log): check the relay, then [the bridge network](#if-the-bridge-network-doesnt-work).

- Cheap clones can stall at obd2's default rates: lower `OBD_POLL_FAST_HZ` (to `2`, say). `OBD_PROTOCOL=6` (CAN 11-bit, 500 kbit/s, what most cars since 2008 use) skips the protocol search.
- A Wi-Fi adapter needs no relay: `OBD_DEVICE=socket://192.168.0.10:35000` (a common address; the phone joins the adapter's Wi-Fi).
- obd2 only reads. It refuses anything that would change the car, clearing trouble codes included: see [extras/obd2/README.md](extras/obd2/README.md).

---

## Offline map

The app's Map tab loads `http://<host>:8088/style.json` (`MAP_PORT` in `extras/.env`). `tiles`, an nginx, serves `extras/data/map/` read-only. While that's empty, every map URL is a 404 and the app shows an online map. `scripts/fetch-map.sh` downloads an area into it:

```bash
scripts/fetch-map.sh --bbox 7.4091,43.7247,7.4398,43.7519     # Monaco, zoom 0-15
```

| Option | Default | |
|---|---|---|
| `--bbox` | *(required)* | `MINLON,MINLAT,MAXLON,MAXLAT`, in degrees |
| `--maxzoom` | `15` | The deepest zoom level, 0-15 (15 is street level) |
| `--build` | the latest | The Protomaps basemap build, `YYYYMMDD` |
| `--flavor` | `light` | `light`, `dark`, `white`, `grayscale` or `black` |
| `--lang` | `en` | The labels' language |
| `--out` | `extras/data/map` | Where to write |

It needs bash, curl and Docker, and works on Linux, Termux and Git Bash on Windows. It runs:
1. `extract` from `protomaps/go-pmtiles:v1.31.2`, which cuts the area out of a worldwide [Protomaps](https://protomaps.com) build, reading only what it needs: `region.pmtiles`.
2. `generate_style` from [`@protomaps/basemaps@5.7.2`](https://github.com/protomaps/basemaps), in `node:24.22.2-alpine`: `style.json`. Its URLs start with `__ORIGIN__`: `pmtiles://__ORIGIN__/region.pmtiles`, `__ORIGIN__/fonts/{fontstack}/{range}.pbf` and `__ORIGIN__/sprites/v4/<flavor>`.
3. A download of [protomaps/basemaps-assets](https://github.com/protomaps/basemaps-assets) at commit `028c18f` (a 6.5 MB archive), for the Noto Sans glyphs (Regular, Medium and Italic) and the sprites.

It works in a staging directory, so a failed run keeps the last map, and it writes the sources and licences to `ATTRIBUTION.txt`. Monaco at zoom 15 is about 13 MB: 1.9 MB of tiles, 11 MB of fonts (the same for any area) and 0.2 MB of sprites. Run it on the phone over Wi-Fi, or on a PC and copy `extras/data/map/` over. `tiles` serves a new map as soon as it's written.

The map server:
- puts the scheme and host the client asked for in place of `__ORIGIN__` in `style.json`, so the same files work on `127.0.0.1` and on any other address;
- answers range requests (`206 Partial Content`), which MapLibre reads `region.pmtiles` with, and never gzips that file;
- serves `.pbf` as `application/x-protobuf` and `.pmtiles` as `application/vnd.pmtiles`;
- lets any origin read the map (CORS), for the dashboard's web build, and answers preflight requests with 204.

```bash
curl -s http://127.0.0.1:8088/style.json | grep -o 'pmtiles://[^"]*'    # pmtiles://http://127.0.0.1:8088/region.pmtiles
curl -sI -H 'Range: bytes=0-15' http://127.0.0.1:8088/region.pmtiles     # HTTP/1.1 206 Partial Content
curl -sI 'http://127.0.0.1:8088/fonts/Noto%20Sans%20Regular/0-255.pbf'  # HTTP/1.1 200 OK
```

**Licences.** The map data is © OpenStreetMap contributors, under the [ODbL](https://www.openstreetmap.org/copyright): credit it wherever the map is shown, as the style's attribution does. The tiles also hold water polygons from osmdata.openstreetmap.de (ODbL), Natural Earth (public domain) and, at low zooms, ESA WorldCover landcover (CC BY 4.0). Protomaps basemaps: code BSD-3-Clause, design CC0. Noto Sans: SIL Open Font License 1.1. The sprites: MIT. The map stays out of git (`.gitignore`).

---

## MQTT topics

`<id>` is the car's device id, `car` by default: `OBD_DEVICE_ID` in `extras/.env`, `GPS_DEVICE_ID` in `iot/.env` and the app's own. Keep them the same, so one dashboard shows the car and its route.

| Topic | Payload | When | From |
|---|---|---|---|
| `sensors/<id>/gps` | `{"lat": 43.73, "lon": 7.42, "alt": 52.0, "speed": 37.5, "heading": 182.0, "acc": 4.0}`: degrees, metres, km/h, degrees from north, and the horizontal accuracy in metres | up to 1 Hz; QoS 0, not retained | the app, or gps-sim |
| `sensors/<id>/rpm`, `speed`, `maf` | `{"value": 2150, "unit": "rpm"}`, in `rpm`, `km/h` and `g/s` | 5 Hz (`OBD_POLL_FAST_HZ`) | obd2 |
| `sensors/<id>/engine_load`, `coolant_temp`, `intake_temp`, `stft_b1`, `ltft_b1`, `throttle_pos`, `fuel_level` | the same, in `%` or `C` | 1 Hz (`OBD_POLL_SLOW_HZ`) | obd2 |
| `sensors/<id>/battery_v` | the same, in `V`: the voltage at the OBD-II port | every 5 s (`OBD_VOLTAGE_INTERVAL`) | obd2 |
| `alerts/<id>/dtc` | `{"codes": ["P0171"], "pending": [], "permanent": [], "mil": true, "descriptions": {"P0171": "System Too Lean (Bank 1)"}, "freeze_frame": {"dtc": "P0171"}}` | at start if there are any, then when the codes or the MIL change, read every 30 s (`OBD_DTC_INTERVAL`); QoS 1 | obd2 |
| `status/<id>/online` | `{"online": true, "vehicle": true, "adapter": "ELM327 v1.5", "protocol": "ISO 15765-4 (CAN 11/500)"}`, and `{"online": false}` when obd2 goes (its last will) | when it changes; retained | obd2 |
| `sensors/<id>/raw` | `{"values": [...]}`, a feature vector for an edge model | only with `OBD_RAW=true` (default `false`); Telegraf drops it | obd2 |

obd2 publishes only what the car supports, and `OBD_PIDS` narrows that down. Any other `sensors/<device-id>/<measurement>` message, from any device, is stored the [`mqtt-influx-grafana`](../mqtt-influx-grafana#data-contract) way.

## InfluxDB

Everything goes to the bucket `raw_telemetry` (`INFLUXDB_BUCKET`, retention `INFLUXDB_RETENTION=30d`):

```
sensor_data,device=car,sensor=rpm  unit="rpm",value=2150
sensor_data,device=car,sensor=gps  acc=4,alt=52,heading=182,lat=43.73,lon=7.42,speed=37.5
dtc,device=car                     codes="P0171",count=1,descriptions="P0171: System Too Lean (Bank 1)",
                                   freeze_frame="P0171",mil=true,pending="",permanent=""
vehicle_status,device=car          adapter="ELM327 v1.5",online=true,protocol="ISO 15765-4 (CAN 11/500)",vehicle=true
```

Numbers are floats, `count` included. Each list of codes is comma-joined, `count` is how many different codes the three lists hold, and the latest `dtc` point is the car's current state: `count=0` once they're cleared. The parsers are in `iot/config/telegraf/`: `parse_sensor.star` (the [`mqtt-influx-grafana`](../mqtt-influx-grafana) one, unchanged), `parse_dtc.star` and `parse_status.star`.

Telegraf also archives every message it stores, as in `mqtt-influx-grafana`: `iot/data/archive/raw/mqtt.jsonl` as received, and `iot/data/archive/lineprotocol/telemetry.lp` as written to InfluxDB, rotated every `ARCHIVE_ROTATION_INTERVAL` (24h). To restore the line protocol, from `iot/`:

```bash
docker compose cp data/archive/lineprotocol/telemetry.lp influxdb:/tmp/telemetry.lp
docker compose exec influxdb influx write --bucket raw_telemetry --precision ns --file /tmp/telemetry.lp
```

## Drive dashboard

Grafana's home page: uid `p4n4-drive`, at `/d/p4n4-drive/drive`. It refreshes every 5 s, shows the last 30 minutes, and follows the car picked in **Car** (`device`, `car` by default):
- gauges for **Speed**, **RPM**, **Coolant**, **Engine load**, **Throttle** and **Fuel**, next to the **Battery** voltage;
- the trip over the time range: **Distance**, **Fuel used** and **Average consumption**;
- **Check engine (MIL)**, **Vehicle** (*Connected*, *Ignition off*, *No adapter* or *obd2 offline*) and **Adapter** (its name and the car's protocol);
- **Speed** (OBD and GPS) and **RPM** over time;
- **Route**, the GPS track on a Geomap, over [OpenFreeMap](https://openfreemap.org)'s Liberty style (Grafana 12's default basemap, CARTO's, now stamps "API key required" on its tiles);
- **Trouble codes**: the last 20 reports, with their descriptions.

The trip figures integrate 10-second averages:
- distance = ∫ speed dt (km/h over hours);
- fuel used = ∫ MAF dt ÷ 14.7 ÷ 737 g/L: the air over petrol's stoichiometric air-fuel ratio, then over its density;
- average consumption = fuel used ÷ distance × 100 (L/100 km), from 0.1 km on.

They're estimates for a petrol car with a MAF sensor. A diesel runs lean, so its fuel isn't its air ÷ 14.7, and a car that meters air by manifold pressure (MAP) has no `maf` reading: Fuel used and Average consumption stay empty.

The **Telemetry** dashboard (uid `p4n4-telemetry`, the `mqtt-influx-grafana` one) lists every device and reading. Both are provisioned read-only: to change one, edit it in Grafana, export the JSON, and replace the file in `iot/config/grafana/provisioning/dashboards/json/`. If you rename the Drive dashboard's uid, update `grafana_path` in `.p4n4.json` too, and if you rename its file, `GF_DASHBOARDS_DEFAULT_HOME_DASHBOARD_PATH` in `iot/docker-compose.yml`. `scripts/validate.py` checks the first, and the smoke test both.

---

## p4n4-dashboard

`.p4n4.json` has the `dashboard` block that [p4n4-dashboard](https://github.com/raisga/p4n4-dashboard) reads through p4n4-api (`GET /api/v1/project`):

```json
"dashboard": {
  "grafana_path": "/d/p4n4-drive/drive",
  "tabs": ["map", "grafana", "video", "services"],
  "theme": "theme",
  "cameras": [
    {"id": "dashcam", "name": "Dashcam", "device": "back"},
    {"id": "copilot", "name": "Copilot", "device": "front"}
  ]
}
```

The cameras are the phone's own: `device` picks the back or the front one, and the app records them itself.

On the phone, p4n4-api is optional. Without it, the app takes its settings from the theme's defaults (`theme/brand.json`), and its Grafana tab opens Grafana's home page, the Drive dashboard:

| `defaults` | |
|---|---|
| `host` | `127.0.0.1`: the stack on the same phone |
| `tabOrder`, `powerTabs`, `normieTabs` | Map first; the normie view has Map, Grafana and Video |
| `cameras` | Dashcam (`device:back`) and Copilot (`device:front`) |
| `gpsPublish` | `true`: publish the phone's GPS fixes on `sensors/<id>/gps` |
| `obdRelay` | `false`: relay the Bluetooth adapter to TCP 35100, once there's one |
| `dashcamAutoStart` | `false`: start the dashcam's loop recording when the app opens |

`theme/` is a sample brand, `p4n4-drive` ("p4n4 Drive"): cyan accents, dark by default, Manrope and DM Mono, and `mqtt-influx-grafana`'s leaf icon as a placeholder. The Map tab is opt-in: the app shows it only to brands whose `tabs` list it, as this one does. Replace the brand with your own, then build the app from the project:

```bash
# in p4n4-dashboard
dart run tool/brand.dart install ~/projects/my-car --apply
flutter build apk      # or --split-per-abi: a Redmi 7 takes app-arm64-v8a-release.apk
```

---

## Configuration

`iot/.env`:

| Variable | Default | Purpose |
|---|---|---|
| `COMPOSE_PROFILES` | `demo` | `demo` starts gps-sim |
| `RESTART_POLICY` | `unless-stopped` | Every container's restart policy |
| `MQTT_USER` / `MQTT_PASSWORD` | *(empty: anonymous)* | Device login on 1883 and 9001; the app needs the same pair |
| `MQTT_READONLY_USER` / `MQTT_READONLY_PASSWORD` | *(empty)* | An optional read-only account |
| `INFLUXDB_USERNAME` / `INFLUXDB_PASSWORD` | `admin` / `adminpassword` | InfluxDB UI login |
| `INFLUXDB_ORG` | `ming` | InfluxDB organization |
| `INFLUXDB_TOKEN` | `p4n4-stack-token` | Admin token, used by Telegraf and Grafana |
| `INFLUXDB_BUCKET` / `INFLUXDB_RETENTION` | `raw_telemetry` / `30d` | The bucket and how long it keeps data, applied on first start only |
| `ARCHIVE_ROTATION_INTERVAL` | `24h` | Archive file rotation |
| `ARCHIVE_UID` / `ARCHIVE_GID` | `1000` | Owner of the archive files |
| `GRAFANA_USER` / `GRAFANA_PASSWORD` | `admin` / `adminpassword` | Grafana login |
| `GRAFANA_ANONYMOUS` | `false` | `true` lets anyone who reaches port 3000 view the dashboards without signing in (Viewer role) |
| `GRAFANA_ALLOW_EMBEDDING` | `false` | `true` lets other origins frame Grafana: the dashboard's web build needs it |
| `GRAFANA_SUB_PATH` | `/` | `/grafana/` when p4n4-dashboard proxies Grafana on its own origin |
| `GPS_DEVICE_ID` | `car` | gps-sim's device id |
| `GPS_INTERVAL` / `GPS_MAX_SPEED` | `1` / `50` | Seconds between fixes, and the top speed in km/h |
| `GPS_ROUTE` | *(empty: Monaco)* | Another closed route to lap: `lon,lat[,alt] lon,lat[,alt] ...` |
| `TZ` | `UTC` | Telegraf's time zone |

`extras/.env`:

| Variable | Default | Purpose |
|---|---|---|
| `COMPOSE_PROFILES` | `demo` | `demo` starts obd2-sim; obd2 and tiles always run |
| `RESTART_POLICY` | `unless-stopped` | Every container's restart policy |
| `OBD_DEVICE` | `socket://p4n4-obd2-sim:35000` | The adapter: `socket://host.docker.internal:35100` for the app's relay |
| `OBD_DEVICE_ID` | `car` | The device id in the topics |
| `OBD_BAUDRATE` / `OBD_PROTOCOL` | *(empty: detect)* | The adapter's serial speed, and the OBD-II protocol (`6`: CAN 11-bit, 500 kbit/s) |
| `OBD_POLL_FAST_HZ` / `OBD_POLL_SLOW_HZ` | `5` / `1` | Reads per second of rpm, speed and MAF, and of the rest |
| `OBD_VOLTAGE_INTERVAL` / `OBD_DTC_INTERVAL` | `5` / `30` | Seconds between battery voltage reads, and between trouble code reads |
| `OBD_PIDS` | *(empty: all the car supports)* | Only these measurements, comma-separated |
| `OBD_RAW` | `false` | Publish `sensors/<id>/raw` (Telegraf drops it) |
| `MQTT_HOST` / `MQTT_PORT` | `p4n4-mqtt` / `1883` | The broker's Docker-only listener |
| `MQTT_USER` / `MQTT_PASSWORD` | *(empty)* | Only for a listener that needs a login: see [If the bridge network doesn't work](#if-the-bridge-network-doesnt-work) |
| `OBD2_BIND` / `OBD2_PORT` | `127.0.0.1` / `8090` | Address and host port of obd2's API |
| `OBD2_LOG_LEVEL` | `INFO` | obd2's log level |
| `OBD_SIM_SCENARIO` | `warmup` | The simulator's scenario, from `extras/obd2/simulator/scenarios/` |
| `OBD_SIM_DROP_EVERY` | `0` | Close the simulator's connection every N seconds, like a pulled adapter |
| `MAP_PORT` | `8088` | The map server's host port |
| `TZ` | `UTC` | obd2's time zone |

InfluxDB's org, bucket, retention, and credentials are applied **only on first start**. Changing them later requires `docker compose down -v` in `iot/`, which deletes the stored data. The file-system archive survives this.

## Security and privacy

- **Footage stays on the phone.** The app records the cameras itself: no video reaches the stack, the broker or Grafana.
- **Where you've driven.** The GPS fixes are a location history. InfluxDB keeps them for `INFLUXDB_RETENTION` (30 days), and the archive in `iot/data/archive/` for its 30 rotations. `docker compose down -v` in `iot/` deletes InfluxDB's copy, with everything else in it; delete the archive's files yourself.
- **The car.** obd2 only reads, never publishes the VIN (`/api/v1/probe` shows a hash of it), and keeps its API on loopback.
- **Ports.** See [Keep the ports on the phone](#keep-the-ports-on-the-phone). If any stay open, set `MQTT_USER` and `MQTT_PASSWORD` in `iot/.env` (the app needs the same pair), recreate `mqtt`, and change every password and `INFLUXDB_TOKEN`. Optional `MQTT_READONLY_USER` / `MQTT_READONLY_PASSWORD` provide read-only access; the stack's own containers keep the private anonymous listener. See [MQTT device logins](../../README.md#mqtt-device-logins).
- **Grafana.** `GRAFANA_ANONYMOUS=true` makes every dashboard, the route included, readable by anyone who can reach port 3000. `GRAFANA_ALLOW_EMBEDDING=true` lets any site frame Grafana (clickjacking risk): enable it only for the dashboard's web build on a trusted network.

## Licences

- `extras/obd2/` is **GPL-2.0-only** ([its LICENSE](extras/obd2/LICENSE)), because the bridge uses [python-OBD](https://github.com/brendan-w/python-OBD), which is. Whoever distributes the `p4n4-obd2` image, or a project with that directory, distributes GPL-2.0 code: its source is all there, and the licence goes with it. [extras/obd2/README.md](extras/obd2/README.md) has the details.
- The rest of the template is under the repository's [MIT License](../../LICENSE).
- The offline map has its own licences: see [Offline map](#offline-map), and `ATTRIBUTION.txt` next to it.
- gps-sim's built-in route was traced from OpenStreetMap (© OpenStreetMap contributors, ODbL).
- The theme's fonts, Manrope and DM Mono, are under the SIL Open Font License 1.1 (`theme/fonts/`).

## Limitations

- **Not run on a phone yet.** It's been tested with Docker Desktop on a PC (x86-64), the two host-network setups included. Docker in Termux depends on the phone's kernel, and its networking varies.
- **64-bit only.** InfluxDB 2, and the Node image that `fetch-map.sh` uses, have no 32-bit ARM builds.
- **Fuel figures** assume petrol and a MAF sensor. **Distance** is the speedometer's (OBD speed), not the GPS's.
- **The Route panel** needs the internet for its basemap, OpenFreeMap's. The app's Map tab doesn't.
- **Trouble codes** are read every `OBD_DTC_INTERVAL` (30 s), so a new one can take that long to show.
- **The demo** doesn't tie the route to the car: gps-sim's speed isn't obd2-sim's. obd2-sim also keeps every request it gets in memory, about 100 MB a day at obd2's default rates: restart it now and then, or leave the demo off on the phone.

---

## Testing

```bash
./tests/smoke.sh          # both layers in a throwaway copy, checked end to end, then torn down
KEEP=1 ./tests/smoke.sh   # leave them running for inspection
```

It starts the two layers as separate Compose projects, the way `p4n4 up` does, with the demo route and a simulated car whose trouble codes are set from the start. Then it checks that:
- the car's readings, trouble codes and status, the route's GPS fixes and a fix in the app's format reach InfluxDB and the archive, and `sensors/<id>/raw` doesn't;
- the map server starts without a map, puts the client's address in a fixture `style.json`, and answers a range request with 206: nothing is downloaded;
- Grafana's datasource works, the Drive dashboard is its home page, and every panel has data (`tests/check_dashboards.py`).

The test stacks drop the fixed container names and host ports and use a network of their own, `p4n4-smoke-net`, so they run alongside any other stack.
