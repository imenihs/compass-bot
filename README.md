# Compass Bot

家庭向けのお小遣いサポート Discord Bot です。

- Gemini でお小遣い査定（固定 / 臨時）を返す
- お小遣い帳（支出記録）を保存する
- 財布残高（ウォレット）を管理する
- 毎月リマインド（支給前通知）と残高監査を行う
- 親向けダッシュボード・低残高アラート・月次サマリー・傾向分析・目標貯金に対応

## 主な機能

1. 査定Bot
- メンション本文または自然文を解析して査定（設定で切替）
- 親ユーザーは `名前の代理` で代理投稿可能
- 応答が遅い場合、10秒ごとに進捗メッセージを送信
- 査定結果は変更通知を `allowance_reminder.channel_id` に送信（変更がある項目のみ）
- ルールベースでマッチしない短いコマンドは Gemini が意図を判定して処理する（ハイブリッド方式）

2. お小遣い帳
- 3項目で記録
- `使った物` / `理由（3語以上）` / `満足度（0-10）`
- ラベル形式・簡易形式どちらでも入力可

3. ウォレット運用
- 初期設定フローで現在所持金を反映
- 残高報告との差分監査、ペナルティ減額

4. 定期運用
- 毎月支給前に固定お小遣いリマインド
- 毎月指定時刻に `allow_channel_ids` の各チャンネルへ残高報告依頼（ウォレット未設定のユーザーには初期設定案内を送信）

5. 親向けダッシュボード（親のみ）
- 「全体確認」コマンドで全ユーザーの残高・報告状況・最終支出日を一覧表示

6. 低残高アラート（自動）
- 査定後に残高が閾値を下回ると親チャンネルに自動通知
- `low_balance_alert` セクションで有効化・閾値・送信先を設定

7. 月次サマリーレポート（自動）
- 毎月1日の指定時刻に前月の支出統計を親チャンネルへ自動送信
- `monthly_summary` セクションで有効化・送信時刻・送信先を設定

8. 支出傾向分析（親のみ）
- 「[名前]の分析」または「全員の分析」で過去3ヶ月の月別件数・満足度平均・Top5品目を表示

9. 目標貯金
- 「貯金目標 ゲーム機 30000円」で目標を設定
- 「目標確認」でプログレスバー付きの進捗を表示
- 「目標削除」で目標をリセット

## ディレクトリ構成

- `app/bot.py`: Discordイベント（on_message/on_ready）・コマンドハンドラ
- `app/bot_utils.py`: グローバル状態不使用の純粋ユーティリティ関数群（bot.py から分離）
- `app/gemini_service.py`: Gemini API 呼び出し・査定抽出（call_silent / call_with_progress）
- `app/message_parser.py`: メッセージ解析（メンション除去・入力パース）
- `app/reminder_service.py`: 月次リマインド・残高監査・月次サマリーの定期処理ループ
- `app/wallet_service.py`: 残高管理・台帳・監査状態・目標貯金（wallet_state.json）
- `app/prompts.py`: Gemini へ渡すプロンプトのビルド関数
- `app/config.py`: 設定ファイル読み込み（users / setting / system）
- `app/storage.py`: JSONL 追記・JST タイムスタンプなど共通I/Oユーティリティ
- `app/server.py`: ヘルスチェック用 FastAPI サーバー
- `settings/system.json`: 共通設定（currency / log_dir 等）
- `settings/setting.json`: 実運用設定（親ID / チャンネル / 各機能設定）
- `settings/users/*.json`: ユーザー個別設定（名前・年齢・固定お小遣い・キーワード等）
- `data/logs/*.jsonl`: 監査/台帳/イベント/お小遣い帳ログ

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
- `settings/users/*.json`

初回はテンプレートからコピーして作成:
```bash
cp settings/setting.example.json settings/setting.json
cp settings/users/user.example.json settings/users/<your_user_key>.json
```

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
  "name": "たろう",
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

テンプレート:
- `settings/users/user.example.json`

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
  - `enabled`, `channel_id`, `payday_day`, `notify_time`（`HH:MM`）
  - `notify_offset`: カンマ区切り文字列または配列で複数日指定可（例: `"-7day,-1day,0day"` または `["-7day", "-1day", "0day"]`）。`0day` は支給日当日を意味する
- `wallet_audit`: 月次残高監査設定
  - `enabled`, `check_day`, `check_time`, `penalty_rate`
  - 送信先は `allow_channel_ids` を使用（`channel_id` 設定は不要・無視される）
- `low_balance_alert`: 低残高アラート設定
  - `enabled`: true で有効化
  - `threshold`: 残高がこの値を下回ったときに通知（円）
  - `channel_id`: 通知先チャンネルID
- `monthly_summary`: 月次サマリーレポート設定
  - `enabled`: true で有効化
  - `channel_id`: 送信先チャンネルID
  - `send_time`: 送信時刻（`HH:MM` 形式、毎月1日に実行）
- `pocket_journal_reminder`: 週次支出記録リマインド設定
  - `enabled`: true で有効化
  - `day_of_week`: 送信曜日（0=月曜〜6=日曜、Python weekday() 準拠）
  - `notify_time`: 送信時刻（`HH:MM` 形式）
  - 過去7日間に支出記録がないユーザーにのみ送信する。`allow_channel_ids` を使用

テンプレート:
- `settings/setting.example.json`

### `settings/system.json`
- `currency`
- `log_dir`
- `default_reply_format`

テンプレート:
- `settings/system.example.json`

## Discordでの使い方

### 1. 査定
```text
@compass-bot 3Dプリンタ買いたい
理由: 工作したい
予算: 3万円
```

### 2. 親による代理投稿
```text
@compass-bot たろうの代理 参考書を買いたい
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

### 9. 残高チェック案内の即時送信（親のみ）
`allow_channel_ids` の各チャンネルに今月の残高チェック案内を即時送信する。ウォレット未設定のユーザーには初期設定案内を送信:
```text
@compass-bot 残高チェック送信
```

または:
```text
@compass-bot 月頭案内送信
```

### 10. リマインダーテスト（親のみ）
```text
@compass-bot reminder test
```

### 11. 全体確認ダッシュボード（親のみ）
全ユーザーの残高・残高報告状況・最終支出日を一覧表示:
```text
全体確認
```

### 12. 支出傾向分析（親のみ）
過去3ヶ月の月別件数・満足度平均・Top5品目を表示:
```text
たろうの分析
全員の分析
```

### 15. 査定履歴確認
直近5件の査定金額（固定・臨時・合計）を一覧表示:
```text
@compass-bot 査定履歴
```

### 14. 今月の振り返り（子供）
当月の支出件数・満足度平均・Top3品目・現在残高を表示:
```text
@compass-bot 振り返り
```

### 13. 目標貯金
目標設定:
```text
貯金目標 ゲーム機 30000円
```

進捗確認（プログレスバー表示）:
```text
目標確認
```

目標リセット:
```text
目標削除
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
- 個人情報を含む実設定（`settings/*.json`, `settings/users/*.json`）は `.gitignore` で除外し、テンプレート（`*.example.json`）のみ共有する運用を推奨します。
