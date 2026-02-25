# Compass Bot

家庭向けのお小遣いサポート Discord Bot です。

- Gemini でお小遣い査定（固定 / 臨時）を返す
- お小遣い帳（支出記録）を保存する
- 財布残高（ウォレット）を管理する
- 毎月リマインド（支給前通知）と残高監査を行う

## 主な機能

1. 査定Bot
- メンション本文または自然文を解析して査定（設定で切替）
- 親ユーザーは `名前の代理` で代理投稿可能
- 応答が遅い場合、10秒ごとに進捗メッセージを送信
- 査定結果は変更通知を `allowance_reminder.channel_id` に送信（変更がある項目のみ）

2. お小遣い帳
- 3項目で記録
- `使った物` / `理由（3語以上）` / `満足度（0-10）`
- ラベル形式・簡易形式どちらでも入力可

3. ウォレット運用
- 初期設定フローで現在所持金を反映
- 残高報告との差分監査、ペナルティ減額

4. 定期運用
- 毎月支給前に固定お小遣いリマインド
- 毎月指定時刻に残高報告依頼

## ディレクトリ構成

- `app/bot.py`: Discordイベントのエントリポイント
- `app/gemini_service.py`: Gemini呼び出し・査定抽出
- `app/message_parser.py`: メッセージ解析
- `app/reminder_service.py`: 月次リマインド/監査ループ
- `app/wallet_service.py`: 残高・台帳・監査状態
- `app/prompts.py`: Geminiプロンプト
- `app/config.py`: 設定読み込み
- `settings/system.json`: 共通設定
- `settings/setting.json`: 実運用設定
- `settings/users/*.json`: ユーザー設定
- `data/logs/*.jsonl`: 監査/台帳/イベントログ

## 必要環境

- Python 3.11+
- Discord Bot Token
- Gemini API Key

依存関係（`requirements.txt`）:
- `discord.py`
- `google-genai`
- `fastapi`
- `uvicorn`
- `python-dotenv`
- `httpx`

## セットアップ

1. 依存インストール
```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

2. 環境変数設定（`.env`）
```env
DISCORD_BOT_TOKEN=xxxxxxxx
GEMINI_API_KEY=xxxxxxxx
```

3. 設定ファイル確認
- `settings/setting.json`
- `settings/system.json`
- `settings/users/*.json`

## 起動

### Discord Bot
```bash
python3 app/bot.py
```

### Health API（任意）
```bash
uvicorn app.server:app --host 0.0.0.0 --port 8000
```

## 設定

### `settings/users/*.json`（例）
```json
{
  "name": "あきら",
  "discord_user_id": 111111111111111111,
  "age": 12,
  "gender": "male",
  "fixed_allowance": 700,
  "temporary_max": 3000,
  "fixed_increase_cap": 100,
  "penalty_cap": 100,
  "keywords": {
    "investment": ["検定", "英語", "参考書"],
    "fun": ["漫画", "ゲーム"],
    "danger": ["高額ガチャ", "課金"]
  }
}
```

`gender` の推奨値:
- `male`
- `female`
- `unspecified`

### `settings/setting.json`
- `parent_ids`: 親（管理者）ユーザーID配列
- `allow_channel_ids`: Botが反応するチャンネルID配列
- `gemini_model`: 利用モデル
- `assess_keyword`: 査定抽出判定キーワード
- `force_assess_test_keyword`: 動作確認用キーワード
- `chat`: 会話モード
  - `natural_chat_enabled`, `require_mention`
- `allowance_reminder`: 支給前リマインド設定
  - `enabled`, `channel_id`, `payday_day`, `notify_offset`（例: `-7day`）, `notify_time`（`HH:MM`）
- `wallet_audit`: 月次残高監査設定
  - `enabled`, `channel_id`, `check_day`, `check_time`, `penalty_rate`

### `settings/system.json`
- `currency`
- `log_dir`
- `default_reply_format`

## Discordでの使い方

### 1. 査定
```text
@compass-bot 3Dプリンタ買いたい
理由: 工作したい
予算: 3万円
```

### 2. 親による代理投稿
```text
@compass-bot りひとの代理 参考書を買いたい
```

### 3. 使い方の説明
```text
@compass-bot 使い方
```

### 4. 初期設定（ウォレット初期残高）
```text
@compass-bot 初期設定
@compass-bot 1200円
```

### 5. 支出記録
案内起動:
```text
@compass-bot 支出記録
```

記録入力（ラベル形式）:
```text
@compass-bot 使った物: ノート
理由: 英語 の テスト 対策
満足度: 8
```

記録入力（簡易形式）:
```text
@compass-bot ノート / 英語のテスト対策で必要だった / 8
```

### 6. 現在残高確認
```text
@compass-bot 残高確認
```

### 7. 残高報告
```text
@compass-bot 残高報告 1200円
```

### 8. 親の一斉アナウンス
親が次を投稿すると、`allow_channel_ids` の全チャネルへ使い方と初期設定を通知:
```text
使い方の説明と初期設定
```

### 9. リマインダーテスト（親のみ）
```text
@compass-bot reminder test
```

## ログ

`settings/system.json` の `log_dir` 配下に JSONL で保存されます。

- `{name}_events.jsonl`: 査定イベント（入力・返信・査定金額）
- `{name}_allowance_amounts.jsonl`: 査定金額ログ（`fixed`, `temporary`, `total`）
- `{name}_wallet_ledger.jsonl`: 残高の増減台帳
- `{name}_pocket_journal.jsonl`: お小遣い帳（支出記録）
- `data/wallet_state.json`: 現在の帳簿残高
- `data/wallet_audit_state.json`: 監査依頼・初期設定/支出記録の待機状態
- `data/reminder_state.json`: 月次リマインド送信状態

## 注意点

- `[#SH-xxx]` 形式は非対応です。
- `chat.require_mention=true` の場合はメンション必須です。
- `allow_channel_ids` を設定している場合、対象チャンネル以外では反応しません。
- Bot文面や査定ルールは `app/prompts.py` で調整できます。
