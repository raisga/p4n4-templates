#!/bin/sh
# ==============================================================================
# Demo publisher: sends temperature and humidity readings for each device in
# SIMULATOR_DEVICES every SIMULATOR_INTERVAL seconds. Temperature uses the JSON
# object format; humidity uses the bare-number format.
# Runs in the eclipse-mosquitto image (busybox sh + awk + mosquitto_pub).
# ==============================================================================
set -eu

MQTT_HOST="${MQTT_HOST:-mqtt}"
MQTT_PORT="${MQTT_PORT:-1883}"
SIMULATOR_DEVICES="${SIMULATOR_DEVICES:-dev-01 dev-02}"
SIMULATOR_INTERVAL="${SIMULATOR_INTERVAL:-5}"

echo "simulator: publishing to ${MQTT_HOST}:${MQTT_PORT} every ${SIMULATOR_INTERVAL}s for: ${SIMULATOR_DEVICES}"

# Prints "<temperature> <humidity>": a slow daily-ish wave plus noise, offset per device
reading() {
    awk -v t="$(date +%s)" -v n="$1" -v seed="$(od -An -N2 -tu2 /dev/urandom)" 'BEGIN {
        srand(seed)
        phase = t / 600 + n
        printf "%.2f %.1f\n", 21 + n + 3 * sin(phase) + rand() - 0.5, 50 + 10 * cos(phase) + 2 * rand() - 1
    }'
}

while true; do
    n=0
    for device in $SIMULATOR_DEVICES; do
        set -- $(reading "$n")
        mosquitto_pub -h "$MQTT_HOST" -p "$MQTT_PORT" -q 1 \
            -t "sensors/${device}/temperature" -m "{\"value\": $1, \"unit\": \"C\"}"
        mosquitto_pub -h "$MQTT_HOST" -p "$MQTT_PORT" -q 1 \
            -t "sensors/${device}/humidity" -m "$2"
        n=$((n + 1))
    done
    sleep "$SIMULATOR_INTERVAL"
done
