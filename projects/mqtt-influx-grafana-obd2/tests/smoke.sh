#!/usr/bin/env bash
# ==============================================================================
# End-to-end smoke test: starts the iot and extras layers in a throwaway copy, as
# separate Compose projects the way `p4n4 up` does, with the demo GPS route and a
# simulated car whose trouble codes are set from the start. Checks that the car's
# readings, trouble codes and status, the route's GPS fixes and a fix in the
# phone app's format reach InfluxDB and the archive, and that sensors/<id>/raw
# doesn't; that the map server starts without a map, then puts the client's
# address in style.json and answers range requests (on fixture files: nothing
# is downloaded); and that the Drive dashboard is Grafana's home page, with
# data in every panel. Tears everything down (including volumes) on exit.
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
# No archive (its directories stay) and no map to start with: the map server
# has to cope without one
find "$WORK_DIR/iot/data/archive" -type f ! -name .gitkeep -delete
find "$WORK_DIR/extras/data/map" -mindepth 1 -maxdepth 1 ! -name .gitkeep -exec rm -rf {} +
cd "$WORK_DIR/iot"

PROJECT=p4n4-drive-smoke
NETWORK=p4n4-smoke-net
# The demo route (profile "demo") laps Monaco as device "car", the obd2
# service's device id. The archive is owned by the current user
sed -e 's/^COMPOSE_PROFILES=.*/COMPOSE_PROFILES=demo/' \
    -e "s/^ARCHIVE_UID=.*/ARCHIVE_UID=$(id -u)/" \
    -e "s/^ARCHIVE_GID=.*/ARCHIVE_GID=$(id -g)/" \
    .env.example > .env
env_value() { grep -E "^$1=" .env | cut -d= -f2-; }

cat > docker-compose.override.yml <<EOF
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
  gps-sim:
    container_name: !reset null
networks:
  p4n4-net:
    name: $NETWORK
    ipam: !reset {}
EOF

# The extras layer: obd2 reads the simulated car (profile "demo") and publishes
# to the iot layer's broker, both by service name as the container names are
# gone. The car plays scenarios/smoke.yaml: steady driving, with a stored and a
# pending code from the start, which obd2 sends as soon as it connects
sed -e 's/^COMPOSE_PROFILES=.*/COMPOSE_PROFILES=demo/' \
    -e 's|^OBD_DEVICE=.*|OBD_DEVICE=socket://obd2-sim:35000|' \
    -e 's/^MQTT_HOST=.*/MQTT_HOST=mqtt/' \
    -e 's/^OBD_SIM_SCENARIO=.*/OBD_SIM_SCENARIO=smoke/' \
    ../extras/.env.example > ../extras/.env
cat > ../extras/docker-compose.override.yml <<EOF
services:
  obd2:
    container_name: !reset null
    ports: !reset []
  obd2-sim:
    container_name: !reset null
  tiles:
    container_name: !reset null
    ports: !reset []
networks:
  p4n4-net:
    name: $NETWORK
EOF
cat > ../extras/obd2/simulator/scenarios/smoke.yaml <<'EOF'
name: smoke
adapter: ELM327 v1.5
loop: 0
values:
  rpm: 2000
  speed: 50
  maf: 12
  engine_load: 40
  coolant_temp: 90
  intake_temp: 30
  stft_b1: 1
  ltft_b1: 2
  throttle_pos: 20
  fuel_level: 60
dtcs:
  - {code: P0171, type: stored}
  - {code: P0300, type: pending}
EOF

INFLUXDB_ORG="$(env_value INFLUXDB_ORG)"
INFLUXDB_BUCKET="$(env_value INFLUXDB_BUCKET)"
GRAFANA_USER="$(env_value GRAFANA_USER)"
GRAFANA_PASSWORD="$(env_value GRAFANA_PASSWORD)"

compose() { docker compose --project-name "$PROJECT" "$@"; }
compose_extras() { docker compose --project-directory ../extras --project-name "$PROJECT-extras" "$@"; }

cleanup() {
  status=$?
  if [ "$status" -ne 0 ]; then
    echo "--- telegraf logs ---"
    compose logs --no-color --tail 50 telegraf || true
    echo "--- obd2 logs ---"
    compose_extras logs --no-color --tail 30 obd2 || true
  fi
  if [ "${KEEP:-0}" = 1 ]; then
    echo "KEEP=1: stacks left running in $WORK_DIR (projects $PROJECT, $PROJECT-extras)"
  else
    compose_extras down -v --remove-orphans >/dev/null 2>&1 || true
    compose down -v --remove-orphans >/dev/null 2>&1 || true
    # Windows can't remove the working directory
    cd "$TEMPLATE_DIR"
    rm -rf "$WORK_DIR"
  fi
  exit "$status"
}
trap cleanup EXIT

