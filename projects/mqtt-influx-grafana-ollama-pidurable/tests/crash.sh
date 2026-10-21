#!/usr/bin/env bash
# ==============================================================================
# Crash test: the agent survives SIGKILL mid-tool under Raspberry Pi 5 limits.
#
# Starts both layers in a throwaway copy with p4n4-emu (rpi5 CPU, memory and
# pids limits), then RUNS times: publishes an alert for a new device, waits
# until the agent is inside its query_influx tool (slowed down by the test
# hook AGENT_TEST_TOOL_DELAY_MS), SIGKILLs the container (docker kill) and
# starts it again. Each run asserts that
#   1. the same submission resumes and completes,
#   2. exactly one investigation exists for the alert (status API, InfluxDB,
#      and one result message on MQTT),
#   3. delivering the same alert again starts no second investigation (and
#      makes no model request).
# Also samples the agent container's memory under the rpi5 limits.
#
#   ./tests/crash.sh                                 # from the template directory
#   RUNS=5 AGENT_IMAGE=p4n4-agent:dev ./tests/crash.sh
#   KEEP=1 ./tests/crash.sh                          # leave the stack running
#   REAL_MODEL=1 ./tests/crash.sh                    # Ollama and OLLAMA_MODEL, not the fake
#
# EMU (default p4n4-emu) runs the emulator, e.g. EMU="uv run --project
# ../../../emu p4n4-emu" from a p4n4 checkout. EMU_ARGS defaults to --native: the
# board's limits on the host's CPU (emulating arm64 with QEMU is too slow for an
# LLM). Like smoke.sh, the stack has no fixed names, host ports or p4n4-net, so
# it runs alongside other stacks. Requires Docker with Compose v2 and python3.
# ==============================================================================
set -euo pipefail

RUNS="${RUNS:-5}"
EMU="${EMU:-p4n4-emu}"
EMU_PROFILE="${EMU_PROFILE:-rpi5}"
EMU_ARGS="${EMU_ARGS:---native}"
TOOL_DELAY_MS="${TOOL_DELAY_MS:-8000}"
REAL_MODEL="${REAL_MODEL:-0}"

TEMPLATE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
WORK_DIR="$(mktemp -d)"
cp -a "$TEMPLATE_DIR/." "$WORK_DIR/"
cd "$WORK_DIR"

TOKEN="crash-$(head -c 16 /dev/urandom | od -An -tx1 | tr -d ' \n')"
{ sed -e 's/^COMPOSE_PROFILES=.*/COMPOSE_PROFILES=/' iot/.env.example; echo "COMPOSE_PROJECT_NAME=p4n4-crash"; } > iot/.env
model_env=()
[ "$REAL_MODEL" = 1 ] || model_env=(-e 's/^OLLAMA_MODEL=.*/OLLAMA_MODEL=/' -e 's/^AGENT_MODEL=.*/AGENT_MODEL=fake/')
{
    sed "${model_env[@]}" \
        -e "s/^AGENT_API_TOKEN=.*/AGENT_API_TOKEN=$TOKEN/" \
        ${AGENT_IMAGE:+-e "s|^AGENT_IMAGE=.*|AGENT_IMAGE=$AGENT_IMAGE|"} \
        ai/.env.example
    echo "COMPOSE_PROJECT_NAME=p4n4-crash-ai"
} > ai/.env

cat > iot/docker-compose.override.yml <<'EOF'
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
    name: p4n4-crash-net
    ipam:
      config: !override
        - subnet: 172.29.0.0/16
EOF
{
    cat <<EOF
services:
  agent:
    container_name: !reset null
    ports: !reset []
    environment:
      AGENT_TEST_MODE: "1"
      AGENT_TEST_TOOL_DELAY_MS: "$TOOL_DELAY_MS"
EOF
    if [ "$REAL_MODEL" != 1 ]; then
        cat <<'EOF'
      OLLAMA_URL: http://fake-llm:11434
    depends_on:
      - fake-llm
  fake-llm:
    image: python:3.13-slim
    command: ["python", "-u", "/fake_llm.py"]
    volumes:
      - ../tests/fake_llm.py:/fake_llm.py:ro
    networks:
      - p4n4-net
EOF
    fi
    cat <<'EOF'
  ollama:
    container_name: !reset null
    ports: !reset []
networks:
  p4n4-net:
    name: p4n4-crash-net
EOF
} > ai/docker-compose.override.yml

