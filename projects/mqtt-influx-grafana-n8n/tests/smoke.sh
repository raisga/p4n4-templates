#!/usr/bin/env bash
# ==============================================================================
# End-to-end smoke test: starts both layers of the template in a throwaway
# copy, iot then ai, as separate Compose projects the way `p4n4 up` does.
# Publishes cold-room readings and drives the n8n workflows through their
# webhooks with short timings: an excursion is started, alerted, escalated,
# acknowledged through the signed link and resolved; a silent unit goes
# offline and is acknowledged before it can escalate; the daily record is
# emailed and saved. Checks Mailpit, a stand-in Slack webhook, InfluxDB, the
# archives and every Grafana panel. Tears everything down (including volumes)
# on exit.
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
chmod 777 "$WORK_DIR/ai/data/reports"   # n8n runs as uid 1000
cd "$WORK_DIR/iot"

# Deterministic data: no simulator, archive owned by the current user. The
# bridge pulls from the "remote" broker below; its password needs quoting.
REMOTE_USER=smoke
REMOTE_PASSWORD='smoke$pa ss#1'
sed -e 's/^COMPOSE_PROFILES=.*/COMPOSE_PROFILES=/' \
    -e "s/^ARCHIVE_UID=.*/ARCHIVE_UID=$(id -u)/" \
    -e "s/^ARCHIVE_GID=.*/ARCHIVE_GID=$(id -g)/" \
    -e "s/^MQTT_REMOTE_HOST=.*/MQTT_REMOTE_HOST=remote/" \
    -e "s/^MQTT_REMOTE_USER=.*/MQTT_REMOTE_USER=$REMOTE_USER/" \
    -e "s/^MQTT_REMOTE_PASSWORD=.*/MQTT_REMOTE_PASSWORD='$REMOTE_PASSWORD'/" \
    .env.example > .env
# Short timings, a known ack secret, and Slack pointed at a stand-in
ALERT_DELAY=20
ESCALATE_AFTER=12
sed -e "s/^ALERT_DELAY=.*/ALERT_DELAY=$ALERT_DELAY/" \
    -e "s/^ESCALATE_AFTER=.*/ESCALATE_AFTER=$ESCALATE_AFTER/" \
    -e 's/^ACK_SECRET=.*/ACK_SECRET=smoke-secret/' \
    -e 's|^SLACK_WEBHOOK_URL=.*|SLACK_WEBHOOK_URL=http://slack:8080/hook|' \
    ../ai/.env.example > ../ai/.env
# A unit that never reports, to go offline
python3 - ../ai/config/coldchain/units.json <<'EOF'
import json, sys
units = json.load(open(sys.argv[1]))
units.append({"id": "smoke-silent", "name": "Silent fridge", "location": "Nowhere", "min": 2, "max": 8})
json.dump(units, open(sys.argv[1], "w"), indent=2)
EOF
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
# "slack" prints every message posted to it
cat > ../ai/docker-compose.override.yml <<'EOF'
services:
  n8n:
    container_name: !reset null
    ports: !reset []
  mailpit:
    container_name: !reset null
    ports: !reset []
  slack:
    image: python:3.13-slim
    command:
      - python
      - -uc
      - |
        from http.server import BaseHTTPRequestHandler, HTTPServer
        class Hook(BaseHTTPRequestHandler):
            def do_POST(self):
                print("slack:", self.rfile.read(int(self.headers["Content-Length"])).decode())
                self.send_response(200); self.end_headers(); self.wfile.write(b"ok")
        HTTPServer(("", 8080), Hook).serve_forever()
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
        compose logs --no-color --tail 30 telegraf || true
        echo "--- n8n logs ---"
        compose_ai logs --no-color --tail 60 n8n || true
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

pub() { compose exec -T mqtt mosquitto_pub -q 1 -t "$1" -m "$2"; }
# n8n publishes no port here: call it, and Mailpit, from inside its container
n8n_get() { compose_ai exec -T n8n wget -qO- "http://localhost:5678/$1"; }
n8n_post() { compose_ai exec -T n8n wget -qO- --post-data "$2" "http://localhost:5678/$1"; }
# Runs the monitor now; prints {"device": "state", ...}
check() {
    n8n_post webhook/coldchain/check '' \
        | python3 -c 'import json, sys; print(json.dumps({u["device"]: u["state"] for u in json.load(sys.stdin)["units"]}, sort_keys=True))'
}
# Number of emails matching a Mailpit search
mail_count() {
    compose_ai exec -T n8n wget -qO- "http://mailpit:8025/api/v1/search?query=$(python3 -c 'import sys, urllib.parse; print(urllib.parse.quote(sys.argv[1]))' "$1")" \
        | python3 -c 'import json, sys; print(json.load(sys.stdin)["messages_count"])'
}
# Text of the newest email matching a search
mail_text() {
    local id
    id="$(compose_ai exec -T n8n wget -qO- "http://mailpit:8025/api/v1/search?query=$(python3 -c 'import sys, urllib.parse; print(urllib.parse.quote(sys.argv[1]))' "$1")" \
        | python3 -c 'import json, sys; print(json.load(sys.stdin)["messages"][0]["ID"])')"
    compose_ai exec -T n8n wget -qO- "http://mailpit:8025/api/v1/message/$id"
}
# expect_state <json from check> <device> <state> <description>
expect_state() {
    python3 -c 'import json, sys; sys.exit(json.loads(sys.argv[1]).get(sys.argv[2]) != sys.argv[3])' "$1" "$2" "$3" \
        && pass "$4" || fail "$4 (states: $1)"
}

