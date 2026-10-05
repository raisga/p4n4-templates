# Template roadmap

Demo templates that show how p4n4's services work together around a real use case. Each one teaches a single new service or pattern, runs offline by default, and ships a simulator so it works with no hardware.

Last updated: 2026-10-05.

## Guidelines for this series

Decided when the series was planned:

| Question | Decision |
|---|---|
| Audience | Both client demos and community learning: a realistic use case, structured to teach one new service per template |
| Services outside the three stacks (Qdrant, ChirpStack, Mailpit, …) | Allowed, when the use case needs them |
| External accounts and APIs | Optional only: the demo profile must work offline with no sign-ups; Slack, Telegram, SMTP, Edge Impulse and price APIs unlock extras |
| Hardware ties (p4n4-hw scripts, p4n4-emu profiles) | Not for now: templates stay hardware-agnostic, with simulators |

Every template follows [authoring.md](authoring.md): `template.yaml`, `.p4n4.json`, a demo profile, a sample theme, and a smoke test that CI runs.

---

## Scaffolded

All three are complete. Each passes `scripts/validate.py`, passes its smoke test, and has been run as a demo.

**None of them is committed yet.** Each one also adds a row to this repo's [README](../README.md) and to p4n4-docs' `reference/template-registry.md`. The rows are in the working tree and also need committing.

| # | Template | Layers | Use case | Teaches | Smoke test |
|---|---|---|---|---|---|
| 1 | [`mqtt-nodered-influx-grafana`](../projects/mqtt-nodered-influx-grafana) | iot | Greenhouse climate control | **Node-RED** closing the loop: rules send MQTT commands to devices | 38 checks, passing |
| 2 | [`mqtt-influx-grafana-n8n`](../projects/mqtt-influx-grafana-n8n) | iot + ai | Cold-chain compliance (fridges, freezers, cold rooms) | **n8n**: alerting, escalation, acknowledgement, daily records | 50 checks, passing (twice in a row) |
| 3 | [`mqtt-influx-grafana-ollama-letta`](../projects/mqtt-influx-grafana-ollama-letta) | iot + ai | Maintenance assistant for pumps and fans | **Letta**: agent memory across conversations | 34 checks, passing |

### 1 · mqtt-nodered-influx-grafana

**What it does:**
- Node-RED switches a fan on temperature and an irrigation valve on soil moisture, each with a gap between its on and off thresholds (hysteresis).
- Operators can override each actuator (`auto`, `on`, `off`).
- A valve watchdog closes a valve when its sensor goes quiet or it runs too long, then locks it out for a while.
- Commands are retained on `actuators/<zone>/<actuator>/set` and carry the reason for the decision. Telegraf stores them as `actuator_command`.
- The Greenhouse dashboard shows a control-decisions table with those reasons. Sample theme: `canopy`.
- The simulated greenhouse obeys the commands.

**Verified:**
- smoke test: every rule, the override, both watchdog cutoffs, the stores, the bridge and all 8 panels;
- demo run: fans cycle around simulated noon, valves water each zone;
- screenshots in both themes.

**Follow-ups:**
- The dashboard's threshold lines are fixed at the defaults (28/26 °C, 30/45 %); Grafana can't read `.env`.
- Mosquitto allows anonymous clients, so anyone on the network can switch actuators. The README asks for authentication and access rules before real equipment.

### 2 · mqtt-influx-grafana-n8n

**What it does:**
- n8n is provisioned on first start: the owner account and SMTP credential from `ai/.env`, then the workflows are imported and published.
- Three workflows: monitor (every minute), acknowledge (signed link to a form), and daily report (07:00, CSV + HTML saved and emailed).
- Excursion lifecycle: start, then alert after `ALERT_DELAY`, escalate after `ESCALATE_AFTER`, then acknowledge (who and the corrective action) and resolve.
- Every decision is a `coldchain_event` in InfluxDB. That's the audit trail, and also the monitor's only state.
- Unit limits live in `ai/config/coldchain/units.json`.
- Mailpit is the demo inbox; Slack and Telegram are optional. Sample theme: `polar`.

