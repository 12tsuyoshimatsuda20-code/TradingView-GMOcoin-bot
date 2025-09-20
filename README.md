# EXEC Lane (GMOレバレッジ成行実行サービス)

## 概要
- Webhookで受けた ENTRY/CLOSE を **成行**で実行（GMOレバレッジ）
- 冪等性(event_id)、検証、冷却、Kill-switch
- Discord通知（成功=緑 / 検証NG=黄 / 失敗=赤+任意@role）
- /healthz, /status（p95 SLO超過で HTTP 529）
- Uptime Kuma 同居（任意）

## セットアップ
```bash
cp config/.env.example config/.env
# TOKEN / GMO_API_* / DISCORD_WEBHOOK を必ず設定
docker compose build --no-cache exec-lane
docker compose up -d exec-lane

疎通
curl -sS http://127.0.0.1:8080/healthz
curl -sS http://127.0.0.1:8080/status | jq .

ダミー送信（curl）
# ENTRY BUY
curl -sS -X POST "http://127.0.0.1:8080/webhook" \
 -H "Content-Type: application/json" \
 -d '{
  "token": "replace-with-shared-secret",
  "event_id": "11111111-2222-3333-4444-555555555555",
  "ts": "2025-09-21T12:34:56Z",
  "symbol": "BTC_JPY",
  "size": 0.001,
  "mode": "ENTRY",
  "side": "BUY"
 }'

# CLOSE TP2 LONG（全量）
curl -sS -X POST "http://127.0.0.1:8080/webhook" \
 -H "Content-Type: application/json" \
 -d '{
  "token": "replace-with-shared-secret",
  "event_id": "aaaaaaa1-bbbb-4ccc-8ddd-eeeeeeeeeeee",
  "ts": "2025-09-21T12:35:10Z",
  "symbol": "BTC_JPY",
  "size": 0.001,
  "mode": "CLOSE",
  "reason": "TP2",
  "position_side": "LONG"
 }'

Uptime Kuma（任意）
docker compose up -d uptime-kuma
# http://<host>:3001 へアクセス
# 監視1: http://exec-lane:8080/healthz (200)
# 監視2: http://exec-lane:8080/status (Keyword: uptime_sec)

運用メモ

単一ポジ前提。想定外はDiscord赤で要手動CLOSE。

MAX_SIZEはENTRYの上限ガード。CLOSEは建玉全量を取得。

tsの許容は±60s。必要なら環境変数で閾値拡張してもよい。

/statusは LATENCY_P95_MAX_MS 超過で HTTP 529 を返す（KumaでDown判定）。


---

## 期待するCodexの出力
- 上記**ファイル/ディレクトリをそのまま生成**すること。
- 生成後、READMEのコマンドで**ビルド→起動→疎通**が通ること。
- **依存バージョンは固定**（requirements.txt記載のとおり）。
- すべてのファイルは**UTF-8**、改行LFで保存。

以上を実施してください。
（完了後、生成したファイルツリーと要変更箇所（API鍵/トークン）を短く再掲してください）
