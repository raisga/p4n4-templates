#!/usr/bin/env bash
# ==============================================================================
# End-to-end smoke test: starts both layers of the template in a throwaway
# copy, iot then ai, as separate Compose projects the way `p4n4 up` does.
# Publishes readings over MQTT and checks they reach InfluxDB, both archive
# formats and Grafana, and drives the agent's InfluxDB tools through a
# scripted fake model (tests/fake_ollama.py).
# Tears everything down (including volumes) on exit.
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
cd "$WORK_DIR/iot"

# Deterministic data: no simulator, archive owned by the current user, and no
# model download (several GB). The bridge pulls from the "remote" broker
# below; its password needs quoting.
REMOTE_USER=smoke
REMOTE_PASSWORD='smoke$pa ss#1'
sed -e 's/^COMPOSE_PROFILES=.*/COMPOSE_PROFILES=/' \
    -e "s/^ARCHIVE_UID=.*/ARCHIVE_UID=$(id -u)/" \
    -e "s/^ARCHIVE_GID=.*/ARCHIVE_GID=$(id -g)/" \
    -e "s/^MQTT_REMOTE_HOST=.*/MQTT_REMOTE_HOST=remote/" \
    -e "s/^MQTT_REMOTE_USER=.*/MQTT_REMOTE_USER=$REMOTE_USER/" \
    -e "s/^MQTT_REMOTE_PASSWORD=.*/MQTT_REMOTE_PASSWORD='$REMOTE_PASSWORD'/" \
    .env.example > .env
sed -e 's/^OLLAMA_MODEL=.*/OLLAMA_MODEL=/' ../ai/.env.example > ../ai/.env
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
# A project tool, as a project adds one in ai/agent/local_tools.py: it uses the
# agent's InfluxDB helpers and adds a line to the system prompt
cat > ../ai/agent/local_tools.py <<'EOF'
PROMPT = "Smoke test project."


def tools(agent):
    def count_devices(args):
        rows = agent.query(agent.readings(agent.parse_range(args.get("range")), "value")
                           + '  |> last() |> group() |> distinct(column: "device")')
        return {"devices": sorted(r["_value"] for r in rows)}

    definition = {"type": "function", "function": {
        "name": "count_devices", "description": "Devices that reported.",
        "parameters": {"type": "object", "properties": {"range": {"type": "string"}}}}}
    return [(definition, count_devices)]
EOF

# The agent talks to a scripted fake model; the real Ollama still starts
cat > ../ai/docker-compose.override.yml <<'EOF'
services:
  agent:
    container_name: !reset null
    ports: !reset []
    environment:
      OLLAMA_URL: http://fake-ollama:11434
    depends_on:
      - fake-ollama
  ollama:
    container_name: !reset null
  fake-ollama:
    image: python:3.13-slim
    command: ["python", "-u", "/fake_ollama.py"]
    volumes:
      - ../tests/fake_ollama.py:/fake_ollama.py:ro
    networks:
      - p4n4-net
networks:
  p4n4-net:
    name: p4n4-smoke-net
EOF
INFLUXDB_BUCKET="$(env_value INFLUXDB_BUCKET)"
GRAFANA_USER="$(env_value GRAFANA_USER)"
GRAFANA_PASSWORD="$(env_value GRAFANA_PASSWORD)"

# The iot layer runs from here (iot/), the ai layer from ../ai
compose() { docker compose --project-name p4n4-smoke "$@"; }
compose_ai() { docker compose --project-directory ../ai --project-name p4n4-smoke-ai "$@"; }

cleanup() {
    status=$?
    if [ "$status" -ne 0 ]; then
        echo "--- telegraf logs ---"
        compose logs --no-color --tail 50 telegraf || true
        echo "--- ai layer logs ---"
        compose_ai logs --no-color --tail 20 agent ollama || true
    fi
    if [ "${KEEP:-0}" = 1 ]; then
        echo "KEEP=1: stack left running in $WORK_DIR"
    else
        # ai first: it is attached to the network the iot layer owns
        compose_ai down --volumes --remove-orphans >/dev/null 2>&1 || true
        compose down --volumes --remove-orphans >/dev/null 2>&1 || true
        rm -rf "$WORK_DIR"
    fi
    exit "$status"
}
trap cleanup EXIT

pass() { echo "  ok   $*"; }
fail() { echo "  FAIL $*"; exit 1; }

# Retries "$@" for up to 60s
eventually() {
    for _ in $(seq 30); do
        if "$@" >/dev/null 2>&1; then return 0; fi
        sleep 2
    done
    return 1
}

echo "Starting the iot and ai layers in $WORK_DIR"
compose up -d --wait --quiet-pull
compose_ai up -d --wait --quiet-pull
# Telegraf has no healthcheck; give its MQTT subscriptions a moment
eventually compose exec -T mqtt sh -c \
    "mosquitto_sub -t '\$SYS/broker/clients/connected' -C 1 -W 2 | grep -qE '^[2-9]'" \
    || fail "telegraf did not connect to the broker"