failed=0
ok() { echo "  ok   $1"; }
fail() { echo "  FAIL $1"; failed=1; }

# Retries "$@" for up to 60s
eventually() {
  for _ in $(seq 30); do
    if "$@" >/dev/null 2>&1; then return 0; fi
    sleep 2
  done
  return 1
}

# Runs a Flux query on the last hour of the bucket ($1 follows range()) and
# prints Python expression $2, evaluated on its result: rows, a list of
# {column: value}, and v, {"<sensor>.<field>" (or "<field>"): value}, for
# queries ending in last(). Values are strings, as in the CSV.
flux() {
  compose exec -T influxdb influx query --org "$INFLUXDB_ORG" --raw \
    "from(bucket: \"$INFLUXDB_BUCKET\") |> range(start: -1h) $1" | python3 -c '
import csv, sys
rows, header = [], None
for line in csv.reader(sys.stdin.read().splitlines()):
    if not line:
        header = None
    elif not line[0].startswith("#"):
        if header is None:
            header = line
        else:
            rows.append(dict(zip(header, line)))
v = {".".join(r[k] for k in ("sensor", "_field") if k in r): r["_value"] for r in rows if "_value" in r}
print(eval("(" + sys.argv[1] + ")", {"rows": rows, "v": v}))
' "$2"
}
flux_true() { [ "$(flux "$1" "$2")" = True ]; }

echo "== starting the iot layer (project $PROJECT)"
compose up -d --wait --quiet-pull >/dev/null 2>&1 || { compose logs --no-color --tail 30; exit 1; }
# Telegraf has no healthcheck: wait for its four MQTT subscriptions, as obd2
# sends the codes it finds when it connects only once, not retained
telegraf_connected() { [ "$(compose logs telegraf | grep -c 'Connected \[tcp://mqtt:1883\]')" -ge 4 ]; }
eventually telegraf_connected || { fail "telegraf did not connect to the broker"; exit 1; }

echo "== starting the extras layer (project $PROJECT-extras): obd2, the simulated car, the map server"
compose_extras build --quiet
compose_extras up -d --wait --quiet-pull >/dev/null 2>&1 || { compose_extras logs --no-color --tail 30; exit 1; }

echo "== publishing"
pub() { compose exec -T mqtt mosquitto_pub -q 1 -t "$1" -m "$2"; }
# obd2's feature vector (with OBD_RAW=true) and a bare number on its topic:
# neither may be stored
pub sensors/car/raw '{"values": [2000.0, 40.0, 90.0, 1.0, 2.0, 12.0, 50.0]}'
pub sensors/car/raw 42
# A fix exactly as the phone app publishes it
pub sensors/smoke-phone/gps '{"lat": 43.73, "lon": 7.42, "alt": 52.0, "speed": 37.5, "heading": 182.0, "acc": 4.0}'

echo "== InfluxDB"
CAR='|> filter(fn: (r) => r._measurement == "sensor_data" and r.device == "car") |> group(columns: ["sensor", "_field"]) |> last()'
eventually flux_true "$CAR" '{"rpm.value", "rpm.unit", "speed.value", "coolant_temp.value", "battery_v.value"} <= set(v)' \
  && ok "the car's readings: rpm, speed, coolant_temp, battery_v" || fail "the car's readings: $(flux "$CAR" 'sorted(v)')"
eventually flux_true "$CAR" 'all("gps." + f in v for f in ("lat", "lon", "alt", "speed", "heading", "acc"))
    and 43.72 < float(v["gps.lat"]) < 43.76 and 7.40 < float(v["gps.lon"]) < 7.45' \
  && ok "the demo route's GPS fixes, in Monaco" \
  || fail "the demo route's GPS fixes: $(flux "$CAR" '{k: x for k, x in v.items() if k.startswith("gps.")}')"
PHONE='|> filter(fn: (r) => r._measurement == "sensor_data" and r.device == "smoke-phone") |> group(columns: ["sensor", "_field"]) |> last()'
eventually flux_true "$PHONE" 'v == {"gps.lat": "43.73", "gps.lon": "7.42", "gps.alt": "52", "gps.speed": "37.5", "gps.heading": "182", "gps.acc": "4"}' \
  && ok "the phone app's fix: sensor_data, sensor=gps, its six fields" || fail "the phone app's fix: $(flux "$PHONE" 'v')"
