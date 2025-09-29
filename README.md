# BTC_JPY Execution Bot

FastAPI + pybotters based execution lane that consumes TradingView webhook alerts and executes BTC/JPY leveraged market orders on GMOコイン. The bot enforces a single-position policy (`ENTRY_POLICY=ignore`) and persists execution state in SQLite for idempotent processing.

## Project layout

```
project-root/
├─ docker-compose.yml
├─ README.md
├─ exec-lane/
│  ├─ app.py
│  ├─ gmo_client.py
│  ├─ notify.py
│  ├─ schemas.py
│  ├─ storage.py
│  ├─ utils.py
│  ├─ requirements.txt
│  └─ Dockerfile
├─ config/
│  └─ .env.example
├─ logs/
│  └─ .gitkeep
└─ runtime/
   └─ .gitkeep
```

All runtime artefacts (log files, SQLite DB) are stored in bind-mounted `logs/` and `runtime/` directories.

## Prerequisites

- Docker Engine 24+
- docker compose plugin 2+
- TradingView Essential plan (webhook alerts)
- GMOコイン レバレッジ API key pair

## Initial setup

1. Clone this repository on the target host.
2. Copy the sample environment file and edit the values.

   ```bash
   cp config/.env.example config/.env
   dos2unix config/.env
   vi config/.env
   ```

   | Key | Required | Description |
   |-----|----------|-------------|
   | `WEBHOOK_TOKEN` | ✅ | Shared secret checked on every webhook call |
   | `GMO_API_KEY` / `GMO_API_SECRET` | ✅ | GMOコイン REST/WS credentials (レバレッジ口座) |
   | `DISCORD_WEBHOOK_URL` | Optional | Discord notifications target |
   | `MAX_SKEW_SECONDS` | Optional | TradingView timestamp tolerance (default `60`) |
   | `EVENT_TTL_SECONDS` | Optional | Idempotency window in seconds (default `600`) |
   | `QTY_STEP` | Optional | Minimum quantity step for BTC/JPY (default `0.01`) |
   | `ALLOWED_SOURCE_IPS` | Optional | Comma-separated whitelist. Leave empty to disable |
   | `ENV` | Optional | Environment label exposed via `/status` |

3. Build and launch the execution lane.

   ```bash
   docker compose up -d --build
   ```

   The container runs as non-root (`uid=1000`) on top of `python:3.11-slim`, installs pinned dependencies from `exec-lane/requirements.txt`, and executes `uvicorn` on port `8080`.

## Smoke test

Wait until the container becomes healthy (`docker compose ps`). Then execute the health and status checks:

```bash
curl -iS http://127.0.0.1:8080/healthz
curl -iS http://127.0.0.1:8080/status
```

To simulate TradingView alerts:

```bash
curl -iS http://127.0.0.1:8080/webhook \
  -H 'Content-Type: application/json' \
  --data-binary '{
    "token":"SECRETxxyyzz",
    "event_id":"dev-ENTRY-001",
    "ts":"2025-09-24T00:00:00Z",
    "mode":"ENTRY",
    "symbol":"BTC_JPY",
    "side":"BUY",
    "size":0.04
  }'

curl -iS http://127.0.0.1:8080/webhook \
  -H 'Content-Type: application/json' \
  --data-binary '{
    "token":"SECRETxxyyzz",
    "event_id":"dev-CLOSE-001",
    "ts":"2025-09-24T00:01:00Z",
    "mode":"CLOSE",
    "symbol":"BTC_JPY"
  }'
```

Duplicate `event_id` values within the configured TTL (`EVENT_TTL_SECONDS`, default 600s) return `{"duplicate":true}` and do not trigger additional orders.

## Runtime behaviour

- **Single position policy** – Entries are ignored while any BTC/JPY position remains open. The API returns HTTP 200 with `{"ignored": true}`.
- **Timestamp enforcement** – Webhooks older/newer than `MAX_SKEW_SECONDS` result in HTTP 422.
- **Idempotency** – Processed events are recorded in SQLite (`runtime/exec_lane.sqlite3`). Duplicate event IDs within the TTL are rejected.
- **Retry** – REST calls to GMO (429/5xx/timeout) are retried with exponential backoff (0.5 → 1.0 → 2.0 seconds).
- **Notifications** – Success/failure messages are pushed to Discord if `DISCORD_WEBHOOK_URL` is configured.
- **WebSocket tracking** – The bot subscribes to `positionSummaryEvents` / `executionEvents` to keep `/status` in sync with live fills.

## Observability

- JSON logs are written to STDOUT (ingest via `docker logs`) and to `/logs` if you bind mount a file.
- `/healthz` exposes a simple ready check (`{"status":"ok"}`).
- `/status` returns the current position snapshot, last processed event, retry counters and WebSocket connectivity. Integrate the endpoint with Uptime Kuma or similar services.

Example `/status` response:

```json
{
  "position_qty": 0.04,
  "position_side": "BUY",
  "last_event_id": "dev-ENTRY-001",
  "last_event_ts": "2025-09-24T00:00:00+00:00",
  "retry_stats": {"entry": 0, "close": 0, "rest": 1},
  "ws_connected": true,
  "env": "production"
}
```

## Troubleshooting

| Symptom | Cause | Action |
|---------|-------|--------|
| HTTP 401 `invalid token` | `WEBHOOK_TOKEN` mismatch | Update TradingView alert JSON token or `.env` |
| HTTP 422 `stale timestamp` | Alert timestamp drift exceeds `MAX_SKEW_SECONDS` | Ensure TradingView clock is synced; increase skew if required |
| GMO error `message_code=5010` | Insufficient margin / quantity | Reduce order size or add collateral |
| GMO error `message_code=5011` | Position not found / already closed | Confirm current open positions before issuing CLOSE |
| HTTP 502 `gmo api error` | Upstream GMO REST failure | Inspect logs for `message_code`/`message_string`, retry later |

## Maintenance tips

- Rotate the Discord webhook URL regularly; the bot masks secrets in logs but does not manage rotation.
- Keep the system clock in sync (chrony or systemd-timesyncd). The container uses `TZ=Asia/Tokyo`.
- Backup the SQLite database (`runtime/exec_lane.sqlite3`) if you need persistent audit trails.

## Development

Local development without Docker is possible:

```bash
cd exec-lane
python3.11 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
uvicorn app:app --host 0.0.0.0 --port 8080 --reload
```

Ensure `WEBHOOK_TOKEN`, `GMO_API_KEY`, and `GMO_API_SECRET` are exported in your shell before running the server. When testing locally without GMO connectivity, stub the credentials or mock pybotters to avoid unwanted orders.
