# TradingView GMOcoin Bot

FastAPI-based webhook relay that accepts TradingView alerts and executes leveraged market orders on GMOコイン via [`pybotters`](https://github.com/ilmsg/pybotters). The service is production-ready for single-position operation with Discord notifications and Docker Compose deployment.

## Repository Layout

```
.
├── config/
│   └── .env.example        # Environment variable template
├── docker-compose.yml      # Compose stack (FastAPI + pybotters worker)
├── exec-lane/
│   ├── app.py              # FastAPI entry point
│   ├── gmo_client.py       # GMO Coin REST client (pybotters based)
│   ├── notify.py           # Discord notifier helper
│   ├── orders.py           # Order orchestration / position policy
│   ├── requirements.txt    # Locked Python dependencies
│   └── Dockerfile          # Application container definition
├── logs/                   # Log output (mounted volume)
│   └── .gitkeep
└── README.md
```

## Prerequisites

- Docker Engine 20.10+ and Docker Compose v2
- GMOコイン レバレッジ口座 API キー/シークレット
- TradingView Essential (webhook alerts)
- Discord Webhook URL (optional but recommended)

## Setup

```bash
git clone <REPO_URL>
cd TradingView-GMOcoin-bot
cp config/.env.example config/.env
# Edit config/.env to set WEBHOOK_TOKEN / GMO_API_* / DISCORD_WEBHOOK etc.
docker compose up -d --build
```

The FastAPI application listens on `http://127.0.0.1:8080`. When exposing externally (e.g. via Cloudflared), publish the `/webhook` endpoint over HTTPS and keep the application itself on plain HTTP.

### Environment Variables (`config/.env`)

| Key | Description |
| --- | --- |
| `WEBHOOK_TOKEN` | Shared secret that TradingView must send in each payload. |
| `GMO_API_KEY`, `GMO_API_SECRET` | GMOコイン API credentials. |
| `DISCORD_WEBHOOK` | Discord webhook URL (leave empty to disable notifications). |
| `LOG_LEVEL` | Log level for Loguru (default: `INFO`). |
| `MAX_SKEW_SECONDS` | Maximum allowed timestamp skew between payload and server (default: `60`). |
| `QTY_STEP` | Order quantity step (default: `0.01`). |
| `SYMBOL` | Trading pair (fixed: `BTC_JPY`). |
| `EVENT_ID_TTL_SECONDS` | TTL for idempotency cache (default: `900`). |

## Health Check

```bash
curl -iS http://127.0.0.1:8080/healthz
```

Expected response:

```
HTTP/1.1 200 OK
...
{"status":"ok"}
```

## Local Webhook Test (ENTRY → CLOSE)

```bash
TOKEN="$(grep -E '^WEBHOOK_TOKEN=' config/.env | cut -d= -f2-)"
EID="$(date -u +%Y%m%dT%H%M%SZ)-$RANDOM"
curl -sS -D - -H "Content-Type: application/json" --data-binary @- \
  http://127.0.0.1:8080/webhook <<JSON
{"token":"$TOKEN","event_id":"$EID","ts":"$(date -u +%Y-%m-%dT%H:%M:%SZ)","symbol":"BTC_JPY","size":0.04,"mode":"ENTRY","side":"BUY","entry_price_hint":16660000,"tp1_price":16662000}
JSON

EID="$(date -u +%Y%m%dT%H%M%SZ)-$RANDOM"
curl -sS -D - -H "Content-Type: application/json" --data-binary @- \
  http://127.0.0.1:8080/webhook <<JSON
{"token":"$TOKEN","event_id":"$EID","ts":"$(date -u +%Y-%m-%dT%H:%M:%SZ)","symbol":"BTC_JPY","size":0.04,"mode":"CLOSE"}
JSON
```

- ENTRY requests are executed only when the account is flat. If a position already exists, the webhook returns HTTP 200 with `{"ignored": true}` without notifying Discord.
- CLOSE requests always attempt to flatten the position; if already flat, Discord still receives a success notification noting the skip.

## TradingView Alert Payload Example

Ensure TradingView emits **pure JSON** (double quotes, no `{{...}}` placeholders left unresolved). Use Pine Script to compute numbers before interpolation.

```json
{"token":"<WEBHOOK_TOKEN>","event_id":"{{alert_id}}","ts":"{{timenow}}","symbol":"BTC_JPY","size":0.04,"mode":"ENTRY","side":"BUY","entry_price_hint":{{close}},"tp1_price":{{close}}+7000}
```

Because TradingView cannot evaluate expressions safely inside JSON, prefer generating the final numeric strings in Pine (`timenow`, `close`, TP calculations) before substituting them.

## Operational Notes

- **Idempotency:** `event_id` values are cached for `EVENT_ID_TTL_SECONDS` to prevent duplicate execution. Re-sending the same `event_id` returns `{"duplicate": true}`.
- **Timestamp Validation:** Payload `ts` must be within `MAX_SKEW_SECONDS` of server time, or the request is rejected and a Discord warning is emitted.
- **Quantity Enforcement:** Sizes must be positive multiples of `QTY_STEP`. Violations return HTTP 400 and trigger a Discord warning.
- **Notifications:** All successful ENTRY/CLOSE executions and all failures post to Discord. ENTRY ignores (position already open) do not notify per specification.
- **Logging:** Loguru writes to stdout and rotates files under `logs/app.log` (mounted via Docker volume).
- **GMO Client:** REST calls are signed and executed exclusively through `pybotters.Client(...).request(..., json=payload)` with exponential backoff retries on API errors.

## Cloudflared Publishing (Example)

Expose the local service securely using a Cloudflared tunnel (replace `my-tunnel` with your identifier):

```bash
cloudflared tunnel --url http://127.0.0.1:8080 --hostname my-tunnel.example.com
```

Then point TradingView webhooks to `https://my-tunnel.example.com/webhook`.

## Development

For local development without Docker, install dependencies and run Uvicorn:

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r exec-lane/requirements.txt
WEBHOOK_TOKEN=dev-token GMO_API_KEY=xxx GMO_API_SECRET=yyy uvicorn app:app --reload --host 0.0.0.0 --port 8080 --app-dir exec-lane
```

## License

MIT
