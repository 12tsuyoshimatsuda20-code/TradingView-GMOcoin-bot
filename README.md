# TradingView GMO Coin Bot

TradingView Webhookから受信したシグナルをGMOコインのレバレッジ取引APIへ中継する最小構成の自動売買ボットです。ENTRY/CLOSEの成行注文のみを扱い、冪等性と安全装置を備えています。

## 特徴

- FastAPI + pybotters による軽量構成
- TradingView Webhook用のトークン認証（クエリ/ヘッダ両対応）
- SQLiteによるイベントIDの冪等管理
- 時刻スキュー検査・シンボル許可リスト・ドライランモード対応
- GMOコインREST API `/v1/order` `/v1/openPositions` `/v1/closeBulkOrder` のみ利用

## セットアップ

1. `.env.example` を `.env` にコピーして環境に合わせて編集します。
2. Dockerイメージをビルドして起動します。

   ```bash
   docker compose up -d --build
   ```

3. 疎通確認（ローカル）:

   ```bash
   curl -s http://localhost:8000/healthz
   curl -s -X POST "http://localhost:8000/webhook/tv?token=$WEBHOOK_TOKEN" \
     -H 'Content-Type: application/json' \
     -d '{"type":"ENTRY","symbol":"BTC_JPY","side":"BUY","size":"0.01","id":"smoke-1","ts":1893456000000}'
   ```

4. （任意）Cloudflare Tunnel 等で `https://<your-domain>/webhook/tv` を `http://web:8000/webhook/tv` に転送します。

## 環境変数

`.env.example` に主要な設定が記載されています。

- `WEBHOOK_TOKEN`: TradingView Webhookからの共有シークレット
- `GMO_API_KEY` / `GMO_API_SECRET`: GMOコインAPI鍵
- `TRADING_ENABLED`: `false` でドライラン（ログのみ）
- `ALLOWED_SYMBOLS`: 取引許可シンボル（カンマ区切り）
- `MAX_SKEW_SECONDS`: 受信時刻とシグナル時刻の許容差
- `SIZE_DECIMALS_<SYMBOL>`: シンボル毎のサイズ小数桁（例: `SIZE_DECIMALS_BTC_JPY=4`）
- `LOG_LEVEL`, `PORT`, `TZ`: ログレベル、HTTPポート、タイムゾーン

## TradingView 設定例

Webhook URL: `https://<your-domain>/webhook/tv?token=<WEBHOOK_TOKEN>`

### ENTRY

```json
{"type":"ENTRY","symbol":"BTC_JPY","side":"BUY","size":"0.01","id":"{{strategy.order.id}}-{{timenow}}","ts":{{timenow}}}
```

### CLOSE

```json
{"type":"CLOSE","symbol":"BTC_JPY","side":"SELL","id":"close-{{timenow}}","ts":{{timenow}}}
```

## API エンドポイント

| メソッド | パス           | 説明                         |
|----------|----------------|------------------------------|
| GET      | `/healthz`     | 健康チェック                 |
| GET      | `/status`      | 直近イベントと設定状況確認   |
| POST     | `/webhook/tv`  | TradingViewシグナル受信      |

`POST /webhook/tv` では、クエリ `token` またはヘッダ `X-TV-Token` に `WEBHOOK_TOKEN` を指定してください。

## ドライランモード

`.env` の `TRADING_ENABLED=false` を設定すると注文は送信せず、ログとレスポンスに `dryRun: true` が含まれます。システム動作確認に利用できます。

## ローカルテスト

サンプルリクエストは `scripts/curl_examples.sh` にまとめています。

```bash
WEBHOOK_TOKEN=changeme ./scripts/curl_examples.sh
```

## 注意事項

- GMOコインの最小取引単位に合わせて `SIZE_DECIMALS_<SYMBOL>` を必ず設定してください。
- TradingViewから送信する `size` は文字列で、指定桁数に一致している必要があります。
- 同一IDのシグナルは一度のみ処理されます。再送する場合は新しいIDを付与してください。
- GMOコインAPIのレスポンスやエラーメッセージはログに要約されますが、秘密鍵や署名値は出力されません。

## ライセンス

[MIT](LICENSE)