echo "Checking provisioning"
n8n_get healthz/readiness >/dev/null && pass "n8n is up" || fail "n8n is not ready"
compose_ai exec -T n8n test -f /home/node/.n8n/.p4n4-workflows-imported \
    && pass "workflows imported on first start" || fail "workflows were not imported"

echo "Publishing readings"
pub sensors/fridge-01/temperature '{"value": 4.8, "unit": "C"}'
pub sensors/freezer-01/temperature '-19.5'
pub sensors/fridge-02/door '1'
pub sensors/fridge-02/temperature '{"value": 9.4, "unit": "C"}'
influx_count() {
    compose exec -T influxdb influx query --raw \
        "from(bucket: \"$INFLUXDB_BUCKET\") |> range(start: -1h)
         |> filter(fn: (r) => r._measurement == \"sensor_data\" and r._field == \"value\" and r.sensor == \"temperature\")
         |> group() |> count()"
}
has_readings() { influx_count | grep -qE ',3\s*$'; }
eventually has_readings && pass "InfluxDB has the readings" || fail "InfluxDB is missing readings"

echo "Checking the excursion lifecycle"
states="$(check)"
expect_state "$states" fridge-01 ok "fridge-01 at 4.8 °C is OK (2–8)"
expect_state "$states" freezer-01 ok "freezer-01 at -19.5 °C is OK (-25 – -15)"
expect_state "$states" fridge-02 excursion "fridge-02 at 9.4 °C starts an excursion (0–5)"
expect_state "$states" smoke-silent offline "a unit that never reported is offline"
[ "$(mail_count 'subject:ALERT')" = 0 ] && pass "no alert before ALERT_DELAY" || fail "alerted before ALERT_DELAY"

# Both excursions started by now (fridge-02's at its reading, the silent unit's here)
sleep $((ALERT_DELAY + 1))
states="$(check)"
expect_state "$states" fridge-02 alerted "fridge-02 alerted after ALERT_DELAY"
expect_state "$states" smoke-silent alerted "the silent unit alerted after ALERT_DELAY"
[ "$(mail_count 'to:on-duty@example.com subject:"ALERT: Dairy walk-in"')" = 1 ] \
    && pass "alert email to ALERT_EMAIL_TO" || fail "no alert email for fridge-02"
mail_text 'subject:"ALERT: Dairy walk-in"' | grep -q 'door open' \
    && pass "the alert mentions the open door" || fail "the alert doesn't mention the door"
slack_got_alert() { compose_ai logs --no-color slack | grep -q 'ALERT: Dairy walk-in'; }
eventually slack_got_alert && pass "alert posted to the Slack webhook" || fail "no Slack message"

# The silent unit is acknowledged at once, so it must never escalate
ack_link() {
    mail_text "subject:\"ALERT: $1\"" | python3 -c '
import json, re, sys
print(re.search(r"http://\S+/webhook/coldchain/ack\?\S+", json.load(sys.stdin)["Text"]).group(0))'
}
SILENT_LINK="$(ack_link 'Silent fridge')"
SILENT_PATH="${SILENT_LINK#http://localhost:5678/}"
n8n_get "$SILENT_PATH" | grep -q 'Corrective action taken' \
    && pass "the ack link opens the form" || fail "the ack link did not open the form"
! n8n_get "${SILENT_PATH%token=*}token=forged" >/dev/null 2>&1 \
    && pass "a link with a forged token is rejected" || fail "a forged token was accepted"
# The form fields an ack link fills in, URL-encoded
form() {
    python3 -c 'import sys, urllib.parse as u; q = u.parse_qs(u.urlsplit(sys.argv[1]).query); print(u.urlencode({k: q[k][0] for k in ("device", "excursion", "token")}))' "$1"
}
FORM="$(form "$SILENT_LINK")"
! n8n_post webhook/coldchain/ack "$FORM&by=&action=" >/dev/null 2>&1 \
    && pass "an ack without name or action is rejected" || fail "an empty ack was accepted"
n8n_post webhook/coldchain/ack "$FORM&by=Smoke+Tester&action=Sensor+cable+reseated" | grep -q 'Acknowledged' \
    && pass "the ack form records the acknowledgement" || fail "the ack was not recorded"

sleep $((ESCALATE_AFTER + 1))
states="$(check)"
expect_state "$states" fridge-02 escalated "fridge-02 escalated after ESCALATE_AFTER without an ack"
expect_state "$states" smoke-silent acknowledged "the acknowledged unit did not escalate"
[ "$(mail_count 'to:manager@example.com subject:"ESCALATION: Dairy walk-in"')" = 1 ] \
    && pass "escalation email to ESCALATION_EMAIL_TO" || fail "no escalation email"
[ "$(mail_count 'subject:"ESCALATION: Silent fridge"')" = 0 ] \
    && pass "no escalation email for the acknowledged unit" || fail "the acknowledged unit escalated"

FORM="$(form "$(ack_link 'Dairy walk-in')")"
n8n_post webhook/coldchain/ack "$FORM&by=Ann+Lee&action=Door+closed%2C+stock+checked" >/dev/null
states="$(check)"
expect_state "$states" fridge-02 acknowledged "fridge-02 acknowledged after its escalation"

pub sensors/fridge-02/door '0'
pub sensors/fridge-02/temperature '3.9'
resolved() { [ "$(mail_count 'subject:"Resolved: Dairy walk-in"')" = 1 ] || { check >/dev/null; false; }; }
eventually resolved && pass "back in range: resolved email" || fail "no resolved email"
[ "$(mail_count 'to:manager@example.com subject:"Resolved: Dairy walk-in"')" = 1 ] \
    && pass "the resolution reaches whoever was escalated to" || fail "the manager wasn't told of the resolution"
expect_state "$(check)" fridge-02 ok "fridge-02 is OK again"

echo "Checking the excursion log in InfluxDB"
events="$(compose exec -T influxdb influx query --raw \
    "from(bucket: \"$INFLUXDB_BUCKET\") |> range(start: -1h)
     |> filter(fn: (r) => r._measurement == \"coldchain_event\" and r.device == \"fridge-02\" and r._field == \"message\")
     |> group() |> keep(columns: [\"type\"])")"
for type in start alert escalate ack resolve; do
    [ "$(grep -c ",$type\s*\$" <<<"$events")" = 1 ] && pass "one $type event for fridge-02" \
        || { echo "$events"; fail "expected one $type event for fridge-02"; }
done
compose exec -T influxdb influx query --raw \
    "from(bucket: \"$INFLUXDB_BUCKET\") |> range(start: -1h)
     |> filter(fn: (r) => r._measurement == \"coldchain_event\" and r.type == \"ack\" and r._field == \"action\")" \
    | grep -q 'Door closed, stock checked' && pass "the corrective action is stored" || fail "corrective action missing"

echo "Checking the daily record"
report="$(n8n_get 'webhook/coldchain/report?date=today')"
python3 - "$report" <<'EOF' && pass "the report summarizes every unit" || fail "unexpected report: $report"
import json, sys
r = json.loads(sys.argv[1])
units = {u["id"]: u for u in r["units"]}
assert units["fridge-01"]["status"] == "OK", units["fridge-01"]
fridge = units["fridge-02"]
assert fridge["status"] == "EXCURSION, ACTIONED", fridge
assert fridge["minutes_out_of_range"] >= 1, fridge
[ex] = fridge["excursions"]
assert ex["escalated"] and ex["acknowledged_by"] == "Ann Lee" and ex["end"], ex
assert units["smoke-silent"]["excursions"][0]["corrective_action"] == "Sensor cable reseated"
EOF
DATE="$(python3 -c 'import json, sys; print(json.loads(sys.argv[1])["date"])' "$report")"
grep -q '^fridge-02,Dairy walk-in,' "../ai/data/reports/coldchain-$DATE.csv" \
    && pass "hourly log saved to ai/data/reports/" || fail "coldchain-$DATE.csv is missing or empty"
grep -q 'Ann Lee' "../ai/data/reports/coldchain-$DATE.html" \
    && pass "HTML record saved with the acknowledgement" || fail "coldchain-$DATE.html is missing the ack"
mail_text "to:manager@example.com subject:\"Daily record $DATE\"" \
    | python3 -c 'import json, sys; m = json.load(sys.stdin); assert [a["FileName"] for a in m["Attachments"]] == ["coldchain-'"$DATE"'.csv"], m["Attachments"]' \
    && pass "the record is emailed with the CSV attached" || fail "no report email with the CSV"

echo "Checking a restart"
compose_ai restart n8n >/dev/null
eventually n8n_get healthz/readiness && pass "n8n restarts" || fail "n8n did not come back"
compose_ai logs --no-color n8n | grep -c 'p4n4: importing workflows' | grep -qx 1 \
    && pass "workflows are not imported again on restart" || fail "workflows were re-imported"
eventually check && pass "the monitor still runs after a restart" || fail "the monitor stopped after a restart"

echo "Checking the iot layer"
LP=data/archive/lineprotocol/telemetry.lp
RAW=data/archive/raw/mqtt.jsonl
eventually grep -q 'sensor_data,device=fridge-02,sensor=temperature unit="C",value=9.4' "$LP" \
    && pass "line-protocol archive has the readings" || fail "$LP is missing the readings"
grep -q '"topic":"sensors/fridge-02/door"' "$RAW" \
    && pass "raw archive has the door messages" || fail "$RAW is missing the door messages"

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
