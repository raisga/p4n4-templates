#!/usr/bin/env bash
# ==============================================================================
# End-to-end smoke test: starts the template in a throwaway copy, publishes
# readings over MQTT and checks that Node-RED's rules send the right commands
# (hysteresis, manual override, valve watchdog), and that readings and
# commands reach InfluxDB, both archive formats and Grafana. Tears everything
# down (including volumes) on exit.
#
#   ./tests/smoke.sh            # from the template directory
#   KEEP=1 ./tests/smoke.sh     # leave the stack running for inspection
#
# Requires Docker with Compose v2 and python3. The test stack drops the fixed
# container names, host ports and network name, so it runs alongside other stacks.
# ==============================================================================
set -euo pipefail

TEMPLATE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
WORK_DIR="$(mktemp -d)"
cp -a "$TEMPLATE_DIR/." "$WORK_DIR/"
cd "$WORK_DIR"

# Deterministic data: no simulator, archive owned by the current user, and a
# valve watchdog fast enough to test. The bridge pulls from the "remote"
# broker below; its password needs quoting.
REMOTE_USER=smoke
REMOTE_PASSWORD='smoke$pa ss#1'
sed -e 's/^COMPOSE_PROFILES=.*/COMPOSE_PROFILES=/' \
    -e "s/^ARCHIVE_UID=.*/ARCHIVE_UID=$(id -u)/" \
    -e "s/^ARCHIVE_GID=.*/ARCHIVE_GID=$(id -g)/" \
    -e "s/^MQTT_REMOTE_HOST=.*/MQTT_REMOTE_HOST=remote/" \
    -e "s/^MQTT_REMOTE_USER=.*/MQTT_REMOTE_USER=$REMOTE_USER/" \
    -e "s/^MQTT_REMOTE_PASSWORD=.*/MQTT_REMOTE_PASSWORD='$REMOTE_PASSWORD'/" \
    -e 's/^STALE_AFTER=.*/STALE_AFTER=6/' \
    -e 's/^VALVE_MAX_RUN=.*/VALVE_MAX_RUN=12/' \
    .env.example > .env
env_value() { grep -E "^$1=" .env | cut -d= -f2-; }

# Isolate the test from anything else on the host: no fixed container names,
# no published ports, and a private network instead of p4n4-net. "remote"
# stands in for an external broker that requires a login.
cat > docker-compose.override.yml <<'EOF'
services:
  remote:
    image: eclipse-mosquitto:2.0.22
    entrypoint: ["/bin/sh", "-c"]
    command:
      - |
        printf 'listener 1883\nallow_anonymous false\npassword_file /tmp/passwd\n' > /tmp/remote.conf
        mosquitto_passwd -b -c /tmp/passwd "$$REMOTE_USER" "$$REMOTE_PASSWORD"
        chown mosquitto /tmp/passwd
        exec mosquitto -c /tmp/remote.conf
    environment:
      REMOTE_USER: ${MQTT_REMOTE_USER}
      REMOTE_PASSWORD: ${MQTT_REMOTE_PASSWORD}
    networks:
      - p4n4-net
  mqtt:
    container_name: !reset null
    ports: !reset []
    depends_on:
      - remote
  influxdb:
    container_name: !reset null
    ports: !reset []
  telegraf:
    container_name: !reset null
  node-red:
    container_name: !reset null
    ports: !reset []
  grafana:
    container_name: !reset null
    ports: !reset []
  simulator:
    container_name: !reset null
networks:
  p4n4-net:
    name: p4n4-smoke-net
    ipam: !reset {}
EOF
INFLUXDB_BUCKET="$(env_value INFLUXDB_BUCKET)"
GRAFANA_USER="$(env_value GRAFANA_USER)"
GRAFANA_PASSWORD="$(env_value GRAFANA_PASSWORD)"

compose() { docker compose --project-name p4n4-smoke "$@"; }

cleanup() {
    status=$?
    if [ "$status" -ne 0 ]; then
        echo "--- telegraf logs ---"
        compose logs --no-color --tail 50 telegraf || true
        echo "--- node-red logs ---"
        compose logs --no-color --tail 50 node-red || true
    fi
    if [ "${KEEP:-0}" = 1 ]; then
        echo "KEEP=1: stack left running in $WORK_DIR"
    else
        compose down --volumes --remove-orphans >/dev/null 2>&1 || true
        rm -rf "$WORK_DIR"
    fi
    exit "$status"
}
trap cleanup EXIT

pass() { echo "  ok   $*"; }
fail() { echo "  FAIL $*"; exit 1; }

# Retries "$@" every 2s, 30 times (60s) or $TRIES times
eventually() {
    for _ in $(seq "${TRIES:-30}"); do
        if "$@" >/dev/null 2>&1; then return 0; fi
        sleep 2
    done
    return 1
}

