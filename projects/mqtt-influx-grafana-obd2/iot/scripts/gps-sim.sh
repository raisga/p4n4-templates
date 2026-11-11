#!/bin/sh
# ==============================================================================
# Demo GPS: a car lapping a closed route, publishing a fix every GPS_INTERVAL
# seconds on sensors/<GPS_DEVICE_ID>/gps, the way p4n4-dashboard publishes
# the phone's:
#
#   {"lat": 43.735027, "lon": 7.421265, "alt": 4.0, "speed": 37.5,
#    "heading": 2.0, "acc": 4.2}
#
# lat/lon in degrees, alt in metres, speed in km/h, heading in degrees
# clockwise from north, acc the horizontal accuracy in metres. The car slows
# down for corners and speeds up after them, up to GPS_MAX_SPEED km/h, and
# each fix is a metre or two off, like a phone's.
#
# GPS_ROUTE replaces the built-in route: "lon,lat[,alt] lon,lat[,alt] ...", a
# closed loop. The built-in one laps Monaco's streets the way the Grand Prix
# circuit runs (3.3 km), traced from OpenStreetMap (© OpenStreetMap
# contributors, ODbL); its altitudes are estimates.
# Runs in the eclipse-mosquitto image (busybox sh + awk + mosquitto_pub).
# ==============================================================================
set -eu

MQTT_HOST="${MQTT_HOST:-mqtt}"
MQTT_PORT="${MQTT_PORT:-1883}"
GPS_DEVICE_ID="${GPS_DEVICE_ID:-car}"
GPS_INTERVAL="${GPS_INTERVAL:-1}"
GPS_MAX_SPEED="${GPS_MAX_SPEED:-50}"

# From the start line on Boulevard Albert 1er: Sainte Dévote, up Beau Rivage to
# Massenet and Casino, Mirabeau, the Grand Hotel hairpin, Portier, the tunnel,
# the chicane, Tabac, Piscine, Rascasse and Anthony Noghès
MONACO="
    7.42127,43.73503,4 7.42123,43.73600,5 7.42156,43.73696,6 7.42291,43.73712,13 7.42420,43.73738,20 7.42494,43.73745,25
    7.42592,43.73777,32 7.42708,43.73794,37 7.42741,43.73805,38 7.42777,43.73830,41 7.42788,43.73854,42 7.42768,43.73903,43
    7.42719,43.73940,44 7.42719,43.73954,43 7.42730,43.73971,42 7.42872,43.74110,32 7.42892,43.74109,31 7.42906,43.74101,30
    7.42915,43.74068,28 7.42959,43.74021,24 7.42971,43.74024,23 7.42974,43.74033,22 7.42933,43.74070,18 7.42932,43.74086,17
    7.43009,43.74108,12 7.43034,43.74105,10 7.43029,43.74008,9 7.43005,43.73938,8 7.42946,43.73864,7 7.42870,43.73810,6
    7.42731,43.73753,4 7.42652,43.73736,3 7.42548,43.73723,3 7.42539,43.73701,3 7.42518,43.73700,3 7.42496,43.73707,3
    7.42229,43.73679,2 7.42211,43.73666,2 7.42185,43.73622,2 7.42177,43.73573,2 7.42183,43.73545,2 7.42221,43.73515,2
    7.42247,43.73413,3 7.42244,43.73404,3 7.42221,43.73391,3 7.42222,43.73380,3 7.42261,43.73315,3 7.42294,43.73288,3
    7.42345,43.73268,3 7.42350,43.73257,3 7.42343,43.73248,3 7.42277,43.73236,3 7.42256,43.73237,3 7.42243,43.73245,3
    7.42234,43.73270,3 7.42181,43.73330,3 7.42143,43.73427,4
"
ROUTE="$(printf '%s' "${GPS_ROUTE:-$MONACO}" | tr -s '\t\r\n' '   ')"
TOPIC="sensors/${GPS_DEVICE_ID}/gps"

