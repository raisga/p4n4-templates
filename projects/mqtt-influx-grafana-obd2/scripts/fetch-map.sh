#!/usr/bin/env bash
# ==============================================================================
# Offline map: downloads one area's map for the tiles service (extras layer),
# which serves it to p4n4-dashboard's Map tab on MAP_PORT (8088). Writes:
#
#   region.pmtiles   vector tiles of the area: an extract of a Protomaps
#                    basemap build (OpenStreetMap data)
#   style.json       a MapLibre style for them, from @protomaps/basemaps; its
#                    URLs start with __ORIGIN__, which tiles fills in
#   fonts/           glyphs: Noto Sans Regular, Medium and Italic, OFL.txt
#   sprites/v4/      icons: a sprite sheet per flavor
#   ATTRIBUTION.txt  where it all comes from, and the licences
#
# Needs bash, curl and Docker: go-pmtiles and Node run in pinned containers.
# Works on Linux, Termux and Git Bash on Windows.
# ==============================================================================
set -euo pipefail

PMTILES_IMAGE=protomaps/go-pmtiles:v1.31.2
NODE_IMAGE=node:24.22.2-alpine
BASEMAPS=@protomaps/basemaps@5.7.2
# github.com/protomaps/basemaps-assets, pinned to a commit
ASSETS_SHA=028c18f713baecad011301ff7a69acc39bcc2ae7
BUILDS_URL=https://build-metadata.protomaps.dev/builds.json

here=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
template=$(dirname "$here")

usage() {
    cat <<EOF
Usage: scripts/fetch-map.sh --bbox MINLON,MINLAT,MAXLON,MAXLAT [options]

  --bbox      the area, in degrees (required)
  --maxzoom   deepest zoom level, 0-15 (default 15: street level)
  --build     Protomaps basemap build, YYYYMMDD (default: the latest)
  --flavor    light, dark, white, grayscale or black (default light)
  --lang      label language, e.g. en, fr, de (default en)
  --out       where to write (default extras/data/map)

Example, Monaco (about 13 MB: 2 MB of tiles, 11 MB of fonts whatever the area):
  scripts/fetch-map.sh --bbox 7.4091,43.7247,7.4398,43.7519
EOF
}

die() { echo "fetch-map: $*" >&2; exit 1; }

bbox=
maxzoom=15
build=
flavor=light
lang=en
out="$template/extras/data/map"

while [ $# -gt 0 ]; do
    case "$1" in
        -h|--help) usage; exit 0 ;;
        --*=*) set -- "${1%%=*}" "${1#*=}" "${@:2}"; continue ;;
    esac
    [ $# -ge 2 ] || die "$1 needs a value (see --help)"
    case "$1" in
        --bbox) bbox=$2 ;;
        --maxzoom) maxzoom=$2 ;;
        --build) build=$2 ;;
        --flavor) flavor=$2 ;;
        --lang) lang=$2 ;;
        --out) out=$2 ;;
        *) die "unknown option $1 (see --help)" ;;
    esac
    shift 2
done

number='-?[0-9]+(\.[0-9]+)?'
[ -n "$bbox" ] || { usage >&2; exit 2; }
[[ $bbox =~ ^$number,$number,$number,$number$ ]] || die "--bbox is MINLON,MINLAT,MAXLON,MAXLAT in degrees"
IFS=, read -r minlon minlat maxlon maxlat <<<"$bbox"
awk -v w="$minlon" -v s="$minlat" -v e="$maxlon" -v n="$maxlat" \
    'BEGIN { exit !(-180 <= w && w < e && e <= 180 && -90 <= s && s < n && n <= 90) }' \
    || die "--bbox $bbox: west must be below east, and south below north"
[[ $maxzoom =~ ^([0-9]|1[0-5])$ ]] || die "--maxzoom is 0 to 15"
[[ -z $build || $build =~ ^[0-9]{8}$ ]] || die "--build is a date, YYYYMMDD"
case "$flavor" in
    light|dark|white|grayscale|black) ;;
    *) die "--flavor is light, dark, white, grayscale or black" ;;
esac
[[ $lang =~ ^[A-Za-z]{2,3}(-[A-Za-z]+)?$ ]] || die "--lang is a language code, e.g. en"
command -v curl >/dev/null || die "needs curl"
command -v docker >/dev/null || die "needs Docker"

mkdir -p "$out"
out=$(cd "$out" && pwd)
[ -w "$out" ] || die "can't write to $out"

# Docker paths: Git Bash would rewrite /data-style arguments into Windows
# paths, and -v needs a Windows path for the host side there. Elsewhere the
# containers run as you, so the files are yours
docker_run() { MSYS_NO_PATHCONV=1 docker run --rm "$@"; }
if command -v cygpath >/dev/null; then
    host_path() { cygpath -w "$1"; }
    as_user=()
else
    host_path() { printf '%s\n' "$1"; }
    as_user=(--user "$(id -u):$(id -g)")
fi

