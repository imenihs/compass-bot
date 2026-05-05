# Compass Bot

家庭向けのお小遣い学習サポート Discord Bot です。

## プロダクト目的

Compass Bot は、子どもが自分のお小遣いを自主的に管理し、親と並走しながらお金の使い方を学ぶためのツールです。
単なる残高管理、親による監視、罰則運用を目的としません。

子どもが「何に使ったか」「なぜ使ったか」「使ってみて満足したか」を記録し、あとから振り返れることを重視します。
親はその記録を見て、必要なときだけ支援・相談・安全確認を行います。

### 設計原則

- 子どもの主体性を優先する。親の管理画面や通知は、子どもの自律を支えるための補助機能とする。
- 感想・理由・満足度は、査定のための点数付けではなく、振り返りと学習の材料として扱う。
- 残高不一致は、まず記録漏れに気づく機会として扱う。自動的な強い罰ではなく、透明で取り戻せる軽い影響に留める。
- 兄弟・他人との比較はしない。比較する場合は過去の本人との比較だけにする。
- お金の判断は、金額だけでなく目的・計画・振り返り・記録習慣を合わせて見る。
- 収集した情報は、必ず子ども本人の振り返り、親との会話、次回査定の説明可能な判断材料のいずれかに活用する。
- 便利さよりも、子どもが自分で気づき、考え、次の使い方を改善できることを優先する。

## 機能概要

- Gemini でお小遣い査定（固定 / 臨時）を返す
- お小遣い帳（支出記録）を保存する
- 財布残高（ウォレット）を管理する
- 支出理由・満足度・記録漏れから、親子会話カードと子どもの小さなチャレンジを作る
- 親が子ども別のAIフォロー方針、会話カード反応、成長行動プランを管理できる
- 毎月リマインド（支給前通知）と残高監査を行う
- 親向けダッシュボード・低残高アラート・月次サマリー・傾向分析・目標貯金に対応
- ブラウザから閲覧できる Webダッシュボード（`compass.rwc.0t0.jp`）を搭載

## 主な機能

1. 査定Bot
- メンション本文または自然文を解析して査定（設定で切替）
- 親ユーザーは `名前の代理` で代理投稿可能
- 応答が遅い場合、10秒ごとに進捗メッセージを送信
- 査定結果は変更通知を `allowance_reminder.channel_id` に送信（変更がある項目のみ）
- ルールベースでマッチしない短いコマンドは Gemini が意図を判定して処理する（ハイブリッド方式）

2. お小遣い帳
- 3〜4項目で記録
- `使った物` / `理由（3語以上）` / `満足度（0-10）` / `金額（任意）`
- ラベル形式・簡易形式どちらでも入力可
- `支出 500円 お菓子` コマンドでも残高減算と同時に記録できる

3. ウォレット運用
- 初期設定フローで現在所持金を反映
- 残高報告との差分監査、記録漏れメモ、次回査定への軽い反映

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

10. Webダッシュボード（ブラウザ閲覧）
- `compass.rwc.0t0.jp` からログインして残高・支出・目標貯金を確認できる
- **親・子供の両方**が利用可能（表示内容はユーザー種別で自動切替）
- 親: 全員の残高・月額お小遣い・支出・報告状況・今週の会話カードを一覧管理、支給・月額変更・AIフォロー方針・成長行動プラン・承認待ち申請を操作
- 子供: 自分の残高・支出記録・貯金目標の進捗・次の小さなチャレンジを閲覧し、`やった` / `あとで` / `ちがう` を残せる
- 申請 → Discord で親が承認 → 仮パスワード発行 → 本パスワード設定 の登録フロー
- `setting.json` の `parent_ids`（Discord ID）と `users/*.json` の `discord_user_id` を照合して管理者権限を自動付与

