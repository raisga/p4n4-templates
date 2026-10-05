# Use case: a greenhouse assistant on a local LLM

This picks up where [greenhouse telemetry](greenhouse-telemetry.md) leaves off. The grower's
staff watch the Telemetry dashboard in the `verdant` app, but they still message the
integrator with questions a chart answers only slowly: "did house 1 get too cold last
night?", "which house is driest right now?". The grower wants:

- Staff ask those questions in plain language, in the same app.
- The answers come from the greenhouse's own readings, and say which ones.
- Nothing leaves the site: no cloud model, no API key, no readings sent anywhere.

This guide builds that with the
[`mqtt-influx-grafana-ollama`](../../projects/mqtt-influx-grafana-ollama) template. Its iot
layer is `mqtt-influx-grafana`, unchanged. Its ai layer runs Gemma 4 E2B on Ollama behind
an agent that answers with tool calls to InfluxDB, so the model quotes readings instead
of guessing them.

```
 sensors ──MQTT──► mqtt ──► telegraf ──┬──► influxdb ──► grafana ◄──────────────────── Grafana tab
 (sensors/<device>/<measurement>)      └──► iot/data/archive    ▲
                                                                │ tool calls (read only)
 dashboard Assistant tab ──► p4n4-api ──► agent :11434 ─────────┤
 ("verdant" brand)      /api/v1/agents/chat     └──► ollama (gemma4:e2b)
```

