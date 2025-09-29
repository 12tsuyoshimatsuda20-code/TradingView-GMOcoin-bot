# TradingView GMOcoin Bot

FastAPI-based webhook receiver that relays TradingView alerts to GMO Coin via `pybotters`. The bot enforces single-position trading for `BTC_JPY`, quantises order sizes, and notifies Discord about execution outcomes.

## Features

- `POST /webhook` authenticates TradingView alerts via a shared token, idempotency cache, and timestamp skew validation.
- `ENTRY` alerts submit market orders when no position is open; existing positions are ignored per policy.
- `CLOSE` alerts flatten any open position with market orders and confirm fills by polling open positions.
- Retry logic for transient GMO API failures (HTTP 429/5xx) with exponential backoff.
- Optional Discord webhook notifications for success and error events.
- `GET /healthz` and `GET /status` endpoints for health checks and runtime diagnostics.

## Configuration

Environment variables:

| Variable | Description |
| --- | --- |
| `WEBHOOK_TOKEN` | Shared secret used to authenticate TradingView alerts. |
| `GMO_API_KEY` / `GMO_API_SECRET` | GMO Coin API credentials (32 / 64 characters). |
| `ALLOWED_SYMBOLS` | Comma-separated list of allowed symbols (defaults to `BTC_JPY`). |
| `ENTRY_POLICY` | Must be `ignore`. Existing positions suppress new entries. |
| `MAX_SKEW_SECONDS` | Maximum allowed skew between alert timestamp and server (default `60`). |
| `QTY_STEP` | Minimum quantity step (default `0.01`). |
| `DISCORD_WEBHOOK_URL` | Optional Discord webhook for notifications. |
| `ENV` | Environment label for logging (default `dev`). |
| `IDEMPOTENCY_TTL_SECONDS` | Lifetime for cached event IDs (default `600`). |
| `STATUS_CACHE_SIZE` | Number of recent events kept for `/status` (default `100`). |

## Running locally

```bash
pip install -r requirements.txt
uvicorn app.app:app --host 0.0.0.0 --port 8000
```

## Testing webhook

```bash
curl -iS http://localhost:8000/webhook \
  -H "Content-Type: application/json" \
  --data-binary '{
    "token":"SECRETxxyyzz",
    "event_id":"dev-ENTRY-001",
    "ts":"2025-09-24T00:00:00Z",
    "mode":"ENTRY",
    "symbol":"BTC_JPY",
    "side":"BUY",
    "size":0.04
  }'
```
