# Authoring a template

This guide covers the conventions every template in this registry follows. [`mqtt-influx-grafana`](../projects/mqtt-influx-grafana) is the reference implementation, so copy it when in doubt.

## Principles

1. **Self-contained.** A template directory is a complete, runnable p4n4 project. `cp -r <template> my-project && docker compose up -d` must work with no CLI and no files from elsewhere. Some config is duplicated from `stacks/*` because of this. That is intentional, and the validator and smoke test keep each copy honest.
2. **Compatible with the base stacks.** Use the conventions of the layer you provide, so dashboards, queries, and other layers keep working:
   - The `p4n4-net` network (`172.20.0.0/16`), plus the stack's container names (`p4n4-mqtt`, `p4n4-influxdb`, …) and service hostnames (`mqtt`, `influxdb`).
   - The stack's env var names (`INFLUXDB_TOKEN`, `INFLUXDB_ORG`, `INFLUXDB_BUCKET`, `GRAFANA_USER`, …).
   - The MING data schema: topic `sensors/<device-id>/<measurement>`, measurement `sensor_data`, tags `device` and `sensor`.
3. **Pinned images.** Pin at least the minor version (`telegraf:1.40`, `influxdb:2.9`). Never use `latest`: `influxdb:latest` is now InfluxDB 3, which has no Flux.
4. **Works on first start.** Defaults in `.env.example` must produce a working stack, and any demo data should sit behind a Compose profile (`demo`) that `.env.example` enables.
5. **Tested end to end.** Every template ships a smoke test that proves data flows through every sink it promises.

## Layout

```
projects/<template-name>/
├── template.yaml        # registry metadata (schema/template.schema.json)
├── .p4n4.json           # project manifest: schema_version, project, layers, template
├── .env.example         # every variable docker-compose.yml uses, with safe defaults
├── .gitignore           # .env, generated data
├── docker-compose.yml
├── config/<service>/    # one directory per configured service
├── scripts/             # helpers mounted into containers (simulators, init scripts)
├── theme/               # optional p4n4-dashboard theme (see below)
├── tests/smoke.sh       # end-to-end test (see below)
├── data/                # host-side data, if any (gitignored except .gitkeep)
└── README.md
```

### Multi-layer templates

A template with more than one layer gives each layer its own directory, named after the layer, with no `docker-compose.yml` at the root. This is the layout `p4n4 init` creates for multi-layer projects (`p4n4_lib.layout`), and it is what keeps `p4n4 up`, `p4n4 down` and `p4n4 validate` working: a root compose file would make p4n4 run it as the whole project, and `p4n4 validate` expects `<layer>/docker-compose.yml` and `<layer>/.env`.

```
projects/<template-name>/
├── template.yaml        # services carry `layer:`
├── .p4n4.json           # "layers": ["iot", "ai"]
├── .gitignore
├── iot/
│   ├── docker-compose.yml   # creates p4n4-net
│   ├── .env.example
│   └── config/ …
├── ai/
│   ├── docker-compose.yml   # p4n4-net as an external network
│   ├── .env.example
│   └── config/ …
├── tests/smoke.sh
└── README.md
```

- Each layer is a separate Compose project. `p4n4 up` starts them in dependency order (iot, ai, edge) and `p4n4 down` stops them in reverse.
- The iot layer creates `p4n4-net`. The others declare it `external: true` with `name: p4n4-net`, like `stacks/ai`.
- Each layer has its own `.env.example`, documenting exactly the variables its compose file uses. A variable both layers need is repeated in both.
- Service names must be unique across layers, and so must host ports. `scripts/validate.py` checks both, since Compose can't when the layers are separate projects.
- The smoke test starts each layer as its own project, iot first, and points the other layers' override at the iot layer's renamed network. [`mqtt-influx-grafana-ollama`](../projects/mqtt-influx-grafana-ollama) is the reference.

## `template.yaml`

This is the registry's source of truth. It is what `p4n4 template search/install` will read, and [`schema/template.schema.json`](../schema/template.schema.json) defines it.

| Key | Rule |
|---|---|
| `name` | Must match the directory name (kebab-case) |
| `version` | Semver. Bump it with every change, and keep `.p4n4.json` → `template.version` in sync |
| `layers` | Must equal `.p4n4.json` → `layers` |
| `services` | One entry per Compose service. `ports` lists the **host** ports it publishes, `profile` names its Compose profile, and in a multi-layer template `layer` names the layer whose compose file defines it |
| `data` | Free-form data contract (topics, buckets, files). Shown to users; not validated |
| `smoke_test` | Path to the end-to-end test |

## `.p4n4.json`

This is a normal p4n4 project manifest with an extra `template` key, so an installed project remembers where it came from:

