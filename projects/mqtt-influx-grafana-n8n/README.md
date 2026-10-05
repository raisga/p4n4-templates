# mqtt-influx-grafana-n8n

> Cold-chain compliance: temperature monitoring for fridges, freezers and cold rooms in a kitchen, pharmacy or lab. Readings flow over **MQTT** into **InfluxDB**. **n8n** workflows then:
> - alert by email when a unit stays out of range;
> - escalate when nobody acknowledges the alert;
> - record who acted and what they did;
> - email and save a daily record.
>
> **Grafana** shows the units and the full excursion log. With no accounts, a local inbox catches every email.

| | |
|---|---|
| Layers | `iot`, `ai` |
| Services | iot: Mosquitto · Telegraf · InfluxDB · Grafana (+ demo cold rooms) · ai: n8n · Mailpit |
| Version | 0.1.0 |

```
  iot/  cold rooms ──MQTT──► mqtt ──► telegraf ──┬──► influxdb ◄─────────────┐ ──► grafana
        sensors/<unit>/temperature               └──► iot/data/archive/      │      :3000
        sensors/<unit>/door                                                  │
                                                       readings, excursions  │ excursion log
  ai/   n8n :5678 ───────────────────────────────────────────────────────────┘
          monitor (every minute) ──► email: alert → escalation → resolved   ──► Mailpit :8025 (demo) or SMTP
                                     + Slack, Telegram (optional)
          acknowledge (signed link in the alert) ──► who, and the corrective action
          daily report (07:00) ──► email with CSV ──► ai/data/reports/
        └───────────── both layers on p4n4-net, which the iot layer creates ─────────────┘
```

Rules alone don't make a cold chain compliant. You also need proof that someone responded to every excursion, and a daily record of it. That part is workflow, not telemetry, which is why it runs in n8n.

---

## Quick start

```bash
cp -r mqtt-influx-grafana-n8n my-coldchain && cd my-coldchain
for layer in iot ai; do cp $layer/.env.example $layer/.env; done   # change passwords and secrets
chmod o+w ai/data/reports                                         # unless your uid is 1000 (see below)
p4n4 up                     # or: (cd iot && docker compose up -d) && (cd ai && docker compose up -d)
```

| Open | For | Login |
|---|---|---|
| <http://localhost:3000> | Grafana: the **Cold chain** dashboard is the home page | `admin` / `adminpassword` |
| <http://localhost:8025> | Mailpit: every email n8n sends | none |
| <http://localhost:5678> | n8n: the three workflows | `admin@example.com` / `adminpassword` |

The `demo` profile simulates three units (`fridge-01`, `fridge-02`, `freezer-01`). Their doors open now and then, which causes short spikes that are logged but not alerted. One minute after start, `fridge-02`'s door is **left open for an hour**. With the default timings, Mailpit then gets:

| After | Email | To |
|---|---|---|
| ~17 min | **ALERT** Dairy walk-in at 7.3 °C, above 5 °C; door open | `ALERT_EMAIL_TO` |
| ~47 min | **ESCALATION** … not acknowledged | `ESCALATION_EMAIL_TO` (and `ALERT_EMAIL_TO`) |
| ~65 min | **Resolved** … back in range | everyone who was told |

Open the alert and follow **Acknowledge and record the corrective action**. Escalation stops, and the excursion log shows your name and action. To see it all in a few minutes, set `ALERT_DELAY=120` and `ESCALATE_AFTER=180` in `ai/.env` and run `docker compose up -d` in `ai/`.

When real sensors are publishing, turn the simulator off in `iot/.env` (`COMPOSE_PROFILES=`). When `SMTP_*` points at a real mail server, turn Mailpit off in `ai/.env`.

---

## Units

Each unit's limits live in `ai/config/coldchain/units.json`, the one file to edit for a new site:

```json
[
  { "id": "fridge-01", "name": "Vaccine fridge", "location": "Pharmacy", "min": 2, "max": 8 },
  { "id": "fridge-02", "name": "Dairy walk-in", "location": "Kitchen", "min": 0, "max": 5 },
  { "id": "freezer-01", "name": "Freezer", "location": "Kitchen", "min": -25, "max": -15 }
]
```

`id` is the `<unit>` in the topic. The monitor reads the file on every run, so changes apply within a minute, with no restart. It watches only the units listed here: a sensor that isn't in the file is stored and graphed, but never alerted. Conversely, a listed unit that has never reported is an excursion (offline).

## Data contract

```
sensors/<unit>/temperature     {"value": 4.6, "unit": "C"} or 4.6   (°C)
sensors/<unit>/door            1 (open) or 0 (closed)               (optional)
```

