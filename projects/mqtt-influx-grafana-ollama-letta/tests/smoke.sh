#!/usr/bin/env bash
# ==============================================================================
# End-to-end smoke test: starts both layers of the template in a throwaway
# copy, iot then ai, as separate Compose projects the way `p4n4 up` does.
# Letta runs for real, against a scripted stand-in for Ollama
# (tests/fake_llm.py), so no model is downloaded and the answers are fixed.
# Checks that letta-init provisions the agent (tools, memory blocks, seeded
# archival memory), that every tool works through Letta, that the agent's
# memory takes what it is told and survives re-provisioning, and that the
# iot layer and every Grafana panel work. Tears everything down (including
# volumes) on exit.
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
# model download. The bridge pulls from the "remote" broker below; its
# password needs quoting.
REMOTE_USER=smoke
REMOTE_PASSWORD='smoke$pa ss#1'
sed -e 's/^COMPOSE_PROFILES=.*/COMPOSE_PROFILES=/' \
    -e "s/^ARCHIVE_UID=.*/ARCHIVE_UID=$(id -u)/" \
    -e "s/^ARCHIVE_GID=.*/ARCHIVE_GID=$(id -g)/" \
    -e "s/^MQTT_REMOTE_HOST=.*/MQTT_REMOTE_HOST=remote/" \
    -e "s/^MQTT_REMOTE_USER=.*/MQTT_REMOTE_USER=$REMOTE_USER/" \
    -e "s/^MQTT_REMOTE_PASSWORD=.*/MQTT_REMOTE_PASSWORD='$REMOTE_PASSWORD'/" \
    .env.example > .env
sed -e 's/^OLLAMA_MODEL=.*/OLLAMA_MODEL=/' -e 's/^OLLAMA_EMBED_MODEL=.*/OLLAMA_EMBED_MODEL=/' \
    ../ai/.env.example > ../ai/.env
env_value() { grep -E "^$1=" .env | cut -d= -f2-; }
LETTA_PASSWORD="$(grep -E '^LETTA_SERVER_PASSWORD=' ../ai/.env | cut -d= -f2-)"

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
# Letta talks to the scripted fake model; the real Ollama still starts
cat > ../ai/docker-compose.override.yml <<'EOF'
services:
  ollama:
    container_name: !reset null
    ports: !reset []
  letta:
    container_name: !reset null
    ports: !reset []
    environment:
    depends_on:
      fake-llm:
        condition: service_started
  gateway:
    container_name: !reset null
    environment:
      UPSTREAM: http://fake-llm:11434
  letta-init:
    container_name: !reset null
    environment:
      OLLAMA_MODEL: fake-chat
      OLLAMA_EMBED_MODEL: fake-embed
  fake-llm:
    image: python:3.13-slim
    command: ["python", "-u", "/fake_llm.py"]
    volumes:
      - ../tests/fake_llm.py:/fake_llm.py:ro
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
        echo "--- letta-init logs ---"
        compose_ai logs --no-color --tail 30 letta-init || true
        echo "--- fake-llm logs ---"
        compose_ai logs --no-color --tail 30 fake-llm || true
        echo "--- letta logs ---"
        compose_ai logs --no-color --tail 40 letta || true
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

echo "Starting the iot layer in $WORK_DIR"
compose up -d --wait --quiet-pull
eventually compose exec -T mqtt sh -c \
    "mosquitto_sub -t '\$SYS/broker/clients/connected' -C 1 -W 2 | grep -qE '^[2-9]'" \
    || fail "telegraf did not connect to the broker"