STATUS='|> filter(fn: (r) => r._measurement == "vehicle_status" and r.device == "car") |> last()'
eventually flux_true "$STATUS" 'v["online"] == v["vehicle"] == "true" and v["adapter"] == "ELM327 v1.5" and v["protocol"] != ""' \
  && ok "vehicle_status: online, the car answering, the adapter and protocol" || fail "vehicle_status: $(flux "$STATUS" 'v')"
DTC='|> filter(fn: (r) => r._measurement == "dtc" and r.device == "car") |> last()'
eventually flux_true "$DTC" 'v["codes"] == "P0171" and v["pending"] == "P0300" and v["permanent"] == "" and v["count"] == "2"
    and v["mil"] == "true" and v["descriptions"].startswith("P0171: ") and "; P0300: " in v["descriptions"]' \
  && ok "dtc: the stored and pending codes, their descriptions, the MIL" || fail "dtc: $(flux "$DTC" 'v')"
# The phone's fix went out after sensors/car/raw, so Telegraf has seen both by now
RAW='|> filter(fn: (r) => r._measurement == "sensor_data" and r.sensor == "raw")'
flux_true "$RAW" 'rows == []' && ok "nothing from sensors/car/raw" || fail "sensors/car/raw was stored: $(flux "$RAW" 'rows[:3]')"

echo "== archive"
LP=data/archive/lineprotocol/telemetry.lp
RAW_ARCHIVE=data/archive/raw/mqtt.jsonl
in_lp() { grep -q "$1" "$LP"; }
in_raw() { grep -q "\"topic\":\"$1\"" "$RAW_ARCHIVE"; }
eventually in_lp '^sensor_data,device=smoke-phone,sensor=gps acc=4,alt=52,heading=182,lat=43.73,lon=7.42,speed=37.5 ' \
  && ok "line protocol: the phone's fix, every number a float" || fail "$LP is missing the phone's fix"
eventually in_lp '^dtc,device=car codes="P0171",count=2,' && in_lp '^vehicle_status,device=car adapter="ELM327 v1.5",online=true,' \
  && ok "line protocol: dtc and vehicle_status" || fail "$LP is missing dtc or vehicle_status"
eventually in_raw sensors/smoke-phone/gps && in_raw alerts/car/dtc && in_raw status/car/online \
  && ok "raw archive: GPS fixes, trouble codes and status" || fail "$RAW_ARCHIVE is missing messages"
! in_raw sensors/car/raw && ok "raw archive: nothing from sensors/car/raw" || fail "$RAW_ARCHIVE has sensors/car/raw"
python3 - "$RAW_ARCHIVE" <<'EOF' && ok "raw archive is valid JSON Lines" || fail "$RAW_ARCHIVE is not valid JSON Lines"
import json, sys
rows = [json.loads(line) for line in open(sys.argv[1])]
assert rows and all(set(r) == {"received_at", "topic", "payload"} for r in rows), rows
EOF

echo "== map server"
# An HTTP request to the map server from the obd2 container, which has Python:
#   tiles METHOD PATH [HEADER ...]  → prints {"status", "headers", "body"} as JSON
# PATH has no leading slash, which Git Bash on Windows would turn into a file path
tiles() {
  compose_extras exec -T obd2 python - "$@" <<'PY'
import json, sys, urllib.error, urllib.request
method, path = sys.argv[1], sys.argv[2]
headers = dict(h.split(": ", 1) for h in sys.argv[3:])
req = urllib.request.Request("http://tiles:8088/" + path, method=method, headers=headers)
try:
    r = urllib.request.urlopen(req, timeout=10)
    status = r.status
except urllib.error.HTTPError as e:
    r, status = e, e.code
print(json.dumps({"status": status, "headers": {k.lower(): v for k, v in r.headers.items()},
                  "body": r.read().decode("latin-1")}))
PY
}
# is RESPONSE EXPR: Python expression EXPR is true of d, the response tiles printed
is() { [ "$(printf '%s' "$1" | python3 -c "import json,sys; d=json.load(sys.stdin); print(eval('(' + sys.argv[1] + ')', {'d': d}))" "$2")" = True ]; }
r="$(tiles GET healthz || true)"
is "$r" 'd["status"] == 200' && ok "/healthz" || fail "/healthz: $r"
r="$(tiles GET style.json || true)"
is "$r" 'd["status"] == 404' && ok "no map yet: /style.json is a 404, so the app shows an online map" || fail "/style.json without a map: $r"

