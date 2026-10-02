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
- An end-to-end smoke test (`tests/smoke.sh`) that CI runs on every change

Every template is **self-contained**: the directory is a runnable project on its own, with no CLI required.

> **Note:** `p4n4 template` CLI commands are not yet implemented. Templates can currently be applied manually by cloning this repo and copying the relevant directory as your project root.

---

## Available Templates

| Template | Layers | Description |
|----------|--------|-------------|
| [`mqtt-influx-grafana`](mqtt-influx-grafana) | iot | MQTT → Telegraf → InfluxDB + file-system archive → Grafana dashboard. The starting point for sensor telemetry |

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
├── .p4n4.json           # Manifest: schema_version 1, project, layers, template {name, version}
├── .env.example         # Environment variable template (no real secrets)
├── docker-compose.yml   # Stack orchestration (complete and runnable)
├── config/              # One directory per configured service (mosquitto/, telegraf/, grafana/, …)
├── scripts/             # Helpers mounted into containers (simulators, init scripts)
├── tests/smoke.sh       # End-to-end test, run by CI
└── README.md            # Template-specific usage guide and data contract
```

Registry tooling lives next to the templates:

```
schema/template.schema.json   # template.yaml schema
scripts/validate.py           # static checks for every template
docs/authoring.md             # conventions for writing a template
```

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