echo "Starting stack in $WORK_DIR"
compose up -d --wait --quiet-pull
# Telegraf's three consumers and Node-RED, plus this mosquitto_sub
eventually compose exec -T mqtt sh -c \
    "mosquitto_sub -t '\$SYS/broker/clients/connected' -C 1 -W 2 | awk '\$1 >= 5 { ok = 1 } END { exit !ok }'" \
    || fail "telegraf and node-red did not connect to the broker"

pub() { compose exec -T mqtt mosquitto_pub -q 1 "$@"; }
# The command retained on actuators/$1/set
command() { compose exec -T mqtt mosquitto_sub -t "actuators/$1/set" -C 1 -W 2; }
# Succeeds when the retained command for $1 has state $2 and contains $3
command_is() {
    local retained
    retained="$(command "$1")" || return 1
    grep -q "\"state\":\"$2\"" <<<"$retained" && grep -qF -- "${3:-}" <<<"$retained"
}
# expect_command <zone/actuator> <state> <description> [text]
expect_command() {
    eventually command_is "$1" "$2" "${4:-}" && pass "$3" \
        || { echo "  retained: $(command "$1" || true)"; fail "$3"; }
}
# still_command <zone/actuator> <state> <description>: unchanged after a moment
still_command() {
    sleep 3
    command_is "$1" "$2" && pass "$3" || { echo "  retained: $(command "$1" || true)"; fail "$3"; }
}

echo "Checking the fan rule"
pub -t sensors/smoke-01/temperature -m '{"value": 31.5, "unit": "C"}'
expect_command smoke-01/fan on "fan on at 31.5 °C (TEMP_HIGH 28)" "31.5 °C ≥ 28 °C"
pub -t sensors/smoke-01/temperature -m '27'
still_command smoke-01/fan on "fan stays on at 27 °C, inside the hysteresis band"
pub -t sensors/smoke-01/temperature -m '25.5'
expect_command smoke-01/fan off "fan off at 25.5 °C (≤ 26)" "25.5 °C ≤ 26 °C"

echo "Checking the manual override"
pub -r -t actuators/smoke-01/fan/mode -m on
expect_command smoke-01/fan on "mode on forces the fan on" "manual override"
pub -t sensors/smoke-01/temperature -m '20'
still_command smoke-01/fan on "readings don't change a forced fan"
pub -r -t actuators/smoke-01/fan/mode -m auto
expect_command smoke-01/fan off "mode auto hands the fan back to the rule" '"mode":"auto"'
pub -r -t actuators/smoke-01/fan/mode -m sideways
still_command smoke-01/fan off "an unknown mode is ignored"

echo "Checking the valve rule and watchdog"
pub -t sensors/smoke-02/soil_moisture -m '{"value": 22, "unit": "%"}'
expect_command smoke-02/valve on "valve opens at 22 % (SOIL_LOW 30)" "22 % ≤ 30 %"
# No more smoke-02 readings: STALE_AFTER=6 closes it
expect_command smoke-02/valve off "watchdog closes a valve whose sensor went quiet" "no soil_moisture reading"

pub -t sensors/smoke-03/soil_moisture -m '25'
expect_command smoke-03/valve on "valve opens at 25 %" "25 % ≤ 30 %"
pub -t sensors/smoke-03/soil_moisture -m '50'
expect_command smoke-03/valve off "valve closes at 50 % (SOIL_HIGH 45)" "50 % ≥ 45 %"
# A sensor stuck dry: readings keep coming, so only VALVE_MAX_RUN=12 stops it
pub -t sensors/smoke-03/soil_moisture -m '10'
expect_command smoke-03/valve on "valve opens again at 10 %"
feed_dry() { pub -t sensors/smoke-03/soil_moisture -m '10'; command_is smoke-03/valve off "locked out"; }
TRIES=15 eventually feed_dry && pass "watchdog closes a valve open for VALVE_MAX_RUN and locks it out" \
    || { echo "  retained: $(command smoke-03/valve || true)"; fail "valve was not cut off after VALVE_MAX_RUN"; }
pub -t sensors/smoke-03/soil_moisture -m '10'
still_command smoke-03/valve off "a locked-out valve stays closed while the soil reads dry"

echo "Publishing readings for the dashboard"
pub -t sensors/smoke-01/humidity -m '61'
pub -t sensors/smoke-01/soil_moisture -m '{"value": 38, "unit": "%"}'
pub -t sensors/smoke-01/fan -m '1'
pub -t sensors/smoke-01/valve -m '0'
pub -t sensors/smoke-01/broken -m 'not json'