| Piece | Repo | Role here |
|---|---|---|
| `mqtt-influx-grafana-ollama` template | `p4n4-templates` | `iot/`: the same pipeline as before. `ai/`: the agent (Ollama's API plus three InfluxDB tools) and Ollama |
| `ai/agent/local_tools.py` | the project | The greenhouse's own tools and prompt: its houses and target ranges |
| `.p4n4.json` `dashboard` block | `p4n4-lib` (validated), the template (declared) | Adds the `agent` tab |
| p4n4-api | `p4n4-api` | Relays the Assistant tab to the agent, and holds everyone to the model an operator chose |
| `theme/` (sample: `verdant`) | the project | The same brand, with the Agent tab and `gemma4:e2b` as the default assistant |
| p4n4-dashboard | `p4n4-dashboard` | The app; its Assistant tab is the agent's chat |

---

## 0. Check the hardware

The model runs on the same host as the stack, on the CPU unless Ollama finds a GPU.

- **Disk.** `gemma4:e2b` is about 4.6 GB, kept in the `ollama-data` volume.
- **Memory.** The model stays loaded for `OLLAMA_KEEP_ALIVE` (default `30m`) after the last
  chat. Set it to `0` to free the memory between questions on a small host, at the cost
  of a slow first answer.
- **Time.** On an 8-core laptop CPU with no GPU, answers took 5–20 s with thinking off
  (`AGENT_THINK=false`, the default) and about 45 s with it on.

A bigger host can run `gemma4:e4b` (`OLLAMA_MODEL` in `ai/.env`), which picks tools more
reliably.

## 1. Move the project onto the template

The template's iot layer is `mqtt-influx-grafana` in an `iot/` directory, so an existing
greenhouse project moves over with its data. (For a new project, copy the template, run
`cp iot/.env.example iot/.env` and `cp ai/.env.example ai/.env`, and skip to
[step 2](#2-configure-the-ai-layer).)

Stop the old project. Its volumes stay:

```bash
cd ~/projects/greenhouse && docker compose down && cd ..
mv greenhouse greenhouse-v1
cp -r tools/templates/projects/mqtt-influx-grafana-ollama greenhouse
```

Carry over the settings, the archive, the theme and any override:

```bash
cp greenhouse-v1/.env greenhouse/iot/.env
cp -a greenhouse-v1/data/archive/. greenhouse/iot/data/archive/
cp greenhouse-v1/docker-compose.override.yml greenhouse/iot/    # if you have one
cp greenhouse/ai/.env.example greenhouse/ai/.env
```

Compose names a project's volumes after its directory. The old one was `greenhouse`, so
its data is in `greenhouse_influxdb-data`. The new iot layer lives in `iot/`, and would
start empty under `iot_influxdb-data`. Name both layers in their `.env` files: the iot
layer after the old project, so it finds the old volumes, and the ai layer after its own:

```bash
echo 'COMPOSE_PROJECT_NAME=greenhouse' >> greenhouse/iot/.env
echo 'COMPOSE_PROJECT_NAME=greenhouse-ai' >> greenhouse/ai/.env
```

Don't change these later: a new name means new, empty volumes.

Rename the project in `greenhouse/.p4n4.json` (`"project": "greenhouse"`) and keep the
rest:

```json
{
  "schema_version": 1,
  "project": "greenhouse",
  "layers": ["iot", "ai"],
  "template": { "name": "mqtt-influx-grafana-ollama", "version": "0.2.0" },
  "dashboard": {
    "grafana_path": "/d/p4n4-telemetry/telemetry",
    "tabs": ["services", "edge", "agent", "grafana"],
    "theme": "theme"
  }
}
```

The template's `theme/` is the sample `verdant` brand with the Agent tab added. If you
replaced `verdant` with the grower's brand, copy yours over it
(`cp -r greenhouse-v1/theme/. greenhouse/theme/`) and add the assistant to it:

```jsonc
"tabs": ["services", "edge", "agent", "grafana"],
"defaults": {
  "normieTabs": "agent,grafana",    // staff: Home, Agent, Grafana (replaces clientTabs)
  "agentBackend": "ollama",
  "ollamaModel": "gemma4:e2b"       // offered as everyone's assistant on first sign-in
}
```

Remove `greenhouse-v1` once the new project shows the old readings.

## 2. Configure the ai layer

`ai/.env` holds the model and what the agent may read:

| Variable | Set it to |
|---|---|
| `INFLUXDB_TOKEN` | A read-only token for the bucket (below). The iot layer's admin token works, but the agent only reads |
| `INFLUXDB_ORG`, `INFLUXDB_BUCKET` | The same as `iot/.env` |
| `OLLAMA_MODEL` | `gemma4:e2b`, or a bigger model on a bigger host |
| `OLLAMA_KEEP_ALIVE` | `30m`, or `0` on a host short of memory |
| `AGENT_THINK` | `false` for quick answers; `true` for more reliable tool use on fast hardware |

Start both layers and check the project:

```bash
cd ~/projects/greenhouse
p4n4 up          # iot, then ai
p4n4 validate    # .p4n4.json, dashboard settings and theme, both layers' compose files and .env
```

Then create the read-only token and give it to the agent:

```bash
cd iot
docker compose exec influxdb influx bucket list --org ming       # note raw_telemetry's id
docker compose exec influxdb influx auth create --org ming \
  --read-bucket <bucket id> --description greenhouse-agent       # copy the token
cd ../ai && $EDITOR .env && docker compose up -d                 # INFLUXDB_TOKEN=<token>
```

On first start Ollama downloads the model in the background. `p4n4 logs ai` prints
`model gemma4:e2b is ready` when it's done. Ask from the terminal to check:

```bash
curl -s localhost:11434/api/chat -d '{"model": "gemma4:e2b", "stream": false,
  "messages": [{"role": "user", "content": "Which house is warmer right now?"}]}' | jq -r .message.content
```

The agent logs every tool call (`docker compose logs -f agent` in `ai/`):

```
tool list_sensors {} -> 4 rows in 0.01s
```

## 3. Teach it the greenhouse

Out of the box the agent has three tools: `list_sensors` (latest values), `get_stats`
(min, max, mean over a range) and `get_history` (a trend). It knows device ids, not what
they grow, and it has no idea what "too cold" means for tomatoes.

Add both in `ai/agent/local_tools.py`. The agent loads it at startup, and you keep
`agent.py` as the template ships it. The tool below compares readings to the grower's
target ranges in InfluxDB and Python, so the model repeats a count instead of comparing
numbers itself:

```python
# ai/agent/local_tools.py: the greenhouse's own tool, loaded by agent.py
HOUSES = {"house-1": "tomatoes", "house-2": "seedlings"}
TARGETS = {"temperature": (18, 28, "C"), "humidity": (50, 80, "%")}

PROMPT = (
    "The devices are greenhouses: "
    + ", ".join(f"{house} ({crop})" for house, crop in HOUSES.items())
    + ". For too hot, too cold, too dry, too humid or out of range, use get_out_of_range: "
    "repeat its numbers, never compare readings to the targets yourself."
)


def tools(agent):
    def get_out_of_range(args):
        seconds = agent.parse_range(args.get("range"))
        device = agent.parse_id("device", args.get("device"))
        rows = []
        for sensor, (low, high, unit) in TARGETS.items():
            data = agent.readings(seconds, "value", sensor, device) + "  |> toFloat()"
            flux = (
                f"data = {data}\n"
                "union(tables: [\n"
                '  data |> count() |> toFloat() |> set(key: "stat", value: "readings"),\n'
                f'  data |> filter(fn: (r) => r._value < {low}.0) |> count() |> toFloat() |> set(key: "stat", value: "below"),\n'
                f'  data |> filter(fn: (r) => r._value > {high}.0) |> count() |> toFloat() |> set(key: "stat", value: "above"),\n'
                '  data |> min() |> set(key: "stat", value: "min"),\n'
                '  data |> max() |> set(key: "stat", value: "max"),\n'
                '])\n  |> keep(columns: ["device", "stat", "_value"])'
            )
            stats = {}
            for r in agent.query(flux):
                stats.setdefault(r["device"], {})[r["stat"]] = agent.number(r["_value"])
            for house, s in sorted(stats.items()):
                total, below, above = s.get("readings", 0), s.get("below", 0), s.get("above", 0)
                rows.append({
                    "device": house, "sensor": sensor, "target": f"{low}–{high} {unit}",
                    "min": s.get("min"), "max": s.get("max"), "readings": total,
                    "below": below, "above": above,
                    "share_outside": f"{round(100 * (below + above) / total)}%" if total else "no readings",
                })
        return {**agent.capped(rows), "range": f"last {args.get('range') or '24h'}"}

    definition = {"type": "function", "function": {
        "name": "get_out_of_range",
        "description": "Whether each greenhouse stayed in its target temperature and humidity: readings below "
                       "and above the target, the share outside it, and the min and max. Use it for too hot, "
                       "too cold, too dry, too humid or out of range.",
        "parameters": {"type": "object", "properties": {
            "range": {"type": "string", "description": "How far back, like 12h, 24h or 7d. Default 24h."},
            "device": {"type": "string", "description": "One greenhouse, e.g. house-1. Omit for all."},
        }},
    }}
    return [(definition, get_out_of_range)]
```

```bash
cd ai && docker compose restart agent && docker compose logs agent | grep "local tools"
# local tools from local_tools.py: get_out_of_range
```

Against house 1's readings of 17, 22.5, 25 and 30.1 °C, it returns:

```json
{"device": "house-1", "sensor": "temperature", "target": "18–28 C", "min": 17, "max": 30.1,
 "readings": 4, "below": 1, "above": 1, "share_outside": "50%"}
```

Build Flux from checked arguments only (`parse_range()`, `parse_id()`, `flux_string()`),
never from the model's text. A small model picks tools by their descriptions, so say in
each one which questions it answers. The template
[README](../../projects/mqtt-influx-grafana-ollama/README.md#project-tools) lists the
helpers `agent` offers.

## 4. Serve it and build the dashboard

Start p4n4-api on the project, as in the
[first guide](greenhouse-telemetry.md#3-serve-the-project-with-p4n4-api), with one more
account if the grower's head grower should choose the model:

```bash
cd api
export P4N4_PROJECT_DIR=~/projects/greenhouse
uv run p4n4-api users add headgrower --role operator    # the power view: may choose the assistant
uv run p4n4-api
```

The Assistant tab never talks to Ollama directly. It calls p4n4-api's
`/api/v1/agents/chat`, which forwards to `P4N4_API_OLLAMA_URL` (default
`http://localhost:11434`, the agent's port). That keeps the agent behind the API's
sign-in, and holds normie accounts to the assistant's model with no `options`.

Rebuild the app with the updated theme, as before:

```bash
cd dashboard
dart run tool/brand.dart check ~/projects/greenhouse
dart run tool/brand.dart install ~/projects/greenhouse --apply
flutter build apk --split-per-abi
dart run tool/brand.dart apply p4n4
```

The first time an admin or operator signs in with the Assistant tab in their view, the
dashboard saves the brand's `ollamaModel` as the deployment's assistant, if Ollama has it.
After that, the choice is the deployment's (`GET/PUT /api/v1/agents/config`), and only an
admin or operator changes it.

## 5. Use it

**Staff (normie view)** see Home, Agent and Grafana. In the Assistant tab:

| Ask | Tool | Answer |
|---|---|---|
| "Which house is warmer right now?" | `list_sensors` | The latest temperature per house, with its time |
| "How dry did house 2 get today?" | `get_stats` | Min, max and mean humidity over the range |
| "Is house 1 warming up?" | `get_history` | Up to 48 averaged points to describe the trend |
| "Did any house get too cold last night?" | `get_out_of_range` | Readings below 18 °C per house, and the share outside the target |

The model only knows what the tools return. It doesn't see Grafana, the archive or the
broker, and when it answers without a tool, it says so.

**Head grower (power view)** also sees Services and Edge, and picks the assistant's model
for everyone from the models Ollama has.

**Integrator (admin view)** sees everything, plus Clients and Views. Services now lists the
AI stack: Ollama shows live status. Letta and n8n, which the template doesn't run, show
*unknown*.

## Checks

These were run on Linux:

- [x] `uv run scripts/validate.py` in p4n4-templates passes, including the `verdant` theme now in this template
- [x] `tests/smoke.sh` passes: both layers, every sink, every Grafana panel query, and each agent tool (through a scripted stand-in model)
- [x] Moving a `mqtt-influx-grafana` project with readings in it, as in step 1: the iot layer reuses the `greenhouse_*` volumes, and the old readings and archive are there
- [x] `p4n4 validate` passes on the moved project
- [x] `GET /api/v1/project` returns both layers and the `dashboard` block; `GET /api/v1/stacks` reports the iot layer's services as running
- [x] `get_out_of_range` from step 3, loaded by `agent.py` against the moved project's InfluxDB, returns the counts above; bad `range` and `device` arguments come back as errors the model can fix
- [ ] `gemma4:e2b` answering the questions above through the dashboard's Assistant tab
- [ ] The `verdant` build with the Agent tab, on a phone
- [ ] Answer times on a Raspberry Pi 5

## Limitations and next steps

- **The model is small.** Without thinking, `gemma4:e2b` sometimes asks which device you
  mean instead of calling a tool; asking again usually works. `AGENT_THINK=true` is more
  reliable and several times slower, and the dashboard shows nothing while the model
  thinks.
- **Answers are only as good as the tools.** The model sees `sensor_data` and nothing
  else. A question no tool answers gets a guess or a question back: add a tool for it.
- **Port 11434 has no authentication.** Anyone who reaches it can ask about every reading,
  and pull or delete models through the Ollama API the agent passes on. p4n4-api is the
  only client the dashboard needs, so keep the port on the loopback interface (the
  template [README](../../projects/mqtt-influx-grafana-ollama/README.md#security) shows how).
- **Targets live in code.** `TARGETS` in `local_tools.py` is per project, and changing it
  means restarting the agent. Grafana thresholds and the tool's targets don't share a
  source, so keep them in step by hand.
- **One model at a time.** Everyone on a deployment talks to the same assistant. The
  model stays loaded for `OLLAMA_KEEP_ALIVE`, so a busy host feels it.