# Fixtures in place of what scripts/fetch-map.sh downloads: a style with
# __ORIGIN__ in its URLs, 64 bytes of "tiles", and a glyph range in a font
# directory with spaces in its name
MAP=../extras/data/map
mkdir -p "$MAP/fonts/Noto Sans Regular"
cat > "$MAP/style.json" <<'EOF'
{"version": 8, "glyphs": "__ORIGIN__/fonts/{fontstack}/{range}.pbf", "sprite": "__ORIGIN__/sprites/v4/light",
 "sources": {"protomaps": {"type": "vector", "url": "pmtiles://__ORIGIN__/region.pmtiles"}}, "layers": []}
EOF
printf '0123456789abcdef%.0s' 1 2 3 4 > "$MAP/region.pmtiles"
printf glyphs > "$MAP/fonts/Noto Sans Regular/0-255.pbf"

style_served() { is "$(tiles GET style.json || true)" 'd["status"] == 200'; }
eventually style_served || true
r="$(tiles GET style.json || true)"
is "$r" 'd["status"] == 200 and "__ORIGIN__" not in d["body"] and "pmtiles://http://tiles:8088/region.pmtiles" in d["body"]
    and "http://tiles:8088/fonts/{fontstack}/{range}.pbf" in d["body"] and "http://tiles:8088/sprites/v4/light" in d["body"]' \
  && ok "/style.json: __ORIGIN__ is the address the client used" || fail "/style.json: $r"
r="$(tiles GET region.pmtiles 'Range: bytes=0-15' || true)"
is "$r" 'd["status"] == 206 and d["body"] == "0123456789abcdef" and d["headers"].get("content-range") == "bytes 0-15/64"
    and d["headers"].get("content-type") == "application/vnd.pmtiles" and d["headers"].get("access-control-allow-origin") == "*"
    and "Content-Range" in d["headers"].get("access-control-expose-headers", "")' \
  && ok "/region.pmtiles: a range request gets 206, its 16 bytes and CORS headers" || fail "/region.pmtiles, bytes 0-15: $r"
r="$(tiles OPTIONS region.pmtiles 'Access-Control-Request-Method: GET' 'Access-Control-Request-Headers: range' || true)"
is "$r" 'd["status"] == 204 and "Range" in d["headers"].get("access-control-allow-headers", "")' \
  && ok "CORS preflight: 204, Range allowed" || fail "CORS preflight: $r"
r="$(tiles GET 'fonts/Noto%20Sans%20Regular/0-255.pbf' || true)"
is "$r" 'd["status"] == 200 and d["body"] == "glyphs" and d["headers"].get("content-type") == "application/x-protobuf"' \
  && ok "/fonts/Noto%20Sans%20Regular/0-255.pbf: a glyph range" || fail "glyphs: $r"
r="$(tiles GET .gitkeep || true)"
is "$r" 'd["status"] == 404' && ok "dotfiles aren't served" || fail "/.gitkeep: $r"

echo "== Grafana"
# Grafana publishes no port here, so call its API from inside the container
grafana_api() {
  compose exec -T grafana wget -qO- "http://$GRAFANA_USER:$GRAFANA_PASSWORD@localhost:3000/api/$1"
}
has() { case "$1" in *"$2"*) return 0 ;; *) return 1 ;; esac; }
has "$(grafana_api datasources/uid/influxdb-telemetry/health || true)" '"status":"OK"' \
  && ok "the InfluxDB datasource is healthy" || fail "Grafana datasource health check failed"
# The dashboard p4n4-dashboard opens: .p4n4.json dashboard.grafana_path is /d/<uid>/<slug>
GRAFANA_PATH="$(python3 -c 'import json; print(json.load(open("../.p4n4.json"))["dashboard"]["grafana_path"])')"
GRAFANA_UID="$(echo "$GRAFANA_PATH" | cut -d/ -f3)"
has "$(grafana_api "dashboards/uid/$GRAFANA_UID" || true)" "\"url\":\"$GRAFANA_PATH\"" \
  && ok "dashboard.grafana_path ($GRAFANA_PATH) is provisioned" || fail "dashboard.grafana_path $GRAFANA_PATH is not provisioned"
# Without p4n4-api, the app's Grafana tab opens Grafana's home page: the same dashboard
has "$(grafana_api dashboards/home || true)" "\"uid\":\"$GRAFANA_UID\"" \
  && ok "it's Grafana's home page" || fail "Grafana's home page is not $GRAFANA_UID"
python3 ../tests/check_dashboards.py "$PROJECT" "$GRAFANA_USER" "$GRAFANA_PASSWORD" || failed=1

if [ "$failed" = 0 ]; then echo "PASS"; else echo "FAILED"; fi
exit "$failed"
