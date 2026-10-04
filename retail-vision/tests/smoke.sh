#!/usr/bin/env bash
# ==============================================================================
# End-to-end smoke test: starts the iot and edge layers in a throwaway copy, as
# separate Compose projects the way `p4n4 up` does, and checks that contract
# documents reach SQLite (the agent API), InfluxDB, MQTT and every Grafana
# panel, that bad ones are refused, and that the video service serves both
# cameras the manifest lists. The ai layer's agent tools are covered offline by
# tests/test_local_tools.py. Tears everything down (including volumes) on exit.
#
#   ./tests/smoke.sh            # from the template directory
#   KEEP=1 ./tests/smoke.sh     # leave the stacks running for inspection
#
# Requires Docker with Compose v2 and python3. The test stacks drop the fixed
# container names, host ports and network name, so they run alongside other stacks.
# ==============================================================================
set -euo pipefail

TEMPLATE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
WORK_DIR="$(mktemp -d)"
cp -a "$TEMPLATE_DIR/." "$WORK_DIR/"
rm -f "$WORK_DIR"/iot/data/ingest/ingest.db*
cd "$WORK_DIR/iot"

STORE=demo-store
STORE_TOKEN=smoke-store-token
READ_TOKEN=smoke-read-token
NETWORK=p4n4-retail-smoke-net
# Deterministic data: no live simulator; `simulate.py --once` posts two seeded days below
sed -e 's/^COMPOSE_PROFILES=.*/COMPOSE_PROFILES=/' \
    -e "s/^DATA_UID=.*/DATA_UID=$(id -u)/" \
    -e "s/^DATA_GID=.*/DATA_GID=$(id -g)/" \
    -e "s/^STORE_ID=.*/STORE_ID=$STORE/" \
    -e "s/^STORE_TOKEN=.*/STORE_TOKEN=$STORE_TOKEN/" \
    -e "s/^EXTRA_STORE_TOKENS=.*/EXTRA_STORE_TOKENS=other-store:other-store-token/" \
    -e "s/^INGEST_READ_TOKEN=.*/INGEST_READ_TOKEN=$READ_TOKEN/" \
    -e 's/^SIMULATOR_BACKFILL_DAYS=.*/SIMULATOR_BACKFILL_DAYS=2/' \
    .env.example > .env
env_value() { grep -E "^$1=" .env | cut -d= -f2-; }

cat > docker-compose.override.yml <<EOF
services:
  ingest:
    container_name: !reset null
    ports: !reset []
  mqtt:
    container_name: !reset null
    ports: !reset []
  influxdb:
    container_name: !reset null
    ports: !reset []
  grafana:
    container_name: !reset null
    ports: !reset []
  simulator:
    container_name: !reset null
networks:
  p4n4-net:
    name: $NETWORK
    ipam: !reset {}
EOF

# The edge layer: the store camera stands in as an HTTP MJPEG URL (the overlay
# stream itself; its ?, = and & exercise video.sh's quoting), the overlay
# loops a clip from edge/clips/, and no uplink (profile "device" off)
sed -e 's|^CAMERA_URL=.*|CAMERA_URL=http://localhost:1984/api/stream.mjpeg?src=overlay\&smoke=1|' \
    ../edge/.env.example > ../edge/.env
cat > ../edge/docker-compose.override.yml <<EOF
services:
  video:
    container_name: !reset null
    ports: !reset []
  uplink:
    container_name: !reset null
networks:
  p4n4-net:
    name: $NETWORK
EOF

INFLUXDB_ORG="$(env_value INFLUXDB_ORG)"
INFLUXDB_BUCKET="$(env_value INFLUXDB_BUCKET)"
GRAFANA_USER="$(env_value GRAFANA_USER)"
GRAFANA_PASSWORD="$(env_value GRAFANA_PASSWORD)"

PROJECT=p4n4-retail-smoke
compose() { docker compose --project-name "$PROJECT" "$@"; }
compose_edge() { docker compose --project-directory ../edge --project-name "$PROJECT-edge" "$@"; }

cleanup() {
  status=$?
  if [ "${KEEP:-0}" = 1 ]; then
    echo "KEEP=1: stacks left running in $WORK_DIR (projects $PROJECT, $PROJECT-edge)"
  else
    compose_edge down -v --remove-orphans >/dev/null 2>&1 || true
    compose down -v --remove-orphans >/dev/null 2>&1 || true
    rm -rf "$WORK_DIR"
  fi
  exit $status
}
trap cleanup EXIT

failed=0
ok() { echo "  ok   $1"; }
fail() { echo "  FAIL $1"; failed=1; }

