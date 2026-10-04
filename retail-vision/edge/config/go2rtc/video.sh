#!/bin/sh
# ==============================================================================
# Writes go2rtc's config from the environment, then runs go2rtc. Two streams,
# which .p4n4.json lists as the dashboard's cameras:
#
#   floor    CAMERA_URL: the store camera (rtsp://user:pass@ip:554/stream, an
#            http:// MJPEG stream, …), transcoded to MJPEG for the Video tab
#            when it isn't MJPEG already.
#   overlay  OVERLAY_URL: the edge device's annotated view, as MJPEG (e.g. the
#            Jetson's live mode, http://<device>:8777/mjpeg).
#
# Without its URL, a stream loops edge/clips/<name>.mp4 (floor.mp4,
# overlay.mp4) at real speed, e.g. recorded footage for a demo; without that,
# a synthetic test pattern (the overlay's with a moving tracked-person box).
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
BOX="color=c=0x22C55E:s=220x420:r=10,drawbox=x=6:y=6:w=208:h=408:color=black:t=fill,colorkey=black:0.05:0"
TRACK="[0][1]overlay=x='300+250*sin(t/3)':y=180,drawtext=text=P001:x='300+250*sin(t/3)':y=146:fontsize=28:fontcolor=white:box=1:boxcolor=0x22C55E:boxborderw=6"
DEMO_OVERLAY="exec:ffmpeg -hide_banner -loglevel error $PATTERN -f lavfi -i $BOX -filter_complex $TRACK -c:v mjpeg -q:v 7 -f mjpeg -"

CLIPS=/p4n4/clips

# A single-quoted YAML scalar: quotes are doubled, nothing else is special
quote() { printf "'%s'" "$(printf '%s' "$1" | sed "s/'/''/g")"; }

# A clip from $CLIPS, looped forever at its own pace, as MJPEG
loop() { echo "exec:ffmpeg -hide_banner -loglevel error -re -stream_loop -1 -i $CLIPS/$1.mp4 -an -c:v mjpeg -q:v 5 -f mjpeg -"; }

{
  echo "streams:"
  echo "  floor:"
  if [ -n "${CAMERA_URL:-}" ]; then
    echo "    - $(quote "$CAMERA_URL")"
    # Used only when the camera's own stream isn't MJPEG (e.g. H.264 over RTSP)
    echo "    - 'ffmpeg:floor#video=mjpeg'"
  elif [ -f "$CLIPS/floor.mp4" ]; then
    echo "    - $(quote "$(loop floor)")"
  else
    echo "    - $(quote "$DEMO_FLOOR")"
  fi
  echo "  overlay:"
  if [ -n "${OVERLAY_URL:-}" ]; then
    echo "    - $(quote "$OVERLAY_URL")"
  elif [ -f "$CLIPS/overlay.mp4" ]; then
    echo "    - $(quote "$(loop overlay)")"
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
echo "video: floor from $(origin CAMERA_URL "${CAMERA_URL:-}" floor), overlay from $(origin OVERLAY_URL "${OVERLAY_URL:-}" overlay)"
exec go2rtc -config "$CONFIG"