compose() { docker compose --project-directory iot "$@"; }
compose_ai() { docker compose --project-directory ai "$@"; }
# The agent's container; kept across docker kill and docker start
agent_id() { compose_ai ps -aq agent; }

cleanup() {
    status=$?
    if [ "$status" -ne 0 ]; then
        echo "--- agent logs ---"
        compose_ai logs --no-color --tail 60 agent || true
    fi
    if [ "${KEEP:-0}" = 1 ]; then
        echo "KEEP=1: stack left running in $WORK_DIR"
    else
        compose_ai down --volumes --remove-orphans >/dev/null 2>&1 || true
        compose down --volumes --remove-orphans >/dev/null 2>&1 || true
        # p4n4-emu creates the shared network itself, so Compose leaves it
        docker network rm p4n4-crash-net >/dev/null 2>&1 || true
        rm -rf "$WORK_DIR"
    fi
    exit "$status"
}
trap cleanup EXIT

pass() { echo "  ok   $*"; }
fail() { echo "  FAIL $*"; exit 1; }
eventually() {
    for _ in $(seq "${TRIES:-30}"); do
        if "$@" >/dev/null 2>&1; then return 0; fi
        sleep 2
    done
    return 1
}

echo "Starting the template under $EMU --profile $EMU_PROFILE $EMU_ARGS in $WORK_DIR"
# shellcheck disable=SC2086
$EMU up --profile "$EMU_PROFILE" $EMU_ARGS --stack iot,ai
eventually compose exec -T mqtt sh -c \
    "mosquitto_sub -t '\$SYS/broker/clients/connected' -C 1 -W 2 | awk '\$1 >= 2 { ok = 1 } END { exit !ok }'" \
    || fail "node-red and the agent did not connect to the broker"
limit="$(docker inspect --format '{{.HostConfig.Memory}}' "$(agent_id)")"
echo "  agent memory limit under $EMU_PROFILE: $((limit / 1048576)) MiB"

