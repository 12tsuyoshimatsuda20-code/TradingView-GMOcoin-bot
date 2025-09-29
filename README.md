# exec-lane — TradingView → GMOコイン（レバレッジ）執行ボット

## 目的と要件
- TradingView の Webhook JSON を FastAPI で受信し、pybotters を利用して GMOコイン（レバレッジ）の成行注文を発注します。
- ENTRY / CLOSE の単一ポジション運用のみをサポートします。
- 署名や WS トークンの管理は pybotters に一任し、httpx や独自署名は使用しません。
- 依存関係は `requirements.txt` にピン留めされています（pydantic v1.10.x 固定）。
- Docker 環境は `python:3.11-slim` ベース、非 root 実行、`curl` ベースの healthcheck を備えます。
- `docker-compose.yml` は Compose v2 互換のため version キーを省略しています。
- `.env` ファイルは LF 改行必須です。Windows からコピーする場合は `dos2unix` 等で LF へ変換してください（CRLF や BOM が含まれると API キー検証や設定読込に失敗します）。

## セットアップ手順
1. 必要なファイルを取得後、環境変数ファイルを用意します。
   ```bash
   cp config/.env.example config/.env
   # Windows の場合は必ず LF 改行へ変換 (例: `wsl dos2unix config/.env`)
   ```
2. `.env` に GMO API キー（長さ: KEY=32, SECRET=64）と Discord Webhook URL 等を設定します。`WS_ENABLED=1`（0 で WS 無効化）と `DRY_RUN=0`（1 で発注スキップ）も用途に応じて変更してください。
3. Docker イメージをビルドし、コンテナをバックグラウンド起動します。
   ```bash
   docker compose build
   docker compose up -d
   ```
4. 起動後、ヘルスチェックを確認します。
   ```bash
   curl http://127.0.0.1:8080/healthz
   ```

## スモークテスト
テスト前に `.env` のトークンが `SECRETxxyyzz` であることを確認してください。

```bash
# ENTRY
curl -iS http://127.0.0.1:8080/webhook -H "Content-Type: application/json" --data-binary '{
  "token":"SECRETxxyyzz","event_id":"dev-ENTRY-001","ts":"2025-09-24T00:00:00Z",
  "mode":"ENTRY","symbol":"BTC_JPY","side":"BUY","size":0.04
}'

# CLOSE
curl -iS http://127.0.0.1:8080/webhook -H "Content-Type: application/json" --data-binary '{
  "token":"SECRETxxyyzz","event_id":"dev-CLOSE-001","ts":"2025-09-24T00:01:00Z",
  "mode":"CLOSE","symbol":"BTC_JPY"
}'
```

## 運用メモ
- 公開する際は Cloudflared 等で `/webhook` のみ外部公開し、`/healthz` と `/status` は Uptime Kuma などで監視してください。
- Discord 通知は成功・失敗に関わらず主要な約定情報と GMO メッセージコードを送信します。通知失敗は警告ログに留め、取引処理は継続されます。
- `WS_ENABLED=0` にすると pybotters の Private WS 購読を停止して切断を止血できます。`DRY_RUN=1` では注文 API を呼ばず検証モードとして動作します（レスポンスは成功扱い）。

## トラブルシュート（よくある罠）
- `.env` の改行が CRLF だったり BOM が混入していると環境変数が正しく読み込めません。必ず LF のみで保存してください。
- GMO API KEY/SECRET の長さが正しいか（KEY:32 桁、SECRET:64 桁）を確認してください。
- TradingView の Webhook JSON に式や `{{plot("x")}}` を入れず、最終値だけを送ってください。
- TradingView から送る `ts` は `YYYY-MM-DDTHH:MM:SSZ` 形式（UTC 秒精度）で、サーバーとの差分が `MAX_SKEW_SECONDS` を超えると 400 で拒否されます。
- `httpx` を使ったり `data=` で POST すると署名が一致せずエラーになります。必ず pybotters の `json=` 引数を利用してください。