# An HTTP request to the ingest service from inside its own container:
#   ingest METHOD PATH [TOKEN] [JSON]  → prints "<status> <body>"
ingest() {
  compose exec -T ingest python - "$@" <<'PY'
import sys, urllib.request, urllib.error
method, path = sys.argv[1], sys.argv[2]
token = sys.argv[3] if len(sys.argv) > 3 else ""
body = sys.argv[4].encode() if len(sys.argv) > 4 else None
req = urllib.request.Request("http://localhost:8080" + path, data=body, method=method,
                             headers={"Content-Type": "application/json", **({"Authorization": "Bearer " + token} if token else {})})
try:
    with urllib.request.urlopen(req, timeout=10) as r:
        print(r.status, r.read().decode())
except urllib.error.HTTPError as e:
    print(e.code, e.read().decode())
PY
}
field() { python3 -c "import json,sys; d=json.loads(sys.stdin.read().split(' ',1)[1]); print(eval(sys.argv[1], {}, {'d': d}))" "$1"; }

echo "== starting the iot layer (project $PROJECT)"
compose up -d --wait --quiet-pull >/dev/null 2>&1 || { compose logs --no-color --tail 30; exit 1; }

echo "== posting two seeded days and a heartbeat (simulate.py --once)"
compose exec -T mqtt sh -c "mosquitto_sub -h localhost -t 'retail/$STORE/heartbeat' -C 1 -W 120" > "$WORK_DIR/mqtt.txt" &
mqtt_pid=$!
sleep 1
compose --profile demo run --rm --no-deps simulator python -u /scripts/simulate.py --once 2>&1 | tail -3

echo "== agent API"
summary="$(ingest GET '/api/v1/agent/summary?period=yesterday' "$READ_TOKEN")"
visitors="$(echo "$summary" | field 'd.get("visitors", 0)')"
[ "$visitors" -gt 0 ] && ok "summary: $visitors visitors yesterday" || fail "summary: $summary"
[ "$(echo "$summary" | field 'd["synthetic_data"]')" = True ] && ok "demo data is marked synthetic" || fail "synthetic_data: $summary"
layout="$(ingest GET '/api/v1/agent/layout?period=2d' "$READ_TOKEN")"
[ "$(echo "$layout" | field 'd["most_interest"]')" != None ] && ok "layout ranks shelves" || fail "layout: $layout"
alerts="$(ingest GET '/api/v1/agent/alerts?period=2d&type=A1' "$READ_TOKEN")"
[ "$(echo "$alerts" | field 'd["total"]')" -gt 0 ] && ok "alerts by A1-A4 code" || fail "alerts: $alerts"
[ "$(echo "$alerts" | field 'd["by_status"].get("attended", 0)')" -gt 0 ] && ok "staff reviews are stored" || fail "reviews: $alerts"
for path in "/api/v1/agent/ranking?category=shirts&period=2d" /api/v1/agent/restock_advice /api/v1/agent/nodes; do
  r="$(ingest GET "$path" "$READ_TOKEN")"
  [ "${r%% *}" = 200 ] && ok "GET $path" || fail "GET $path: $r"
done

echo "== access rules"
r="$(ingest GET /api/v1/agent/summary)"
[ "${r%% *}" = 401 ] && ok "reads need a token" || fail "unauthenticated read: $r"
r="$(ingest GET /api/v1/agent/summary other-store-token)"
[ "${r%% *}" = 200 ] && [ "$(echo "$r" | field 'd["store_id"]')" = other-store ] && ok "a store token reads only its store" || fail "store-token read: $r"
r="$(ingest GET "/api/v1/agent/summary?store_id=$STORE" other-store-token)"
[ "${r%% *}" = 403 ] && ok "a store token can't read another store" || fail "cross-store read: $r"
EVENT='{"schema":"aioros.boutique.event/0.1","store_id":"'$STORE'","camera_id":"cam-smoke","event_id":"EVT-20260101-000001","type":"customer_entry","ts_start":"2026-01-01T10:00:00-05:00","ts_end":"2026-01-01T10:00:00-05:00","track_id":"P001","role":"customer","synthetic":true}'
r="$(ingest POST /api/v1/events wrong-token "$EVENT")"
[ "${r%% *}" = 401 ] && ok "writes need the store's token" || fail "bad token write: $r"
r="$(ingest POST /api/v1/events other-store-token "$EVENT")"
[ "${r%% *}" = 400 ] && ok "a token writes only its own store" || fail "cross-store write: $r"
r="$(ingest POST /api/v1/events "$STORE_TOKEN" "$EVENT")"
[ "$(echo "$r" | field 'd["created"]')" = 1 ] && ok "a contract event is stored" || fail "event: $r"
r="$(ingest POST /api/v1/events "$STORE_TOKEN" "$EVENT")"
[ "$(echo "$r" | field 'd["unchanged"]')" = 1 ] && ok "a re-sent event is not counted twice" || fail "re-send: $r"
ALERT='{"schema":"aioros.boutique.alert/0.1","alert_id":"ALT-20260101-000001","store_id":"'$STORE'","camera_id":"cam-smoke","alert_type":"loss_risk_review","severity":"medium","timestamp":"2026-01-01T10:00:00-05:00","title":"Possible theft","description":"x","review_required":true,"synthetic":true}'
r="$(ingest POST /api/v1/alerts "$STORE_TOKEN" "$ALERT")"
[ "${r%% *}" = 400 ] && ok "accusatory alerts are refused" || fail "accusatory alert: $r"