# Everything lands in a staging directory first, so a failed run leaves the
# last good map in place
stage="$out/.incoming"
rm -rf "$stage"
mkdir -p "$stage"
trap 'rm -rf "$stage"' EXIT

if [ -z "$build" ]; then
    build=$(curl -fsSL "$BUILDS_URL" | grep -o '"key": *"[0-9]\{8\}\.pmtiles"' | grep -o '[0-9]\{8\}' | sort | tail -n 1) \
        || die "couldn't read the list of builds ($BUILDS_URL)"
fi

echo "fetch-map: tiles of $bbox, zoom 0-$maxzoom, from Protomaps build $build"
docker_run "${as_user[@]}" -v "$(host_path "$stage"):/data" "$PMTILES_IMAGE" \
    extract "https://build.protomaps.com/$build.pmtiles" /data/region.pmtiles \
    --bbox="$bbox" --maxzoom="$maxzoom"

echo "fetch-map: fonts and sprites (protomaps/basemaps-assets@${ASSETS_SHA:0:7})"
curl -fL --progress-bar -o "$stage/assets.zip" \
    "https://github.com/protomaps/basemaps-assets/archive/$ASSETS_SHA.zip"

echo "fetch-map: style ($BASEMAPS, $flavor, $lang)"
docker_run -i "${as_user[@]}" -e HOME=/tmp -e npm_config_update_notifier=false \
    -e BASEMAPS="$BASEMAPS" -e FLAVOR="$flavor" -e LABELS="$lang" \
    -e ASSETS="basemaps-assets-$ASSETS_SHA" \
    -v "$(host_path "$stage"):/data" -w /tmp "$NODE_IMAGE" sh -eu <<'SCRIPT'
# (stdin is this script: keep the commands off it)
npx -y -p tsx -p "$BASEMAPS" generate_style /tmp/style.json \
    "pmtiles://__ORIGIN__/region.pmtiles" "$FLAVOR" "$LABELS" \
    "__ORIGIN__/sprites/v4/$FLAVOR" "__ORIGIN__/fonts/{fontstack}/{range}.pbf" </dev/null >/dev/null
# Minified, with \u escapes for anything outside ASCII
node -e '
    const fs = require("fs");
    const style = JSON.stringify(JSON.parse(fs.readFileSync("/tmp/style.json", "utf8")));
    const ascii = style.replace(/[\u007f-\uffff]/g, (c) => "\\u" + c.charCodeAt(0).toString(16).padStart(4, "0"));
    fs.writeFileSync("/data/style.json", ascii + "\n");
' </dev/null
cd /tmp
unzip -q /data/assets.zip "$ASSETS/fonts/OFL.txt" "$ASSETS/fonts/Noto Sans Regular/*" \
    "$ASSETS/fonts/Noto Sans Medium/*" "$ASSETS/fonts/Noto Sans Italic/*" "$ASSETS/sprites/v4/*" </dev/null
mv "$ASSETS/fonts" "$ASSETS/sprites" /data/
SCRIPT

rm -rf "$out/fonts" "$out/sprites"
mv -f "$stage/region.pmtiles" "$stage/style.json" "$out/"
mv "$stage/fonts" "$stage/sprites" "$out/"
rm -rf "$stage"

cat >"$out/ATTRIBUTION.txt" <<EOF
Offline map for p4n4, written by scripts/fetch-map.sh on $(date -u +%Y-%m-%d)

  region.pmtiles  bbox $bbox, zoom 0-$maxzoom, an extract of Protomaps basemap
                  build $build (https://build.protomaps.com/$build.pmtiles)
  style.json      $BASEMAPS, flavor $flavor, labels $lang
  fonts/          Noto Sans, from protomaps/basemaps-assets@$ASSETS_SHA
  sprites/v4/     from protomaps/basemaps-assets@$ASSETS_SHA

Licences
  Map data        (c) OpenStreetMap contributors, Open Database License 1.0:
                  https://www.openstreetmap.org/copyright. Credit it wherever
                  the map is shown (the style's attribution does). The tiles
                  also hold water polygons from osmdata.openstreetmap.de
                  (ODbL), Natural Earth (public domain) and, at low zooms,
                  landcover from ESA WorldCover (CC BY 4.0)
  Tiles and style Protomaps basemaps: code BSD-3-Clause, visual design CC0:
                  https://github.com/protomaps/basemaps
  Fonts           Noto Sans, SIL Open Font License 1.1 (fonts/OFL.txt)
  Sprites         protomaps/basemaps-assets, MIT (icons derived from
                  Mapzen's): https://github.com/protomaps/basemaps-assets
EOF

echo "fetch-map: wrote $out"
(cd "$out" && du -sh region.pmtiles style.json fonts sprites ATTRIBUTION.txt | sed 's/^/  /')
echo "  $(du -sh "$out" | cut -f1) in all. Serve it with the extras layer's tiles service:"
echo "  http://<host>:\${MAP_PORT:-8088}/style.json"
