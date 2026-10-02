# mqtt-influx-grafana

> Minimal telemetry pipeline: devices publish over **MQTT**, readings are stored in **InfluxDB** and archived on the **file system**, and **Grafana** renders them.

| | |
|---|---|
| Layers | `iot` |
| Services | Mosquitto · Telegraf · InfluxDB · Grafana (+ demo simulator) |
| Version | 0.1.0 |

```
  devices ──MQTT──► mqtt ──► telegraf ──┬──► influxdb ──► grafana
                    :1883               │      :8086       :3000
                                        └──► ./data/archive/
                                               ├── raw/mqtt.jsonl             (every message as received)
                                               └── lineprotocol/telemetry.lp  (every point written to InfluxDB)
```

Unlike the full MING stack (`stacks/iot`), this template has no Node-RED. Ingest is a single declarative Telegraf config.

---

## Quick start

```bash
cp -r mqtt-influx-grafana my-project && cd my-project
cp .env.example .env        # change passwords and the token
docker compose up -d
```

Then open Grafana at <http://localhost:3000> (default login `admin` / `adminpassword`). The **Telemetry** dashboard is the home page. The `demo` profile is enabled in `.env.example`, so a simulator fills it with readings from `dev-01` and `dev-02` right away.

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

### Topic

```
sensors/<device-id>/<measurement>
```

This is the same convention as the MING stack. Topics with a different shape are kept in the raw archive but are not written to InfluxDB.

### Payload

Either a JSON object or a bare JSON value:

```bash
mosquitto_pub -t sensors/greenhouse-1/temperature -m '{"value": 23.4, "unit": "C"}'
mosquitto_pub -t sensors/greenhouse-1/humidity    -m '51.2'
```

| Payload member | Stored as |
|---|---|
| number | float field (integers are written as floats so `23` and `23.4` don't conflict) |
| string, boolean | string / boolean field |
| `device`, `sensor` | ignored: they come from the topic |
| nested objects, arrays, `null` | ignored |
| bare value (`51.2`) | field `value` |

Dashboards plot the `value` field. Other fields (for example `unit`) are stored and shown in the *Devices* table.

### InfluxDB schema

```
bucket       raw_telemetry   (INFLUXDB_BUCKET, retention INFLUXDB_RETENTION=30d)
measurement  sensor_data
tags         device=<device-id>, sensor=<measurement>
fields       value, unit, ...
```

This is the schema the MING stack's Node-RED flow writes, so dashboards and queries carry over between the two.

---

## File-system archive

Telegraf writes two archives under `./data/archive/`. Files rotate every `ARCHIVE_ROTATION_INTERVAL` (default `24h`). Up to 30 rotated files are kept per format; change `rotation_max_archives` in `config/telegraf/telegraf.conf` (`-1` keeps all of them).

| Path | Format | Contains | Use it to |
|---|---|---|---|
| `raw/mqtt.jsonl` | JSON Lines | every message on `sensors/#`, including ones that failed to parse | audit what devices actually sent, debug parsing |
| `lineprotocol/telemetry.lp` | InfluxDB line protocol | exactly the points written to InfluxDB | back up, restore, or replay into another bucket |

```jsonc
// raw/mqtt.jsonl
{"payload":"{\"value\": 22.41, \"unit\": \"C\"}","received_at":"2026-10-02T14:03:11.402Z","topic":"sensors/dev-01/temperature"}
```

```
# lineprotocol/telemetry.lp
sensor_data,device=dev-01,sensor=temperature unit="C",value=22.41 1790949791402000000
```

Restore or replay the line-protocol archive:

```bash
docker compose cp data/archive/lineprotocol/telemetry.lp influxdb:/tmp/telemetry.lp
docker compose exec influxdb influx write --bucket raw_telemetry --precision ns --file /tmp/telemetry.lp
```

Raw payloads are stored with surrounding whitespace trimmed. Telegraf flushes both files every 5 s.

---

## Configuration

| Variable | Default | Purpose |
|---|---|---|
| `COMPOSE_PROFILES` | `demo` | `demo` starts the simulator |
| `INFLUXDB_USERNAME` / `INFLUXDB_PASSWORD` | `admin` / `adminpassword` | InfluxDB UI login |
| `INFLUXDB_ORG` | `ming` | InfluxDB organization |
| `INFLUXDB_TOKEN` | `p4n4-stack-token` | Admin token, used by Telegraf and Grafana |
| `INFLUXDB_BUCKET` | `raw_telemetry` | Bucket for readings (dashboards follow it) |
| `INFLUXDB_RETENTION` | `30d` | Bucket retention, applied on first start only |
| `ARCHIVE_ROTATION_INTERVAL` | `24h` | Archive file rotation interval |
| `ARCHIVE_UID` / `ARCHIVE_GID` | `1000` | Owner of archive files |
| `GRAFANA_USER` / `GRAFANA_PASSWORD` | `admin` / `adminpassword` | Grafana login |
| `SIMULATOR_DEVICES` | `dev-01 dev-02` | Device ids the simulator publishes as |
| `SIMULATOR_INTERVAL` | `5` | Seconds between simulated readings |

InfluxDB's org, bucket, retention, and credentials are applied **only on first start**. Changing them later requires `docker compose down -v`, which deletes the stored data. The file-system archive survives this.

### Where things live

```
config/mosquitto/mosquitto.conf              broker (anonymous access: see Security)
config/telegraf/telegraf.conf                inputs, outputs and archive settings
config/telegraf/parse_sensor.star            topic + payload → sensor_data point
config/grafana/provisioning/datasources/     InfluxDB datasource (uid influxdb-telemetry)
config/grafana/provisioning/dashboards/      Telemetry dashboard (uid p4n4-telemetry)
scripts/simulate.sh                          demo publisher
tests/smoke.sh                               end-to-end test
data/archive/                                file-system archive (gitignored)
```

Dashboards are provisioned read-only. To change one, edit it in Grafana, export the JSON, and replace `telemetry.json`.

---

## Security

These defaults are for a trusted local network:

- Mosquitto allows anonymous clients. For authentication, follow the *MQTT authentication* section of the [p4n4-iot README](https://github.com/raisga/p4n4-iot#readme) and mount the files via `docker-compose.override.yml`.
- Change every password and `INFLUXDB_TOKEN` in `.env` before exposing any port.

---

## Testing

```bash
./tests/smoke.sh          # starts a throwaway copy, publishes, checks every sink, tears down
KEEP=1 ./tests/smoke.sh   # leave it running for inspection
```

The test stack drops the fixed container names, host ports and `p4n4-net` network, so it runs alongside any other stack. Besides checking InfluxDB and both archives, it runs every dashboard panel query through Grafana and expects data from each one.
