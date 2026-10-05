# mqtt-nodered-influx-grafana

> Greenhouse climate control: the [`mqtt-influx-grafana`](https://github.com/raisga/p4n4-templates/tree/main/projects/mqtt-influx-grafana) telemetry pipeline plus **Node-RED** rules that act on the readings. A fan switches on temperature and an irrigation valve on soil moisture. Node-RED publishes the commands back over **MQTT**, and **Grafana** shows the readings, the actuators and the reason behind every decision.

| | |
|---|---|
| Layers | `iot` |
| Services | Mosquitto · Telegraf · InfluxDB · Node-RED · Grafana (+ demo greenhouse) |
| Version | 0.1.0 |

```
  zones ──sensors/<zone>/<measurement>──► mqtt ──► telegraf ──┬──► influxdb ──► grafana
    ▲                                     :1883  (readings +  │      :8086       :3000
    │                                       │      commands)  └──► ./data/archive/
    │                                       ▼
    │                                   node-red :1880
    │                                   fan rule · valve rule · manual override · valve watchdog
    │                                       │
    └──actuators/<zone>/<actuator>/set──────┘   (retained)
```

The other templates only read data. This one closes the loop: readings go in, decisions come out, and devices act on them. It's the pattern behind most building, farm and plant automation.

---

## Quick start

```bash
cp -r mqtt-nodered-influx-grafana my-greenhouse && cd my-greenhouse
cp .env.example .env        # change passwords and the token
docker compose up -d
```

| Open | For | Login |
|---|---|---|
| <http://localhost:3000> | Grafana: the **Greenhouse** dashboard is the home page | `admin` / `adminpassword` |
| <http://localhost:1880> | Node-RED: the rules, in the *Greenhouse control* flow | `admin` / `adminpassword` |

The `demo` profile is on in `.env.example`, so a simulated greenhouse with two zones (`zone-a`, `zone-b`) starts with the stack. It obeys the commands: the fan cools the zone, and the valve wets the soil. The sun follows a compressed 15-minute day (`SIMULATOR_DAY`), so within a few minutes the fans cycle around simulated noon and the valves water each zone.

Try the manual override while it runs:

```bash
docker compose exec mqtt mosquitto_pub -r -t actuators/zone-a/fan/mode -m on     # force on
docker compose exec mqtt mosquitto_pub -r -t actuators/zone-a/fan/mode -m auto   # back to the rule
```

Each change appears in Grafana's *Control decisions* table with its reason.

When real devices are publishing, turn the simulator off:

```bash
# .env
COMPOSE_PROFILES=
```

```bash
docker compose up -d --remove-orphans
```

On Linux, set `ARCHIVE_UID` / `ARCHIVE_GID` in `.env` to the output of `id -u` / `id -g`. Telegraf writes the archive as that user.

---

## Data contract

A **zone** is one controlled area (a greenhouse, a bench, a bed) with its own sensors and actuators. It takes the `<device-id>` position of the MING topic convention, so dashboards and queries carry over.

### Readings: devices → broker

```
sensors/<zone>/<measurement>
```

| Measurement | Payload | Used by |
|---|---|---|
| `temperature` | °C, `{"value": 27.4, "unit": "C"}` or `27.4` | fan rule |
| `soil_moisture` | %, same formats | valve rule, valve watchdog |
| `humidity` | %, same formats | dashboard only |
| `fan`, `valve` | `1` (on) or `0` (off), the **actual** state the device reports | dashboard |

Payloads follow the [`mqtt-influx-grafana` contract](https://github.com/raisga/p4n4-templates/tree/main/projects/mqtt-influx-grafana#payload): a JSON object whose `value` is plotted, or a bare number. Any other measurement is stored and shown too; no rule acts on it.

Reporting `fan` and `valve` separately from the commands is deliberate. A command is what Node-RED asked for; the reported state is what happened. When they differ (a tripped breaker, a device offline), the dashboard shows it.

### Commands: Node-RED → devices

```
actuators/<zone>/<actuator>/set        actuator: fan | valve
```

```json
{"state": "on", "mode": "auto", "reason": "temperature 28.6 °C ≥ 28 °C", "at": "2026-10-05T12:38:29.985Z"}
```

Commands are **retained** and sent at QoS 1, so a device that reconnects gets its current state straight away. Node-RED publishes only when the state or the mode changes. A device needs just `state`; `reason` and `mode` are there for people.

### Overrides: operator → Node-RED

```
actuators/<zone>/<actuator>/mode       auto | on | off   (retained)
```

| Mode | Effect |
|---|---|
| `auto` (default) | The rules decide |
| `on` / `off` | Forced; readings and the watchdog are ignored until the mode is `auto` again |

Publish them retained, so they survive a Node-RED restart. An empty retained message clears the override (`mosquitto_pub -r -n -t …/mode`). Unknown modes are logged and ignored.

### InfluxDB schema

```
bucket       raw_telemetry   (INFLUXDB_BUCKET, retention INFLUXDB_RETENTION=30d)

measurement  sensor_data                                  every reading
tags         device=<zone>, sensor=<measurement>
fields       value, unit, ...

measurement  actuator_command                             every command Node-RED sent
tags         device=<zone>, actuator=fan|valve
fields       state (1 on, 0 off), mode, reason
```

`sensor_data` is the MING schema. `actuator_command` is stored by Telegraf from the command topic (`config/telegraf/parse_command.star`), not by Node-RED, so the flow only decides and never writes to a database. Retained commands are stored again each time Telegraf reconnects: that repeats a point, not a decision.

---

## The rules

Everything lives in one Node-RED flow, *Greenhouse control* (`config/node-red/flows/flows.json`), built from core nodes only. Every rule is a short function node.

```
sensors/+/+ ──► Normalize reading ──► By measurement ─┬─► Fan rule (hysteresis) ───┐
                                                      └─► Valve rule (hysteresis) ─┤
actuators/+/+/mode ──► Store mode ─────────────────────────────────────────────────┼─► Decide and publish ──► actuators/<zone>/<actuator>/set
every 5 s ──► Valve watchdog ──────────────────────────────────────────────────────┘     on change
actuators/+/+/set ──► Remember last command   (picks up retained commands after a restart)
```

| Rule | Behavior | Settings (`.env`) |
|---|---|---|
| Fan | On at `TEMP_HIGH`; off once it has fallen `TEMP_HYSTERESIS` below. In between it keeps its state, so it doesn't chatter around one threshold | `TEMP_HIGH=28`, `TEMP_HYSTERESIS=2` |
| Valve | Opens at `SOIL_LOW`, closes at `SOIL_HIGH` | `SOIL_LOW=30`, `SOIL_HIGH=45` |
| Valve watchdog | Closes a valve the rules opened if its zone sent no `soil_moisture` for `STALE_AFTER` s (a dead sensor shouldn't water blind), or if it has been open for `VALVE_MAX_RUN` s (stuck sensor, burst pipe). After a max-run cutoff the valve stays closed for `VALVE_LOCKOUT` s, whatever the readings say | `STALE_AFTER=120`, `VALVE_MAX_RUN=900`, `VALVE_LOCKOUT=1800` |
| Manual override | `mode` `on` / `off` beats the rules and the watchdog | none |

Change a threshold in `.env`, then `docker compose up -d node-red`. The Temperature and Soil moisture panels draw the default thresholds as dashed lines, so update them in the dashboard if you change these.

State lives in Node-RED's memory. After a restart, *Remember last command* reloads the retained commands. The watchdog closes any open valve until the next soil reading arrives, which fails safe.

### Changing the flow

Edit in the Node-RED editor and click **Deploy**. Node-RED saves the flow back into `config/node-red/flows/flows.json`, because the directory is mounted read-write, so your changes stay with the project. (On Linux, Node-RED runs as uid 1000; if that isn't you, `chmod o+w config/node-red/flows` lets it save.) Some ideas:

- a humidity rule that also runs the fan above, say, 85 %;
- a schedule, e.g. an `inject` node with a crontab that waters at 06:00 regardless;
- notifications: an `http request` node to a webhook when the watchdog trips.

Palette nodes you install go into the `node-red-data` volume, not the template. Add them to a custom image if the project depends on them.

---

## Wiring real devices

Any device that speaks MQTT works: an ESP32 with a relay board, a Shelly or a Tasmota plug, a PLC gateway. Each one needs to:

1. publish readings on `sensors/<zone>/<measurement>`;
2. subscribe to `actuators/<zone>/<actuator>/set` and switch on `"state"`;
3. publish what it actually did on `sensors/<zone>/fan` or `…/valve` (`1` / `0`), ideally on every change and periodically.

Off-the-shelf firmware usually wants its own topics. Bridge them with a small Node-RED flow: subscribe to `actuators/zone-a/fan/set`, map `state` to the plug's command topic, and map the plug's status topic back to `sensors/zone-a/fan`.

Devices on an external broker can't be controlled through the [bridge](#external-mqtt-broker): it is inbound only, so commands never leave the local broker.

Make the device fail safe as well. A valve controller should close on its own when it loses the broker, rather than trust that the last retained command still holds.

---

## File-system archive

The same two archives as [`mqtt-influx-grafana`](https://github.com/raisga/p4n4-templates/tree/main/projects/mqtt-influx-grafana#file-system-archive), under `./data/archive/`. They are rotated every `ARCHIVE_ROTATION_INTERVAL`, with 30 files kept.

| Path | Format | Contains |
|---|---|---|
| `raw/mqtt.jsonl` | JSON Lines | every message on `sensors/#` and `actuators/#` (readings, commands, mode changes), as received |
| `lineprotocol/telemetry.lp` | InfluxDB line protocol | exactly the points written to InfluxDB: `sensor_data` and `actuator_command` |

```
actuator_command,actuator=fan,device=zone-a mode="auto",reason="temperature 28.64 °C ≥ 28 °C",state=1 1791204709985000000
```

The raw archive is the audit trail of who switched what: rules (`…/set`) and operators (`…/mode`).

---

## Configuration

| Variable | Default | Purpose |
|---|---|---|
| `COMPOSE_PROFILES` | `demo` | `demo` starts the simulated greenhouse |
| `NODE_RED_USER` / `NODE_RED_PASSWORD` | `admin` / `adminpassword` | Node-RED editor login; an empty password refuses every login |
| `TEMP_HIGH` / `TEMP_HYSTERESIS` | `28` / `2` | Fan rule (°C) |
| `SOIL_LOW` / `SOIL_HIGH` | `30` / `45` | Valve rule (% soil moisture) |
| `STALE_AFTER` / `VALVE_MAX_RUN` / `VALVE_LOCKOUT` | `120` / `900` / `1800` | Valve watchdog (seconds) |
| `INFLUXDB_USERNAME` / `INFLUXDB_PASSWORD` | `admin` / `adminpassword` | InfluxDB UI login |
| `INFLUXDB_ORG` | `ming` | InfluxDB organization |
| `INFLUXDB_TOKEN` | `p4n4-stack-token` | Admin token, used by Telegraf and Grafana |
| `INFLUXDB_BUCKET` | `raw_telemetry` | Bucket for readings and commands (dashboards follow it) |
| `INFLUXDB_RETENTION` | `30d` | Bucket retention, applied on first start only |
| `ARCHIVE_ROTATION_INTERVAL` | `24h` | Archive file rotation interval |
| `ARCHIVE_UID` / `ARCHIVE_GID` | `1000` | Owner of archive files |
| `GRAFANA_USER` / `GRAFANA_PASSWORD` | `admin` / `adminpassword` | Grafana login |
| `GRAFANA_SUB_PATH` | `/` | Path Grafana serves under; `/grafana/` when p4n4-dashboard proxies it on its own origin |
| `GRAFANA_ALLOW_EMBEDDING` | `false` | `true` lets pages on other origins frame Grafana. The dashboard's web build needs it for its Grafana tab |
| `GRAFANA_ANONYMOUS` | `false` | `true` lets anyone who reaches port 3000 view dashboards without signing in (Viewer role) |
| `SIMULATOR_DEVICES` | `zone-a zone-b` | Zones the simulator runs |
| `SIMULATOR_INTERVAL` | `5` | Seconds between simulated readings |
| `SIMULATOR_DAY` | `900` | Length of a simulated day, in seconds |
| `MQTT_REMOTE_*` | *(empty: disabled)* | Pull topics from an external broker: see below |

InfluxDB's org, bucket, retention, and credentials are applied **only on first start**. Changing them later requires `docker compose down -v`, which deletes the stored data. The file-system archive survives this.

### External MQTT broker

The local broker can pull readings in from another broker. The setup is the same as in [`mqtt-influx-grafana`](https://github.com/raisga/p4n4-templates/tree/main/projects/mqtt-influx-grafana#external-mqtt-broker), with the same `MQTT_REMOTE_*` variables. The bridge is **inbound only**: Node-RED's rules see bridged readings, but its commands reach only devices on the local broker.

### Where things live

```
config/mosquitto/mosquitto.conf              broker (anonymous access: see Security)
config/mosquitto/bridge.sh                   external broker bridge, from MQTT_REMOTE_*
config/node-red/flows/flows.json             the rules (saved here by the editor)
config/node-red/settings.js                  editor login, from NODE_RED_USER / NODE_RED_PASSWORD
config/telegraf/telegraf.conf                inputs, outputs and archive settings
config/telegraf/parse_sensor.star            sensors/<zone>/<measurement> → sensor_data
config/telegraf/parse_command.star           actuators/<zone>/<actuator>/set → actuator_command
config/grafana/provisioning/datasources/     InfluxDB datasource (uid influxdb-telemetry)
config/grafana/provisioning/dashboards/      Greenhouse dashboard (uid p4n4-greenhouse)
theme/                                       p4n4-dashboard white-label theme (sample: canopy)
scripts/simulate.sh                          demo greenhouse
tests/smoke.sh                               end-to-end test
data/archive/                                file-system archive (gitignored)
```

Dashboards are provisioned read-only. To change one, edit it in Grafana, export the JSON, and replace `greenhouse.json`.

---

## p4n4-dashboard

`.p4n4.json` has a `dashboard` block that [p4n4-dashboard](https://github.com/raisga/p4n4-dashboard) reads through p4n4-api:

```json
"dashboard": {
  "grafana_path": "/d/p4n4-greenhouse/greenhouse",
  "tabs": ["services", "edge", "grafana"],
  "theme": "theme"
}
```

The Grafana tab opens the Greenhouse dashboard in kiosk mode. Set `GRAFANA_ANONYMOUS=true` so client users can view it without a Grafana login, and `GRAFANA_ALLOW_EMBEDDING=true` for the dashboard's web build. `theme/` ships `canopy`, a sample brand; replace it with your client's (see the [greenhouse use case](../../docs/use-cases/greenhouse-telemetry.md) for the white-label build).

## Security

These defaults are for a trusted local network, and here they control equipment:

- Mosquitto allows anonymous clients, so **anyone on the network can switch the actuators** by publishing to `actuators/#`. Before connecting real equipment, turn on authentication and an ACL that lets only Node-RED publish `…/set` and only operators publish `…/mode`. See the *MQTT authentication* section of the [p4n4-iot README](https://github.com/raisga/p4n4-iot#readme), and mount the files via `docker-compose.override.yml`.
- The Node-RED editor can change every rule. Set a strong `NODE_RED_PASSWORD`, and don't expose port 1880 beyond the people who maintain the rules.
- Change every password and `INFLUXDB_TOKEN` in `.env` before exposing any port.
- Keep hardware interlocks (thermal cut-outs, float switches, valve timers) in place. Software rules are a convenience, not a safety system.
- `GRAFANA_ANONYMOUS=true` and `GRAFANA_ALLOW_EMBEDDING=true` carry the same caveats as in [`mqtt-influx-grafana`](https://github.com/raisga/p4n4-templates/tree/main/projects/mqtt-influx-grafana#security).

---

## Testing

```bash
./tests/smoke.sh          # starts a throwaway copy, drives every rule, checks every sink, tears down
KEEP=1 ./tests/smoke.sh   # leave it running for inspection
```

The test runs with the simulator off and a fast watchdog (`STALE_AFTER=6`, `VALVE_MAX_RUN=12`), and checks:
- the fan's hysteresis band;
- the manual override, including an unknown mode;
- the valve rule, the stale-sensor cutoff, and the max-run cutoff with its lockout;
- commands and readings in InfluxDB and both archives;
- the external broker bridge;
- every dashboard panel query.

The test stack drops the fixed container names, host ports and `p4n4-net` network, so it runs alongside any other stack.