echo "Publishing readings"
pub() { compose exec -T mqtt mosquitto_pub -q 1 -t "$1" -m "$2"; }
pub sensors/pump-1/vibration '{"value": 1.9, "unit": "mm/s"}'
pub sensors/pump-1/bearing_temp '{"value": 46.5, "unit": "C"}'
pub sensors/pump-1/current '{"value": 13.6, "unit": "A"}'
pub sensors/pump-2/vibration '{"value": 5.6, "unit": "mm/s"}'
pub sensors/pump-2/bearing_temp '{"value": 61.2, "unit": "C"}'
pub sensors/pump-2/current '{"value": 21.3, "unit": "A"}'
pub sensors/pump-2/broken 'not json'
influx_count() {
    compose exec -T influxdb influx query --raw \
        "from(bucket: \"$INFLUXDB_BUCKET\") |> range(start: -1h)
         |> filter(fn: (r) => r._measurement == \"$1\" and r._field == \"$2\")
         |> group() |> count()"
}
has_readings() { influx_count sensor_data value | grep -qE ',6\s*$'; }
eventually has_readings && pass "InfluxDB has the readings" || fail "InfluxDB is missing readings"

echo "Starting the ai layer"
compose_ai up -d --wait --quiet-pull
# Prints 'container "<id>" exited with status code <n>'
init_ok() { compose_ai wait letta-init | grep -q 'status code 0$'; }
init_ok && pass "letta-init provisioned the agent" || fail "letta-init failed"

# Letta publishes no port here: call its API from inside its container
letta() {
    compose_ai exec -T letta curl -sf -X "$1" -H "Authorization: Bearer $LETTA_PASSWORD" \
        -H 'Content-Type: application/json' "http://localhost:8283$2" ${3:+-d "$3"}
}
json() { python3 -c "import json, sys; d = json.load(sys.stdin); print($1)"; }
AGENT="$(letta GET '/v1/agents/?name=maintenance-assistant' | json 'd[0]["id"] if len(d) == 1 else ""')"
[ -n "$AGENT" ] && pass "one agent named maintenance-assistant" || fail "expected one maintenance-assistant agent"
letta GET "/v1/agents/$AGENT" | python3 -c '
import json, sys
a = json.load(sys.stdin)
tools = {t["name"] for t in a["tools"]}
need = {"equipment_status", "equipment_trend", "maintenance_history", "log_maintenance",
        "archival_memory_search", "archival_memory_insert", "memory_insert"}
assert need <= tools, need - tools
blocks = {b["label"]: b["value"] for b in a["memory"]["blocks"]}
assert {"persona", "human", "equipment"} <= set(blocks), blocks.keys()
assert "ISO 10816" in blocks["equipment"] and "log_maintenance" in blocks["persona"]
assert "{date:" not in blocks["equipment"], "unreplaced date placeholder"
' && pass "the agent has its tools and memory blocks" || fail "agent tools or memory blocks are wrong"
archival() { letta GET "/v1/agents/$AGENT/archival-memory?limit=100" | json 'len(d)'; }
[ "$(archival)" = 4 ] && pass "archival memory seeded with 4 past records" || fail "archival memory has $(archival) records, expected 4"

# Sends $1 to the agent; prints its reply
ask() {
    letta POST "/v1/agents/$AGENT/messages" "$(python3 -c 'import json, sys; print(json.dumps({"messages": [{"role": "user", "content": sys.argv[1]}]}))' "$1")" \
        | json '"\n".join(m["content"] for m in d["messages"] if m["message_type"] == "assistant_message" and isinstance(m["content"], str))'
}
# expect <reply> <text> <description>
expect() { grep -qF -- "$2" <<<"$1" && pass "$3" || { echo "$1" | head -20; fail "$3"; }; }

echo "Asking the agent"
reply="$(ask 'How is pump-2 doing?')"
expect "$reply" "pump-2, daily means" "equipment_trend reads pump-2's history from InfluxDB"
expect "$reply" "vibration (mm/s): 5.6" "the trend has the published vibration"
expect "$reply" "drive-end bearing failure" "maintenance_history recalls pump-2's past failure from archival memory"
fake_logs="$(compose_ai logs --no-color fake-llm)"
grep -q 'reasoning_effort=none' <<<"$fake_logs" && ! grep -q 'reasoning_effort=None' <<<"$fake_logs" \
    && pass "the gateway turns thinking off (AGENT_THINK=false)" || fail "chat requests reached the model without reasoning_effort=none"

reply="$(ask 'Is anything wrong?')"
expect "$reply" "pump-2 vibration: now 5.6 mm/s" "equipment_status reports every machine"
expect "$reply" "pump-1 bearing_temp: now 46.5" "equipment_status covers all sensors"

reply="$(ask 'We replaced the bearing on pump-2 today, Ann Lee did it.')"
expect "$reply" "Logged for pump-2" "log_maintenance confirms"
influx_count maintenance_event work | grep -qE ',1\s*$' \
    && pass "the work is in InfluxDB (maintenance_event)" || fail "no maintenance_event in InfluxDB"
compose exec -T influxdb influx query --raw \
    "from(bucket: \"$INFLUXDB_BUCKET\") |> range(start: -1h) |> filter(fn: (r) => r._measurement == \"maintenance_event\" and r._field == \"technician\")" \
    | grep -q 'Ann Lee' && pass "the technician is recorded" || fail "technician missing"
[ "$(archival)" = 5 ] && pass "the work is in archival memory too" || fail "archival memory has $(archival) records, expected 5"

reply="$(ask 'Note: pump-3 is now on standby.')"
letta GET "/v1/agents/$AGENT/core-memory/blocks/equipment" | json 'd["value"]' | grep -q 'pump-3: Standby' \
    && pass "memory_insert adds what it learns to the equipment block" || fail "the equipment block was not updated"

reply="$(ask 'What was done on pump-2 lately?')"
expect "$reply" "Replaced the drive-end bearing" "a later conversation finds the logged work (archival_memory_search)"

echo "Checking re-provisioning"
compose_ai start letta-init >/dev/null
init_ok && pass "letta-init runs again" || fail "letta-init failed on its second run"
compose_ai logs --no-color letta-init | grep -q 'is up to date; its memory was left as it is' \
    && pass "the second run updates the existing agent" || fail "letta-init did not update the agent"
[ "$(letta GET '/v1/agents/?name=maintenance-assistant' | json 'len(d)')" = 1 ] \
    && pass "still one agent" || fail "re-provisioning created another agent"
[ "$(archival)" = 5 ] && pass "archival memory is not seeded twice" || fail "archival memory has $(archival) records after re-provisioning"
letta GET "/v1/agents/$AGENT/core-memory/blocks/equipment" | json 'd["value"]' | grep -q 'pump-3: Standby' \
    && pass "what the agent learned survives re-provisioning" || fail "re-provisioning overwrote the equipment block"

echo "Checking the iot layer"
LP=data/archive/lineprotocol/telemetry.lp
RAW=data/archive/raw/mqtt.jsonl
eventually grep -q 'sensor_data,device=pump-2,sensor=vibration unit="mm/s",value=5.6' "$LP" \
    && pass "line-protocol archive has the readings" || fail "$LP is missing the readings"
eventually grep -q '"payload":"not json"' "$RAW" \
    && pass "raw archive keeps the unparseable payload" || fail "$RAW is missing the unparseable payload"
bridge_connected() {
    compose exec -T mqtt mosquitto_sub -t '$SYS/broker/connection/p4n4-remote/state' -C 1 -W 2 \
        | grep -qx 1
}
eventually bridge_connected && pass "bridge logged in to the external broker" \
    || { compose logs --no-color --tail 20 mqtt; fail "bridge did not connect"; }

# Grafana publishes no port here, so call its API from inside the container
grafana_api() {
    compose exec -T grafana wget -qO- "http://$GRAFANA_USER:$GRAFANA_PASSWORD@localhost:3000/api/$1"
}
grafana_api datasources/uid/influxdb-telemetry/health | grep -q '"status":"OK"' \
    && pass "Grafana datasource is healthy" || fail "Grafana datasource health check failed"
GRAFANA_PATH="$(python3 -c 'import json; print(json.load(open("../.p4n4.json"))["dashboard"]["grafana_path"])')"
GRAFANA_UID="$(echo "$GRAFANA_PATH" | cut -d/ -f3)"
grafana_api "dashboards/uid/$GRAFANA_UID" >/dev/null \
    && pass "dashboard.grafana_path ($GRAFANA_PATH) is provisioned" || fail "dashboard.grafana_path $GRAFANA_PATH is not provisioned"
python3 ../tests/check_dashboards.py p4n4-smoke "$GRAFANA_USER" "$GRAFANA_PASSWORD" \
    || fail "dashboard queries"

echo "Smoke test passed"
