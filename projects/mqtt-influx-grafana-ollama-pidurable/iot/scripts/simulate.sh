#!/bin/sh
# ==============================================================================
# Demo pumps with a scripted bearing fault
# ==============================================================================
# Runs in the eclipse-mosquitto image (busybox sh and awk, mosquitto_pub).
# Every SIMULATOR_INTERVAL seconds each pump publishes
#   sensors/<pump>/temperature   {"value": 41.3, "unit": "C"}
#   sensors/<pump>/vibration     {"value": 2.1, "unit": "mm/s"}
#   inference/<pump>/result      what ei-runner publishes: anomaly_score, label, ...
# SIMULATOR_FAULT_AFTER seconds after start, SIMULATOR_FAULT_DEVICE's bearing
# starts to fail for SIMULATOR_FAULT_DURATION seconds (and again every
# SIMULATOR_FAULT_EVERY): vibration climbs, the bearing runs hot and the
# anomaly score crosses Node-RED's threshold, which raises the alert the agent
# investigates.
# ==============================================================================
set -u

HOST="${MQTT_HOST:-mqtt}"
DEVICES="${SIMULATOR_DEVICES:-pump-1 pump-2}"
INTERVAL="${SIMULATOR_INTERVAL:-5}"
FAULTY="${SIMULATOR_FAULT_DEVICE:-pump-2}"
AFTER="${SIMULATOR_FAULT_AFTER:-90}"
DURATION="${SIMULATOR_FAULT_DURATION:-60}"
EVERY="${SIMULATOR_FAULT_EVERY:-1800}"

pub() { mosquitto_pub -h "$HOST" -q 1 -t "$1" -m "$2" || echo "simulator: publish to $1 failed" >&2; }

start=$(date +%s)
echo "simulator: pumps $DEVICES every ${INTERVAL}s; $FAULTY's bearing fails after ${AFTER}s"
while :; do
    now=$(date +%s)
    elapsed=$((now - start))
    ts=$(date -u +%Y-%m-%dT%H:%M:%SZ)
    n=0
    for device in $DEVICES; do
        n=$((n + 1))
        # How far into a fault this pump is, 0 to 1; -1 when healthy
        progress=-1
        if [ "$device" = "$FAULTY" ] && [ "$elapsed" -ge "$AFTER" ]; then
            into=$(((elapsed - AFTER) % EVERY))
            [ "$into" -lt "$DURATION" ] && progress=$(awk -v i="$into" -v d="$DURATION" 'BEGIN { print i / d }')
        fi
        # temperature vibration anomaly_score confidence label
        set -- $(awk -v seed="$((now * 7 + n))" -v p="$progress" -v n="$n" 'BEGIN {
            srand(seed)
            temp = 40 + 2 * n + rand() - 0.5
            vib = 2 + 0.4 * rand()
            score = 0.12 + 0.12 * rand()
            label = "running"
            if (p >= 0) {
                # The bearing wears in over the first third of the fault
                w = p < 0.33 ? p / 0.33 : 1
                temp += 6 * w
                vib += 7 * w
                score = 0.55 + 0.4 * w + 0.03 * rand()
                if (score > 0.99) score = 0.99
                label = w > 0.5 ? "anomaly" : "vibration"
            }
            printf "%.1f %.2f %.3f %.3f %s\n", temp, vib, score, 0.8 + 0.15 * rand(), label
        }')
        pub "sensors/$device/temperature" "{\"value\": $1, \"unit\": \"C\"}"
        pub "sensors/$device/vibration" "{\"value\": $2, \"unit\": \"mm/s\"}"
        pub "inference/$device/result" "{\"device\": \"$device\", \"timestamp\": \"$ts\", \"label\": \"$5\", \"confidence\": $4, \"anomaly_score\": $3, \"latency_ms\": 12.5, \"mode\": \"simulated\"}"
    done
    sleep "$INTERVAL"
done
