# mqtt-influx-grafana-ollama-pidurable

> Pumps publish readings and inference results over MQTT. **Node-RED** stores them in **InfluxDB** and raises an alert when a pump's anomaly score crosses a threshold. A durable, read-only **agent** ([Pi Durable](https://github.com/earendil-works/pi/tree/main/packages/durable) on a local **Ollama** model) investigates each alert with tools that query InfluxDB, and publishes its finding to MQTT and InfluxDB, where **Grafana** shows it. Every step of an investigation is checkpointed, so a crash or restart resumes it, and repeated alerts for one fault are one investigation.

| | |
|---|---|
| Layers | `iot`, `ai` |
| Services | iot: Mosquitto · Node-RED · InfluxDB · Grafana (+ demo pumps) · ai: agent · Ollama |
| Version | 0.1.0 |

```
  iot/  pumps ──MQTT──► mqtt ──► node-red ──┬──► influxdb ──► grafana
        sensors/, inference/ :1883  :1880   │      :8086       :3000
                                            └── anomaly_score > 0.7 ──► alerts/<device>/anomaly
                                                                               │
  ai/   agent ◄────────────────────────────────────────────────────────────────┘
        :8095 ──► ollama (gemma4:e2b)          tools: query_influx, recent_inference (read only)
          │   └─► /data/agent.sqlite           every step checkpointed (Pi Durable)
          └─────► agent/investigations/<device> + agent_investigation in InfluxDB
        └───────────── both on p4n4-net, which the iot layer creates ─────────────┘
```

---

## Layout

Each layer is its own Compose project in its own directory, the layout `p4n4 init` gives multi-layer projects, so `p4n4 up`, `p4n4 down`, `p4n4 status` and `p4n4 validate` work on it.

```
.p4n4.json                       manifest: layers [iot, ai], template, dashboard
iot/docker-compose.yml           Mosquitto, InfluxDB, Node-RED, Grafana, simulator; creates p4n4-net
iot/.env.example                 InfluxDB, Grafana, Node-RED (alert threshold), simulator settings
iot/config/mosquitto/            broker config: persistent, so queued alerts survive a restart
iot/config/node-red/             ingest and the alert rule (flows/flows.json)
iot/config/grafana/              datasource and the Investigations dashboard
iot/scripts/simulate.sh          demo pumps; pump-2's bearing fails on schedule
ai/docker-compose.yml            agent + Ollama; joins p4n4-net as an external network
ai/.env.example                  model, agent and InfluxDB settings (match iot/.env)
ai/config/ollama/entrypoint.sh   starts Ollama, pulls OLLAMA_MODEL on first start
tests/smoke.sh                   end-to-end test of both layers, with a scripted model
tests/crash.sh                   SIGKILL the agent mid-tool under Raspberry Pi 5 limits
tests/fake_llm.py                the scripted model (OpenAI-compatible), so tests download none
```

---

## Quick start

```bash
cp -r mqtt-influx-grafana-ollama-pidurable my-project && cd my-project
cp iot/.env.example iot/.env    # change passwords and the token
cp ai/.env.example ai/.env      # INFLUXDB_* must match iot/.env
sed -i "s/^AGENT_API_TOKEN=.*/AGENT_API_TOKEN=$(openssl rand -hex 32)/" ai/.env
p4n4 validate
p4n4 up                         # iot, then ai
```

Without the CLI: `(cd iot && docker compose up -d) && (cd ai && docker compose up -d)`.

On first start Ollama downloads `gemma4:e2b` (about 4.6 GB) in the background; follow it with `docker compose logs -f ollama` in `ai/` until it prints `model gemma4:e2b is ready`. The `demo` profile starts two simulated pumps, and 90 seconds in, pump-2's bearing starts to fail: its vibration climbs, its anomaly score crosses 0.7, Node-RED publishes `alerts/pump-2/anomaly` and the agent investigates. Watch it:

```bash
docker compose -f ai/docker-compose.yml logs -f agent      # investigation opened … reported
mosquitto_sub -t 'agent/investigations/#' -v               # the finding
```

Grafana at <http://localhost:3000> (`admin` / `adminpassword`) opens on the **Investigations** dashboard: anomaly scores, vibration and the findings table. If the model is still downloading when the first alert comes, the investigation waits for it (`AGENT_INVESTIGATION_TIMEOUT_S`, 30 minutes) and the next fault, every `SIMULATOR_FAULT_EVERY` seconds, starts another.

Don't run this project alongside another p4n4 iot or ai stack: they use the same container names, ports and `p4n4-net` network.

---

## iot layer

```
sensors/<device>/<sensor>    {"value": 41.3, "unit": "C"} or a bare number
inference/<device>/result    ei-runner's result: {"device", "timestamp", "label", "confidence", "anomaly_score", ...}
```

Node-RED stores both in `raw_telemetry` as the core p4n4-iot flow does (`sensor_data` with tags `device`, `sensor`; `inference` with tag `device`), and checks every inference result: above `ANOMALY_THRESHOLD` it publishes

```
alerts/<device>/anomaly      {"device", "anomaly_score", "label", "threshold", "timestamp"}, QoS 1
```

