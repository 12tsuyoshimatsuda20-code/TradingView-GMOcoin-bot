# TradingView GMO Coin Bot

## 概要
TradingView Essential から送信される Webhook を受信し、pybotters を利用して GMOコイン（レバレッジ）へ成行でのエントリーと全量クローズを自動実行する単一ポジション Bot です。Webhook の重複防止、Discord 通知、SQLite でのイベント記録を備えています。Cloudflared トンネルを利用して `/webhook` のみを公開する構成を想定しています。

## 前提条件
- Docker / Docker Compose が利用可能なこと
- ホストの時刻同期（NTP）が有効であること（`MAX_SKEW_SECONDS=60` のバリデーションに抵触しないため）
- GMOコインレバレッジ取引 API のキー・シークレットを保有していること
- Discord Webhook URL を準備していること

## リポジトリ構成
```
.
├── Dockerfile
├── docker-compose.yml
├── README.md
├── config/
│   └── .env.example
├── app/
│   ├── main.py
│   ├── schemas.py
│   ├── notify.py
│   ├── store.py
│   ├── domain.py
│   ├── version.py
│   └── infra/
│       ├── gmocoin_client.py
│       └── positions.py
├── requirements.txt
├── tests/
│   ├── test_webhook_contract.py
│   └── test_sign_gmocoin.py
├── data/
│   └── .gitkeep
└── logs/
    └── .gitkeep
```

## セットアップ手順
1. リポジトリをクローンします。
   ```bash
   git clone <このリポジトリのURL>
   cd TradingView-GMOcoin-bot
   ```
2. 設定ファイルを作成します。
   ```bash
   cp config/.env.example config/.env
   ```
3. `config/.env` を編集し、以下の項目を全て設定します。
   - `WEBHOOK_TOKEN`: TradingView 側のトークンと一致させる固定文字列
   - `GMO_API_KEY`: GMOコイン API キー（32 文字を想定）
   - `GMO_API_SECRET`: GMOコイン API シークレット（64 文字の 16 進数など）。クリップボード経由で貼り付ける際は改行やスペースが混入しないようにし、必要に応じて `cat -vet` や `xxd` などで確認してください。
   - `DISCORD_WEBHOOK`: Discord Webhook URL（成功/失敗/無視の全通知を送信）
   - `SYMBOL`, `ENTRY_POLICY`, `MAX_SKEW_SECONDS`, `QTY_STEP`, `TZ`: 仕様に合わせて調整

   > **注意:** `.env` を Windows で編集した場合、CRLF が混入しているとシェル実行時に不具合が発生します。`dos2unix config/.env` を実行して LF に変換してください。

## 依存ピンの理由
We pin pybotters==1.9.1 to use an available wheel compatible with Python 3.11 and to align with aiohttp 3.11.x. Earlier pins like pybotters==0.21.1 do not exist on PyPI and cause build failures. During the Docker build we upgrade pip first so the resolver fully supports manylinux wheels and fails fast if incompatible packages are introduced.

## ビルドと起動
1. コンテナをビルドしてバックグラウンドで起動します。
   ```bash
   docker compose up -d --build
   ```
2. ヘルスチェックが 200 を返すことを確認します。
   ```bash
   curl -sS http://127.0.0.1:8080/healthz
   # => {"status":"ok"}
   ```

## Webhook 動作確認
`.env` の値を利用して以下のテストを行います。`jq` を使う場合は事前にインストールしてください。

1. 変数セットアップ
   ```bash
   TOKEN_VALUE="$(grep -E '^WEBHOOK_TOKEN=' config/.env | cut -d= -f2-)"
   EID="$(date -u +%Y%m%dT%H%M%SZ)-$RANDOM"
   TS="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
   ```

2. ENTRY テスト（BUY）
   ```bash
   cat >/tmp/payload-entry.json <<JSON
   {"token":"$TOKEN_VALUE","event_id":"$EID","ts":"$TS","symbol":"BTC_JPY","size":0.04,"mode":"ENTRY","side":"BUY","entry_price_hint":16660000,"tp1_price":16662000}
   JSON
   curl -iS -H "Content-Type: application/json" --data-binary @/tmp/payload-entry.json http://127.0.0.1:8080/webhook
   ```

