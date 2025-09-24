#!/usr/bin/env bash
set -euo pipefail
BOT_URL="${BOT_URL:-http://127.0.0.1:8080}"
TOKEN="${TOKEN:-SECRETxxyyzz}"
now_utc=$(date -u +"%Y-%m-%dT%H:%M:%SZ")
cat > /tmp/payload-close.json <<JSON
{
  "token":"$TOKEN",
  "event_id":"dev-CLOSE-$(date +%s)",
  "ts":"$now_utc",
  "mode":"CLOSE",
  "symbol":"BTC_JPY"
}
JSON
curl -iS -H "Content-Type: application/json" --data-binary @/tmp/payload-close.json "$BOT_URL/webhook"
