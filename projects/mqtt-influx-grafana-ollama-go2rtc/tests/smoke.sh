#!/usr/bin/env bash
# ==============================================================================
# End-to-end smoke test: starts the iot and edge layers in a throwaway copy, as
# separate Compose projects the way `p4n4 up` does, and checks that contract
# reads reach SQLite (the agent API), InfluxDB, MQTT and every Grafana panel,
# that no plate reaches InfluxDB or MQTT, that bad reads are refused, and that
# the video service serves both cameras the manifest lists. The ai layer's
# agent tools are covered offline by tests/test_local_tools.py. Tears
# everything down (including volumes) on exit.
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

SITE=demo-road
SITE_TOKEN=smoke-site-token
READ_TOKEN=smoke-read-token
NETWORK=p4n4-alpr-smoke-net
# Deterministic data: no live simulator; `simulate.py --once` posts two seeded days below
sed -e 's/^COMPOSE_PROFILES=.*/COMPOSE_PROFILES=/' \
    -e "s/^DATA_UID=.*/DATA_UID=$(id -u)/" \
    -e "s/^DATA_GID=.*/DATA_GID=$(id -g)/" \
    -e "s/^SITE_ID=.*/SITE_ID=$SITE/" \
    -e "s/^SITE_TOKEN=.*/SITE_TOKEN=$SITE_TOKEN/" \
    -e "s/^EXTRA_SITE_TOKENS=.*/EXTRA_SITE_TOKENS=other-site:other-site-token/" \
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

# The edge layer: the road camera stands in as an HTTP MJPEG URL (the plates
# stream itself; its ?, = and & exercise video.sh's quoting), the plates stream
# loops a clip from edge/clips/, and no uplink (profile "device" off)
sed -e 's|^CAMERA_URL=.*|CAMERA_URL=http://localhost:1984/api/stream.mjpeg?src=plates\&smoke=1|' \
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

PROJECT=p4n4-alpr-smoke
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
compose exec -T mqtt sh -c "mosquitto_sub -h localhost -t 'traffic/$SITE/#' -C 40 -W 120" > "$WORK_DIR/mqtt.txt" &
mqtt_pid=$!
sleep 1
compose --profile demo run --rm --no-deps simulator python -u /scripts/simulate.py --once 2>&1 | tail -3

echo "== agent API"
summary="$(ingest GET '/api/v1/agent/summary?period=yesterday' "$READ_TOKEN")"
vehicles="$(echo "$summary" | field 'd.get("vehicles", 0)')"
[ "$vehicles" -gt 0 ] && ok "summary: $vehicles vehicles yesterday" || fail "summary: $summary"
[ "$(echo "$summary" | field 'd["synthetic_data"]')" = True ] && ok "demo data is marked synthetic" || fail "synthetic_data: $summary"
[ "$(echo "$summary" | field 'd["retention_days"]')" = 30 ] && ok "answers say how long plates are kept" || fail "retention_days: $summary"
frequent="$(ingest GET '/api/v1/agent/frequent?period=2d&limit=3' "$READ_TOKEN")"
[ "$(echo "$frequent" | field 'd["plates"][0]["plate"][:3]')" = MTB ] && ok "frequent plates: the buses first" || fail "frequent: $frequent"
plate="$(ingest GET '/api/v1/agent/plate?plate=mtb-1101' "$READ_TOKEN")"
[ "$(echo "$plate" | field 'd["passages_total"]')" -gt 0 ] && ok "plate lookup" || fail "plate: $plate"
for path in "/api/v1/agent/traffic?period=2d&direction=inbound" /api/v1/agent/checks /api/v1/agent/nodes; do
  r="$(ingest GET "$path" "$READ_TOKEN")"
  [ "${r%% *}" = 200 ] && ok "GET $path" || fail "GET $path: $r"
done

echo "== access rules"
r="$(ingest GET '/api/v1/agent/plate?plate=MTB-1101')"
[ "${r%% *}" = 401 ] && ok "plate lookups need a token" || fail "unauthenticated read: $r"
r="$(ingest GET /api/v1/agent/summary other-site-token)"
[ "${r%% *}" = 200 ] && [ "$(echo "$r" | field 'd["site_id"]')" = other-site ] && ok "a site token reads only its site" || fail "site-token read: $r"
r="$(ingest GET "/api/v1/agent/plate?plate=MTB-1101&site_id=$SITE" other-site-token)"
[ "${r%% *}" = 403 ] && ok "a site token can't look up another site's plates" || fail "cross-site read: $r"
WHEN="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
READ='{"schema":"p4n4.alpr.read/0.1","read_id":"RD-20260101-000001","site_id":"'$SITE'","camera_id":"cam-smoke","timestamp":"'$WHEN'","direction":"inbound","vehicle_type":"car","plate":"SMK-0001","plate_confidence":0.9,"synthetic":true}'
r="$(ingest POST /api/v1/reads wrong-token "$READ")"
[ "${r%% *}" = 401 ] && ok "writes need the site's token" || fail "bad token write: $r"
r="$(ingest POST /api/v1/reads other-site-token "$READ")"
[ "${r%% *}" = 400 ] && ok "a token writes only its own site" || fail "cross-site write: $r"
r="$(ingest POST /api/v1/reads "$SITE_TOKEN" "$READ")"
[ "$(echo "$r" | field 'd["created"]')" = 1 ] && ok "a contract read is stored" || fail "read: $r"
r="$(ingest POST /api/v1/reads "$SITE_TOKEN" "$READ")"
[ "$(echo "$r" | field 'd["unchanged"]')" = 1 ] && ok "a re-sent read is not counted twice" || fail "re-send: $r"
OLD="$(echo "$READ" | sed -e 's/RD-20260101-000001/RD-20200101-000001/' -e "s/$WHEN/2020-01-01T08:00:00Z/")"
r="$(ingest POST /api/v1/reads "$SITE_TOKEN" "$OLD")"
[ "${r%% *}" = 400 ] && ok "reads older than the retention period are refused" || fail "old read: $r"
compose logs ingest 2>&1 | grep -q "SMK-0001\|MTB-1101" && fail "the ingest log shows a plate" || ok "the ingest log shows no plates"

