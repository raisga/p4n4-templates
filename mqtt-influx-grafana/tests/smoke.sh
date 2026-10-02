#!/usr/bin/env bash
# ==============================================================================
# End-to-end smoke test: starts the template in a throwaway copy, publishes
# readings over MQTT and checks they reach InfluxDB, both archive formats and
# Grafana. Tears everything down (including volumes) on exit.
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

# Deterministic data: no simulator, archive owned by the current user
sed -e 's/^COMPOSE_PROFILES=.*/COMPOSE_PROFILES=/' \
    -e "s/^ARCHIVE_UID=.*/ARCHIVE_UID=$(id -u)/" \
    -e "s/^ARCHIVE_GID=.*/ARCHIVE_GID=$(id -g)/" \
    .env.example > .env
env_value() { grep -E "^$1=" .env | cut -d= -f2-; }

# Isolate the test from anything else on the host: no fixed container names,
# no published ports, and a private network instead of p4n4-net.
cat > docker-compose.override.yml <<'EOF'
services:
  mqtt:
    container_name: !reset null
    ports: !reset []
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
INFLUXDB_BUCKET="$(env_value INFLUXDB_BUCKET)"
GRAFANA_USER="$(env_value GRAFANA_USER)"
GRAFANA_PASSWORD="$(env_value GRAFANA_PASSWORD)"

compose() { docker compose --project-name p4n4-smoke "$@"; }

cleanup() {
    status=$?
    if [ "$status" -ne 0 ]; then
        echo "--- telegraf logs ---"
        compose logs --no-color --tail 50 telegraf || true
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

# Retries "$@" for up to 60s
eventually() {
    for _ in $(seq 30); do
        if "$@" >/dev/null 2>&1; then return 0; fi
        sleep 2
    done
    return 1
}

echo "Starting stack in $WORK_DIR"
compose up -d --wait --quiet-pull
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
influx_query() {
    compose exec -T influxdb influx query --raw \
        "from(bucket: \"$INFLUXDB_BUCKET\") |> range(start: -1h)
         |> filter(fn: (r) => r._measurement == \"sensor_data\" and r.device == \"smoke-01\" and r._field == \"value\")
         |> group() |> count()"
}
has_both_readings() { influx_query | grep -qE ',2\s*$'; }
eventually has_both_readings && pass "InfluxDB has both readings" \
    || { influx_query; fail "InfluxDB is missing readings"; }

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

# Grafana publishes no port here, so call its API from inside the container
grafana_api() {
    compose exec -T grafana wget -qO- "http://$GRAFANA_USER:$GRAFANA_PASSWORD@localhost:3000/api/$1"
}
grafana_api datasources/uid/influxdb-telemetry/health | grep -q '"status":"OK"' \
    && pass "Grafana datasource is healthy" || fail "Grafana datasource health check failed"
grafana_api dashboards/uid/p4n4-telemetry >/dev/null \
    && pass "Grafana dashboard is provisioned" || fail "Grafana dashboard is missing"
python3 tests/check_dashboards.py p4n4-smoke "$GRAFANA_USER" "$GRAFANA_PASSWORD" \
    || fail "dashboard queries"

echo "Smoke test passed"
