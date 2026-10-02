# Authoring a template

This guide covers the conventions every template in this registry follows. [`mqtt-influx-grafana`](../mqtt-influx-grafana) is the reference implementation, so copy it when in doubt.

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
<template-name>/
├── template.yaml        # registry metadata (schema/template.schema.json)
├── .p4n4.json           # project manifest: schema_version, project, layers, template
├── .env.example         # every variable docker-compose.yml uses, with safe defaults
├── .gitignore           # .env, generated data
├── docker-compose.yml
├── config/<service>/    # one directory per configured service
├── scripts/             # helpers mounted into containers (simulators, init scripts)
├── tests/smoke.sh       # end-to-end test (see below)
├── data/                # host-side data, if any (gitignored except .gitkeep)
└── README.md
```

## `template.yaml`

This is the registry's source of truth. It is what `p4n4 template search/install` will read, and [`schema/template.schema.json`](../schema/template.schema.json) defines it.

| Key | Rule |
|---|---|
| `name` | Must match the directory name (kebab-case) |
| `version` | Semver. Bump it with every change, and keep `.p4n4.json` → `template.version` in sync |
| `layers` | Must equal `.p4n4.json` → `layers` |
| `services` | One entry per Compose service. `ports` lists the **host** ports it publishes, and `profile` names its Compose profile |
| `data` | Free-form data contract (topics, buckets, files). Shown to users; not validated |
| `smoke_test` | Path to the end-to-end test |

## `.p4n4.json`

This is a normal p4n4 project manifest with an extra `template` key, so an installed project remembers where it came from:

```json
{
  "schema_version": 1,
  "project": "<template-name>",
  "layers": ["iot"],
  "template": { "name": "<template-name>", "version": "0.1.0" }
}
```

## Smoke test contract

`tests/smoke.sh` runs from any directory. It must:

- work on a **temporary copy** of the template and never write into the repo;
- create `.env` from `.env.example`, and disable demo profiles so its data is deterministic;
- isolate itself with a `docker-compose.override.yml` in the copy that resets `container_name`, `ports`, and the network name (`!reset`), so it runs next to other stacks and on CI;
- start the stack, push data in, and assert that every sink received it;
- run every provisioned dashboard query and expect data (see `mqtt-influx-grafana/tests/check_dashboards.py`). An empty panel is a bug;
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
<template-name>/tests/smoke.sh                 # end-to-end (needs Docker)
```

Then add the template to the table in the root [README](../README.md).
