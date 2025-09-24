# TradingView GMO Coin Execution Lane v1

## プロジェクト概要
TradingView（Essential プラン）から送信される Webhook を FastAPI で受け取り、`pybotters` を通じて GMO コイン（レバレッジ）へ成行エントリー / 成行クローズ（全量フラット）を実行するミニマル構成のボットです。単一ポジション運用を前提とし、ENTRY_POLICY は `ignore` 固定、Webhook 鮮度チェック（±60 秒）、数量丸め (`QTY_STEP=0.01`) を実装しています。全ての成功・失敗は Discord Webhook へ通知されます。

## 前提
- Docker / Docker Compose が稼働する VPS (Ubuntu 22.04 / 24.04 想定)
- TradingView Essential プラン（Webhook アラート機能）
- Cloudflared トンネル（`/webhook` を HTTPS 公開）
- Discord Webhook URL（運用通知受信用）

## セットアップ手順
1. ソースを配置し、環境変数ファイルをコピーします。
   ```bash
   cp config/.env.example config/.env
   ```
2. `.env` に実値を入力します。**CRLF 禁止 / 末尾改行 1 つ / 余計な空白・引用符なし**です。Windows 編集後は必ず次を実行してください。
   ```bash
   dos2unix config/.env
   ```
3. ビルド＆起動します。
   ```bash
   docker compose build
   docker compose up -d
   ```
4. 動作確認：
   ```bash
   curl -fsS 127.0.0.1:8080/healthz
   ```

## Webhook 設定（TradingView）
アラート本文には以下の JSON を**そのまま**貼り付けてください。式・変数展開は使用しません。

### ENTRY（BUY/SELL）
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

### CLOSE（全量フラット）
```json
{
  "token": "SECRETxxyyzz",
  "event_id": "{{alert_id}}",
  "ts": "{{timenow}}",
  "mode": "CLOSE",
  "symbol": "BTC_JPY"
}
```

## スモークテスト
コンテナ起動後、TradingView からの送信を模したテストを行えます。

```bash
bash scripts/smoke_entry.sh
bash scripts/smoke_close.sh
```
環境変数 `BOT_URL`（既定: `http://127.0.0.1:8080`）と `TOKEN` を上書き可能です。

## 運用
- `GET /status` で現在の保有数量、最終処理イベント、リトライ回数などを確認できます。
- 成功・失敗時は Discord に `[INFO]` / `[ERROR]` 形式で 1 行通知されます。
- `/logs/exec-lane.log` に JSON ログが出力され、Uptime Kuma 等で `http://<host>:8080/status` や `.../webhook` を監視できます。
- VPS の時刻同期（`chrony` など）、Cloudflared トンネル常時稼働を維持してください。

## トラブルシューティング
- **`.env` が読み込まれない / 認証エラー**: 改行コード (CRLF) や引用符、末尾スペースが原因の場合があります。`python -c "import pathlib; print(repr(pathlib.Path('config/.env').read_text()))"` で確認してください。
- **`ERR-5010` など署名・認証系エラー**: API Key / Secret の桁数や余計な改行を確認し、再度 `dos2unix` を実行してください。
- **`settle_qty > settable_qty` エラー**: CLOSE 時に数量超過が検出されると自動的に数量を `QTY_STEP` 分だけ縮小し再投入します（最大 5 回）。
- **Docker Compose 警告**: `version:` キーは不要です。YAML のクォート漏れに注意してください。
- **切り分けワンライナー**: `curl -fsS http://127.0.0.1:8080/healthz` で FastAPI の稼働可否を即確認できます。

## 伸びしろ (v1.1 以降のアイデア)
- ENTRY ポリシーを `flip` / `close_then_entry` へ拡張
- スリッページや手数料の記録・分析
- `min_qty` / `qty_step` を API から自動取得
- APScheduler による健全性チェック
- DuckDB / SQLite による約定履歴保存