11. 学習支援・会話カード
- `learning_insights` が既存ログを読み取り専用で分析し、品目・金額・満足度・理由・記録完全率・貯金目標への影響を会話カードに変換する
- 新規支出には `entry_id` を付け、後追いの理由・満足度は元支出へ結びつける
- 親向けには根拠、伸ばしたい力、親の1問、今日の伴走、避けたい声かけを表示する
- 子ども向けには親メモや内部方針を出さず、1回1つの小さな行動だけを表示する
- `learning_support_state` にカード反応・子どもの反応・抑制テーマを保存し、短期間に同じテーマを繰り返さない
- `growth_plans` に、増額相談を家庭内役割・継続行動・確認日・報酬額へつなげる成長行動プランを保存する
- Discord 親コマンド `フォロー方針 たろう` / `フォロー方針 たろう 記録習慣を重視` / `フォロー強さ たろう 軽め` でAIフォロー方針を確認・変更できる

12. 能動的な子ども伴走
- `proactive_child_nudge` を有効にすると、Botが毎日指定時刻に子ども別の状況を確認する
- 定期ループは約10分ごとに起動し、指定時刻ぴったりではなく「指定時刻を過ぎていて、当日分が未処理なら実行」で判定する
- 各通知処理は個別に例外・タイムアウトを捕捉し、1つが失敗しても同じループ内の後続通知を続行する
- 支出記録が一定日数ない、前回チャレンジに反応がない、成長行動プランの確認日が近い場合に、子ども本人のチャンネルへ短い声かけを送る
- 同じ子へ短期間に連投しないよう、`reminder_state.json` に送信履歴を保存する
- 同じ日の再スキャンを避けるため、`reminder_state.json` に `proactive_child_nudge_last_run_at` を保存する
- 文面は「放置」「サボり」「ペナルティ」ではなく、戻りやすい小さな行動だけを促す

13. 異常処理・運用診断
- Discordメッセージ処理は、想定外エラー時も内部原因を出さず短い失敗応答を返し、待てばよい一時障害か管理者確認が必要な異常かをユーザーが判断できる文面にする
- Geminiのtimeout/429/503系は再試行案内にし、認証・設定・未捕捉例外系は管理者連絡案内にする。詳細は `runtime_diagnostics.jsonl` にだけ残す
- Web/APIの未捕捉例外は共通ハンドラで500応答にし、内部原因はレスポンスに出さず、管理者連絡案内と診断ログを残す
- 設定JSON・認証JSON・ウォレット状態の読み書きは、破損や保存途中失敗を想定して診断ログ・原子的保存・fail-closedを使う
- 固定お小遣い自動支給は操作キーで二重実行を防ぐ

## ディレクトリ構成

- `app/bot.py`: Discordイベント（on_message/on_ready）・コアハンドラ（初期設定・支出記録・Gemini査定フロー）
- `app/handlers_parent.py`: 親専用コマンドハンドラ群（支給・残高調整・ダッシュボード等10機能）
- `app/handlers_child.py`: 子供向けコマンドハンドラ群（支出記録・目標貯金・振り返り等7機能）
- `app/bot_utils.py`: グローバル状態不使用の純粋ユーティリティ関数群
- `app/gemini_service.py`: Gemini API 呼び出し・査定抽出（call_silent / call_with_progress）
- `app/message_parser.py`: メッセージ解析（メンション除去・入力パース）
- `app/reminder_service.py`: 月次リマインド・残高監査・月次サマリーの定期処理ループ
- `app/wallet_service.py`: 残高管理・台帳・監査状態・目標貯金（wallet_state.json）
- `app/prompts.py`: Gemini へ渡すプロンプトのビルド関数
- `app/learning_insights.py`: 支出ログから親子会話カード、チャレンジ、プロンプト要点を生成する分析エンジン
- `app/config.py`: 設定ファイル読み込み（users / setting / system）
- `app/storage.py`: JSONL 追記・JST タイムスタンプなど共通I/Oユーティリティ
- `app/server.py`: FastAPI サーバー（ヘルスチェック + Webダッシュボード全エンドポイント）
- `app/web_auth.py`: Webダッシュボード認証モジュール（申請・承認・セッション管理）
- `templates/`: Jinja2 HTML テンプレート群（login / register / set_password / dashboard）
- `settings/system.json`: 共通設定（currency / log_dir 等）
- `settings/setting.json`: 実運用設定（親ID / チャンネル / 各機能設定）
- `settings/users/*.json`: 子供ユーザー個別設定（名前・年齢・固定お小遣い・キーワード等）
- `settings/users/parents/*.json`: 親ユーザー設定（名前・Discord IDのみのシンプル形式）
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
- `jinja2`（Webダッシュボード用テンプレートエンジン）
- `python-multipart`（フォーム送信の受け取り）

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