```json
{
  "schema_version": 1,
  "project": "<template-name>",
  "layers": ["iot"],
  "template": { "name": "<template-name>", "version": "0.1.0" },
  "dashboard": {
    "grafana_path": "/d/<uid>/<slug>",
    "tabs": ["services", "grafana", "video"],
    "theme": "theme",
    "cameras": [{ "id": "floor", "name": "Sales floor", "port": 1984, "path": "/api/stream.mjpeg?src=floor" }]
  }
}
```

Because of the `template` key, `p4n4 validate` checks only each layer's `docker-compose.yml` and that its `.env` sets every variable in its `.env.example`, not the base stack's file list. Your template doesn't need Node-RED files just because it provides the `iot` layer.

`dashboard` is optional. It tells p4n4-dashboard how to present the project: `grafana_path` is the Grafana page its Grafana tab opens, and `tabs` lists the tabs the project can serve (`services`, `edge`, `agent`, `grafana`, `video`). `scripts/validate.py` checks the tab names, and checks that `grafana_path` points at the uid of a provisioned dashboard. Listing `grafana` requires a `grafana_path`.

`theme` names a directory in the template that holds a p4n4-dashboard theme (see *Themes*).

`cameras` lists the streams the Video tab shows until a user saves their own. Each has an `id` (lowercase letters, digits and dashes) and a `name`, plus either an absolute http(s) `url` or a `port` and `path` on the host the dashboard is connected to. The manifest can't know that host, so a template's own streams use `port`. The dashboard plays MJPEG and still images; [go2rtc](https://github.com/AlexxIT/go2rtc) turns RTSP cameras into MJPEG at `/api/stream.mjpeg?src=<name>`. `scripts/validate.py` checks each camera, and that a `port` is published by one of the template's services. Listing `video` requires `cameras`. [`mqtt-influx-grafana-ollama-go2rtc`](../projects/mqtt-influx-grafana-ollama-go2rtc) is the reference.

## Themes

A template aimed at a use case can ship a sample white-label theme, so a project made from it carries its own dashboard branding. Users replace it with their client's. The format is p4n4-dashboard's brand folder:

```
theme/
├── brand.json     # schema/theme.schema.json
├── icon.png       # optional 1024×1024 launcher icon
├── logo.png       # optional; only if brand.json sets "logo"
└── fonts/         # <Family>-<Weight>.ttf and <Family>-LICENSE.txt for the display and mono families
```

- **Self-contained.** Ship the fonts and their licenses (Google Fonts families are OFL, Apache or UFL, all redistributable), so the dashboard builds offline. In p4n4-dashboard, `dart run tool/brand.dart fonts <path-to-theme>` downloads them into the theme.
- **Don't repeat the manifest.** Leave the Grafana page to `.p4n4.json` `dashboard.grafana_path` rather than `defaults.grafanaPath`. The dashboard reads it from the project through p4n4-api.
- **Ids.** Use lowercase letters, digits and dashes. `p4n4` is reserved for the dashboard's built-in brand. A project's theme is installed as `brands/<id>/` in the dashboard, so ids should be unique per client.
- **Checks.** `scripts/validate.py` validates `brand.json` against the schema and checks that the logo and fonts exist. Run `dart run tool/brand.dart check <path-to-theme>` in p4n4-dashboard for color contrast (WCAG AA).

## Smoke test contract

`tests/smoke.sh` runs from any directory. It must:

- work on a **temporary copy** of the template and never write into the repo;
- create `.env` from `.env.example`, and disable demo profiles so its data is deterministic;
- isolate itself with a `docker-compose.override.yml` in the copy that resets `container_name`, `ports`, and the network name (`!reset`), so it runs next to other stacks and on CI;
- start the stack, push data in, and assert that every sink received it;
- run every provisioned dashboard query and expect data (see `projects/mqtt-influx-grafana/tests/check_dashboards.py`). An empty panel is a bug;
- tear down containers **and volumes** on exit, pass or fail (`KEEP=1` leaves them running);
- exit `0` on success.

CI runs every template's smoke test on each PR.

## Gotchas found so far

- **Telegraf ≥ 1.38 only substitutes `${VAR}` into string settings.** Integer and boolean settings (`rotation_max_archives`) must be literal in the config.
- **Telegraf's `/var/lib/telegraf` is `0770 root:telegraf`.** If you run the container as the host user (so bind-mounted files are yours), mount data outside it, for example at `/archive`.
- **Flux `map()` with a fresh record drops group columns**, which merges every series into one table. Use `keep()` and name series in Grafana with `displayName: ${__field.labels.<tag>}`.

## Checklist

```bash
uv run scripts/validate.py <template-name>     # static checks
projects/<template-name>/tests/smoke.sh        # end-to-end (needs Docker)
```

Then add the template to the table in the root [README](../README.md).
