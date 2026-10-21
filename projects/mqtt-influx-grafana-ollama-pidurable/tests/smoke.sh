#!/usr/bin/env bash
# ==============================================================================
# End-to-end smoke test: starts both layers of the template in a throwaway
# copy, iot then ai, as separate Compose projects the way `p4n4 up` does.
# Publishes readings and inference results over MQTT and checks that they
# reach InfluxDB, that Node-RED alerts only above the threshold, that the
# agent investigates the alert through a scripted fake model
# (tests/fake_llm.py) and publishes one finding, that a repeated alert joins
# that investigation, and that Grafana's panels return data.
# Tears everything down (including volumes) on exit.
#
#   ./tests/smoke.sh                               # from the template directory
#   KEEP=1 ./tests/smoke.sh                        # leave the stack running
#   AGENT_IMAGE=p4n4-agent:dev ./tests/smoke.sh    # a locally built agent image
#
# Requires Docker with Compose v2 and python3. The test stack drops the fixed
# container names, host ports and network name, so it runs alongside other stacks.
# ==============================================================================
set -euo pipefail

TEMPLATE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
WORK_DIR="$(mktemp -d)"
cp -a "$TEMPLATE_DIR/." "$WORK_DIR/"
cd "$WORK_DIR/iot"

TOKEN="smoke-$(head -c 16 /dev/urandom | od -An -tx1 | tr -d ' \n')"
# Deterministic data: no simulator, no model download (several GB), the fake model
sed -e 's/^COMPOSE_PROFILES=.*/COMPOSE_PROFILES=/' .env.example > .env
sed -e 's/^OLLAMA_MODEL=.*/OLLAMA_MODEL=/' \
    -e 's/^AGENT_MODEL=.*/AGENT_MODEL=fake/' \
    -e "s/^AGENT_API_TOKEN=.*/AGENT_API_TOKEN=$TOKEN/" \
    ${AGENT_IMAGE:+-e "s|^AGENT_IMAGE=.*|AGENT_IMAGE=$AGENT_IMAGE|"} \
    ../ai/.env.example > ../ai/.env
env_value() { grep -E "^$1=" .env | cut -d= -f2-; }

# Isolate the test from anything else on the host: no fixed container names,
# no published ports, and a private network instead of p4n4-net
cat > docker-compose.override.yml <<'EOF'
services:
  mqtt:
    container_name: !reset null
    ports: !reset []
  influxdb:
    container_name: !reset null
    ports: !reset []
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
# The agent talks to a scripted fake model; the real Ollama still starts
cat > ../ai/docker-compose.override.yml <<'EOF'
services:
  agent:
    container_name: !reset null
    ports: !reset []
    environment:
      OLLAMA_URL: http://fake-llm:11434
    depends_on:
      - fake-llm
  ollama:
    container_name: !reset null
    ports: !reset []
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
        echo "--- node-red logs ---"
        compose logs --no-color --tail 30 node-red || true
        echo "--- agent logs ---"
        compose_ai logs --no-color --tail 40 agent || true
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
compose_ai up -d --wait --quiet-pull
# Node-RED and the agent (the retained count is from before this mosquitto_sub)
eventually compose exec -T mqtt sh -c \
    "mosquitto_sub -t '\$SYS/broker/clients/connected' -C 1 -W 2 | awk '\$1 >= 2 { ok = 1 } END { exit !ok }'" \
    || fail "node-red and the agent did not connect to the broker"

pub() { compose exec -T mqtt mosquitto_pub -q 1 "$@"; }
# GET <path> on the agent's status API, from inside its container (no host port here)
agent_api() {
    compose_ai exec -T agent node -e "
        fetch('http://127.0.0.1:8095$1', { headers: { authorization: 'Bearer ${2:-$TOKEN}' } })
            .then(async (r) => { console.log(await r.text()); process.exit(r.ok ? 0 : 1); }, () => process.exit(2));"
}
llm_requests() {
    compose_ai exec -T fake-llm python -c \
        "import json, urllib.request; print(json.load(urllib.request.urlopen('http://127.0.0.1:11434/stats'))['requests'])"
}
now_iso() { date -u +%Y-%m-%dT%H:%M:%SZ; }