echo "Publishing"
pub() { compose exec -T mqtt mosquitto_pub -q 1 -t "$1" -m "$2"; }
pub sensors/smoke-01/temperature '{"value": 21.5, "unit": "C"}'
pub sensors/smoke-01/humidity '48'
pub sensors/smoke-01/broken 'not json'

echo "Checking"
# Number of sensor_data values stored for device $1
influx_query() {
    compose exec -T influxdb influx query --raw \
        "from(bucket: \"$INFLUXDB_BUCKET\") |> range(start: -1h)
         |> filter(fn: (r) => r._measurement == \"sensor_data\" and r.device == \"$1\" and r._field == \"value\")
         |> group() |> count()"
}
has_both_readings() { influx_query smoke-01 | grep -qE ',2\s*$'; }
eventually has_both_readings && pass "InfluxDB has both readings" \
    || { influx_query smoke-01; fail "InfluxDB is missing readings"; }

LP=data/archive/lineprotocol/telemetry.lp
RAW=data/archive/raw/mqtt.jsonl
eventually grep -q 'sensor_data,device=smoke-01,sensor=temperature unit="C",value=21.5' "$LP" \
    && pass "line-protocol archive has the JSON reading" || fail "$LP is missing the JSON reading"
grep -q 'sensor_data,device=smoke-01,sensor=humidity value=48 ' "$LP" \
    && pass "line-protocol archive has the bare-number reading" || fail "$LP is missing the bare-number reading"
! grep -q 'sensor=broken' "$LP" \
    && pass "unparseable payload kept out of InfluxDB/line protocol" || fail "unparseable payload was written"

eventually grep -q '"payload":"not json"' "$RAW" \
    && pass "raw archive keeps the unparseable payload" || fail "$RAW is missing the unparseable payload"
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
remote_pub -t sensors/smoke-02/temperature -m '{"value": 19.0, "unit": "C"}'
# Retained, so it would still be on the local broker had it been bridged
remote_pub -r -t elsewhere/smoke-02/temperature -m '99'
has_bridged_reading() { influx_query smoke-02 | grep -qE ',1\s*$'; }
eventually has_bridged_reading && pass "InfluxDB has the reading published on the external broker" \
    || { influx_query smoke-02; fail "bridged reading did not reach InfluxDB"; }
! compose exec -T mqtt mosquitto_sub -t 'elsewhere/#' -C 1 -W 3 >/dev/null 2>&1 \
    && pass "topics outside MQTT_REMOTE_TOPICS are not bridged" || fail "an unrequested topic was bridged"

# Grafana publishes no port here, so call its API from inside the container
grafana_api() {
    compose exec -T grafana wget -qO- "http://$GRAFANA_USER:$GRAFANA_PASSWORD@localhost:3000/api/$1"
}
grafana_api datasources/uid/influxdb-telemetry/health | grep -q '"status":"OK"' \
    && pass "Grafana datasource is healthy" || fail "Grafana datasource health check failed"
grafana_api dashboards/uid/p4n4-telemetry >/dev/null \
    && pass "Grafana dashboard is provisioned" || fail "Grafana dashboard is missing"
# The dashboard p4n4-dashboard opens: .p4n4.json dashboard.grafana_path is /d/<uid>/<slug>
GRAFANA_PATH="$(python3 -c 'import json; print(json.load(open("../.p4n4.json"))["dashboard"]["grafana_path"])')"
GRAFANA_UID="$(echo "$GRAFANA_PATH" | cut -d/ -f3)"
grafana_api "dashboards/uid/$GRAFANA_UID" >/dev/null \
    && pass "dashboard.grafana_path ($GRAFANA_PATH) is provisioned" || fail "dashboard.grafana_path $GRAFANA_PATH is not provisioned"
python3 ../tests/check_dashboards.py p4n4-smoke "$GRAFANA_USER" "$GRAFANA_PASSWORD" \
    || fail "dashboard queries"

echo "Checking the ai layer"
compose_ai exec -T ollama ollama list >/dev/null \
    && pass "Ollama serves its API" || fail "Ollama is not answering"
# p4n4-dashboard reaches the agent over p4n4-net, which the iot layer created
compose exec -T grafana wget -qO- http://agent:11434/api/tags | grep -q '"models"' \
    && pass "the agent is reachable over the iot layer's network" || fail "the agent is not on the iot layer's network"
compose_ai exec -T agent python - < ../tests/check_agent.py || fail "agent tools"
# Read the logs first: grep -q closing a pipe early fails `compose logs`, and
# with pipefail the negation would then pass whatever the logs say
ollama_logs="$(compose_ai logs --no-color ollama)"
! grep -q 'p4n4: pulling' <<<"$ollama_logs" \
    && pass "empty OLLAMA_MODEL pulls nothing" || fail "Ollama pulled a model although OLLAMA_MODEL is empty"

echo "Smoke test passed"