at most once per `ALERT_HOLDOFF` seconds per device. In a full p4n4 project, `ei-runner` (the ai stack's `ei-runner` profile) publishes the inference results; here the simulator stands in for it.

## ai layer

### Agent

The agent opens one investigation per device and alert window (`AGENT_ALERT_WINDOW_S`, 15 minutes, aligned to the clock): the request ID is `<device>:<window start>`, taken from the alert's own timestamp, so a redelivered or repeated alert for the same fault joins the investigation already open. It gives the model the alert and these tools:

| Tool | Reads |
|------|-------|
| `recent_inference` | the device's newest inference results |
| `query_influx` | an aggregated sensor or inference series of one device, at most 24 h back. The Flux query is a fixed template; the model only picks checked parameters (device, sensor, field, minutes, window, aggregate) |
| `report_finding` | records the cause, confidence and summary in the agent's own store |

Then it publishes the finding:

```
agent/investigations/<device>   {"incident", "device", "status", "cause", "confidence", "summary",
                                 "error", "alert_at", "started_at", "finished_at"}, QoS 1
```

and writes it to InfluxDB as `agent_investigation` (tags `device`, `status`). Nothing else: phase 1 of the agent changes nothing outside itself.

### Durability

Pi Durable commits every conversation entry, tool call and the incident's state to `/data/agent.sqlite` (the `agent-data` volume) before it takes effect. When the container is killed (an OOM, `docker kill`, a crash), the restart resumes the same investigation from its last commit, runs the interrupted read-only tool again and finishes. Alerts are acknowledged on MQTT only once stored, and the broker keeps them while the agent is down. `AGENT_SQLITE_SYNC=FULL` makes every commit survive a power cut too, which matters on a Raspberry Pi's SD card; `NORMAL` writes less but may lose the last commit on power loss. The results go out at least once: a crash between publishing and recording that, redelivers the MQTT message (consumers dedupe on `incident`) and rewrites the same InfluxDB point.

### Status API

```bash
curl localhost:8095/health
curl -H "Authorization: Bearer $(grep ^AGENT_API_TOKEN ai/.env | cut -d= -f2)" \
  'localhost:8095/v1/investigations?state=recent'     # open | recent | all; /v1/investigations/<key>
```

### Settings

| Variable | Default | |
|---|---|---|
| `OLLAMA_MODEL` / `AGENT_MODEL` | `gemma4:e2b` | keep them the same: Ollama holds one model in memory |
| `OLLAMA_CONTEXT_LENGTH` / `AGENT_CONTEXT_TOKENS` | `8192` | Ollama cuts longer contexts silently |
| `AGENT_ALERT_WINDOW_S` | `900` | alerts for one device in one window are one investigation |
| `AGENT_MAX_TOOL_CALLS` | `8` | after these, only `report_finding` |
| `AGENT_INVESTIGATION_TIMEOUT_S` | `1800` | then it ends `failed` |
| `AGENT_SQLITE_SYNC` | `FULL` | `NORMAL` / `FULL` / `EXTRA` |

The full reference, including the System 1 and Letta tools of the p4n4-ai stack's `agent` profile, is in p4n4-docs: [Incident investigator](https://github.com/raisga/p4n4-docs/blob/main/reference/agent.md).

---

## Security

These defaults are for a trusted local network:

- Change every password, `INFLUXDB_TOKEN` and `AGENT_API_TOKEN` in `iot/.env` and `ai/.env` before exposing any port. The agent refuses to start with a token under 32 characters.
- Mosquitto allows anonymous clients, and anyone who can publish to `alerts/<device>/anomaly` can start an investigation (they only cost model time: the agent's tools read).
- Ollama (11434) and the agent's status API (8095) are published on `127.0.0.1` only. Ollama has no authentication.
- Give the agent a read-only InfluxDB token where you can; it needs write access only to record `agent_investigation`.

---

## Testing

```bash
./tests/smoke.sh                       # both layers in a throwaway copy, then torn down
RUNS=5 ./tests/crash.sh                # SIGKILL mid-tool under p4n4-emu's rpi5 limits
AGENT_IMAGE=p4n4-agent:dev ./tests/smoke.sh   # until ghcr.io/raisga/p4n4-agent is published
```

Both drop the fixed container names, host ports and `p4n4-net`, so they run alongside other stacks, and point the agent at `tests/fake_llm.py`, a scripted OpenAI-compatible model whose next step depends only on the conversation (so it answers a resumed one exactly as before). `smoke.sh` checks the alert rule, an investigation end to end, the status API, that a repeated alert joins the open investigation, InfluxDB and every Grafana panel. `crash.sh` starts the template with `p4n4-emu up --profile rpi5 --native`, slows `query_influx` down with the agent's test hook, and for each of `RUNS` alerts kills the agent inside the tool and checks that the same submission completes, that there is exactly one investigation, result message and InfluxDB point, and that redelivering the alert changes nothing. It reports the agent's memory under the limit. `REAL_MODEL=1` uses Ollama and `OLLAMA_MODEL` instead of the fake; `EMU` sets how to run p4n4-emu.
