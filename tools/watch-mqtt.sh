#!/bin/bash

# Simple MQTT Alert Watcher - just login and print payloads

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

mosquitto_sub \
    --cafile "$SCRIPT_DIR/certs/ca.crt" \
    --cert "$SCRIPT_DIR/certs/client.crt" \
    --key "$SCRIPT_DIR/certs/client.key" \
    -h alert.d-t.pw \
    -p 42690 \
    -t alerts/input \
    -v