echo "== InfluxDB"
influx_count() {
  compose exec -T influxdb influx query --org "$INFLUXDB_ORG" --raw "$1" \
    | grep -v '^#' | sed -n 2p | awk -F, '{print $NF}' | tr -d '\r'
}
count="$(influx_count "from(bucket: \"$INFLUXDB_BUCKET\") |> range(start: -3d) |> filter(fn: (r) => r._measurement == \"alpr_read\" and r._field == \"count\") |> group() |> count()")"
[ "${count:-0}" -gt 0 ] && ok "$count reads copied to InfluxDB" || fail "no alpr_read points"
plates="$(compose exec -T influxdb influx query --org "$INFLUXDB_ORG" --raw "from(bucket: \"$INFLUXDB_BUCKET\") |> range(start: -3d) |> filter(fn: (r) => r._measurement == \"alpr_read\") |> limit(n: 200)" | grep -c 'MTB-\|SMK-' || true)"
[ "$plates" = 0 ] && ok "InfluxDB holds no plates" || fail "plates in InfluxDB: $plates rows"
health="$(influx_count "from(bucket: \"$INFLUXDB_BUCKET\") |> range(start: -1h) |> filter(fn: (r) => r._measurement == \"sensor_data\" and r.sensor == \"temperature_gpu_c\") |> group() |> count()")"
[ "${health:-0}" -gt 0 ] && ok "device health stored as sensor_data" || fail "no heartbeat sensor_data"

echo "== MQTT"
if wait "$mqtt_pid" && [ -s "$WORK_DIR/mqtt.txt" ]; then
  ok "reads are published under traffic/$SITE/"
  grep -q '"plate_read"' "$WORK_DIR/mqtt.txt" && ! grep -q '"plate":' "$WORK_DIR/mqtt.txt" \
    && ok "MQTT messages carry no plates" || fail "MQTT plates: $(head -c 300 "$WORK_DIR/mqtt.txt")"
else
  fail "no message under traffic/$SITE/"
fi

echo "== Grafana"
for _ in $(seq 1 30); do
  compose exec -T grafana wget -qO /dev/null http://localhost:3000/api/health 2>/dev/null && break
  sleep 2
done
python3 "$WORK_DIR/tests/check_dashboards.py" "$PROJECT" "$GRAFANA_USER" "$GRAFANA_PASSWORD" || failed=1

echo "== edge layer: video (project $PROJECT-edge)"
# A 2-second clip for the plates stream to loop, made with the video image's own ffmpeg
docker run --rm --user "$(id -u):$(id -g)" --entrypoint ffmpeg -v "$WORK_DIR/edge/clips:/out" alexxit/go2rtc:1.9.14 \
  -hide_banner -loglevel error -f lavfi -i testsrc2=size=320x240:rate=10 -t 2 -c:v libx264 -pix_fmt yuv420p /out/plates.mp4
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
bytes="$(compose_edge exec -T video sh -c "timeout 4 wget -qO- 'http://localhost:1984/api/stream.mjpeg?src=road' | wc -c" 2>/dev/null | tr -d ' \r' || true)"
[ "${bytes:-0}" -gt 100000 ] && ok "the road camera streams MJPEG ($bytes bytes in 4 s)" || fail "road MJPEG stream: ${bytes:-0} bytes"
config="$(compose_edge exec -T video cat /tmp/go2rtc.yaml)"
echo "$config" | grep -q "src=plates&smoke=1'" && echo "$config" | grep -q "ffmpeg:road#video=mjpeg" \
  && ok "CAMERA_URL is quoted, with an MJPEG transcoding fallback" || fail "go2rtc config: $config"
compose_edge logs video 2>&1 | grep -q "smoke=1" && fail "the video log shows the camera URL" || ok "the video log leaves camera URLs out"
echo "$config" | grep -q -- "-stream_loop -1 -i /p4n4/clips/plates.mp4" && compose_edge logs video 2>&1 | grep -q "plates from clips/plates.mp4" \
  && ok "a clip in edge/clips/ replaces the plates stream's demo pattern" || fail "plates clip: $config"

if [ "$failed" = 0 ]; then echo "PASS"; else echo "FAILED"; fi
exit "$failed"