### Webダッシュボード
Discord Botと同一プロセスで自動起動します。

```bash
# Bot を起動すれば Web ダッシュボードも同時に起動する（port 8765）
sudo systemctl start compass
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
- `web_base_url`: WebダッシュボードのベースURL（例: `https://compass.rwc.0t0.jp`）
- `allow_channel_ids`: Botが反応するチャンネルID配列
- `gemini_model`: 利用モデル
- `assess_keyword`: 査定抽出判定キーワード
- `force_assess_test_keyword`: 動作確認用キーワード
- `chat`: 会話モード
  - `natural_chat_enabled`, `require_mention`
  - `natural_chat_enabled=true` でも、他ユーザー宛ての `@...` メンションを含む発言は無視します。反応対象はメンションなし、または `@compass-bot` / DiscordのBot宛てメンションのみです。
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
- `proactive_child_nudge`: 能動的な子ども伴走設定
  - `enabled`: true で有効化
  - `notify_time`: 毎日確認する時刻（`HH:MM` 形式）
  - Botの定期ループは約10分間隔。時刻ぴったりの1分判定ではなく、予定時刻を過ぎた後に未処理なら実行する
  - `no_record_days`: 支出記録が何日空いたら声をかけるか
  - `challenge_stale_days`: 子どもチャレンジ未反応を何日で声かけ対象にするか
  - `growth_plan_review_days_before`: 成長行動プラン確認日の何日前から声をかけるか
  - `min_days_between_nudges`: 同じ子どもへ連投しないための最低間隔
  - `max_per_run`: 1回のスキャンで送る最大人数

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

### 5. 支出記録（お小遣い帳）
案内起動:
```text
@compass-bot 支出記録
```

記録入力（ラベル形式）:
```text
@compass-bot 使った物: ノート
理由: 英語 の テスト 対策
満足度: 8
金額: 300円
```

記録入力（簡易形式・金額は任意）:
```text
@compass-bot ノート / 英語のテスト対策で必要だった / 8
@compass-bot ノート / 英語のテスト対策で必要だった / 8 / 300円
```

### 6. 現在残高確認
```text
@compass-bot 残高確認
```
親が子ども用チャンネルで `残高おしえて` と送った場合は、親本人ではなくそのチャンネルの子どもの残高を返します。
親は `りかの残高おしえて` のように子どもの名前を明示して確認することもできます。

### 7. 残高報告
```text
@compass-bot 残高報告 1200円
```
残高報告・初期設定・財布チェック中の金額入力は、誤入力防止のため `円` まで書いてください。
Discord IDのような巨大な数字列や、現実的な上限を超える金額は反映せず再入力待ちにします。

### 8. 手動支出（残高から差し引く）
残高を減らしてお小遣い帳にも同時記録する:
```text
@compass-bot 支出 500円 お菓子
```

### 9. 臨時収入（残高に加算）
```text
@compass-bot 入金 3000円 お年玉
```

### 10. 入出金台帳確認
直近10件の入出金履歴を表示する:
```text
@compass-bot 入出金履歴
@compass-bot 台帳確認
```

### 11. 今月の振り返り
当月の支出件数・合計額・満足度平均・Top3品目・現在残高を表示:
```text
@compass-bot 振り返り
```

### 12. 査定履歴確認
直近5件の査定金額（固定・臨時・合計）を一覧表示:
```text
@compass-bot 査定履歴
```

### 13. 目標貯金（最大5件）
目標設定（同名タイトルなら金額を上書き更新）:
```text
貯金目標 ゲーム機 30000円
貯金目標 旅行費 10000円
```
目標設定の補完入力でも金額は `円` 必須です。金額だけ先に入力した後に `残高おしえて` など別コマンドらしい文が来た場合は、目標名として保存せず再入力を促します。

