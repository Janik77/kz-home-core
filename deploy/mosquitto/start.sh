#!/bin/sh
set -eu

# Never print input contents or enable shell tracing. Do not preserve host modes.
umask 077
for name in ca.crt server.crt server.key passwords acl; do
    if [ ! -f "/bootstrap/$name" ] || [ ! -s "/bootstrap/$name" ]; then
        echo "Missing or empty Mosquitto input: $name" >&2
        exit 1
    fi
    cat "/bootstrap/$name" > "/run/mosquitto/$name"
    chown mosquitto:mosquitto "/run/mosquitto/$name"
    chmod 0600 "/run/mosquitto/$name"
done
chown mosquitto:mosquitto /run/mosquitto /mosquitto/data
chmod 0700 /run/mosquitto

# Mosquitto's explicit 'user mosquitto' directive drops privileges. exec preserves
# signal delivery. TLS/authentication failures remain fatal; there is no fallback.
exec /usr/sbin/mosquitto -c /mosquitto/config/mosquitto.conf
