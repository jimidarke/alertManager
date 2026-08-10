#!/bin/bash
# Test alert normalization
#
# Credentials come from the environment -- this script used to hardcode
# "admin:password". Export before running:
#   export ALERT_USER=... ALERT_PASSWORD=...
set -u
: "${ALERT_USER:?set ALERT_USER}"
: "${ALERT_PASSWORD:?set ALERT_PASSWORD}"

echo "Testing alert normalization with 'error' level (should map to 'critical')"
curl -X POST http://localhost:8080/alert \
  -u "$ALERT_USER:$ALERT_PASSWORD" \
  -H "Content-Type: application/json" \
  -d '{
    "timestamp": '$(date +%s)',
    "level": "error",
    "category": "SYSTEM",
    "title": "Test Alert",
    "message": "Testing normalization",
    "source": "test_script",
    "client": " TEST-CLIENT "
  }'

echo -e "\n\nTesting with 'ERROR' and extra whitespace"
curl -X POST http://localhost:8080/alert \
  -u "$ALERT_USER:$ALERT_PASSWORD" \
  -H "Content-Type: application/json" \
  -d '{
    "timestamp": '$(date +%s)',
    "level": " ERROR ",
    "category": " Security ",
    "title": "Test Alert 2",
    "message": "Testing normalization with whitespace",
    "source": "test_script"
  }'

echo -e "\n\nTesting with 'warn' alias"
curl -X POST http://localhost:8080/alert \
  -u "$ALERT_USER:$ALERT_PASSWORD" \
  -H "Content-Type: application/json" \
  -d '{
    "timestamp": '$(date +%s)',
    "level": "warn",
    "category": "system",
    "title": "Test Warning",
    "message": "Testing warn alias",
    "source": "test_script"
  }'

echo -e "\n\nAll tests completed"