echo "== InfluxDB"
influx_count() {
  compose exec -T influxdb influx query --org "$INFLUXDB_ORG" --raw "$1" \
    | grep -v '^#' | sed -n 2p | awk -F, '{print $NF}' | tr -d '\r'
}
count="$(influx_count "from(bucket: \"$INFLUXDB_BUCKET\") |> range(start: -3d) |> filter(fn: (r) => r._measurement == \"boutique_event\" and r._field == \"count\") |> group() |> count()")"
[ "${count:-0}" -gt 0 ] && ok "$count events copied to InfluxDB" || fail "no boutique_event points"
health="$(influx_count "from(bucket: \"$INFLUXDB_BUCKET\") |> range(start: -1h) |> filter(fn: (r) => r._measurement == \"sensor_data\" and r.sensor == \"temperature_gpu_c\") |> group() |> count()")"
[ "${health:-0}" -gt 0 ] && ok "device health stored as sensor_data" || fail "no heartbeat sensor_data"

echo "== MQTT"
if wait "$mqtt_pid" && [ -s "$WORK_DIR/mqtt.txt" ]; then
  ok "heartbeats are published on retail/$STORE/heartbeat"
else
  fail "no message on retail/$STORE/heartbeat"
fi

echo "== Grafana"
for _ in $(seq 1 30); do
  compose exec -T grafana wget -qO /dev/null http://localhost:3000/api/health 2>/dev/null && break
  sleep 2
done
python3 "$WORK_DIR/tests/check_dashboards.py" "$PROJECT" "$GRAFANA_USER" "$GRAFANA_PASSWORD" || failed=1

echo "== edge layer: video (project $PROJECT-edge)"
# A 2-second clip for the overlay to loop, made with the video image's own ffmpeg
docker run --rm --user "$(id -u):$(id -g)" --entrypoint ffmpeg -v "$WORK_DIR/edge/clips:/out" alexxit/go2rtc:1.9.14 \
  -hide_banner -loglevel error -f lavfi -i testsrc2=size=320x240:rate=10 -t 2 -c:v libx264 -pix_fmt yuv420p /out/overlay.mp4
compose_edge up -d --wait --quiet-pull >/dev/null 2>&1 || { compose_edge logs --no-color --tail 30; exit 1; }
# One JPEG from each camera .p4n4.json lists, as the dashboard's Video tab asks for them
for src in $(python3 -c "import json; print(' '.join(c['path'].split('src=')[1] for c in json.load(open('$WORK_DIR/.p4n4.json'))['dashboard']['cameras']))"); do
  magic=""
  for _ in $(seq 1 15); do
    magic="$(compose_edge exec -T video sh -c "wget -qO- 'http://localhost:1984/api/frame.jpeg?src=$src' | head -c 3 | od -An -tx1" 2>/dev/null | tr -d ' \n')" || true
    [ "$magic" = ffd8ff ] && break
    sleep 2
  done
  [ "$magic" = ffd8ff ] && ok "camera '$src' serves JPEG frames" || fail "camera '$src': no JPEG (got '$magic')"
done
bytes="$(compose_edge exec -T video sh -c "timeout 4 wget -qO- 'http://localhost:1984/api/stream.mjpeg?src=floor' | wc -c" 2>/dev/null | tr -d ' \r' || true)"
[ "${bytes:-0}" -gt 100000 ] && ok "the floor camera streams MJPEG ($bytes bytes in 4 s)" || fail "floor MJPEG stream: ${bytes:-0} bytes"
config="$(compose_edge exec -T video cat /tmp/go2rtc.yaml)"
echo "$config" | grep -q "src=overlay&smoke=1'" && echo "$config" | grep -q "ffmpeg:floor#video=mjpeg" \
  && ok "CAMERA_URL is quoted, with an MJPEG transcoding fallback" || fail "go2rtc config: $config"
compose_edge logs video 2>&1 | grep -q "smoke=1" && fail "the video log shows the camera URL" || ok "the video log leaves camera URLs out"
echo "$config" | grep -q -- "-stream_loop -1 -i /p4n4/clips/overlay.mp4" && compose_edge logs video 2>&1 | grep -q "overlay from clips/overlay.mp4" \
  && ok "a clip in edge/clips/ replaces the overlay's demo pattern" || fail "overlay clip: $config"

if [ "$failed" = 0 ]; then echo "PASS"; else echo "FAILED"; fi
exit "$failed"