進捗確認（全目標のプログレスバー表示）:
```text
目標確認
```

特定目標を削除:
```text
目標削除 ゲーム機
```

タイトル未指定で削除（一覧を表示してタイトルを案内）:
```text
目標削除
```

全目標を一括削除:
```text
目標全削除
```

---

### 親専用コマンド

### 14. 全体確認ダッシュボード
全ユーザーの残高・残高報告状況・最終支出日を一覧表示:
```text
全体確認
```

### 15. 支出傾向分析
過去3ヶ月の月別件数・満足度平均・Top5品目を表示:
```text
たろうの分析
全員の分析
```

### 16. 手動支給
```text
支給 たろう 700円
```

### 17. 残高調整（加算・減算）
```text
残高調整 たろう +500円
残高調整 たろう -300円
```

### 18. 設定変更（固定お小遣い・臨時上限）
```text
設定変更 たろう 固定 800円
設定変更 たろう 臨時 5000円
```
親が `りかのお小遣い金額を変えたい` のように自然文で送った場合は、誤操作防止のためこの明示コマンド形式を案内します。

### 19. 一括支給（全員の固定お小遣いを加算）
```text
一括支給
```

### 20. 他ユーザーの台帳確認
```text
@compass-bot たろうの台帳
```

### 21. 全チャンネルへの一斉アナウンス
`allow_channel_ids` の全チャンネルに任意メッセージを送信:
```text
アナウンス 今月の支給日は5日です
```

### 22. 使い方の送信（単体チャンネル）
コマンドを送ったチャンネル1つだけに使い方を送信:
```text
使い方の説明
```

### 26. Webダッシュボード アクセス承認（親のみ）
ユーザーからの申請を承認して仮パスワードを発行:
```text
web承認 たろう
```
承認すると Discord に仮パスワードが通知される。

---

### 25. 使い方の一斉通知（全チャンネル）
`allow_channel_ids` の全チャンネルへ使い方と初期設定を通知:
```text
使い方の説明と初期設定
```

### 23. 残高チェック案内の即時送信
`allow_channel_ids` の各チャンネルに今月の残高チェック案内を即時送信:
```text
@compass-bot 残高チェック送信
@compass-bot 月頭案内送信
```

### 24. リマインダーテスト
```text
@compass-bot reminder test
```

## Webダッシュボード

ブラウザから `https://compass.rwc.0t0.jp` にアクセスしてお小遣いの状況を確認できます。
**親・子供の両方が利用可能**です。ログイン後の表示内容はユーザー種別で自動的に切り替わります。

### 登録フロー（初回のみ）

1. **アクセス申請**
   - `https://compass.rwc.0t0.jp/compass-bot/register` を開く
   - ユーザー名を入力して申請
     - **親の場合**: `settings/users/*.json` の `name` フィールドと一致するユーザー名を入力
     - **子供の場合**: 同上（`name` フィールドと一致させること）

2. **親が Discord で承認**
   - Discord に申請通知が届く
   - 親が `web承認 [ユーザー名]` と送信
   - Discord に仮パスワードが通知される

3. **本パスワード設定**
   - `https://compass.rwc.0t0.jp/compass-bot/set_password` を開く
   - 仮パスワードを入力 → 本パスワード（8文字以上）を設定

4. **ログイン**
   - `https://compass.rwc.0t0.jp/compass-bot/login` またはトップページからログイン

### 表示内容

| ユーザー種別 | 表示内容 |
|---|---|
| 親（管理者） | 全ユーザーの残高・月額お小遣い・今月支出・低残高フラグ・貯金目標・残高報告状況、支給/残高調整/月額変更、承認待ち申請一覧 |
| 子供（一般） | 自分の残高・今月支出・直近5件の支出記録・貯金目標の進捗 |

> **管理者権限の自動付与**: Web 登録時のユーザー名が `settings/users/*.json` の `name` と一致し、かつその `discord_user_id` が `setting.json` の `parent_ids` に含まれる場合、パスワード設定時に自動的に管理者（親）として登録されます。

