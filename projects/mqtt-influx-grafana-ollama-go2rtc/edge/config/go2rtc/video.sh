#!/bin/sh
# ==============================================================================
# Writes go2rtc's config from the environment, then runs go2rtc. Two streams,
# which .p4n4.json lists as the dashboard's cameras:
#
#   road     CAMERA_URL: the road camera (rtsp://user:pass@ip:554/stream, an
#            http:// MJPEG stream, …), transcoded to MJPEG for the Video tab
#            when it isn't MJPEG already.
#   plates   OVERLAY_URL: the ALPR device's annotated view, as MJPEG, with the
#            vehicles it tracks and the plates it reads.
#
# Without its URL, a stream loops edge/clips/<name>.mp4 (road.mp4,
# plates.mp4) at real speed, e.g. recorded footage for a demo; without that,
# a synthetic test pattern (plates' with a moving vehicle box and a made-up plate).
#
# Every stream is served at http://<host>:1984/api/stream.mjpeg?src=<name>,
# and as a snapshot at /api/frame.jpeg?src=<name>. The config lives in /tmp,
# so the API can't rewrite it; go2rtc refuses exec: sources from its API.
# ==============================================================================
set -eu

CONFIG=/tmp/go2rtc.yaml
# The demo patterns: 10 fps 720p MJPEG straight from ffmpeg, no transcoding.
# go2rtc splits exec: commands on spaces, so the filters carry none.
PATTERN="-re -f lavfi -i testsrc2=size=1280x720:rate=10"
DEMO_FLOOR="exec:ffmpeg -hide_banner -loglevel error $PATTERN -c:v mjpeg -q:v 7 -f mjpeg -"
BOX="color=c=0xF59E0B:s=420x240:r=10,drawbox=x=6:y=6:w=408:h=228:color=black:t=fill,colorkey=black:0.05:0"
TRACK="[0][1]overlay=x='mod(t*160,1700)-420':y=300,drawtext=text=ABC-1234:x='mod(t*160,1700)-420':y=262:fontsize=28:fontcolor=black:box=1:boxcolor=0xF59E0B:boxborderw=6"
DEMO_OVERLAY="exec:ffmpeg -hide_banner -loglevel error $PATTERN -f lavfi -i $BOX -filter_complex $TRACK -c:v mjpeg -q:v 7 -f mjpeg -"

CLIPS=/p4n4/clips

# A single-quoted YAML scalar: quotes are doubled, nothing else is special
quote() { printf "'%s'" "$(printf '%s' "$1" | sed "s/'/''/g")"; }

# A clip from $CLIPS, looped forever at its own pace, as MJPEG
loop() { echo "exec:ffmpeg -hide_banner -loglevel error -re -stream_loop -1 -i $CLIPS/$1.mp4 -an -c:v mjpeg -q:v 5 -f mjpeg -"; }

{
  echo "streams:"
  echo "  road:"
  if [ -n "${CAMERA_URL:-}" ]; then
    echo "    - $(quote "$CAMERA_URL")"
    # Used only when the camera's own stream isn't MJPEG (e.g. H.264 over RTSP)
    echo "    - 'ffmpeg:road#video=mjpeg'"
  elif [ -f "$CLIPS/road.mp4" ]; then
    echo "    - $(quote "$(loop road)")"
  else
    echo "    - $(quote "$DEMO_FLOOR")"
  fi
  echo "  plates:"
  if [ -n "${OVERLAY_URL:-}" ]; then
    echo "    - $(quote "$OVERLAY_URL")"
  elif [ -f "$CLIPS/plates.mp4" ]; then
    echo "    - $(quote "$(loop plates)")"
  else
    echo "    - $(quote "$DEMO_OVERLAY")"
  fi
  echo "api:"
  echo "  listen: ':1984'"
  echo "rtsp:"
  echo "  listen: ':8554'"
} > "$CONFIG"

# Which sources, not their URLs: those can carry a camera's password
origin() {
  if [ -n "$2" ]; then echo "$1"; elif [ -f "$CLIPS/$3.mp4" ]; then echo "clips/$3.mp4"; else echo "the demo pattern"; fi
}
echo "video: road from $(origin CAMERA_URL "${CAMERA_URL:-}" road), plates from $(origin OVERLAY_URL "${OVERLAY_URL:-}" plates)"
exec go2rtc -config "$CONFIG"
