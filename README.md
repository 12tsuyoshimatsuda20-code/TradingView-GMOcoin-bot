# TradingView → GMOコイン 執行ボット

TradingView の Webhook シグナルを受け取り、pybotters を用いて GMOコイン（レバレッジ）に対して成行エントリー / 成行クローズを実行する FastAPI ベースのボットです。単一ポジション運用と冪等性を重視し、Discord Webhook へ結果通知を送ります。

## 機能概要

- `POST /webhook` で TradingView からの JSON シグナルを受信
- `token`・`event_id`・`ts` の検証、±60 秒以内の時刻スキュー判定
- `event_id` の TTL キャッシュによる冪等制御（重複イベントの無視）
- GMOコイン REST API（pybotters）での成行エントリー / closeBulkOrder による全量クローズ
- 既存ポジションがある場合の ENTRY 無視（ENTRY_POLICY=ignore）
- Discord Webhook への成功 / 失敗通知
- `GET /healthz`・`GET /status` による運用監視

## セットアップ

1. **リポジトリの取得**

   ```bash
   git clone <repo-url>
   cd TradingView-GMOcoin-bot
   ```

2. **依存関係のインストール**

   Python 3.11 を前提としています。

   ```bash
   python -m pip install --upgrade pip
   pip install -r requirements.txt
   ```

3. **環境変数の設定**

   `config/.env.example` をコピーして `.env` を作成し、必要な値を設定します。Windows で編集した場合は LF 改行へ変換してください。

   ```bash
   cp config/.env.example config/.env
   # 編集後に改行コードを確認
   dos2unix config/.env
   ```

   主要項目:

   - `WEBHOOK_TOKEN`: TradingView との共有シークレット
   - `GMO_API_KEY` / `GMO_API_SECRET`: GMOコイン API 鍵（32 桁 / 64 桁）
   - `NOTIFY_DISCORD_WEBHOOK_URL`: Discord Webhook URL（未設定で通知無効）
   - `QTY_STEP`: 発注数量刻み（既定 `0.01`）
   - `MAX_SKEW_SECONDS`: シグナル時刻許容差（既定 `60` 秒）
   - `DRY_RUN=1` にすると発注をスキップし動作確認だけを行います

4. **起動**

   ```bash
   uvicorn app.app:app --host 0.0.0.0 --port 8080
   ```

   起動後、以下でヘルスチェックが成功することを確認してください。

   ```bash
   curl -sS http://127.0.0.1:8080/healthz
   ```

## Webhook コントラクト

TradingView から送信する JSON は以下の形式を厳守してください。全ての値は Pine 側で最終値まで計算し、テンプレートや式（`{{...}}`）を含めないでください。

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

- `ts` は `YYYY-MM-DDTHH:MM:SSZ`（UTC、秒精度）のみ許容
- `event_id` は 10 分間キャッシュされ、重複時は `duplicate` として処理
- `symbol` は `BTC_JPY` 固定。ENTRY は建玉が無い場合のみ発注
- `size` は `QTY_STEP` の倍数に切り下げられ、0 になった場合は 400 応答

## 手動テスト

サービス起動後、以下のコマンドで疎通を確認できます（時刻は適宜更新してください）。

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

## エンドポイント

| Method | Path      | 説明                                      |
| ------ | --------- | ----------------------------------------- |
| GET    | /healthz  | 常に `{"status":"ok"}` を返すヘルスチェック |
| GET    | /status   | 直近イベントやリトライ上限などの状態参照    |
| POST   | /webhook  | TradingView からのシグナル受付             |

## 運用メモ

- GMO API の署名は `pybotters.Client(apis={"gmocoin": (API_KEY, API_SECRET)})` で自動付与されます
- Discord 通知に失敗しても取引処理は継続し、WARN ログに記録されます
- `.env` に余分な空白や CRLF が混入すると署名が失敗します。`dos2unix` を活用してください
- VPS の時刻は chrony 等で同期し、`MAX_SKEW_SECONDS` を超えないようにしてください

## ライセンス

本プロジェクトは [MIT License](LICENSE) の下で公開されています。
