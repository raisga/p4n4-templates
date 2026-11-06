#!/bin/sh
# Device logins are opt-in; container clients keep the private anonymous listener.
set -eu
AUTH_DIR=/mosquitto/auth
CONFIG_DIR=/p4n4
umask 077
mkdir -p "$AUTH_DIR"
rm -f "$AUTH_DIR/passwd" "$AUTH_DIR/acl"

fail() { echo "p4n4 mqtt: $*" >&2; exit 1; }
user="${MQTT_USER:-}"
password="${MQTT_PASSWORD:-}"
reader="${MQTT_READONLY_USER:-}"
reader_password="${MQTT_READONLY_PASSWORD:-}"
if [ -n "$reader$reader_password" ]; then
    [ -n "$reader" ] && [ -n "$reader_password" ] && [ -n "$password" ] \
        || fail "read-only credentials need each other and MQTT_PASSWORD"
    [ "$reader" != "$user" ] || fail "the read-only account needs a different username"
    case "$reader" in *[[:space:]:]*) fail "MQTT_READONLY_USER may not contain whitespace or ':'" ;; esac
fi
if [ -n "$password" ]; then
    [ -n "$user" ] || fail "MQTT_PASSWORD needs MQTT_USER"
    case "$user" in *[[:space:]:]*) fail "MQTT_USER may not contain whitespace or ':'" ;; esac
    if [ -f "$CONFIG_DIR/passwd" ]; then
        cp "$CONFIG_DIR/passwd" "$AUTH_DIR/passwd"
    else
        : >"$AUTH_DIR/passwd"
    fi
    mosquitto_passwd -b "$AUTH_DIR/passwd" "$user" "$password"
    if [ -n "$reader_password" ]; then
        mosquitto_passwd -b "$AUTH_DIR/passwd" "$reader" "$reader_password"
    fi
    if [ -f "$CONFIG_DIR/acl" ] || [ -n "$reader_password" ]; then
        if [ -n "$reader_password" ] && [ -f "$CONFIG_DIR/acl" ]; then
            awk '$1 == "pattern" && $2 != "read" && $2 != "deny" { exit 1 }' "$CONFIG_DIR/acl" \
                || fail "read-only accounts need per-user writes in acl, not global write patterns"
        fi
        {
            if [ -f "$CONFIG_DIR/acl" ]; then
                cat "$CONFIG_DIR/acl"
            else
                while IFS=: read -r account _; do
                    [ "$account" = "$reader" ] && continue
                    printf 'user %s\ntopic readwrite #\ntopic read $SYS/#\n' "$account"
                done <"$AUTH_DIR/passwd"
            fi
            printf '\nuser %s\ntopic readwrite #\ntopic read $SYS/#\n' "$user"
            if [ -n "$reader_password" ]; then
                printf '\nuser %s\ntopic read #\ntopic read $SYS/#\n' "$reader"
            fi
        } >"$AUTH_DIR/acl"
    fi
else
    echo "p4n4 mqtt: device listeners accept anonymous clients (MQTT_PASSWORD is empty)"
fi
device_auth() {
    if [ -n "$password" ]; then
        echo "allow_anonymous false"
        echo "password_file $AUTH_DIR/passwd"
        if [ -f "$AUTH_DIR/acl" ]; then echo "acl_file $AUTH_DIR/acl"; fi
    else
        echo "allow_anonymous true"
    fi
}
{
    echo "max_packet_size 4194304"
    echo "per_listener_settings true"
    printf 'listener 1883\nprotocol mqtt\nallow_anonymous true\n'
    printf 'listener 1884\nprotocol mqtt\n'
    device_auth
    printf 'listener 9001\nprotocol websockets\n'
    device_auth
} >"$AUTH_DIR/listeners.conf"
if [ "$(id -u)" = 0 ]; then chown -R mosquitto:mosquitto "$AUTH_DIR"; fi
exec "$@"