### 注意事項
- Web ユーザー名は登録済みの `name` フィールドと一致させること（親・子供ともに必須）
  - 子供: `settings/users/*.json` の `name`
  - 親: `settings/users/parents/*.json` の `name`
- 管理者権限は `parents/` ディレクトリへの登録で自動付与される（`parent_ids` は不要）
- セッションは7日間有効。期限切れ後は再ログインが必要

## ログ

`settings/system.json` の `log_dir` 配下に JSONL で保存されます。

- `{name}_events.jsonl`: 査定イベント（入力・返信・査定金額）
- `{name}_allowance_amounts.jsonl`: 査定金額ログ（`fixed`, `temporary`, `total`）
- `{name}_wallet_ledger.jsonl`: 残高の増減台帳
- `{name}_pocket_journal.jsonl`: お小遣い帳（支出記録）— `amount` フィールドは任意（省略可）
- `runtime_diagnostics.jsonl`: 実運用診断ログ（対象ユーザー補正、Gemini分類結果、処理ハンドラ、違和感タグ）
- `data/wallet_state.json`: 現在の帳簿残高
- `data/wallet_audit_state.json`: 監査依頼・初期設定/支出記録の待機状態
- `data/reminder_state.json`: 月次リマインド送信状態

`runtime_diagnostics.jsonl` の主な `issue_tags`:
- `gemini_low_confidence`: Gemini分類が低信頼で確認質問に入った
- `money_related_but_intent_none`: お金系の入力なのに雑談扱いになった可能性
- `reply_asks_clarification`: 返答が追加説明を求めており、会話が噛み合っていない可能性
- `parent_message_used_author_context`: 親発言を発言者本人の文脈で処理した
- `parent_message_used_child_channel_context`: 親発言を子どもチャンネル文脈で補正した

## 自動テスト

疑似Discordメッセージを `app.bot.on_message` に投入する統合テストを用意しています。

```bash
python3 tests/fake_discord_flow_tests.py --markdown テスト実施結果.md
```

親子ID重複、親の子どもチャンネル文脈、査定履歴、入出金履歴、振り返り、親ダッシュボード/分析、実運用診断ログ、親明示コマンド、金額入力の `円` 必須、pending優先順位、低信頼度確認、親専用情報の非表示を自動判定し、入力・応答・判定理由・追加対応要否を `テスト実施結果.md` に記録します。

実Geminiを呼び出して、Discord操作を再現するE2Eテストも用意しています。実Discordには送信せず、`.env` の `GEMINI_API_KEY` で Gemini を呼び、FakeChannel に返った応答を判定します。

```bash
.venv/bin/python tests/gemini_discord_replay_tests.py --markdown テスト実施結果.md
```

残高確認の表記揺れ、親/子の権限境界、入金、支出、財布チェック、初期設定、貯金目標、査定履歴、雑談、査定相談、巨大金額、プロンプト注入を含むケースを検証します。追加質問後の `わからない`、裸数字、曖昧金額、`円/えん/万円`、誤字表記も対象です。

Geminiの待ち時間は環境変数で調整できます。デフォルトではHTTP要求15秒、分類系8秒、通常返答/査定の総待ち40秒で打ち切り、Discordには再送案内を返します。

- `GEMINI_TIMEOUT_MS`
- `GEMINI_RETRY_ATTEMPTS`
- `GEMINI_SILENT_TIMEOUT_SEC`
- `GEMINI_PROGRESS_INTERVAL_SEC`
- `GEMINI_MAX_WAIT_SEC`

## 注意点

- `chat.require_mention=true` の場合はメンション必須です。
- `chat.require_mention=false` の自然会話モードでも、他ユーザー宛てメンションを含む発言には反応しません。
- `allow_channel_ids` を設定している場合、対象チャンネル以外では反応しません。
- Bot文面や査定ルールは `app/prompts.py` で調整できます。
- 個人情報を含む実設定（`settings/*.json`, `settings/users/*.json`）は `.gitignore` で除外し、テンプレート（`*.example.json`）のみ共有する運用を推奨します。
