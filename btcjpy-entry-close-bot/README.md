# BTCJPY ENTRY/CLOSE Bot

TradingView Essential からの Webhook を受信し、pybotters 経由で GMOコイン（レバレッジ）に対して BTC_JPY の成行 ENTRY / CLOSE を実行するためのコンテナ一式です。Discord Webhook への通知、冪等チェック、鮮度検査、Cloudflared トンネル公開を前提としています。

## ディレクトリ構成

```
btcjpy-entry-close-bot/
├─ docker-compose.yml
├─ README.md
├─ config/
│  ├─ .env.example
│  └─ gunicorn_conf.py
├─ exec-lane/
│  ├─ Dockerfile
│  ├─ app.py
│  ├─ gmo.py
│  ├─ notify.py
│  ├─ requirements.txt
│  ├─ runtime.py
│  └─ ws.py
└─ logs/
   └─ .gitkeep
```

`logs/` は SQLite と構造化ログの格納ディレクトリです。コンテナ実行ユーザ（非 root）が書き込めるように docker-compose 側でボリュームマウントします。

## 事前準備

### 1. リポジトリ一式の転送

1. ローカルで ZIP 化
   - **Windows (PowerShell)**
     ```powershell
     cd <プロジェクト配置ディレクトリ>
     Compress-Archive -Path btcjpy-entry-close-bot -DestinationPath btcjpy-entry-close-bot.zip -Force
     ```
   - **Linux / macOS**
     ```bash
     cd <プロジェクト配置ディレクトリ>
     zip -r btcjpy-entry-close-bot.zip btcjpy-entry-close-bot
     ```
2. ConoHa VPS へ転送
   - **Windows**: `pscp` (PuTTY) などを利用
     ```powershell
     pscp .\btcjpy-entry-close-bot.zip user@<your_vps_ip>:/home/user/
     ```
   - **Linux / macOS**
     ```bash
     scp btcjpy-entry-close-bot.zip user@<your_vps_ip>:/home/user/
     ```
3. VPS 上で展開
   ```bash
   ssh user@<your_vps_ip>
   cd /home/user
   unzip btcjpy-entry-close-bot.zip
   ```
4. 配置確認
   ```bash
   find btcjpy-entry-close-bot -maxdepth 2 -type f
   ```

### 2. `.env` の作成

`config/.env.example` をベースに実運用用の `config/.env` を作成し、改行コードを LF へ統一します。`dos2unix` を推奨します。

```bash
cd /home/user/btcjpy-entry-close-bot
cp config/.env.example config/.env
nano config/.env  # 値を編集
sudo apt-get update && sudo apt-get install -y dos2unix
find config -maxdepth 1 -type f -name '*.env' -exec dos2unix {} +
```

> **注意**: `.env` にクォート（`"`）やスペースが紛れ込むと API キー長チェックで検知されます。起動時ログに `length` と `repr_len` が記録されるので、期待値（KEY=32, SECRET=64）が一致するか必ず確認してください。

### 3. 必須環境変数

| 変数 | 説明 |
| ---- | ---- |
| `WEBHOOK_TOKEN` | TradingView Webhook JSON の `token` と一致させる値 |
| `GMO_API_KEY` | GMOコイン API KEY（32文字） |
| `GMO_API_SECRET` | GMOコイン API SECRET（64文字） |
| `ALLOWED_SYMBOLS` | 取引対象シンボル（カンマ区切り）。初期値は `BTC_JPY` |
| `ENTRY_POLICY` | `ignore` 固定（建玉中のENTRYは無視） |
| `MAX_SKEW_SECONDS` | Webhook 時刻の許容ズレ秒数。デフォルト 60 |
| `QTY_STEP` | 数量丸め単位。GMOコイン BTC レバレッジは 0.01 |
| `NOTIFY_DISCORD_WEBHOOK_URL` | Discord Webhook URL |
| `ENV` | 任意の環境識別子（例: `prod`, `stg`） |

### 4. GMOコイン API 準備

- API キーを **現物/レバレッジ共通 API** から取得し、IP 制限を設定してください。
- `pybotters` が自動で署名ヘッダーを付与するため、追加の署名実装は不要です。

### 5. 時刻同期

Webhook の鮮度チェックは ±60 秒以内が必須です。VPS では `chrony` などで NTP 同期を行ってください。

```bash
sudo apt-get install -y chrony
sudo systemctl enable --now chrony
```

## Docker / Cloudflared セットアップ

