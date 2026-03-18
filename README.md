# p4n4-templates

> Community template registry for the **P4N4** platform.

Pre-built project configurations for common IoT + GenAI + Edge AI deployment patterns. Templates are applied via `p4n4 template apply <name>` and scaffold a complete project directory with pre-configured stacks, flows, and dashboards.

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

- A `.p4n4.json` manifest declaring the active layers
- Pre-configured `config/` files (Mosquitto, Node-RED flows, Grafana dashboards)
- A `.env.example` with use-case-specific variable names and defaults
- A `README.md` describing the template and its expected sensor data format

> **Note:** `p4n4 template` CLI commands are not yet implemented. Templates can currently be applied manually by cloning this repo and copying the relevant directory as your project root.

---

## Available Templates

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

# Apply a template to a new project
p4n4 template apply factory-baseline
```

### Manually

```bash
# Clone the registry
git clone https://github.com/raisga/p4n4-templates.git

# Copy the template as your project directory
cp -r p4n4-templates/factory-baseline my-project
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
├── .p4n4.json           # Manifest — project name, active layers, schema_version: 1
├── .env.example         # Environment variable template (no real secrets)
├── docker-compose.yml   # Stack orchestration
├── config/
│   ├── mosquitto/       # MQTT broker config
│   ├── node-red/        # Flows and settings
│   └── grafana/         # Provisioned datasources and dashboards
├── scripts/
│   └── init-buckets.sh  # InfluxDB bucket initialization
└── README.md            # Template-specific usage guide
```

---

## Contributing

1. Fork this repository
2. Create a directory for your template following the structure above
3. Add a `README.md` describing the use case, required hardware, and data format
4. Open a pull request

Template guidelines:
- Never include `.env` files with real secrets — only `.env.example`
- Keep flows and dashboards self-contained (no external dependencies)
- Test with `p4n4 validate` before submitting

---

## Resources

- [p4n4 Platform](https://github.com/raisga/p4n4) — umbrella repo and architecture docs
- [p4n4-cli](https://github.com/raisga/p4n4-cli) — CLI reference (`p4n4 template` commands)
- [p4n4-docs](https://github.com/raisga/p4n4-docs) — full documentation site

---

## License

This project is licensed under the [MIT License](LICENSE).