# fixes check: validates the route and prints its length. fixes run: prints a
# JSON fix per line, one every GPS_INTERVAL seconds, forever
fixes() {
    awk -v mode="$1" -v route="$ROUTE" -v dt="$GPS_INTERVAL" -v top="$GPS_MAX_SPEED" \
        -v seed="$(od -An -N2 -tu2 /dev/urandom)" '
    function wrap(a) { while (a < 0) a += 360; while (a >= 360) a -= 360; return a }
    function bend(a) { a = wrap(a); return a > 180 ? 360 - a : a }
    function along(a, b) { return b >= a ? b - a : b - a + total }
    function noise(r) { return (2 * rand() - 1) * r }
    function die(msg) { print "gps-sim: " msg > "/dev/stderr"; exit 1 }

    # The highest speed (m/s) at s metres into the lap that still makes every
    # corner: braking ahead of it, accelerating out of it
    function limit(s,    v, i, w) {
        v = vmax
        for (i = 1; i <= m; i++) {
            if (vc[i] >= vmax) continue
            w = sqrt(vc[i] * vc[i] + 2 * BRAKE * along(s, at[i])); if (w < v) v = w
            w = sqrt(vc[i] * vc[i] + 2 * ACCEL * along(at[i], s)); if (w < v) v = w
        }
        return v
    }

    BEGIN {
        srand(seed)
        pi = atan2(0, -1)
        ACCEL = 1.5; BRAKE = 2.0      # m/s², an unhurried driver
        if (dt + 0 <= 0) die("GPS_INTERVAL must be a number of seconds above 0")
        if (top + 0 <= 0) die("GPS_MAX_SPEED must be a speed in km/h above 0")
        vmax = top / 3.6
        vmin = vmax < 15 / 3.6 ? vmax : 15 / 3.6

        n = split(route, pts, " ")
        r = 0
        for (i = 1; i <= n; i++) {
            if (pts[i] == "") continue
            k = split(pts[i], c, ",")
            if (k < 2 || k > 3 || c[1] !~ /^-?[0-9]+(\.[0-9]+)?$/ || c[2] !~ /^-?[0-9]+(\.[0-9]+)?$/ \
                || (k == 3 && c[3] !~ /^-?[0-9]+(\.[0-9]+)?$/))
                die("GPS_ROUTE: \"" pts[i] "\" is not lon,lat or lon,lat,alt")
            r++; rlon[r] = c[1] + 0; rlat[r] = c[2] + 0; ralt[r] = k == 3 ? c[3] + 0 : 0
        }
        if (r < 3) die("GPS_ROUTE needs at least 3 points")

        # Metres east and north of the first point, dropping repeated points (a
        # loop that ends where it starts, too)
        ky = 6371008.8 * pi / 180
        kx = ky * cos(rlat[1] * pi / 180)
        m = 0
        for (i = 1; i <= r; i++) {
            px = (rlon[i] - rlon[1]) * kx; py = (rlat[i] - rlat[1]) * ky
            if (m > 0 && (px - x[m]) ^ 2 + (py - y[m]) ^ 2 < 1) continue
            m++; x[m] = px; y[m] = py; lon[m] = rlon[i]; lat[m] = rlat[i]; alt[m] = ralt[i]
        }
        if (m > 1 && x[m] ^ 2 + y[m] ^ 2 < 1) m--
        if (m < 3) die("GPS_ROUTE needs at least 3 different points")

        # Segment i runs from point i to the next one (the last back to the first)
        total = 0
        for (i = 1; i <= m; i++) {
            j = i % m + 1
            seglen[i] = sqrt((x[j] - x[i]) ^ 2 + (y[j] - y[i]) ^ 2)
            heading[i] = wrap(atan2(x[j] - x[i], y[j] - y[i]) * 180 / pi)
            at[i] = total
            total += seglen[i]
        }
        if (total < 100) die("GPS_ROUTE is under 100 m long")

        # Corner speed at each point: the more the road turns within 25 m of
        # it, the slower, down to 15 km/h for a hairpin
        for (i = 1; i <= m; i++) turn[i] = bend(heading[i] - heading[i == 1 ? m : i - 1])
        for (i = 1; i <= m; i++) {
            sum = 0
            for (j = 1; j <= m; j++) {
                d = along(at[i], at[j]); if (d > total - d) d = total - d
                if (d <= 25) sum += turn[j]
            }
            f = sum / 120; if (f > 1) f = 1
            vc[i] = vmax - (vmax - vmin) * f
        }

        if (mode == "check") {
            printf "gps-sim: a %.1f km lap of %d points, at up to %s km/h\n", total / 1000, m, top
            exit 0
        }

        s = 0; v = 0; seg = 1
        while (1) {
            while (seg < m && at[seg + 1] <= s) seg++
            j = seg % m + 1
            t = (s - at[seg]) / seglen[seg]
            la = lat[seg] + (lat[j] - lat[seg]) * t + noise(1.5) / ky
            lo = lon[seg] + (lon[j] - lon[seg]) * t + noise(1.5) / kx
            al = alt[seg] + (alt[j] - alt[seg]) * t + noise(1)
            kmh = v * 3.6 + noise(0.4); if (kmh < 0) kmh = 0
            h = int(wrap(heading[seg] + noise(1.5)) * 10 + 0.5) / 10; if (h >= 360) h = 0
            printf "{\"lat\": %.6f, \"lon\": %.6f, \"alt\": %.1f, \"speed\": %.1f, \"heading\": %.1f, \"acc\": %.1f}\n", \
                la, lo, al, kmh, h, 3 + 3 * rand()
            fflush()
            system("sleep " dt)

            # Speed up (or brake) towards the limit, then move on
            w = v + ACCEL * dt; lim = limit(s); if (w > lim) w = lim
            s += (v + w) / 2 * dt; v = w
            if (s >= total) { s -= total; seg = 1 }
        }
    }'
}

fixes check
echo "gps-sim: publishing to ${MQTT_HOST}:${MQTT_PORT} on ${TOPIC} every ${GPS_INTERVAL}s"
# mosquitto_pub -l sends each line as a message, on one connection
while true; do
    fixes run | mosquitto_pub -h "$MQTT_HOST" -p "$MQTT_PORT" -q 0 -t "$TOPIC" -l \
        || echo "gps-sim: lost the broker; reconnecting"
    sleep 2
done
