#!/bin/sh
# ==============================================================================
# Demo cold rooms: one simulated unit per "<id>:<setpoint °C>" in
# SIMULATOR_UNITS, every SIMULATOR_INTERVAL seconds.
#
#   publishes   sensors/<unit>/temperature   {"value": 4.6, "unit": "C"}
#               sensors/<unit>/door          1 (open) or 0 (closed)
#
# Each unit holds its setpoint with a compressor cycle and noise; doors open
# now and then for a few seconds, which shows as a short spike (logged, not
# alerted). SIMULATOR_FAULT_UNIT has its door left open for
# SIMULATOR_FAULT_FOR seconds, SIMULATOR_FAULT_AFTER seconds after start and
# then every SIMULATOR_FAULT_EVERY seconds: a real excursion, long enough to
# alert and escalate with the default ALERT_DELAY and ESCALATE_AFTER.
# Runs in the eclipse-mosquitto image (busybox sh + awk + mosquitto_pub).
# ==============================================================================
set -eu

MQTT_HOST="${MQTT_HOST:-mqtt}"
MQTT_PORT="${MQTT_PORT:-1883}"
SIMULATOR_UNITS="${SIMULATOR_UNITS:-fridge-01:5 fridge-02:2.5 freezer-01:-20}"
SIMULATOR_INTERVAL="${SIMULATOR_INTERVAL:-5}"
SIMULATOR_FAULT_UNIT="${SIMULATOR_FAULT_UNIT:-fridge-02}"
SIMULATOR_FAULT_AFTER="${SIMULATOR_FAULT_AFTER:-60}"
SIMULATOR_FAULT_FOR="${SIMULATOR_FAULT_FOR:-3600}"
SIMULATOR_FAULT_EVERY="${SIMULATOR_FAULT_EVERY:-14400}"
STATE=/tmp/coldchain
mkdir -p "$STATE"
STARTED="$(date +%s)"

echo "simulator: units ${SIMULATOR_UNITS} on ${MQTT_HOST}:${MQTT_PORT}, every ${SIMULATOR_INTERVAL}s;" \
     "${SIMULATOR_FAULT_UNIT:-no unit}'s door is left open ${SIMULATOR_FAULT_AFTER}s after start for ${SIMULATOR_FAULT_FOR}s, every ${SIMULATOR_FAULT_EVERY}s"

# 1 while the fault unit's door is left open
fault() {
    [ "$1" = "$SIMULATOR_FAULT_UNIT" ] || { echo 0; return; }
    elapsed=$(($(date +%s) - STARTED - SIMULATOR_FAULT_AFTER))
    [ "$elapsed" -ge 0 ] && [ $((elapsed % SIMULATOR_FAULT_EVERY)) -lt "$SIMULATOR_FAULT_FOR" ] && echo 1 || echo 0
}

# Advances unit $1 (setpoint $2, index $3) one step. Reads and writes
# $STATE/<unit> ("<temperature> <door ticks left>") and prints
# "<temperature> <door>".
step() {
    awk -v t="$(date +%s)" -v set="$2" -v n="$3" -v fault="$(fault "$1")" \
        -v seed="$(od -An -N2 -tu2 /dev/urandom)" -v file="$STATE/$1" 'BEGIN {
        srand(seed)
        temp = set; open = 0
        if ((getline line < file) > 0) { split(line, s, " "); temp = s[1]; open = s[2] }
        close(file)

        # A door opens now and then (about every 10 minutes) for 1-3 steps
        if (open > 0) open--
        else if (rand() < 0.008) open = 1 + int(rand() * 3)
        door = (open > 0 || fault == 1) ? 1 : 0

        if (fault == 1) {
            # Door left open: the room warms towards the kitchen air
            temp += (22 - temp) * 0.012
        } else {
            # Compressor cycle around the setpoint; a door opening lets warm air in
            target = set + 0.8 * sin(2 * 3.14159265 * t / (1200 + 300 * n)) + (door ? 3 : 0)
            temp += (target - temp) * (door ? 0.3 : 0.08)
        }
        temp += (rand() - 0.5) * 0.15

        printf "%.2f %d\n", temp, open > file
        printf "%.2f %d\n", temp, door
    }'
}

pub() { mosquitto_pub -h "$MQTT_HOST" -p "$MQTT_PORT" -q 1 -t "$1" -m "$2"; }

while true; do
    n=0
    for spec in $SIMULATOR_UNITS; do
        unit="${spec%%:*}"
        set -- $(step "$unit" "${spec#*:}" "$n")
        pub "sensors/$unit/temperature" "{\"value\": $1, \"unit\": \"C\"}"
        pub "sensors/$unit/door" "$2"
        n=$((n + 1))
    done
    sleep "$SIMULATOR_INTERVAL"
done