pub() { compose exec -T mqtt mosquitto_pub -q 1 "$@"; }
# GET <path> on the agent's status API from inside its container; prints the body
agent_api() {
    docker exec "$(agent_id)" node -e "
        fetch('http://127.0.0.1:8095$1', { headers: { authorization: 'Bearer $TOKEN' } })
            .then(async (r) => { console.log(await r.text()); process.exit(r.ok ? 0 : 1); }, () => process.exit(2));"
}
# Field $2 of the JSON object on stdin (dotted path), or the length of a list with "len:"
json_get() { python3 -c "import json, sys; v = json.load(sys.stdin)
for k in sys.argv[1].split('.'): v = v[k] if not k.startswith('len:') else len(v[k[4:]])
print(v)" "$1"; }
llm_requests() {
    if [ "$REAL_MODEL" = 1 ]; then echo n/a; return; fi
    compose_ai exec -T fake-llm python -c \
        "import json, urllib.request; print(json.load(urllib.request.urlopen('http://127.0.0.1:11434/stats'))['requests'])"
}
influx_points() {
    compose exec -T influxdb influx query --raw \
        "from(bucket: \"raw_telemetry\") |> range(start: -1h)
         |> filter(fn: (r) => r._measurement == \"agent_investigation\" and r.device == \"$1\" and r._field == \"cause\")
         |> group() |> count()" | tr -d '\r' | awk -F, '/^,/ && !/result,table/ { n = $NF } END { print n + 0 }'
}
# Memory samples of the agent container, in MiB, appended to $WORK_DIR/mem
sample_memory() {
    while :; do
        docker stats --no-stream --format '{{.MemUsage}}' "$(agent_id)" 2>/dev/null \
            | awk '{ v = $1; u = v; sub(/[0-9.]+/, "", u); sub(/[A-Za-z]+/, "", v);
                     print (u == "GiB" ? v * 1024 : u == "KiB" ? v / 1024 : v) }' >>"$WORK_DIR/mem"
        sleep 2
    done
}
sample_memory &
SAMPLER=$!

for run in $(seq "$RUNS"); do
    device="crash-$run-$(date +%s)"
    at="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
    key="$(python3 -c 'import sys, datetime as d
t = d.datetime.strptime(sys.argv[1], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=d.timezone.utc).timestamp()
w = int(t // 900 * 900)
print(sys.argv[2] + ":" + d.datetime.fromtimestamp(w, d.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z"))' "$at" "$device")"
    echo "Run $run/$RUNS: $key"
    results="$WORK_DIR/results-$run"
    compose exec -T mqtt mosquitto_sub -q 1 -t "agent/investigations/$device" -W 600 >"$results" 2>/dev/null &
    sub=$!
    sleep 1
    alert="{\"device\": \"$device\", \"anomaly_score\": 0.93, \"label\": \"anomaly\", \"threshold\": 0.7, \"timestamp\": \"$at\"}"
    pub -t "alerts/$device/anomaly" -m "$alert"

    in_tool() { agent_api "/v1/investigations/$key" | grep -q '"name":"query_influx","status":"running"'; }
    TRIES=150 eventually in_tool || fail "the agent never reached query_influx"
    submission="$(agent_api "/v1/investigations/$key" | json_get submissionId)"
    docker kill --signal KILL "$(agent_id)" >/dev/null
    [ "$(docker inspect --format '{{.State.ExitCode}}' "$(agent_id)")" = 137 ] \
        && pass "SIGKILLed inside query_influx (submission $submission)" || fail "the agent was not killed"
    docker start "$(agent_id)" >/dev/null

    reported() { [ "$(agent_api "/v1/investigations/$key" | json_get status)" = reported ]; }
    TRIES=150 eventually reported || fail "the investigation did not finish after the restart"
    detail="$(agent_api "/v1/investigations/$key")"
    [ "$(json_get submissionId <<<"$detail")" = "$submission" ] \
        && pass "1. the same submission resumed and completed ($(json_get outcome <<<"$detail"): $(json_get cause <<<"$detail"))" \
        || fail "1. submission changed: $detail"

    sleep 3
    n_api="$(agent_api /v1/investigations | python3 -c "import json, sys
print(sum(1 for i in json.load(sys.stdin)['investigations'] if i['device'] == '$device'))")"
    n_mqtt="$(grep -c "\"device\":\"$device\"" "$results" || true)"
    n_influx="$(influx_points "$device")"
    [ "$n_api" = 1 ] && [ "$n_mqtt" = 1 ] && [ "$n_influx" = 1 ] \
        && pass "2. exactly one investigation (status API, MQTT result, InfluxDB point)" \
        || fail "2. investigations: api=$n_api mqtt=$n_mqtt influx=$n_influx"

    before="$(llm_requests)"
    pub -t "alerts/$device/anomaly" -m "$alert"
    sleep 4
    alerts="$(agent_api "/v1/investigations/$key" | json_get alerts)"
    n_api="$(agent_api /v1/investigations | python3 -c "import json, sys
print(sum(1 for i in json.load(sys.stdin)['investigations'] if i['device'] == '$device'))")"
    [ "$alerts" = 2 ] && [ "$n_api" = 1 ] && [ "$(llm_requests)" = "$before" ] \
        && [ "$(grep -c "\"device\":\"$device\"" "$results" || true)" = 1 ] \
        && pass "3. the redelivered alert joined it: no new investigation, model request or result" \
        || fail "3. alerts=$alerts investigations=$n_api model requests $before -> $(llm_requests)"
    kill "$sub" 2>/dev/null || true
done

kill "$SAMPLER" 2>/dev/null || true
python3 - "$WORK_DIR/mem" "$limit" <<'EOF'
import sys
values = [float(v) for v in open(sys.argv[1]) if v.strip()]
limit = int(sys.argv[2]) / 1048576
print(f"  agent memory under the limit of {limit:.0f} MiB: mean {sum(values) / len(values):.1f} MiB, "
      f"peak {max(values):.1f} MiB over {len(values)} samples")
EOF
echo "Crash test passed: $RUNS runs"