The topics and payloads follow the [`mqtt-influx-grafana` contract](https://github.com/raisga/p4n4-templates/tree/main/projects/mqtt-influx-grafana#data-contract). Report at least once a minute, or within `OFFLINE_AFTER`. A door sensor is optional; when it reports open during an excursion, the alert says so.

### What n8n writes to InfluxDB

| Measurement | Tags | Fields | Written |
|---|---|---|---|
| `coldchain_event` | `device`, `excursion` (its start, ISO 8601), `type` | `message`, `temperature`; for an `ack`, also `by` and `action` | on every decision |
| `coldchain_status` | `device` | `state`, `in_range` (1/0), `temperature` | every run |
| `coldchain_limit` | `device` | `min`, `max` | every run, for Grafana |

`coldchain_event` types: `start`, `alert`, `escalate`, `ack`, `resolve`. This log is the audit trail, and it is also the monitor's only state. Each run reads the open excursions back from it, so restarts, edits and acknowledgements that arrive mid-run lose nothing.

`state`: `ok`, `excursion` (not alerted yet), `offline`, `alerted`, `escalated`, `acknowledged`.

---

## The workflows

Three workflows in `ai/config/n8n/workflows/`, imported and published on n8n's first start. Each one carries a note explaining it.

### Cold chain · monitor

Runs every minute. You can also trigger a run with `POST /webhook/coldchain/check`, which returns what it saw.

```
Every minute ─┐                                                                     ┌─► Send email ─────────┐
Check now ────┴─► Read units ─► Query readings ─► Query excursions ─► Evaluate ─► Route ─┼─► Slack? ─► Post to Slack
                                                                                    ├─► Telegram? ─► Send to Telegram
                                                                                    └─► Write to InfluxDB ─► Summary
```

| Step | When | Who hears |
|---|---|---|
| start | a unit is out of range, or silent for `OFFLINE_AFTER` | nobody: short blips such as loading stock are only logged |
| alert | the excursion has lasted `ALERT_DELAY` | `ALERT_EMAIL_TO` (+ Slack, Telegram) |
| escalate | alerted, not acknowledged for `ESCALATE_AFTER` | `ESCALATION_EMAIL_TO` and `ALERT_EMAIL_TO` |
| resolve | back in range | everyone who was alerted. If nobody acknowledged it, the email asks for the corrective action |

n8n runs one execution at a time (`N8N_CONCURRENCY_PRODUCTION_LIMIT=1`), so a scheduled run and a manual check can't both send the same alert. Notifications go out before the run writes its decisions. If the email fails, the run stops, nothing is recorded, and the next run tries again: a failed alert is retried, never silently lost. Failed runs show in n8n under **Executions**. Slack and Telegram are best effort: when they fail, the email still counts.

### Cold chain · acknowledge

The link in an alert opens a short form (`GET /webhook/coldchain/ack`) asking for a name and the corrective action. Submitting it records a `coldchain_event` of type `ack`, which stops escalation. Links are signed with `ACK_SECRET`, so a link only works for the unit and excursion it was sent for. Unsigned or forged links are refused.

The link points at `http://<N8N_HOST>:5678/`. For staff who acknowledge from their phones, set `N8N_HOST` to a name or IP the phones can reach.

### Cold chain · daily report

Runs at 07:00 in `TZ` and covers the previous day. For each unit it lists:
- min, max and mean temperature;
- minutes out of range;
- minutes without data;
- every excursion, with who acknowledged it and the corrective action.

Each unit gets a status: `OK`, `EXCURSION, ACTIONED` or `ACTION NEEDED`. The record is:
- saved as `ai/data/reports/coldchain-<date>.csv` (hourly log) and `.html`;
- emailed to `REPORT_EMAIL_TO` with the CSV attached.

```bash
curl 'http://localhost:5678/webhook/coldchain/report?date=today'        # or ?date=2026-10-04
```

InfluxDB keeps readings for `INFLUXDB_RETENTION` (30 days). The saved reports are the long-term record, so back up `ai/data/reports/`.

### Changing them

Edit in n8n and publish. The template's workflows are imported only on the first start, so your edits survive restarts and upgrades of the template files. `N8N_REIMPORT_WORKFLOWS=true` overwrites them with the template's versions on the next start. n8n publishes the version named by each file's `versionId`, so if you edit the JSON files directly, give the changed file a new `versionId` (any new UUID), or the old version stays live. Workflows exported from n8n already carry a new one. Workflows read their settings from `ai/.env` through `$env`, so most changes need no editing at all. Ideas:
- an SMS gateway node next to Slack and Telegram;
- a weekly summary;
- an error workflow that pages someone when the monitor itself keeps failing.

---

## Configuration

`iot/.env` holds the same variables as [`mqtt-influx-grafana`](https://github.com/raisga/p4n4-templates/tree/main/projects/mqtt-influx-grafana#configuration), plus the simulator settings below. `ai/.env`:

| Variable | Default | Purpose |
|---|---|---|
| `COMPOSE_PROFILES` | `mailpit` | `mailpit` starts the local inbox |
| `N8N_OWNER_EMAIL` / `N8N_OWNER_PASSWORD` | `admin@example.com` / `adminpassword` | n8n login, applied on every start; n8n won't start without a password |
| `N8N_HOST` | `localhost` | Where people reach n8n; the base of the acknowledge links |
| `N8N_ENCRYPTION_KEY` | `change-me-…` | Encrypts stored credentials; set it once, before the first start |
| `N8N_REIMPORT_WORKFLOWS` | `false` | `true` replaces the workflows with the template's on the next start |
| `ALERT_DELAY` / `ESCALATE_AFTER` / `OFFLINE_AFTER` | `900` / `1800` / `600` | Rule timings, in seconds |
| `ACK_SECRET` | `change-me-ack-secret` | Signs acknowledge links (`openssl rand -hex 32`) |
| `EMAIL_FROM` | `coldchain@example.com` | Sender address |
| `ALERT_EMAIL_TO` / `ESCALATION_EMAIL_TO` / `REPORT_EMAIL_TO` | `on-duty@…` / `manager@…` / `manager@…` | Comma-separated recipients |
| `SMTP_HOST` / `SMTP_PORT` | `mailpit` / `1025` | Mail server |
| `SMTP_SECURE` / `SMTP_STARTTLS` | `false` / `false` | TLS from the start (465) / upgrade with STARTTLS (587) |
| `SMTP_USER` / `SMTP_PASSWORD` | *(empty)* | Mail server login |
| `SLACK_WEBHOOK_URL` | *(empty: off)* | Slack incoming webhook |
| `TELEGRAM_BOT_TOKEN` / `TELEGRAM_CHAT_ID` | *(empty: off)* | Telegram bot and chat |
| `INFLUXDB_TOKEN` / `INFLUXDB_ORG` / `INFLUXDB_BUCKET` | as in `iot/.env` | Must match the iot layer |

The SMTP settings become an n8n credential on every start (`ai/config/n8n/entrypoint.sh`), so the password lives only in `ai/.env`. Change `.env`, then run `docker compose up -d` in `ai/`.

Simulator (`iot/.env`, profile `demo`):

| Variable | Default | Purpose |
|---|---|---|
| `SIMULATOR_UNITS` | `fridge-01:5 fridge-02:2.5 freezer-01:-20` | `<unit>:<setpoint °C>`; ids must match `units.json` |
| `SIMULATOR_FAULT_UNIT` | `fridge-02` | Unit whose door is left open; empty for none |
| `SIMULATOR_FAULT_AFTER` / `_FOR` / `_EVERY` | `60` / `3600` / `14400` | When, for how long and how often (seconds) |

### Reports directory

n8n runs as uid 1000 and writes `ai/data/reports/`. If your uid isn't 1000, run `chmod o+w ai/data/reports` (or `chown 1000 ai/data/reports`), or the daily report fails when it saves.

### Where things live

```
iot/config/…                                   broker, Telegraf, Grafana (as in mqtt-influx-grafana)
iot/config/grafana/provisioning/dashboards/    Cold chain dashboard (uid p4n4-coldchain)
iot/scripts/simulate.sh                        demo cold rooms
ai/config/coldchain/units.json                 units and their limits
ai/config/n8n/workflows/                       monitor, acknowledge, daily report
ai/config/n8n/entrypoint.sh                    owner, SMTP credential, first-start import
ai/data/reports/                               daily records (gitignored)
theme/                                         p4n4-dashboard white-label theme (sample: polar)
tests/smoke.sh                                 end-to-end test
```

---

## p4n4-dashboard

```json
"dashboard": {
  "grafana_path": "/d/p4n4-coldchain/cold-chain",
  "tabs": ["services", "edge", "grafana"],
  "theme": "theme"
}
```

The Grafana tab opens the Cold chain dashboard. Set `GRAFANA_ANONYMOUS=true` and `GRAFANA_ALLOW_EMBEDDING=true` in `iot/.env` as in the [greenhouse use case](../../docs/use-cases/greenhouse-telemetry.md). `theme/` ships `polar`, a sample brand with a link to the demo inbox; replace both with your client's.

## Security

- The acknowledge and report webhooks need no login. Ack links are signed, so change `ACK_SECRET`. The report webhook can only email the configured recipients, but don't expose port 5678 to the internet. Put n8n behind HTTPS (a reverse proxy) when phones reach it over untrusted networks.
- Change `N8N_OWNER_PASSWORD`, `N8N_ENCRYPTION_KEY`, every password and `INFLUXDB_TOKEN` before exposing any port.
- Mailpit accepts and shows every email without a login. Keep it to demos and trusted networks.
- Mosquitto allows anonymous clients, so anyone on the network can publish readings. See the *MQTT authentication* section of the [p4n4-iot README](https://github.com/raisga/p4n4-iot#readme).
- This template helps you keep records. It is not a certified monitoring system: check your regulator's requirements (HACCP plan, sensor calibration, record retention) for your site.

---

## Testing

```bash
./tests/smoke.sh          # starts a throwaway copy of both layers, drives every workflow, tears down
KEEP=1 ./tests/smoke.sh   # leave it running for inspection
```

The test runs with short timings (`ALERT_DELAY=20`, `ESCALATE_AFTER=12`) and a stand-in Slack webhook. It walks one excursion through alert, escalation, acknowledgement through the signed link, and resolution. A unit that never reports goes offline and is acknowledged in time, so it must not escalate. The test also checks:
- forged links and empty forms are refused;
- every event and the corrective action are in InfluxDB;
- the daily record is saved and emailed with its CSV;
- a restart doesn't re-import the workflows;
- the iot layer's archives and bridge;
- every Grafana panel.
