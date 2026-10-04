# mqtt-influx-grafana-ollama

> The [`mqtt-influx-grafana`](https://github.com/raisga/p4n4-templates/tree/main/mqtt-influx-grafana) telemetry pipeline as an **iot** layer, plus an **ai** layer running a local LLM (Gemma 4 E2B on **Ollama**) that you chat with from p4n4-dashboard's Agent tab. The model answers questions about your readings by querying **InfluxDB** through tool calls.

| | |
|---|---|
| Layers | `iot`, `ai` |
| Services | iot: Mosquitto · Telegraf · InfluxDB · Grafana (+ demo simulator) · ai: agent · Ollama |
| Version | 0.2.0 |

```
  iot/  devices ──MQTT──► mqtt ──► telegraf ──┬──► influxdb ──► grafana
                          :1883               │      :8086       :3000
                                              └──► iot/data/archive/

  ai/   p4n4-dashboard Agent tab ──► agent ──┬──► ollama (gemma4:e2b)
                                     :11434   └──► influxdb (tool calls, read only)
        └───────────── both on p4n4-net, which the iot layer creates ─────────────┘
```

---

## Layout

Each layer is its own Compose project in its own directory, the layout `p4n4 init` gives multi-layer projects. That keeps `p4n4 up`, `p4n4 down`, `p4n4 status` and `p4n4 validate` working, and lets you restart or replace one layer without touching the other.

```
.p4n4.json                       manifest: layers [iot, ai], template, dashboard
iot/docker-compose.yml           MQTT → Telegraf → InfluxDB + archive → Grafana; creates p4n4-net
iot/.env.example                 InfluxDB, Grafana, archive, MQTT bridge, simulator settings
iot/config/                      mosquitto/, telegraf/, grafana/ (as in mqtt-influx-grafana)
iot/scripts/simulate.sh          demo publisher
iot/data/archive/                file-system archive (gitignored)
ai/docker-compose.yml            agent + Ollama; joins p4n4-net as an external network
ai/.env.example                  model, agent and InfluxDB settings (match iot/.env)
ai/agent/agent.py                Ollama API with InfluxDB tools (Python standard library)
ai/agent/local_tools.py          optional: the project's own tools (see Project tools)
ai/config/ollama/entrypoint.sh   starts Ollama, pulls OLLAMA_MODEL on first start
tests/smoke.sh                   end-to-end test of both layers
tests/check_agent.py             agent checks the smoke test runs, against...
tests/fake_ollama.py             ...a scripted stand-in model, so CI downloads none
```

The iot layer must be up before the ai layer, because the ai layer joins the `p4n4-net` network the iot layer creates. `p4n4 up` starts them in that order and `p4n4 down` stops them in reverse.

---

## Quick start

With the CLI:

```bash
cp -r mqtt-influx-grafana-ollama my-project && cd my-project
cp iot/.env.example iot/.env    # change passwords and the token
cp ai/.env.example ai/.env      # INFLUXDB_* must match iot/.env
p4n4 validate
p4n4 up                         # iot, then ai
```

Without it:

```bash
(cd iot && docker compose up -d) && (cd ai && docker compose up -d)
```

Then open Grafana at <http://localhost:3000> (default login `admin` / `adminpassword`). The **Telemetry** dashboard is the home page, and the `demo` profile in `iot/.env.example` starts a simulator that fills it with readings from `dev-01` and `dev-02`.

On first start Ollama downloads `gemma4:e2b` (about 4.6 GB) in the background. Follow the download with `docker compose logs -f ollama` in `ai/` (or `p4n4 logs ai`) until it prints `model gemma4:e2b is ready`. Then ask from the [dashboard](#p4n4-dashboard), or from the terminal through the agent's Ollama API:

```bash
curl -s localhost:11434/api/chat -d '{"model": "gemma4:e2b", "stream": false,
  "messages": [{"role": "user", "content": "Which device is warmer right now?"}]}' | jq -r .message.content
```

When real devices are publishing, set `COMPOSE_PROFILES=` in `iot/.env` and run `docker compose up -d --remove-orphans` in `iot/`. On Linux, set `ARCHIVE_UID` / `ARCHIVE_GID` in `iot/.env` to the output of `id -u` / `id -g`.

Don't run this project alongside another p4n4 iot or ai stack (`stacks/iot`, `stacks/ai`, `mqtt-influx-grafana`): they use the same container names, ports and `p4n4-net` network.

---

## iot layer

The iot layer is a copy of `mqtt-influx-grafana`. That template's README covers the [data contract](https://github.com/raisga/p4n4-templates/tree/main/mqtt-influx-grafana#data-contract), the [file-system archive](https://github.com/raisga/p4n4-templates/tree/main/mqtt-influx-grafana#file-system-archive) and the [external MQTT broker bridge](https://github.com/raisga/p4n4-templates/tree/main/mqtt-influx-grafana#external-mqtt-broker); paths there are relative to `iot/` here. In short:

```bash
mosquitto_pub -t sensors/greenhouse-1/temperature -m '{"value": 23.4, "unit": "C"}'
mosquitto_pub -t sensors/greenhouse-1/humidity    -m '51.2'
```

```
bucket       raw_telemetry   (INFLUXDB_BUCKET, retention INFLUXDB_RETENTION=30d)
measurement  sensor_data
tags         device=<device-id>, sensor=<measurement>
fields       value, unit, ...
```

## ai layer

### Agent

`agent` serves the Ollama API on port 11434, under Ollama's container name `p4n4-ollama`, so p4n4-dashboard uses it with no configuration. On `POST /api/chat` it adds a system prompt and three tools, runs the tool calls the model makes against InfluxDB, and streams the answer back. Every other request (`/api/tags`, `/api/pull`, …) goes to Ollama unchanged.

| Tool | Returns |
|---|---|
| `list_sensors(range, device?)` | Latest value, unit and time of every sensor on every device (or one device). The model uses it for current values and to learn the ids |
| `get_stats(sensor, device?, range)` | Minimum, maximum, mean, number of readings, first and last value, per device |
| `get_history(sensor, device?, range, points?)` | Values averaged into up to 48 evenly spaced points, per device, for trends |

The model never writes Flux. The tools build the queries from checked arguments: ids must be MQTT topic levels, and `range` must be like `15m`, `24h` or `7d`. Results are capped at 100 rows. The InfluxDB token stays in the agent container. The agent logs each tool call: `docker compose logs -f agent` in `ai/`.

```
tool list_sensors {} -> 4 rows in 0.01s
tool get_stats {"device": "dev-02", "range": "10m", "sensor": "humidity"} -> 1 rows in 0.01s
```

The model only knows what the tools return: readings of the iot layer's `sensor_data` measurement. It doesn't see Grafana, the archive or the MQTT broker. When it answers a question without the tools, it says so or asks.

### Project tools

A project adds its own tools in `ai/agent/local_tools.py`, which the agent loads at startup, and keeps `agent.py` as the template ships it, so template updates to `agent.py` copy over unchanged. The file defines `tools(agent)`: it gets the agent module, for its InfluxDB helpers (`query()`, `readings()`, `parse_id()`, `parse_range()`, `flux_string()`, `number()`, `iso()`, `capped()`, `ToolError`, the `INFLUXDB_*` settings), and returns `(Ollama tool definition, function)` pairs. An optional `PROMPT` string is appended to the system prompt.

```python
# ai/agent/local_tools.py
PROMPT = "Use count_devices for how many devices report."

def tools(agent):
    def count_devices(args):
        rows = agent.query(agent.readings(agent.parse_range(args.get("range")), "value")
                           + '  |> last() |> group() |> distinct(column: "device")')
        return {"devices": sorted(r["_value"] for r in rows)}

    definition = {"type": "function", "function": {
        "name": "count_devices", "description": "Devices that reported in a time range.",
        "parameters": {"type": "object", "properties": {"range": {"type": "string"}}}}}
    return [(definition, count_devices)]
```

Restart the agent to load changes (`docker compose restart agent` in `ai/`); its log lists the tools it loaded. Build Flux from checked arguments only (`parse_id()`, `parse_range()`, `flux_string()`), never from raw model output. A small model picks tools by their descriptions, so say in each one what questions it answers.

### Settings

| Variable (`ai/.env`) | Default | Purpose |
|---|---|---|
| `OLLAMA_MODEL` | `gemma4:e2b` | Model pulled on first start and offered in the Agent tab; any [Ollama library](https://ollama.com/library) tag, e.g. `gemma4:e4b` on a bigger machine. A model without tool support still chats, without data access. Empty pulls nothing |
| `OLLAMA_KEEP_ALIVE` | `30m` | How long the model stays in memory after the last chat (`0`: unload right away, `-1`: never) |
| `AGENT_THINK` | `false` | Whether thinking models think before answering. See below. Empty: the model's default |
| `INFLUXDB_TOKEN` / `INFLUXDB_ORG` / `INFLUXDB_BUCKET` | as in `iot/.env.example` | What the tools query. Must match `iot/.env`. The agent only reads, so a read-only token for the bucket is enough |

The model is pulled only when it isn't in the `ollama-data` volume yet, so later starts work offline. A failed pull is logged and Ollama keeps running; restart the layer to retry. Changing `OLLAMA_MODEL` pulls the new model on the next start; remove the old one with `docker compose exec ollama ollama rm <model>`.

### Thinking

Gemma 4 thinks before answering by default. On an 8-core laptop CPU with no GPU, that took about 45 s per answer, against 5–20 s without it, and p4n4-dashboard shows nothing while the model thinks. So `AGENT_THINK=false` turns it off. Without thinking, `gemma4:e2b` sometimes asks which device you mean instead of calling `list_sensors`; asking again usually works. Set `AGENT_THINK=true` (and `docker compose up -d` in `ai/`) for more reliable tool use on faster hardware. A request that sets `think` itself keeps its choice.

---

## p4n4-dashboard

`.p4n4.json` has a `dashboard` block that [p4n4-dashboard](https://github.com/raisga/p4n4-dashboard) reads through p4n4-api (`GET /api/v1/project`):

```json
"dashboard": {
  "grafana_path": "/d/p4n4-telemetry/telemetry",
  "tabs": ["services", "agent", "grafana"]
}
```

While connected to this project, the dashboard shows the IoT and AI stacks and these tabs:

- **Agent** chats with the model through the [agent](#agent), so answers can use your readings. The dashboard container reaches it as `p4n4-ollama` on `p4n4-net` (its default `OLLAMA_UPSTREAM`), native apps on port 11434, and the Agent tab selects the first model Ollama lists.
- **Grafana** opens the Telemetry dashboard in kiosk mode. Set `GRAFANA_ANONYMOUS=true` in `iot/.env` so client users can view it without a Grafana login, and `GRAFANA_ALLOW_EMBEDDING=true` for the dashboard's web build, which shows Grafana in an iframe.

---

## Security

These defaults are for a trusted local network:

- Mosquitto allows anonymous clients. For authentication, follow the *MQTT authentication* section of the [p4n4-iot README](https://github.com/raisga/p4n4-iot#readme) and mount the files via `iot/docker-compose.override.yml`.
- Change every password and `INFLUXDB_TOKEN` in `iot/.env` (and `ai/.env`) before exposing any port.
- The agent has no authentication: anyone who can reach port 11434 can ask about every reading, and can pull and delete models through the Ollama API it passes on. Remove the port in `ai/docker-compose.override.yml` if only the dashboard (over `p4n4-net`) needs it. Ollama itself publishes no port.
- Give the agent a read-only InfluxDB token rather than the admin token: `docker compose exec influxdb influx auth create --org <org> --read-bucket <bucket id>` in `iot/`, then set it as `INFLUXDB_TOKEN` in `ai/.env`.
- `GRAFANA_ANONYMOUS=true` makes every dashboard readable by anyone who can reach port 3000, and `GRAFANA_ALLOW_EMBEDDING=true` lets any site frame Grafana (clickjacking risk).

---

## Testing

```bash
./tests/smoke.sh          # starts a throwaway copy of both layers, checks every sink, tears down
KEEP=1 ./tests/smoke.sh   # leave it running for inspection
```

The test runs each layer as its own Compose project, iot first, as `p4n4 up` does, and drops the fixed container names, host ports and `p4n4-net` network so it runs alongside any other stack. It checks InfluxDB, both archives and every dashboard panel query. In the ai layer, the agent talks to `tests/fake_ollama.py`, a scripted model that makes whichever tool call the test asks for and echoes the result, so `tests/check_agent.py` checks each tool's InfluxDB results, argument validation, a `local_tools.py` tool, streaming and pass-through without a model download. Ollama itself starts without a model.