**Verified:**
- smoke test: the full lifecycle, a silent unit acknowledged before it can escalate, forged and empty acknowledgements refused, the report, a restart, all 7 panels;
- demo run with shortened timings;
- screenshots of the alert email, the acknowledge form and the dashboard.

**Learned the hard way (already handled):**
- n8n 2.x publishes the version named by `versionId`, so the workflow files carry a content-derived `versionId`. Hand edits need a new one.
- `N8N_CONCURRENCY_PRODUCTION_LIMIT=1`, so a scheduled run and a manual check can't both send the same alert.
- `WEBHOOK_URL` is deprecated; the template uses `N8N_WEBHOOK_URL`.

**Follow-ups:**
- n8n runs as uid 1000. On other uids, `ai/data/reports` needs `chmod o+w` (documented).
- The workflow and dashboard generators lived in a scratch directory and aren't in the repo. The JSON files are the source of truth now.

### 3 · mqtt-influx-grafana-ollama-letta

**What it does:**
- A one-shot `letta-init` container provisions the agent: its tools, its memory blocks (`persona`, `human`, `equipment`), and four seeded records in archival memory. Dates are relative to the first start.
- Re-provisioning refreshes the tools, secrets and model, and never touches memory.
- Tools: `equipment_status`, `equipment_trend`, `maintenance_history`, `log_maintenance`. The last writes to archival memory and to InfluxDB `maintenance_event`, which Grafana shows as a log and as annotations.
- A gateway between Letta and Ollama turns thinking off (`AGENT_THINK=false`).
- Ollama runs with `OLLAMA_CONTEXT_LENGTH=16384`.
- The demo backfills 14 days of history in which pump-2's bearing wear repeats the pattern before its last failure.
- Sample theme: `gearwise`, with the Agent tab defaulting to Letta.

**Verified:**
- smoke test: real Letta against a scripted fake model (`tests/fake_llm.py`), covering provisioning, every tool, both kinds of memory, re-provisioning, and all 7 panels;
- a real-model run with `gemma4:e2b`, results below.

| Message | Time | Result |
|---|---|---|
| *How is pump-2 doing?* | 122 s | Trend and zone right; skipped the history |
| Logging Ann Lee's bearing replacement | 40 s | Logged, pulled the history, linked the missed re-greasing |
| *When was pump-2 last serviced, and by whom?* | 19 s | Correct answer |

**Learned the hard way (already handled):**
- Letta posts embeddings to a path Ollama doesn't serve unless its Ollama URL ends in `/v1`.
- Ollama's default 4096-token context truncates Letta's prompt of about 6,000 tokens.
- With thinking on, an answer took 447 s, over p4n4-api's 5-minute Letta timeout.
- The small model invents tags for `archival_memory_search`; `maintenance_history` takes just the machine ID.
- Letta reads every function in a tool file as a tool, so tools can't have nested helpers.

**Follow-ups:**
- `gemma4:e2b` sometimes skips a step or misreads a limit. A bigger model on a GPU is the realistic setup (documented).
- The simulator ignores logged maintenance. pump-2 keeps degrading after a "repair"; it could reset its wear when a bearing replacement is logged.
- The dashboard generator isn't in the repo either.

### To commit

The user handles git. These are the commands:

```bash
git -C tools/templates add projects/mqtt-nodered-influx-grafana projects/mqtt-influx-grafana-n8n \
    projects/mqtt-influx-grafana-ollama-letta README.md docs/roadmap.md
git -C tools/templates commit   # one commit per template, or one for the series
git -C docs add reference/template-registry.md
git -C docs commit -m "docs: list the new demo templates"
```

---

## Pending ideas

In suggested order. The headings keep the numbering from the original brainstorm.