1. Docker / Docker Compose v2 を導入（Ubuntu 24.04+）
   ```bash
   sudo apt-get update
   sudo apt-get install -y ca-certificates curl gnupg
   sudo install -m 0755 -d /etc/apt/keyrings
   curl -fsSL https://download.docker.com/linux/ubuntu/gpg | sudo gpg --dearmor -o /etc/apt/keyrings/docker.gpg
   echo \
     "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] https://download.docker.com/linux/ubuntu \
     $(. /etc/os-release && echo $VERSION_CODENAME) stable" | \
     sudo tee /etc/apt/sources.list.d/docker.list > /dev/null
   sudo apt-get update
   sudo apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
   sudo usermod -aG docker $USER
   newgrp docker
   ```

2. Cloudflared トンネル
   - [Cloudflare ドキュメント](https://developers.cloudflare.com/cloudflare-one/connections/connect-networks/) に従ってトンネルを作成し、`http://127.0.0.1:8080` へルーティングしてください。
   - 例: `cloudflared tunnel run btcjpy-entry-close-bot` で起動。常駐化には systemd サービスを推奨。

3. Uptime Kuma などの監視ツールで `https://<domain>/healthz` を監視すると稼働監視が容易です。

## ビルド & 起動手順

```bash
cd /home/user/btcjpy-entry-close-bot
ls -a  # .env が存在するか確認

docker compose build

docker compose up -d
```

- 起動後、ログは以下で確認できます。
  ```bash
  docker compose logs -f
  ```
- `docker compose ps` でコンテナ状態を確認。

## ヘルスチェック

- `GET http://127.0.0.1:8080/healthz` → `{"status":"ok"}`
- `GET http://127.0.0.1:8080/status` でポジション情報、直近イベント、リトライ統計、WS 接続状態を取得。

## スモークテスト

Cloudflared で公開されたドメイン（例: `https://example.trycloudflare.com`）に対して以下の cURL を実行してください。ローカル検証は `http://127.0.0.1:8080` でも同様です。

### ENTRY

```bash
curl -iS https://<domain>/webhook \
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

### CLOSE

```bash
curl -iS https://<domain>/webhook \
 -H "Content-Type: application/json" \
 --data-binary '{
  "token":"SECRETxxyyzz",
  "event_id":"dev-CLOSE-001",
  "ts":"2025-09-24T00:01:00Z",
  "mode":"CLOSE",
  "symbol":"BTC_JPY"
 }'
```

**期待挙動**

- FastAPI が 200 系レスポンスを返却。
- GMOコイン API への注文が完了し、約定確認後に Discord へ「ENTRY OK」/「CLOSE OK」が送信。
- `logs/runtime.log` および標準出力に JSON ログが記録される（`event_id`, `mode`, `result`, `latency_ms` 等）。
- `.env` のキー長チェックログが `gmo_api_key_length_check` / `gmo_api_secret_length_check` として出力される。

## 運用 Tips

- **dos2unix**: Windows で `.env` を編集した場合は必ず `dos2unix config/.env` を実施してください。CRLF が残ると認証に失敗する恐れがあります。
- **リトライ**: 429/5xx/timeout は指数バックオフ（0.5 → 1.0 → 2.0 秒）で最大 3 回自動再試行します。最終失敗時は Discord へ ERROR 通知。
- **時刻ズレ**: `MAX_SKEW_SECONDS` を一時的に 120 に増やすと検証が容易ですが、本番では 60 以下を推奨します。
- **ログ**: `logs/runtime.db` にイベント履歴が保存され、同一 `event_id` の二重実行を防ぎます。バックアップ時はこのファイルも保存してください。
- **WS 監視**: 現バージョンでは REST ベースで完結しますが、将来的な WS 拡張用に `ws.py` を配置しています。`/status` の `ws_connected` フラグで接続状態を確認できます（未接続の場合は `false`）。
- **監視**: Uptime Kuma などで `https://<domain>/healthz` を 1 分間隔で監視し、アラートを設定してください。
- **更新**: 変更時は `docker compose pull` → `docker compose up -d --build` を実施し、稼働中ログを確認してください。

## トラブルシューティング

| 症状 | 確認ポイント |
| ---- | ---- |
| `.env` ロード失敗 | `.env` の改行コード・余計なクォート、`dos2unix` 実施状況 |
| 429/5xx が頻発 | GMOコイン側のレート制限。API 設定を見直し、ネットワーク経路を確認 |
| Discord 通知が届かない | Webhook URL の再発行、ファイアウォール設定、`docker compose logs` で `discord_notification_failed` の有無を確認 |
| Cloudflared 経由アクセス不可 | トンネルのバインド先 (`http://127.0.0.1:8080`) とポート開放を確認。トンネルログを確認 |

## ライセンス

本リポジトリ内のコードは MIT ライセンスに基づき配布されます。