echo "Checking the stores"
# Number of field $3 values of measurement $2 stored for device $1
influx_count() {
    compose exec -T influxdb influx query --raw \
        "from(bucket: \"$INFLUXDB_BUCKET\") |> range(start: -1h)
         |> filter(fn: (r) => r._measurement == \"$2\" and r.device == \"$1\" and r._field == \"$3\")
         |> group() |> count()"
}
influx_query() { influx_count "$1" sensor_data value; }
# temperature ×4, humidity, soil_moisture, fan, valve
has_readings() { influx_query smoke-01 | grep -qE ',8\s*$'; }
eventually has_readings && pass "InfluxDB has the readings" \
    || { influx_query smoke-01; fail "InfluxDB is missing readings"; }
# smoke-01's fan: on, off, on (override), off (auto)
has_commands() { influx_count smoke-01 actuator_command state | grep -qE ',([4-9]|[1-9][0-9]+)\s*$'; }
eventually has_commands && pass "InfluxDB has the fan commands" \
    || { influx_count smoke-01 actuator_command state; fail "InfluxDB is missing commands"; }

LP=data/archive/lineprotocol/telemetry.lp
RAW=data/archive/raw/mqtt.jsonl
eventually grep -q 'sensor_data,device=smoke-01,sensor=temperature unit="C",value=31.5' "$LP" \
    && pass "line-protocol archive has the JSON reading" || fail "$LP is missing the JSON reading"
grep -q 'sensor_data,device=smoke-01,sensor=humidity value=61 ' "$LP" \
    && pass "line-protocol archive has the bare-number reading" || fail "$LP is missing the bare-number reading"
grep -q 'actuator_command,actuator=valve,device=smoke-02 mode="auto",reason="no soil_moisture reading' "$LP" \
    && pass "line-protocol archive has the watchdog's command and reason" \
    || fail "$LP is missing the watchdog's command"
! grep -q 'sensor=broken' "$LP" \
    && pass "unparseable payload kept out of InfluxDB/line protocol" || fail "unparseable payload was written"

eventually grep -q '"payload":"not json"' "$RAW" \
    && pass "raw archive keeps the unparseable payload" || fail "$RAW is missing the unparseable payload"
grep -q '"topic":"actuators/smoke-01/fan/mode"' "$RAW" \
    && pass "raw archive has the mode changes" || fail "$RAW is missing the mode changes"
python3 - "$RAW" <<'EOF' && pass "raw archive is valid JSON Lines" || fail "$RAW is not valid JSON Lines"
import json, sys
rows = [json.loads(line) for line in open(sys.argv[1])]
assert rows and all(set(r) == {"received_at", "topic", "payload"} for r in rows), rows
EOF

echo "Checking the external broker bridge"
bridge_connected() {
    compose exec -T mqtt mosquitto_sub -t '$SYS/broker/connection/p4n4-remote/state' -C 1 -W 2 \
        | grep -qx 1
}
eventually bridge_connected && pass "bridge logged in to the external broker" \
    || { compose logs --no-color --tail 20 mqtt; fail "bridge did not connect"; }
remote_pub() {
    compose exec -T remote mosquitto_pub -u "$REMOTE_USER" -P "$REMOTE_PASSWORD" -q 1 "$@"
}
remote_pub -t sensors/smoke-04/temperature -m '{"value": 19.0, "unit": "C"}'
# Retained, so it would still be on the local broker had it been bridged
remote_pub -r -t elsewhere/smoke-04/temperature -m '99'
has_bridged_reading() { influx_query smoke-04 | grep -qE ',1\s*$'; }
eventually has_bridged_reading && pass "InfluxDB has the reading published on the external broker" \
    || { influx_query smoke-04; fail "bridged reading did not reach InfluxDB"; }
! compose exec -T mqtt mosquitto_sub -t 'elsewhere/#' -C 1 -W 3 >/dev/null 2>&1 \
    && pass "topics outside MQTT_REMOTE_TOPICS are not bridged" || fail "an unrequested topic was bridged"

# Grafana publishes no port here, so call its API from inside the container
grafana_api() {
    compose exec -T grafana wget -qO- "http://$GRAFANA_USER:$GRAFANA_PASSWORD@localhost:3000/api/$1"
}
grafana_api datasources/uid/influxdb-telemetry/health | grep -q '"status":"OK"' \
    && pass "Grafana datasource is healthy" || fail "Grafana datasource health check failed"
grafana_api dashboards/uid/p4n4-greenhouse >/dev/null \
    && pass "Grafana dashboard is provisioned" || fail "Grafana dashboard is missing"
# The dashboard p4n4-dashboard opens: .p4n4.json dashboard.grafana_path is /d/<uid>/<slug>
GRAFANA_PATH="$(python3 -c 'import json; print(json.load(open(".p4n4.json"))["dashboard"]["grafana_path"])')"
GRAFANA_UID="$(echo "$GRAFANA_PATH" | cut -d/ -f3)"
grafana_api "dashboards/uid/$GRAFANA_UID" >/dev/null \
    && pass "dashboard.grafana_path ($GRAFANA_PATH) is provisioned" || fail "dashboard.grafana_path $GRAFANA_PATH is not provisioned"
python3 tests/check_dashboards.py p4n4-smoke "$GRAFANA_USER" "$GRAFANA_PASSWORD" \
    || fail "dashboard queries"

echo "Smoke test passed"
