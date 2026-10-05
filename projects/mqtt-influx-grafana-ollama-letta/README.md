# mqtt-influx-grafana-ollama-letta

> A maintenance assistant with memory. Pumps and fans report vibration, bearing temperature and motor current over **MQTT** into **InfluxDB**. A **Letta** agent, running on a local **Ollama** model, is the maintenance team's colleague in p4n4-dashboard's Agent tab. It reads trends through tools, and it **remembers**:
> - the equipment register;
> - past incidents and repairs;
> - what the team tells it.
>
> When the team reports work, the agent logs it to its memory and to InfluxDB, and **Grafana** marks it on the charts.

| | |
|---|---|
| Layers | `iot`, `ai` |
| Services | iot: Mosquitto · Telegraf · InfluxDB · Grafana (+ demo machines) · ai: Ollama · gateway · Letta · letta-init |
| Version | 0.1.0 |

```
  iot/  machines ──MQTT──► mqtt ──► telegraf ──┬──► influxdb ──► grafana :3000
        sensors/<machine>/vibration            │       ▲   ▲     (vibration zones, maintenance markers)
                         /bearing_temp         │       │   │
                         /current              └──► iot/data/archive/
                                                       │   │ maintenance_event
  ai/   p4n4-dashboard Agent tab ──► p4n4-api ──► letta :8283 ── tools: equipment_status, equipment_trend,
                                                   │    ▲         maintenance_history, log_maintenance
                                       core memory │    │ archival memory (past incidents, logged work)
                                                   ▼    │
                                   gateway ──► ollama :11434 (chat model + embeddings)
        └───────────── both layers on p4n4-net, which the iot layer creates ─────────────┘
```

