# TradingView-GMOcoin-bot-exec-lane-v1

最小構成の TradingView → GMOコイン（レバレッジ）自動売買ボットです。TradingView の Webhook から受信したシグナルに基づき、GMO コインに対して成行 ENTRY / 成行 CLOSE（全量クローズ）を発注します。FastAPI + pybotters による非同期実装で、Docker Compose で即時起動できます。

## 機能概要

- `POST /webhook` に TradingView からの JSON を受信し、GMO コインへ成行注文を発注
- 建玉が存在する状態での新規 ENTRY は `ENTRY_POLICY=ignore` により無視（通知なし）
- `event_id` を SQLite で短期保存し、600 秒間の冪等性を担保
- WebSocket (`positionSummaryEvents`, `executionEvents`) で建玉と約定を監視
- Discord Webhook へ ENTRY / CLOSE 成功と失敗を通知
- `/healthz` `/status` によるヘルスチェック・稼働状況把握
- Docker（python:3.11-slim）ベース、uvloop（Linux）の利用

## ディレクトリ構成

```
TradingView-GMOcoin-bot-exec-lane-v1/
├─ docker-compose.yml
├─ README.md
├─ .gitignore
├─ config/
│  └─ .env.example
├─ exec-lane/
│  ├─ Dockerfile
│  ├─ requirements.txt
│  ├─ app.py
│  ├─ notify.py
│  ├─ storage.py
│  └─ gmo.py
└─ kuma-data/
   └─ .gitkeep
```

## セットアップ手順

1. **環境変数ファイルを作成**

   `config/.env.example` をコピーし、API キーなどを設定します。

   ```bash
   cp config/.env.example config/.env
   vi config/.env
   ```

2. **Docker Compose を起動**

   ```bash
   docker compose up -d
   ```

   `exec-lane` コンテナが起動し、`http://127.0.0.1:8080/healthz` が `{"status":"ok"}` を返せば準備完了です。

## TradingView アラート設定

TradingView のアラート本文には以下の JSON をそのまま貼り付けてください。`token` と `symbol` は `.env` の設定と一致させます。

### ENTRY

```json
{
  "token": "SECRETxxyyzz",
  "event_id": "{{alert_id}}",
  "ts": "{{timenow}}",
  "mode": "ENTRY",
  "symbol": "BTC_JPY",
  "side": "BUY",
  "size": 0.04
}
```

### CLOSE

```json
{
  "token": "SECRETxxyyzz",
  "event_id": "{{alert_id}}",
  "ts": "{{timenow}}",
  "mode": "CLOSE",
  "symbol": "BTC_JPY"
}
```

## エンドポイント

| メソッド | パス       | 説明 |
|----------|------------|------|
| GET      | `/healthz` | アプリケーションおよび SQLite の軽量チェック |
| GET      | `/status`  | 建玉数量、最終イベント、WS ステータス、再試行統計を JSON で返却 |
| POST     | `/webhook` | TradingView からの Webhook 受付。ENTRY / CLOSE を実行 |

## スモークテスト

`.env` の `WEBHOOK_TOKEN` に合わせて、以下の `curl` コマンドで疎通確認ができます。時刻 `ts` は ±60 秒以内の値に差し替えてください。

### ENTRY リクエスト

```bash
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
```

### CLOSE リクエスト

```bash
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

## 運用チェックリスト

- **時刻同期**: `chrony` などでサーバ時刻を NTP 同期（`ts` ±60 秒チェックを通すため）
- **公開設定**: Cloudflared / Caddy などで `/webhook` を HTTPS 公開（本アプリは HTTP バインド）
- **監視**: `/healthz` を Uptime Kuma 等で監視。必要なら `kuma-data/` をマウント
- **GMO API 制限**: 429/5xx を監視し、レートリミットを踏んでいないか確認

## トラブルシュート

| 症状 | ログ/通知 | 対応策 |
|------|-----------|--------|
| `timestamp skew too large` | `/webhook` 400 | サーバ時刻と TradingView 時刻を確認。NTP 再同期 |
| `duplicate event` | `/webhook` 応答 `{"status":"duplicate"}` | `event_id` が 600 秒以内に重複。TradingView の alert_id をユニークにする |
| Discord に ERROR 通知 | `code=ERR-5010` 等 | GMO API エラーコードを確認。API キー/シークレット、ポジション上限、証拠金をチェック |
| `close failed` | Discord ERROR | ポジション数量超過。`market_close_all` が自動縮小するが、ポジション照会と数量を確認 |
| WebSocket 切断 | `/status` の `ws_connected=false` | ネットワーク状況と API 接続を確認。再接続は自動実行 |

## 主要設定項目

`.env` で指定する主要項目:

- `WEBHOOK_TOKEN`: TradingView からの共有シークレット
- `GMO_API_KEY` / `GMO_API_SECRET`: GMO コイン API 認証情報
- `ALLOWED_SYMBOLS`: 許可するシンボル（カンマ区切り）。標準は `BTC_JPY`
- `MAX_SKEW_SECONDS`: `ts` の許容ずれ秒数（標準 60 秒）
- `QTY_STEP` / `MIN_QTY`: 発注数量の丸め・最小数量
- `NOTIFY_DISCORD_WEBHOOK_URL`: Discord 通知先

## 起動手順まとめ

1. `config/.env` を用意
2. `docker compose up -d`
3. `curl http://127.0.0.1:8080/healthz`

## スモークテスト再掲

ENTRY:

```bash
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
```

CLOSE:

```bash
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
