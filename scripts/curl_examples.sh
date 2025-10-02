#!/usr/bin/env bash
# Usage: WEBHOOK_TOKEN=changeme ./scripts/curl_examples.sh
set -euo pipefail

BASE_URL="http://localhost:8000"
TOKEN="${WEBHOOK_TOKEN:-changeme}"

curl -s "${BASE_URL}/healthz"

curl -s -X POST "${BASE_URL}/webhook/tv?token=${TOKEN}" \
  -H "Content-Type: application/json" \
  -d '{"type":"ENTRY","symbol":"BTC_JPY","side":"BUY","size":"0.01","id":"smoke-entry","ts":1893456000000}'

curl -s -X POST "${BASE_URL}/webhook/tv?token=${TOKEN}" \
  -H "Content-Type: application/json" \
  -d '{"type":"CLOSE","symbol":"BTC_JPY","side":"SELL","id":"smoke-close","ts":1893456000000}'
