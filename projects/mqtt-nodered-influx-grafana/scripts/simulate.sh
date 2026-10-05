#!/bin/sh
# ==============================================================================
# Demo greenhouse: one simulated zone per name in SIMULATOR_DEVICES, every
# SIMULATOR_INTERVAL seconds. Each zone obeys the commands Node-RED sends, so
# the loop is closed: the fan cools the zone, the valve wets the soil.
#
#   publishes   sensors/<zone>/temperature     {"value": 27.4, "unit": "C"}
#               sensors/<zone>/humidity        bare number (%)
#               sensors/<zone>/soil_moisture   {"value": 38.2, "unit": "%"}
#               sensors/<zone>/fan, …/valve    1 or 0: the actuator's actual state
#   obeys       actuators/<zone>/fan/set, actuators/<zone>/valve/set
#
# The sun follows a compressed day of SIMULATOR_DAY seconds, so the fan cycles
# around noon and stays off at night. Runs in the eclipse-mosquitto image
# (busybox sh + awk + mosquitto_pub/sub).
# ==============================================================================
set -eu

MQTT_HOST="${MQTT_HOST:-mqtt}"
MQTT_PORT="${MQTT_PORT:-1883}"
SIMULATOR_DEVICES="${SIMULATOR_DEVICES:-zone-a zone-b}"
SIMULATOR_INTERVAL="${SIMULATOR_INTERVAL:-5}"
SIMULATOR_DAY="${SIMULATOR_DAY:-900}"
STATE=/tmp/greenhouse
mkdir -p "$STATE"

echo "simulator: zones ${SIMULATOR_DEVICES} on ${MQTT_HOST}:${MQTT_PORT}, every ${SIMULATOR_INTERVAL}s, a day every ${SIMULATOR_DAY}s"

# Commands → $STATE/<zone>.<actuator> holding 1 or 0. Retained commands arrive
# on subscribe, so a restarted simulator picks up the current states.
mosquitto_sub -h "$MQTT_HOST" -p "$MQTT_PORT" -q 1 -v -t 'actuators/+/+/set' | while read -r topic payload; do
    zone="$(echo "$topic" | cut -d/ -f2)"
    actuator="$(echo "$topic" | cut -d/ -f3)"
    case "$payload" in
        *'"state":"on"'* | *'"state": "on"'*) echo 1 >"$STATE/$zone.$actuator" ;;
        *'"state":"off"'* | *'"state": "off"'*) echo 0 >"$STATE/$zone.$actuator" ;;
    esac
    echo "simulator: $zone $actuator <- $payload"
done &

# Current state of actuator $2 in zone $1 (off until told otherwise)
actuator() { cat "$STATE/$1.$2" 2>/dev/null || echo 0; }

# Advances zone $1 (index $2) by one step. Reads and writes
# $STATE/<zone>.climate ("<temperature> <soil moisture>") and prints
# "<temperature> <humidity> <soil moisture>".
step() {
    awk -v t="$(date +%s)" -v day="$SIMULATOR_DAY" -v n="$2" \
        -v fan="$(actuator "$1" fan)" -v valve="$(actuator "$1" valve)" \
        -v seed="$(od -An -N2 -tu2 /dev/urandom)" -v file="$STATE/$1.climate" 'BEGIN {
        srand(seed)
        temp = 20; soil = 40 - 4 * n
        if ((getline line < file) > 0) { split(line, s, " "); temp = s[1]; soil = s[2] }
        close(file)

        sun = sin(2 * 3.14159265 * t / day)
        outside = 18 + 6 * sun
        # Without the fan, the sun heats the house well above outside;
        # with it, the house is pulled back towards the outside air
        target = outside + (fan == 1 ? 1 : 10) * (sun > 0 ? sun : 0) + n
        temp += (target - temp) * 0.15 + (rand() - 0.5) * 0.3
        # Plants drink faster when it is hot; the valve refills quickly
        soil -= 0.25 + 0.04 * (temp > 20 ? temp - 20 : 0) + 0.1 * n
        if (valve == 1) soil += 3
        if (soil < 5) soil = 5
        if (soil > 80) soil = 80
        humidity = 70 - 1.5 * (temp - 20) + (valve == 1 ? 8 : 0) - (fan == 1 ? 5 : 0) + 2 * rand() - 1
        if (humidity < 20) humidity = 20
        if (humidity > 98) humidity = 98

        printf "%.2f %.2f\n", temp, soil > file
        printf "%.2f %.1f %.1f\n", temp, humidity, soil
    }'
}

pub() { mosquitto_pub -h "$MQTT_HOST" -p "$MQTT_PORT" -q 1 -t "$1" -m "$2"; }

while true; do
    n=0
    for zone in $SIMULATOR_DEVICES; do
        set -- $(step "$zone" "$n")
        pub "sensors/$zone/temperature" "{\"value\": $1, \"unit\": \"C\"}"
        pub "sensors/$zone/humidity" "$2"
        pub "sensors/$zone/soil_moisture" "{\"value\": $3, \"unit\": \"%\"}"
        pub "sensors/$zone/fan" "$(actuator "$zone" fan)"
        pub "sensors/$zone/valve" "$(actuator "$zone" valve)"
        n=$((n + 1))
    done
    sleep "$SIMULATOR_INTERVAL"
done
