# p4n4-templates

> Community template registry for the **P4N4** platform.

Pre-built project configurations for common IoT + GenAI + Edge AI deployment patterns. Templates are installed via `p4n4 template install <name>` and scaffold a complete project directory with pre-configured stacks, flows, and dashboards.

Part of the [p4n4](https://github.com/raisga/p4n4) platform — an EdgeAI + GenAI integration platform for IoT deployments.

---

## Table of Contents

- [Overview](#overview)
- [Available Templates](#available-templates)
- [Using Templates](#using-templates)
- [Template Structure](#template-structure)
- [Contributing](#contributing)
- [Resources](#resources)
- [License](#license)

---

## Overview

Templates extend `p4n4 init` with pre-configured project layouts tailored to specific use cases. Each template bundles:

- A `template.yaml` with registry metadata (layers, services, ports, data contract)
- A `.p4n4.json` manifest declaring the active layers and the template it came from
- A complete `docker-compose.yml` and pre-configured `config/` files (Mosquitto, Telegraf, Grafana dashboards, …)
- A `.env.example` with use-case-specific variable names and defaults
- A `README.md` describing the template and its expected sensor data format
- Optionally, a `theme/`: a white-label theme for p4n4-dashboard, so the project carries its own branding
- An end-to-end smoke test (`tests/smoke.sh`) that CI runs on every change

Every template is **self-contained**: the directory is a runnable project on its own, with no CLI required.

> **Note:** `p4n4 template` CLI commands are not yet implemented. Templates can currently be applied manually by cloning this repo and copying the relevant directory as your project root.

---

## Available Templates

| Template | Layers | Description |
|----------|--------|-------------|
| [`mqtt-influx-grafana`](mqtt-influx-grafana) | iot | MQTT → Telegraf → InfluxDB + file-system archive → Grafana dashboard. The starting point for sensor telemetry |
| [`mqtt-influx-grafana-ollama`](mqtt-influx-grafana-ollama) | iot + ai | The same pipeline as an iot layer, plus an ai layer: a local LLM (Gemma 4 E2B on Ollama) for p4n4-dashboard's Agent tab that queries InfluxDB through tool calls |
| [`retail-vision`](retail-vision) | iot + ai + edge | Camera analytics for a clothing store: an ingest service for the AIOROS Alpha Boutique device ↔ web contract (SQLite + InfluxDB + MQTT), a Grafana store dashboard, an agent that answers with deterministic numbers, and go2rtc streams for p4n4-dashboard's Video tab. Demo store and synthetic video included |

### Planned

| Template | Layers | Description |
|----------|--------|-------------|
| `factory-baseline` | iot + ai + edge | Full stack for discrete manufacturing — vibration anomaly detection, alert enrichment, incident escalation |
| `iot-minimal` | iot | Minimal MING stack for sensor monitoring with no AI layer |

---

## Using Templates

### Via CLI (coming soon)

```bash
# Search available templates
p4n4 template search

# Install a template into a new project
p4n4 template install mqtt-influx-grafana
```

### Manually

```bash
# Clone the registry
git clone https://github.com/raisga/p4n4-templates.git

# Copy the template as your project directory
cp -r p4n4-templates/mqtt-influx-grafana my-project
cd my-project

# Generate secrets
cp .env.example .env
# Edit .env to change passwords and tokens

# Start the stack
docker compose up -d
```

---

## Template Structure

Each template is a directory at the root of this repository:

```
<template-name>/
├── template.yaml        # Registry metadata, validated against schema/template.schema.json
├── .p4n4.json           # Manifest: schema_version 1, project, layers, template {name, version}, dashboard {…}
├── .env.example         # Environment variable template (no real secrets)
├── docker-compose.yml   # Stack orchestration (complete and runnable)
├── config/              # One directory per configured service (mosquitto/, telegraf/, grafana/, …)
├── scripts/             # Helpers mounted into containers (simulators, init scripts)
├── theme/               # Optional p4n4-dashboard theme (brand.json, icon.png, fonts/), named by .p4n4.json dashboard.theme
├── tests/smoke.sh       # End-to-end test, run by CI
└── README.md            # Template-specific usage guide and data contract
```

A multi-layer template keeps `docker-compose.yml`, `.env.example` and the layer's files in one directory per layer (`iot/`, `ai/`, …) instead of the root, as `p4n4 init` lays out multi-layer projects. See [docs/authoring.md](docs/authoring.md#multi-layer-templates).

Registry tooling lives next to the templates:

```
schema/template.schema.json   # template.yaml schema
schema/theme.schema.json      # theme/brand.json schema (p4n4-dashboard's brand format)
scripts/validate.py           # static checks for every template
docs/authoring.md             # conventions for writing a template
```

### Themes

A theme is a white-label brand for [p4n4-dashboard](https://github.com/raisga/p4n4-dashboard): its name, wordmark or logo, colors, fonts, tabs, first-run defaults, app IDs and launcher icon. The dashboard repository commits only the default `p4n4` brand. A client's theme belongs to the client's project, so a template that targets a use case can ship a sample theme for its users to replace:

```
theme/
├── brand.json     # schema/theme.schema.json
├── icon.png       # 1024×1024 launcher icon (optional)
├── logo.png       # app-bar logo, if brand.json sets "logo" (optional)
└── fonts/         # <Family>-<Weight>.ttf + <Family>-LICENSE.txt, so builds work offline
```

`.p4n4.json` points at it with `"dashboard": {"theme": "theme"}`, and the dashboard installs it from the project:

```bash
# in p4n4-dashboard
dart run tool/brand.dart install ~/projects/greenhouse --apply
```

`scripts/validate.py` checks the theme against the schema and checks that its fonts are present. Contrast (WCAG AA) is checked by `dart run tool/brand.dart check <theme-dir>` in the dashboard. Projects made from a template edit or replace `theme/` with their client's own brand. The `id` must be unique per client, and `p4n4` is reserved.

---

## Contributing

1. Fork this repository
2. Read [docs/authoring.md](docs/authoring.md) and copy [`mqtt-influx-grafana`](mqtt-influx-grafana) as a starting point
3. Add a `README.md` describing the use case, required hardware, and data format
4. Run the checks, then open a pull request:

```bash
uv run scripts/validate.py <template-name>   # static checks
<template-name>/tests/smoke.sh               # end-to-end (needs Docker)
```

Template guidelines:
- Never include `.env` files with real secrets — only `.env.example`
- Keep flows and dashboards self-contained (no external dependencies)
- Pin image versions (at least the minor version, never `latest`)

---

## Resources

- [p4n4 Platform](https://github.com/raisga/p4n4) — umbrella repo and architecture docs
- [p4n4-cli](https://github.com/raisga/p4n4-cli) — CLI reference (`p4n4 template` commands)
- [p4n4-docs](https://github.com/raisga/p4n4-docs) — full documentation site

---

## License

This project is licensed under the [MIT License](LICENSE).
