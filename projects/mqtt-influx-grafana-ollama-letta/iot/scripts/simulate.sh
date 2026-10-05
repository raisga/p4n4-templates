#!/bin/sh
# ==============================================================================
# Demo machines: pump-1, pump-2 and fan-1, each with three sensors.
#
#   sensors/<machine>/vibration      {"value": 2.31, "unit": "mm/s"}   velocity RMS
#   sensors/<machine>/bearing_temp   {"value": 48.2, "unit": "C"}
#   sensors/<machine>/current        {"value": 19.6, "unit": "A"}
#
# pump-2's drive-end bearing is wearing: its vibration and bearing temperature
# have been creeping up for days, the pattern that preceded its last failure
# (which the assistant remembers). pump-1 and fan-1 are healthy.
#
# On first start the bucket is empty, so the script backfills
# SIMULATOR_BACKFILL_DAYS of history straight into InfluxDB (10-minute steps),
# and the trend is there from the first minute. The wear started 5 days into
# that history and is measured from the oldest pump-2 reading, so restarts
# carry on where they left off. Then it publishes live readings over MQTT
# every SIMULATOR_INTERVAL seconds. Runs in the eclipse-mosquitto image
# (busybox sh + awk + wget + mosquitto_pub).
# ==============================================================================
set -eu

MQTT_HOST="${MQTT_HOST:-mqtt}"
MQTT_PORT="${MQTT_PORT:-1883}"
SIMULATOR_INTERVAL="${SIMULATOR_INTERVAL:-10}"
SIMULATOR_BACKFILL_DAYS="${SIMULATOR_BACKFILL_DAYS:-14}"
INFLUX="${INFLUXDB_URL:-http://influxdb:8086}"
AUTH="Authorization: Token ${INFLUXDB_TOKEN}"
QUERY_URL="$INFLUX/api/v2/query?org=$INFLUXDB_ORG"
WRITE_URL="$INFLUX/api/v2/write?org=$INFLUXDB_ORG&bucket=$INFLUXDB_BUCKET&precision=s"

# Oldest pump-2 vibration reading (epoch seconds), or nothing
oldest() {
    wget -qO- --header "$AUTH" --header "Content-Type: application/json" --header "Accept: application/csv" \
        --post-data "{\"type\": \"flux\", \"dialect\": {\"header\": false, \"annotations\": []}, \"query\": \"from(bucket: \\\"$INFLUXDB_BUCKET\\\") |> range(start: -400d) |> filter(fn: (r) => r._measurement == \\\"sensor_data\\\" and r.device == \\\"pump-2\\\" and r.sensor == \\\"vibration\\\" and r._field == \\\"value\\\") |> first() |> map(fn: (r) => ({_value: uint(v: r._time) / uint(v: 1000000000)})) |> keep(columns: [\\\"_value\\\"])\"}" \
        "$QUERY_URL" | tr -d '\r' | awk -F, 'NF { print $NF; exit }'
}

# Readings of every machine from epoch second $1 up to (not including) $2,
# every $3 seconds, given the wear onset $4. Format $5: "lp" (InfluxDB line
# protocol, for the backfill) or "mqtt" ("<machine> <sensor> <value> <unit>")
readings() {
    awk -v from="$1" -v to="$2" -v step="$3" -v onset="$4" -v format="$5" \
        -v seed="$(od -An -N2 -tu2 /dev/urandom)" '
    function noise(a) { return (rand() - 0.5) * 2 * a }
    function out(m, sensor, value, unit) {
        if (format == "lp") printf "sensor_data,device=%s,sensor=%s unit=\"%s\",value=%.2f %d\n", m, sensor, unit, value, t
        else printf "%s %s %.2f %s\n", m, sensor, value, unit
    }
    function machine(m, base_v, base_temp, base_i, wear,   day) {
        day = sin(2 * 3.14159265 * (t % 86400) / 86400 - 1.2)     # load peaks mid-afternoon
        out(m, "vibration", base_v + 0.15 * day + wear + noise(0.12), "mm/s")
        out(m, "bearing_temp", base_temp + 2.5 * day + 3 * wear + noise(0.3), "C")
        out(m, "current", base_i * (1 + 0.08 * day) + 0.3 * wear + noise(0.2), "A")
    }
    BEGIN {
        srand(seed)
        for (t = from; t < to; t += step) {
            # Bearing wear on pump-2: nothing before onset, then an accelerating
            # rise (about +3.4 mm/s after 9 days), levelling off at +4.6
            d = (t - onset) / 86400
            wear = d <= 0 ? 0 : 3.4 * (exp(0.18 * d) - 1) / (exp(0.18 * 9) - 1)
            if (wear > 4.6) wear = 4.6
            machine("pump-1", 1.8, 46, 13.5, 0)
            machine("pump-2", 2.2, 49, 19.5, wear)
            machine("fan-1", 1.4, 38, 9.8, 0)
        }
    }'
}

now="$(date +%s)"
first="$(oldest || true)"
if [ -z "$first" ]; then
    first=$((now - SIMULATOR_BACKFILL_DAYS * 86400))
    echo "simulator: backfilling ${SIMULATOR_BACKFILL_DAYS} days of history into InfluxDB"
    readings "$first" "$now" 600 $((first + 5 * 86400)) lp >/tmp/backfill.lp
    wget -qO- --header "$AUTH" --header "Content-Type: text/plain; charset=utf-8" \
        --post-file /tmp/backfill.lp "$WRITE_URL" && echo "simulator: wrote $(wc -l </tmp/backfill.lp) points"
    rm -f /tmp/backfill.lp
fi
onset=$((first + 5 * 86400))
echo "simulator: pump-2 bearing wear since $(date -u -d "@$onset" '+%Y-%m-%d %H:%M' 2>/dev/null || echo "$onset") UTC;" \
     "publishing to ${MQTT_HOST}:${MQTT_PORT} every ${SIMULATOR_INTERVAL}s"

while true; do
    t="$(date +%s)"
    readings "$t" $((t + 1)) 1 "$onset" mqtt | while read -r machine sensor value unit; do
        mosquitto_pub -h "$MQTT_HOST" -p "$MQTT_PORT" -q 1 \
            -t "sensors/$machine/$sensor" -m "{\"value\": $value, \"unit\": \"$unit\"}"
    done
    sleep "$SIMULATOR_INTERVAL"
done