### 4 · `mqtt-influx-grafana-ollama-qdrant`: RAG over equipment manuals

- **Use case:** "pump-3 pressure is high: what does the manual say to check?" The agent answers from live telemetry plus retrieval over PDF manuals and SOPs, citing the page.
- **Teaches:** RAG and grounded answers, compared with model guesses.
- **New services:** Qdrant (vector store), plus an ingest step that chunks and embeds PDFs with Ollama embeddings.
- **Externals:** none. A few sample manuals would ship with it; check their licenses, or write original sample manuals.
- **Notes:** it could reuse the gateway and context settings from #3. Decide whether retrieval lives in a custom agent like `mqtt-influx-grafana-ollama`'s, or as a Letta tool.

### 5 · `chirpstack-mqtt-influx-grafana`: LoRaWAN agriculture

- **Use case:** field soil-moisture, rain and tank-level sensors over LoRaWAN, shown on a Grafana geomap.
- **Teaches:** LoRaWAN payload decoders, a device registry, and long-range, low-power devices.
- **New services:** ChirpStack (plus its Postgres, Redis and gateway bridge), forwarding to Mosquitto.
- **Externals:** a real gateway is optional. The demo needs a simulated gateway or device (for example ChirpStack's simulator, or a UDP packet-forwarder stand-in).
- **Notes:** the heaviest stack of the list. Map ChirpStack's uplink topics onto `sensors/<device>/<measurement>`, or decode into that schema with Telegraf.

### 6 · `mqtt-influx-grafana-edgeimpulse`: vibration anomaly detection on the edge

- **Use case:** `ei-runner` classifies windows of accelerometer data on the device; only scores and labels go over MQTT.
- **Teaches:** the edge-to-cloud split, and why raw data stays on the device.
- **Externals:** an Edge Impulse key, to use your own model.
- **Open question:** `.eim` models are built per platform and normally downloaded with an account, which conflicts with "offline by default". Options:
  1. **Recommended:** a built-in fallback (a statistical anomaly score) in demo mode, switching to `ei-runner` when a key is set.
  2. ONNX Runtime with a bundled public model.

### 7 · `mqtt-nodered-influx-grafana-n8n`: energy and solar load shifting

- **Use case:** Shelly or Tasmota plugs and a solar inverter over MQTT. n8n pulls day-ahead electricity prices; Node-RED moves flexible loads (EV charger, water heater) into cheap or solar-heavy hours. Grafana shows self-consumption and savings.
- **Teaches:** combining Node-RED control (#1) with n8n integration (#2).
- **Externals:** a live price API is optional; the demo uses a canned price curve.

### 8 · `mqtt-influx-grafana-go2rtc-edgeimpulse`: occupancy and footfall counting

- **Use case:** camera, on-device person detection, counts only (privacy by design, like the ALPR template), and the Video tab with optionally blurred frames.
- **Teaches:** computer vision with counts only.
- **Externals:** an Edge Impulse key. It has the same model-distribution question as #6; solve it there first.

---

## Notes for other repos

Spotted while building these templates; nothing has been changed outside `tools/templates` and p4n4-docs.

- **p4n4-iot:**
  - `config/node-red/settings.js` uses `editorTheme.palette.editable`, which Node-RED 5 deprecates in favor of `externalModules.palette.allowInstall`. Template #1 already uses the new setting.
- **p4n4-ai:**
  - The `n8n` service still sets `N8N_BASIC_AUTH_*`, which n8n removed in 1.0.
  - Its manual "import, then publish" steps likely hit the `versionId` issue when workflows are re-imported (untested).
  - `WEBHOOK_URL` is deprecated in favor of `N8N_WEBHOOK_URL`.
  - For Letta, the same `/v1` base URL and context-length fixes from #3 probably apply (untested).
- **p4n4-api:**
  - Letta calls time out after 300 s (`LETTA_TIMEOUT`). On CPU-only hosts, multi-step agent answers come close to that limit.
