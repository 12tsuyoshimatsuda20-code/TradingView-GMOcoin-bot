# TradingView → GMO Coin FastAPI Bot

```
TradingView (Pine) --HTTPS(Webhook JSON)--> FastAPI(uvicorn) on VPS --pybotters--> GMO Coin REST/WS
                                          └--> Discord Webhook (notifications)
```

Minimal FastAPI + pybotters stack that consumes TradingView Essential webhooks and executes GMOコイン レバレッジ BTC_JPY market ENTRY/CLOSE orders. Designed for single-position operation with simple operational controls and Discord notifications.

## Features

- FastAPI behind Gunicorn/uvicorn workers (asyncio + uvloop)
- Idempotent webhook handling with SQLite cache (~10 minute window)
- GMO REST bridge via pybotters with retry/backoff and order fill confirmation
- Discord notifications for ENTRY/CLOSE success and failures
- Health/status endpoints for uptime checks (/healthz, /status)
- Docker Compose deployment with volume mounts for config and logs

## Getting Started

### 1. Prepare environment

```bash
cp config/.env.example config/.env
```

Edit `config/.env` and fill in the credentials/token values. Keys must be exact lengths (KEY=32, SECRET=64).

### 2. Build & run

```bash
docker compose build
docker compose up -d
curl -iS http://127.0.0.1:8080/healthz
```

### 3. Verify secrets length

```bash
docker compose exec -T exec-lane python - <<'PY'
import os
print(len(os.getenv("GMO_API_KEY", "")), len(os.getenv("GMO_API_SECRET", "")))
PY
```

If editing the `.env` on Windows, run `dos2unix config/.env` before deploying.

### Smoke tests

```bash
# ENTRY
curl -iS http://127.0.0.1:8080/webhook \
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

# CLOSE
curl -iS http://127.0.0.1:8080/webhook \
 -H "Content-Type: application/json" \
 --data-binary '{
   "token":"SECRETxxyyzz",
   "event_id":"dev-CLOSE-001",
   "ts":"2025-09-24T00:01:00Z",
   "mode":"CLOSE",
   "symbol":"BTC_JPY"
 }'
```

### TradingView alert body

Use the exact JSON payloads from the smoke test above. **Do not** embed Pine expressions—compute values in Pine and pass concrete numbers only.

### Cloudflared

Expose only the `/webhook` endpoint via your Cloudflared tunnel. Keep `/healthz` and `/status` restricted to internal networks/monitoring tools.

### Uptime monitoring

Configure Uptime Kuma (or similar) for:

- `POST /webhook` expecting HTTP 200
- `GET /status` expecting HTTP 200

## Repository layout

```
docker-compose.yml
Dockerfile
config/
  ├── .env.example
  └── gunicorn_conf.py
exec-lane/
  ├── app.py
  ├── gmo_client.py
  ├── notify.py
  ├── store.py
  ├── validators.py
  └── requirements.txt
logs/
  └── .gitkeep
```

## Operational notes

- ENTRY requests are ignored while a position is open (`ENTRY_POLICY=ignore`).
- Timestamp skew beyond 60 seconds is rejected.
- Duplicate `event_id` values within ~10 minutes are ignored (idempotency cache).
- CLOSE operations handle GMO ERR-200 by retrying once with the settable quantity.
- Structured JSON logs are written to stdout. Mount `/app/logs` for SQLite runtime data.
- Optional: provide `NOTIFY_DISCORD_WEBHOOK_URL` in `.env` for production notifications.

## Cloud deployment tips

- Ensure VPS clock sync (chrony/systemd-timesyncd) for correct request signing.
- Cloudflared tunnel command example:
  ```bash
  cloudflared tunnel run --url http://localhost:8080 --hostname YOURDOMAIN --metrics localhost:8081
  ```
- Only expose `/webhook` path through Cloudflare; keep `/healthz` and `/status` internal.

## TradingView Pine reminders

- Use `alert()` with the payloads shown above.
- Provide fixed numbers via `{{strategy.position_size}}` etc., but no expressions inside JSON.
- Always include `event_id` (e.g., `{{alert_id}}`) for idempotency and `{{timenow}}` for timestamp validation.

## Troubleshooting

- Check Docker logs: `docker compose logs -f exec-lane`
- Inspect SQLite idempotency cache: `docker compose exec exec-lane sqlite3 /app/logs/runtime.db 'select * from events;'`
- Discord failures are logged with HTTP status/response text.

## Licensing

MIT License (see `LICENSE`).