**Why Letta.** A plain chat model, like the agent in [`mqtt-influx-grafana-ollama`](https://github.com/raisga/p4n4-templates/tree/main/projects/mqtt-influx-grafana-ollama), starts every conversation knowing nothing. A Letta agent keeps state between conversations:

| Memory | What this agent keeps there | Who writes it |
|---|---|---|
| Core memory: `persona` | Its role and working rules | the template (first start) |
| Core memory: `equipment` | The equipment register: machines, sensors, vibration limits, maintenance plan, and the last failure | the template (first start), then the agent as it learns (`memory_insert`) |
| Core memory: `human` | Who it works with | the agent |
| Archival memory | Past incidents and every logged repair, tagged by machine: listed per machine by `maintenance_history`, searchable by meaning with `archival_memory_search` | the template (4 seeded records), then `log_maintenance` |
| Recall memory | Every message so far | Letta |

Core memory is in the prompt on every turn. Archival memory is read on demand.

---

## Quick start

```bash
cp -r mqtt-influx-grafana-ollama-letta my-plant && cd my-plant
for layer in iot ai; do cp $layer/.env.example $layer/.env; done   # change passwords
p4n4 up                     # or: (cd iot && docker compose up -d) && (cd ai && docker compose up -d)
```

The first start downloads `gemma4:e2b` (about 4.6 GB) and `nomic-embed-text` (about 0.3 GB). Letta waits for them, because it reads Ollama's models only when it starts. Then `letta-init` creates the agent and exits. To follow along:

```bash
cd ai && docker compose logs -f ollama letta-init
# … p4n4: created agent maintenance-assistant (agent-…) on ollama/gemma4:e2b
# … p4n4: seeded archival memory with 4 past maintenance records
```

The `demo` profile simulates `pump-1`, `pump-2` and `fan-1`. On the first start it backfills **two weeks of history** into InfluxDB. In that history, pump-2's drive-end bearing has been wearing for nine days: vibration has climbed from 2.3 to about 5.6 mm/s and bearing temperature from 49 to 61 °C. That is the same pattern that preceded its last failure, and the agent remembers that failure. Open Grafana's **Equipment health** dashboard at <http://localhost:3000> (`admin` / `adminpassword`) to see it.

### Talking to the agent

**In p4n4-dashboard.** p4n4-api reaches Letta at `http://localhost:8283`, with `LETTA_SERVER_PASSWORD` from `ai/.env` (see `P4N4_API_LETTA_URL` in the p4n4-api README). The template's theme (`gearwise`) offers Letta as the assistant (`defaults.agentBackend`), and Letta's first agent is this one. An operator can also choose it in the Agent tab's settings. Letta keeps the conversation itself, so the dashboard sends only the new message.

**Directly**, for scripts and testing:

```bash
cd ai
AGENT=$(docker compose exec -T letta curl -s -H 'Authorization: Bearer lettapassword' \
    'http://localhost:8283/v1/agents/?name=maintenance-assistant' | python3 -c 'import json,sys; print(json.load(sys.stdin)[0]["id"])')
docker compose exec -T letta curl -s -H 'Authorization: Bearer lettapassword' -H 'Content-Type: application/json' \
    "http://localhost:8283/v1/agents/$AGENT/messages" -d '{"messages": [{"role": "user", "content": "How is pump-2 doing?"}]}'
```

Things to try:

| Say | What happens |
|---|---|
| *How is pump-2 doing?* | `equipment_trend` (daily means, change over the week) and `maintenance_history`, judged against the ISO zones and the pattern before pump-2's last failure |
| *Is anything wrong?* | `equipment_status` for every machine and sensor |
| *Ann Lee replaced the drive-end bearing on pump-2 this morning.* | `log_maintenance`: saved to archival memory and InfluxDB; it appears in Grafana's maintenance log and as a marker on the charts |
| *We installed pump-3 as a standby pump.* | `memory_insert` into the `equipment` block, where it stays for every later conversation |
| *When was pump-2 last serviced?* | `maintenance_history`, or `archival_memory_search` |

### Model and speed

A Letta agent takes several model steps per answer: one per tool call, then the reply. Each step processes a prompt of about 6,000 tokens (instructions, memory blocks, tool schemas), most of it cached between steps.

Measured on an 8-core CPU with no GPU, using `gemma4:e2b` with thinking off (the default), on a fresh agent:

| Message | Steps | Time |
|---|---|---|
| *How is pump-2 doing?* | 2 tool calls + reply | 122 s |
| *Ann Lee replaced the drive-end bearing on pump-2 …* | `log_maintenance`, `maintenance_history` + reply | 40 s |
| *When was pump-2 last serviced, and by whom?* | `maintenance_history` + reply | 19 s |

With thinking on, the first question took 447 s, in an earlier run with a longer persona.

p4n4-api gives Letta **5 minutes** per message, so on a CPU keep thinking off. Answers become seconds with a GPU: copy the `ollama` GPU block of [p4n4-ai's `docker-compose.override.yml.example`](https://github.com/raisga/p4n4-ai#gpu-support) into `ai/docker-compose.override.yml`.

`gemma4:e2b` reads trends and calls the tools well. Being a 2-billion-parameter model, it sometimes misreads a limit or skips a step. One example: given `archival_memory_search`, it invented a tag and found nothing. That's why `maintenance_history` exists: one argument, the machine id, so finding a machine's history doesn't depend on the model choosing tags. A bigger model, such as `OLLAMA_MODEL=gemma4:e4b` or `qwen3:8b` on a GPU, uses the memory more reliably. After changing it, run `docker compose up -d` in `ai/`. The new model is pulled, and `letta-init` switches the agent to it without touching the agent's memory.

**Why there's a gateway.** Letta can't tell Ollama to stop a thinking model from reasoning before every step. The gateway (`ai/config/gateway/gateway.py`) adds `"reasoning_effort": "none"` to chat requests when `AGENT_THINK=false`, and passes everything else through.

---

## Data contract

```
sensors/<machine>/vibration      {"value": 2.31, "unit": "mm/s"}   velocity RMS, drive-end bearing
sensors/<machine>/bearing_temp   {"value": 48.2, "unit": "C"}
sensors/<machine>/current        {"value": 19.6, "unit": "A"}
```

The topic and payload rules are those of the [`mqtt-influx-grafana` contract](https://github.com/raisga/p4n4-templates/tree/main/projects/mqtt-influx-grafana#data-contract). The tools read any sensor in `sensor_data`; these three are the ones the dashboard and the equipment register know.

`log_maintenance` writes `maintenance_event,device=<machine>,source=assistant work="…",technician="…"`. Grafana shows it as the maintenance log, as the latest work per machine, and as markers on the time series.

### The demo history

The simulator writes its backfill straight to InfluxDB in 10-minute steps, so it isn't in `iot/data/archive/`. Only the live readings, published over MQTT every `SIMULATOR_INTERVAL`, go through Telegraf. Pump-2's wear is timed from the oldest pump-2 reading, so it carries on across restarts, levelling off at about 6.8 mm/s. The four records seeded into archival memory, and the failure date in the `equipment` block, are dated relative to the first start, so the story holds whenever you run it.

---

## Making it yours

| File | Used | Change it to |
|---|---|---|
| `ai/config/letta/equipment.md` | first start, as the `equipment` block | your machines, sensors, limits and plan. `{date:N}` becomes the date N days before the first start |
| `ai/config/letta/history.json` | first start, into archival memory | your past incidents and repairs (`days_ago`, `tags` with the machine id, `text`) |
| `ai/config/letta/persona.md` | first start, as the `persona` block | the agent's role and rules |
| `ai/config/letta/tools/*.py` | every start (`letta-init`) | the tools: one self-contained function per file, with a Google-style docstring. Letta reads every function in a file as a tool, so don't nest helpers |

`letta-init` runs on every `docker compose up`. It updates the tools and the agent's tool secrets, but it never touches the agent's memory once the agent exists: that memory is the agent's own. To change memory later, tell the agent, or use Letta's API (`PATCH /v1/agents/<id>/core-memory/blocks/<label>`). Letta's web ADE can also connect to a self-hosted server. To start over, either set a new `AGENT_NAME` (the old agent stays), or delete the agent through the API, then run `docker compose up -d` in `ai/`.

Real sensors: publish the three topics per machine, add the machines to the `equipment` block, and turn the simulator off (`COMPOSE_PROFILES=` in `iot/.env`). The simulator backfills only an empty bucket.

---

## Configuration

`iot/.env` holds the same variables as [`mqtt-influx-grafana`](https://github.com/raisga/p4n4-templates/tree/main/projects/mqtt-influx-grafana#configuration), plus `SIMULATOR_INTERVAL` (`10` s) and `SIMULATOR_BACKFILL_DAYS` (`14`). `ai/.env`:

| Variable | Default | Purpose |
|---|---|---|
| `OLLAMA_MODEL` | `gemma4:e2b` | Chat model; must support tool calls. Empty pulls nothing |
| `OLLAMA_EMBED_MODEL` | `nomic-embed-text` | Embeddings for archival memory. Set it once: memories stored with one model can't be searched with another |
| `OLLAMA_CONTEXT_LENGTH` | `16384` | Context window in tokens, for Ollama and for the agent; Letta summarizes older messages to stay inside it |
| `OLLAMA_KEEP_ALIVE` | `30m` | How long a model stays loaded |
| `AGENT_THINK` | `false` | Let thinking models reason before each step (slower; see *Model and speed*) |
| `OLLAMA_BIND` / `LETTA_BIND` | `127.0.0.1` | Addresses Ollama's and Letta's APIs are published on |
| `LETTA_SERVER_PASSWORD` | `lettapassword` | Letta's API password; p4n4-api reads it from this file |
| `AGENT_NAME` | `maintenance-assistant` | The agent `letta-init` creates or updates |
| `INFLUXDB_TOKEN` / `INFLUXDB_ORG` / `INFLUXDB_BUCKET` | as in `iot/.env` | For the tools: must match the iot layer |

### Where things live

```
iot/config/grafana/provisioning/dashboards/   Equipment health dashboard (uid p4n4-equipment)
iot/scripts/simulate.sh                       demo machines and the two-week backfill
ai/config/ollama/entrypoint.sh                pulls OLLAMA_MODEL and OLLAMA_EMBED_MODEL
ai/config/gateway/gateway.py                  Letta ↔ Ollama, thinking off
ai/config/letta/provision.py                  letta-init: tools, agent, first memories
ai/config/letta/tools/                        equipment_status, equipment_trend, maintenance_history, log_maintenance
ai/config/letta/{persona,human,equipment}.md  first core memory
ai/config/letta/history.json                  first archival memory
theme/                                        p4n4-dashboard theme (sample: gearwise, Agent tab on Letta)
tests/smoke.sh, tests/fake_llm.py             end-to-end test with a scripted model
```

---

## p4n4-dashboard

```json
"dashboard": {
  "grafana_path": "/d/p4n4-equipment/equipment-health",
  "tabs": ["services", "edge", "agent", "grafana"],
  "theme": "theme"
}
```

The Agent tab talks to the Letta agent, and the Grafana tab opens Equipment health. For client users, set `GRAFANA_ANONYMOUS=true` and `GRAFANA_ALLOW_EMBEDDING=true` in `iot/.env`, as in the [greenhouse use case](../../docs/use-cases/greenhouse-telemetry.md). The theme lets client users ("normies") see the Agent and Grafana tabs.

## Security

- Change `LETTA_SERVER_PASSWORD`. Anyone with it can read and change the agent, including its memory and its tool secrets (the InfluxDB token and the Letta password). Ollama and Letta are published on `127.0.0.1` only. Let people reach the agent through p4n4-api, which signs users in and keeps the password on the server.
- The tools can write to InfluxDB with `INFLUXDB_TOKEN`. A token limited to the bucket is safer: `docker compose exec influxdb influx auth create --read-bucket <id> --write-bucket <id> --org <org>` in `iot/`.
- The agent's memory holds what people tell it, names included. It lives in the `letta-pgdata` volume. Back it up like any other maintenance record.
- Mosquitto allows anonymous clients; see the *MQTT authentication* section of the [p4n4-iot README](https://github.com/raisga/p4n4-iot#readme).
- The assistant advises; it doesn't replace a vibration analyst or the manufacturer's limits.

---

## Testing

```bash
./tests/smoke.sh          # throwaway copy of both layers; real Letta, scripted model
KEEP=1 ./tests/smoke.sh   # leave it running for inspection
```

The test runs Letta for real, against `tests/fake_llm.py`. That fake serves the Ollama endpoints Letta uses, and decides which tools to call from keywords, so nothing is downloaded and every answer is fixed. It checks:
- `letta-init` provisions the agent: tools, memory blocks with the dates filled in, and four archival records;
- each tool works through Letta: trend, status, history, and logging to InfluxDB and archival memory;
- `memory_insert` changes the `equipment` block;
- a later question recalls logged work;
- the gateway turns thinking off;
- re-provisioning keeps one agent and its memory, and doesn't seed twice;
- the iot layer's archives and bridge;
- every Grafana panel.
