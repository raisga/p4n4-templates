# Use case: greenhouse telemetry with a white-label dashboard

A grower runs a few greenhouses. Each one has temperature and humidity sensors that
publish over MQTT. The grower wants two things:

- Their staff watch live readings on a phone, tablet or wall screen, in an app with the
  grower's own name and colors. No ports, URLs or p4n4 branding.
- Their integrator (you) manages the deployment from the same app, and from other
  clients' deployments too.

This guide builds that with the [`mqtt-influx-grafana`](../../projects/mqtt-influx-grafana)
template, [p4n4-api](https://github.com/raisga/p4n4-docs/blob/main/reference/api.md) and a white-labelled
[p4n4-dashboard](https://github.com/raisga/p4n4-docs/blob/main/reference/dashboard.md). Each piece already exists. What ties them
together is the project manifest, `.p4n4.json`: its `dashboard` block tells the dashboard
which Grafana dashboard to open and which tabs make sense for the project.

```
 sensors ──MQTT──► mqtt ──► telegraf ──┬──► influxdb ──► grafana ◄────────────┐
 (sensors/<device>/<measurement>)      └──► ./data/archive                     │ Grafana tab
                                                                               │ (kiosk, anonymous viewer)
 .p4n4.json ──► p4n4-api ── GET /api/v1/project ──► dashboard ("verdant" brand)
                         ── GET /api/v1/stacks  ──►   Services / Home status
                         ── GET /api/v1/edge/metrics ► Edge tab
```

| Piece | Repo | Role here |
|---|---|---|
| `mqtt-influx-grafana` template | `p4n4-templates` | Mosquitto → Telegraf → InfluxDB + file archive → Grafana, with a provisioned *Telemetry* dashboard |
| `.p4n4.json` `dashboard` block | `p4n4-lib` (validated), `p4n4-templates` (declared) | Grafana page to open, tabs the project serves, and where its theme is |
| p4n4-api | `p4n4-api` | Serves the manifest, service status and host metrics to the dashboard |
| `theme/` (sample: `verdant`) | the project (`p4n4-templates`) | The white-label theme, installed into the dashboard at build time |
| p4n4-dashboard | `p4n4-dashboard` | The app staff and admins use; ships only the `p4n4` brand |

---

## 1. Create the project from the template

`p4n4 template install` isn't implemented yet, so copy the template:

```bash
cp -r tools/templates/projects/mqtt-influx-grafana ~/projects/greenhouse
cd ~/projects/greenhouse
cp .env.example .env
```

Edit `.env`:

| Variable | Set it to |
|---|---|
| `INFLUXDB_PASSWORD`, `INFLUXDB_TOKEN`, `GRAFANA_PASSWORD` | Your own secrets |
| `ARCHIVE_UID` / `ARCHIVE_GID` | `id -u` / `id -g` |
| `GRAFANA_ANONYMOUS` | `true`, so staff see dashboards without a Grafana login (Viewer role, read-only) |
| `GRAFANA_ALLOW_EMBEDDING` | `true` if staff use the dashboard in a browser (it frames Grafana) |
| `COMPOSE_PROFILES` | Keep `demo` until real sensors publish; then clear it |

Rename the project in `.p4n4.json` (`"project": "greenhouse"`) and leave the `template` and
`dashboard` blocks as they are:

```json
{
  "schema_version": 1,
  "project": "greenhouse",
  "layers": ["iot"],
  "template": { "name": "mqtt-influx-grafana", "version": "0.2.0" },
  "dashboard": {
    "grafana_path": "/d/p4n4-telemetry/telemetry",
    "tabs": ["services", "edge", "grafana"],
    "theme": "theme"
  }
}
```

Start it and check it:

```bash
docker compose up -d
p4n4 validate        # .p4n4.json, dashboard settings and theme, compose file and .env
```

For a project created from a template, `p4n4 validate` checks the compose file and the
`.env` variables that the template's `.env.example` documents. It doesn't check the base
IoT stack's file list (Node-RED flows, `init-buckets.sh`), because the template replaces
that stack. The template's own validator and smoke test cover its files.

Open Grafana at `http://<host>:3000`. The **Telemetry** dashboard is the home page, and the
simulator fills it within a few seconds.

## 2. Connect the sensors

Sensors publish to `sensors/<device-id>/<measurement>`, with a JSON object or a bare number:

```bash
mosquitto_pub -h <host> -t sensors/house-1/temperature -m '{"value": 23.4, "unit": "C"}'
mosquitto_pub -h <host> -t sensors/house-1/humidity    -m '61.2'
```

Every reading lands in InfluxDB (`sensor_data`, tags `device` and `sensor`) and in
`data/archive/`. The dashboard's *Device* and *Sensor* variables pick up new devices on
their own. The template [README](../../projects/mqtt-influx-grafana/README.md#data-contract)
has the full data contract.

Once real sensors publish, set `COMPOSE_PROFILES=` in `.env` and run
`docker compose up -d --remove-orphans` to stop the simulator.

## 3. Serve the project with p4n4-api

The dashboard reads the manifest, service status and host metrics from p4n4-api:

```bash
cd api
uv venv && uv pip install -e ../lib -e .
export P4N4_PROJECT_DIR=~/projects/greenhouse
uv run p4n4-api users add integrator --role admin     # you: the admin view
uv run p4n4-api users add staff --role normie         # the grower's staff: the normie view
uv run p4n4-api
```

The dashboard signs in with these accounts. Admins get the admin view and normies the
simplified normie view (an `operator` account would get the power view); the role comes
from the account, not from a picker.

## 4. Build the white-label dashboard

The branding lives in the project. `theme/` holds the dashboard theme, and `.p4n4.json`
points at it with `"dashboard": {"theme": "theme"}`. The template ships `verdant`, a sample
greenhouse brand. Replace it with the grower's brand:

```
theme/
├── brand.json     # name, wordmark, colors, fonts, tabs, defaults, app IDs
├── icon.png       # 1024×1024 launcher icon
└── fonts/         # Manrope + DM Mono and their licenses, so builds work offline
```

```jsonc
{
  "id": "verdant",                            // unique per client; "p4n4" is reserved
  "appName": "Verdant Grow Console",
  "wordmark": { "text": "verdant", "suffix": ".grow" },
  "platform": "verdant",                      // verdant-iot, verdant-api
  "fonts": { "display": "Manrope", "mono": "DM Mono" },
  "colors": { "light": { "accent": "#3F6212", … }, "dark": { "accent": "#A3E635", … } },
  "tabs": ["services", "edge", "grafana"],    // no Agent or Video in this build
  "defaults": { "host": "localhost", "clientTabs": "grafana", "themeMode": "light" },  // staff see Home + Grafana
  "native": { "displayName": "Verdant Grow", "applicationId": "com.example.verdant.grow", … }
}
```

The Grafana page isn't repeated in the theme: the dashboard gets it from the project's
`dashboard.grafana_path`.

p4n4-dashboard commits only its default `p4n4` brand. Install the project's theme into it,
then build:

```bash
cd dashboard
dart run tool/brand.dart check ~/projects/greenhouse              # schema, IDs and WCAG AA contrast
dart run tool/brand.dart install ~/projects/greenhouse --apply    # copies theme/ to brands/verdant/ (gitignored), bundles it, patches native projects, regenerates icons
flutter build apk --split-per-abi                                 # or: linux, windows, macos, ios
dart run tool/brand.dart apply p4n4                               # back to the default before committing
```

After changing a color or font, run `dart run tool/brand.dart fonts ~/projects/greenhouse/theme`
to fetch the new fonts into the project, then install again. Only the applied brand is
bundled, so the grower's build contains nothing from other clients. In the template
registry, `uv run scripts/validate.py` checks the theme against `schema/theme.schema.json`.

For staff who'd rather open a browser than install an app, build the web version with the
same theme and serve it from the edge device:

```bash
flutter build web --release --no-web-resources-cdn
cd build/web && python3 -m http.server 8088      # or nginx, Caddy, …
```

Start p4n4-api with `P4N4_API_CORS_ORIGINS=http://<host>:8088` so the browser may call it (or use the dashboard container, which proxies it).
The web build defaults to the host it was served from, so `http://<host>:8088` works from any
device on the LAN.

## 5. Use it

Set the host in ⚙ (or rely on the brand default). Use `10.0.2.2` from the Android
emulator. On connect, the dashboard reads `GET /api/v1/project`:

| Manifest | Effect in the dashboard |
|---|---|
| `layers: ["iot"]` | Services and Home show only the IoT stack (and the API), not empty AI and Edge sections |
| `dashboard.tabs` | Tabs outside the list are hidden while connected (Agent and Video, if the brand had them) |
| `dashboard.grafana_path` | The Grafana tab opens `/d/p4n4-telemetry/telemetry?kiosk=1`, unless the admin set a path |

Without the API, or with an older manifest, the dashboard keeps today's behavior: every
brand tab and stack, and the brand's Grafana path.

**Admin (integrator).** Services shows live status from p4n4-api for Grafana, InfluxDB and
MQTT. Node-RED shows *unknown*, since the template has no Node-RED. Edge shows the host's
CPU, memory, temperature, disk and load. Clients lists every grower's deployment, and
**Connect** switches to one. The project info reloads with it.

**Client (grower's staff).** Home shows overall health and the edge readings. Grafana shows
the telemetry dashboard in kiosk mode, readable without a login because of
`GRAFANA_ANONYMOUS=true`. On Android, iOS and macOS it's embedded. On Linux and Windows,
Flutter has no web view, so the tab opens it in the browser.

## Checks

These were run against the template stack, p4n4-api (auth off) and the `verdant` build on
Linux:

- [x] `tests/smoke.sh` passes, including a check that `dashboard.grafana_path` is a provisioned dashboard
- [x] `uv run scripts/validate.py` in p4n4-templates checks the `dashboard` block (tab names; `grafana_path` uid exists)
- [x] `p4n4 validate` passes on the project (it used to fail on missing Node-RED files)
- [x] `GET /api/v1/project` returns the `template` and `dashboard` blocks
- [x] Grafana's `/api/dashboards/uid/p4n4-telemetry` answers `200` without credentials when `GRAFANA_ANONYMOUS=true`; the kiosk page renders live data
- [x] `tool/brand.dart install <project> --apply` installs `theme/` and the test suite passes with it applied; `apply p4n4` restores the committed files exactly
- [x] Dashboard (Linux, `verdant`): the admin sees Services, Edge, Grafana and Clients; only the IoT and API stacks appear, with live status; Edge shows API metrics; Grafana opens the project's dashboard in kiosk mode
- [x] Dashboard sign-in against p4n4-api with auth on (Linux): an `admin` account gets the admin view, an `operator` the client view; it stays signed in across restarts, and signing out revokes the session
- [x] Web build (`verdant`, Chromium): client Home with live status and edge readings, Grafana embedded in an iframe, no requests outside the LAN
- [ ] The Grafana web view on Android, iOS and macOS
- [ ] A phone on the LAN against a Raspberry Pi running the stack

## Limitations and next steps

- **Accounts are created on the server.** `p4n4-api users add` runs in the API's shell;
  there's no user management in the dashboard yet.
- **Anonymous Grafana is all-or-nothing.** Anyone who reaches port 3000 can view every
  dashboard. For several clients on one Grafana, use organizations or a reverse proxy
  with auth instead.
- **Node-RED shows *unknown*.** The service catalog is fixed. A manifest-driven catalog
  (a `dashboard.services` list, or the API reporting the services each stack defines)
  would hide it. Hiding services the API doesn't list isn't safe today, because
  `docker compose ps` leaves out stopped containers, so a stopped service would vanish
  instead of showing as down.
- **Themes apply at build time.** The running app can't switch themes, so each client gets
  its own build. When the dashboard ships as a container image
  (`dashboard/SERVICE_INTEGRATION.md`), the image build can install the project's
  `theme/` the same way.
- **One Grafana look.** Grafana OSS can't take the theme's logo or colors (that's Grafana
  Enterprise). Kiosk mode hides most of its chrome.
- **Telegraf has no healthcheck.** p4n4-api reports it as `running` even if it can't
  write to InfluxDB. The smoke test catches that in CI, but nothing does at runtime.

**Next:** [greenhouse assistant](greenhouse-assistant.md) adds a local LLM to the same
project, so staff can ask about their readings in plain language.