echo "Checking the alert rule"
pub -t sensors/smoke-01/vibration -m '{"value": 2.1, "unit": "mm/s"}'
pub -t sensors/smoke-01/temperature -m '41.5'
pub -t inference/smoke-01/result -m "{\"device\": \"smoke-01\", \"timestamp\": \"$(now_iso)\", \"label\": \"running\", \"confidence\": 0.9, \"anomaly_score\": 0.2}"
! compose exec -T mqtt mosquitto_sub -t 'alerts/#' -C 1 -W 4 >/dev/null 2>&1 \
    && pass "no alert at anomaly_score 0.2 (ANOMALY_THRESHOLD 0.7)" || fail "alerted below the threshold"

echo "Checking an investigation"
RESULTS="$WORK_DIR/results.jsonl"
compose exec -T mqtt mosquitto_sub -q 1 -t 'agent/investigations/#' -W 90 >"$RESULTS" 2>/dev/null &
SUB=$!
sleep 1
pub -t sensors/smoke-01/vibration -m '{"value": 9.4, "unit": "mm/s"}'
AT="$(now_iso)"
pub -t inference/smoke-01/result -m "{\"device\": \"smoke-01\", \"timestamp\": \"$AT\", \"label\": \"anomaly\", \"confidence\": 0.9, \"anomaly_score\": 0.92}"
has_result() { grep -q '"device":"smoke-01"' "$RESULTS"; }
TRIES=40 eventually has_result && pass "the agent published a finding on agent/investigations/smoke-01" \
    || fail "no finding on agent/investigations/smoke-01"
python3 - "$RESULTS" <<'EOF' && pass "the finding names the cause the model reported" || fail "unexpected finding"
import json, sys
result = json.loads(open(sys.argv[1]).readline())
assert result["status"] == "answered" and result["cause"] == "bearing wear", result
assert result["incident"].startswith("smoke-01:"), result
EOF

echo "Checking the status API"
agent_api /health | grep -q '"mqtt":true' && pass "health reports the broker connection" || fail "health"
agent_api '/v1/investigations?state=recent' | grep -q '"device":"smoke-01"' \
    && pass "the investigation is listed" || fail "the investigation is not listed"
! agent_api /v1/investigations wrong-token >/dev/null && pass "the list needs AGENT_API_TOKEN" \
    || fail "the list answered a wrong token"

echo "Checking that a repeated alert joins the investigation"
before="$(llm_requests)"
pub -t alerts/smoke-01/anomaly -m "{\"device\": \"smoke-01\", \"anomaly_score\": 0.95, \"timestamp\": \"$AT\"}"
sleep 4
[ "$(llm_requests)" = "$before" ] && pass "no model request for the repeated alert" || fail "the repeated alert called the model"
count="$(agent_api /v1/investigations | python3 -c 'import json, sys; print(len(json.load(sys.stdin)["investigations"]))')"
[ "$count" = 1 ] && pass "still one investigation" || fail "$count investigations"
kill "$SUB" 2>/dev/null || true
[ "$(grep -c '"device":"smoke-01"' "$RESULTS")" = 1 ] && pass "one finding published" || fail "the finding was published twice"

echo "Checking the stores"
influx_count() {
    compose exec -T influxdb influx query --raw \
        "from(bucket: \"$INFLUXDB_BUCKET\") |> range(start: -1h)
         |> filter(fn: (r) => r._measurement == \"$1\" and r.device == \"smoke-01\" and r._field == \"$2\")
         |> group() |> count()"
}
# vibration ×2, temperature
has_readings() { influx_count sensor_data value | grep -qE ',3\s*$'; }
eventually has_readings && pass "InfluxDB has the readings" || { influx_count sensor_data value; fail "InfluxDB is missing readings"; }
influx_count inference anomaly_score | grep -qE ',2\s*$' \
    && pass "InfluxDB has the inference results" || { influx_count inference anomaly_score; fail "inference results"; }
influx_count agent_investigation cause | grep -qE ',1\s*$' \
    && pass "InfluxDB has one agent_investigation point" || { influx_count agent_investigation cause; fail "agent_investigation"; }

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
