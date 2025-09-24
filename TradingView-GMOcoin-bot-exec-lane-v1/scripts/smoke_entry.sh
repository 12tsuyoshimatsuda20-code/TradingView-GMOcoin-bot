#!/usr/bin/env bash
set -euo pipefail
BOT_URL="${BOT_URL:-http://127.0.0.1:8080}"
TOKEN="${TOKEN:-SECRETxxyyzz}"
now_utc=$(date -u +"%Y-%m-%dT%H:%M:%SZ")
cat > /tmp/payload-entry.json <<JSON
{
  "token":"$TOKEN",
  "event_id":"dev-ENTRY-$(date +%s)",
  "ts":"$now_utc",
  "mode":"ENTRY",
  "symbol":"BTC_JPY",
  "side":"BUY",
  "size":0.04
}
JSON
curl -iS -H "Content-Type: application/json" --data-binary @/tmp/payload-entry.json "$BOT_URL/webhook"