3. 重複送信（idempotency 確認）
   ```bash
   curl -iS -H "Content-Type: application/json" --data-binary @/tmp/payload-entry.json http://127.0.0.1:8080/webhook
   # => HTTP/1.1 200 OK + {"status":"duplicate",...}
   ```

4. CLOSE テスト
   ```bash
   EID="$(date -u +%Y%m%dT%H%M%SZ)-$RANDOM"
   cat >/tmp/payload-close.json <<JSON
   {"token":"$TOKEN_VALUE","event_id":"$EID","ts":"$TS","symbol":"BTC_JPY","size":0.04,"mode":"CLOSE","side":"SELL","entry_price_hint":16660000,"tp1_price":null}
   JSON
   curl -iS -H "Content-Type: application/json" --data-binary @/tmp/payload-close.json http://127.0.0.1:8080/webhook
   ```

5. ステータス確認
   ```bash
   curl -sS http://127.0.0.1:8080/status | jq .
   ```

## Cloudflared を利用した公開
1. Cloudflared をインストールし、認証済みであることを確認します。
2. Bot コンテナが稼働しているホストで以下を実行し、`/webhook` のみを公開します。
   ```bash
   cloudflared tunnel --url http://127.0.0.1:8080 --hostname <your-domain>
   ```
3. アクセス制御は TradingView 側の固定トークンで行われるため、外部公開時も `/webhook` パスのみ送信対象にしてください。
4. Cloudflared の設定ファイルを用いる場合は `ingress` ルールで `/webhook` を Bot に転送し、その他は `404` にする構成を推奨します。

## 運用上の注意
- TradingView からの `ts` と受信時刻の差分が `MAX_SKEW_SECONDS`（デフォルト 60 秒）を超えると 400 を返します。ホストと TradingView 双方で NTP 同期を行ってください。
- ENTRY_POLICY は `ignore` 固定です。建玉がある状態で ENTRY が届いた場合は通知のみ行い、発注は行いません。
- Discord 通知は成功（緑）、無視・建玉なし（灰）、エラー（赤）で Embed を送信します。
- SQLite (`data/bot.db`) には `event_id` を主キーとしてイベント履歴が記録されます。バックアップやローテーションが必要な場合は停止後にファイルを退避してください。
- `.env` に設定する API キー・シークレットは平文保存となるため、アクセス権限を最小限にし、バージョン管理しないでください。
- GMO API シークレット貼り付け時は余計な空白・改行が混入しないように十分確認してください。必要に応じて `printf %s "$GMO_API_SECRET" | wc -c` で文字数を検証できます。

## トラブルシュート
| 事象 | 原因・対処 |
| --- | --- |
| `/webhook` が 400 `Invalid token` | TradingView 側のトークンと `.env` の `WEBHOOK_TOKEN` が一致しているか確認してください。 |
| `/webhook` が 400 `Timestamp skew too large` | ホストと TradingView の時計がずれている可能性があります。NTP を確認し、`MAX_SKEW_SECONDS` を適切に調整してください。 |
| `/webhook` が 400 `Size must align with step` | `size` が `QTY_STEP` の倍数かを確認してください。0.01 刻みに揃えてください。 |
| `/webhook` が 502 `GMO Coin API error` | API キー/シークレット、レバレッジ口座の残高、発注上限などを確認します。Discord に赤色の通知が届きます。 |
| `/status` が 502 | GMO コイン API から建玉情報を取得できなかった場合に発生します。ネットワークと API 資格情報を確認してください。 |
| Discord 通知が届かない | `.env` の `DISCORD_WEBHOOK` を確認し、外部への HTTPS 通信が許可されているか確認してください。 |

## テスト実行
ホスト環境で依存関係をセットアップした上で以下を実行します。
```bash
pip install -r requirements.txt
pytest
```

## バージョン情報
- アプリケーションバージョン: `v1.0.0`
- Python ベースイメージ: `python:3.11-slim`

