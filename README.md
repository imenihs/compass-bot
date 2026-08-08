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

- AI（claude CLI）が会話を主導し、お小遣い査定（固定 / 臨時）を提案する（親承認で支給）
- お小遣い帳（支出記録）を保存する
- 財布残高（ウォレット）を管理する
- 支出理由・満足度・記録漏れから、親子会話カードと子どもの小さなチャレンジを作る
- 親が子ども別のAIフォロー方針、会話カード反応、成長行動プランを管理できる
- 毎月リマインド（支給前通知）と残高監査を行う
- 親向けダッシュボード・低残高アラート・月次サマリー・傾向分析・目標貯金に対応
- ブラウザから閲覧できる Webダッシュボード（`compass.example.com`）を搭載

## 主な機能

### 会話Bot（AIが会話を主導）
- 子どもは Discord で自然な言葉で話しかけるだけ。AI が会話を主導し、文脈は会話が途切れても続きます。
- 残高・支出・入金・目標・査定などお金を動かす処理は AI が直接計算せず、Bot 側で厳密に実行します（金額の誤りが残高に反映されない設計）。
- 増額の相談には、AI が突き放さず一緒に計画を考え、正当な理由があれば査定額を「提案」します。実際の支給は親の承認で確定します（子どもが自分でお金を増やせない）。
- 支給できる金額には上限があり、Bot 側で必ず守られます。
- 親は `名前の代理` で子どもに代わって投稿できます。

### お小遣い帳
- `使った物` / `理由` / `満足度（0〜10）` / `金額（任意）` を記録します。
- ラベル形式でも簡易形式でも入力でき、`支出 500円 お菓子` のように残高を減らしつつ同時に記録することもできます。

### ウォレット（残高管理）
- 初期設定で現在の所持金を登録します。
- 残高報告との差分を確認し、記録漏れに気づく材料にします。

### 定期運用（自動）
- 毎月の支給前に固定お小遣いのリマインドを送ります。
- 毎月決まった時刻に、対象チャンネルへ残高報告のお願いを送ります。

### 親向け機能
- **全体確認**: 全員の残高・報告状況・最終支出日を一覧表示します。
- **支出傾向分析**: 過去3ヶ月の件数・満足度平均・よく買った品目を表示します。
- **低残高アラート**（自動）: 残高が設定した閾値を下回ると親チャンネルに通知します。
- **月次サマリー**（自動）: 毎月、前月の支出統計を親チャンネルへ送ります。

### 目標貯金
- 目標を設定し、進捗をプログレスバーで確認できます（1人あたり複数登録可）。

### 学習支援・親子の会話づくり
- 支出の記録から、親子で話すきっかけになるカードや、子どもへの小さな行動提案を作ります。
- 子どもには内部のメモや方針は見せず、一度に1つだけ前向きな行動を提案します。
- 親は子どもごとに、AI のフォロー方針（重視する観点・声かけの強さ）を設定できます。

### 能動的な声かけ
- 記録が途切れている、前に決めた行動に反応がない、といったときに、Bot から子どもへ短い声かけを送ります（有効化した場合）。
- 「放置」「サボり」といった責める表現は使わず、戻りやすい小さな行動だけを促します。

### Webダッシュボード（ブラウザ）
- 親・子どもの両方が利用でき、表示内容は自動で切り替わります。
- 親: 全員の残高・お小遣い・支出・報告状況の一覧管理、支給・残高調整、承認待ち申請の処理など。
- 子ども: 自分の残高・支出記録・貯金目標の進捗の閲覧。
- 申請 → Discord で親が承認 → 仮パスワード発行 → 本パスワード設定 の流れで登録します。

### 安全・プライバシー
- お金を動かす処理には上限と本人確認があり、なりすましや過剰な自己入金を防ぎます。
- エラー時は内部の原因を利用者に見せず、やさしい案内を返します。
- 子どもへの声かけは、他人や兄弟との比較・人格否定・強い叱責をしません。

## ディレクトリ構成

- `app/`: Bot 本体。Discord 連携、AI 会話、残高・支出・目標の管理、定期通知、Web サーバーなど。
- `templates/`: Web ダッシュボードの HTML テンプレート。
- `config/wallet_mcp.json`: AI が財布操作ツールを呼ぶための設定（実環境のパスに書き換えて使う）。
- `settings/`: 設定ファイル。実運用の設定・ユーザー情報は各自が作成します（テンプレートの `*.example.json` のみ同梱）。
- `scripts/`: 常駐用の systemd unit サンプルなど。
- `data/logs/`: 実行時に生成されるログ（残高台帳・お小遣い帳・イベント等）。リポジトリには含まれません。

## 必要環境

- Python 3.11+
- Discord Bot Token（Discord Developer Portal で作成）
- **claude CLI**（会話の主役。ローカルにインストールし、ログイン済みであること）

会話は claude CLI が主導します。金額を動かす処理だけを Python の MCP wallet tool（`app/mcp_wallet.py`）が担当します。外部の生成AI APIキー（Gemini 等）は不要です。

依存関係（`requirements.txt`）:
- `discord.py`
- `fastapi` / `uvicorn`（Webダッシュボード）
- `python-dotenv`
- `httpx`
- `jinja2`（テンプレートエンジン）
- `python-multipart`（フォーム送信の受け取り）

## セットアップ

### 0. claude CLI を用意する

会話処理には claude CLI が必要です。インストールしてログインし、コマンドラインから会話できる状態にします。

```bash
# claude CLI をインストール（公式の手順に従う）
which claude          # 例: /usr/local/bin/claude にあることを確認
claude --version      # 動作確認
```

Bot は既定で `/usr/local/bin/claude` を使います。別の場所にある場合は `.env` の `COMPASS_CLAUDE_BIN` で指定します。systemd 配下では PATH が最小限になりがちなので、絶対パス指定を推奨します。

### 1. 依存インストール

```bash
git clone <このリポジトリ>
cd compass-bot
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

### 2. 環境変数設定（`.env`）

`.env.example` をコピーして値を入れます。

```bash
cp .env.example .env
```
```env
DISCORD_BOT_TOKEN=xxxxxxxx
# COMPASS_CLAUDE_BIN=/usr/local/bin/claude   # claude CLI が別の場所にあるときだけ設定
```

Discord Bot Token は [Discord Developer Portal](https://discord.com/developers/applications) で Bot を作成し、`MESSAGE CONTENT INTENT` を有効にして取得します。作成した Bot を対象サーバーへ招待してください。

### 3. MCP wallet 設定（`config/wallet_mcp.json`）

claude CLI が財布操作ツールを呼べるよう、MCP サーバのパスを実環境へ合わせます。**リポジトリのパス（`cwd`）と venv の python パスを自分の環境に書き換えてください**。

```json
{
  "mcpServers": {
    "wallet": {
      "command": "/path/to/compass-bot/.venv/bin/python",
      "args": ["-m", "app.mcp_wallet"],
      "cwd": "/path/to/compass-bot"
    }
  }
}
```

### 4. 設定ファイルを作る

テンプレート（`*.example.json`）をコピーして実設定を作ります。実設定は個人情報を含むため `.gitignore` 済みで、リポジトリには入りません。

```bash
cp settings/setting.example.json settings/setting.json
# 子どもユーザーを追加（下記「ユーザーの追加」を参照）
cp settings/users/user.example.json settings/users/<子どもの名前>.json
# 親ユーザーを追加
cp settings/users/parents/parent.example.json settings/users/parents/<親の名前>.json
```

その後、[ユーザーの追加](#ユーザーの追加) の手順で中身を編集します。

## 起動

### 手動起動（開発時）
```bash
source .venv/bin/activate
python3 -m app.bot
```
Web ダッシュボードは Bot と同一プロセスで自動起動します（既定 port 8765）。

### systemd で常駐（本番）

`scripts/compass.service.example` をコピーし、パス・ユーザー名を自分の環境へ書き換えて `/etc/systemd/system/compass.service` に置きます。

```bash
sudo cp scripts/compass.service.example /etc/systemd/system/compass.service
sudo nano /etc/systemd/system/compass.service   # WorkingDirectory / ExecStart / User を実環境へ
sudo systemctl daemon-reload
sudo systemctl enable --now compass.service
sudo systemctl status compass.service
```

> **重要**: コード変更を本番へ反映するには、コミットだけでなく `sudo systemctl restart compass.service` が必要です（実行中プロセスは古いコードのまま動き続けます）。

## Webダッシュボードを公開する

Web ダッシュボードは Bot と同じプロセスで **`127.0.0.1:8765`（ローカルのみ）** で待ち受けます。外部のブラウザからアクセスするには、Web サーバー（Apache / Nginx）をリバースプロキシとして前段に置き、`https://<あなたのドメイン>/compass-bot/` を `127.0.0.1:8765` へ転送します。

ローカル bind のままにするのは、アプリを直接インターネットへ晒さず、HTTPS 終端・アクセス制御を Web サーバー側に任せるためです。

### Apache の例

```apache
# https の VirtualHost 内
ProxyPreserveHost On
ProxyPass        /compass-bot/ http://127.0.0.1:8765/compass-bot/
ProxyPassReverse /compass-bot/ http://127.0.0.1:8765/compass-bot/
```

必要なモジュールを有効化します。

```bash
sudo a2enmod proxy proxy_http
sudo systemctl reload apache2
```

同梱の `.htaccess` は、ソースコード・設定・データファイルへの直接アクセスを拒否します。ドキュメントルート配下にアプリを置く構成では、この `.htaccess` が効くようにしてください（`AllowOverride All`）。

### Nginx の例

```nginx
location /compass-bot/ {
    proxy_pass http://127.0.0.1:8765/compass-bot/;
    proxy_set_header Host $host;
    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    proxy_set_header X-Forwarded-Proto $scheme;
}
```

### HTTPS

ログインとパスワード設定を扱うため、公開時は必ず HTTPS にしてください（[Let's Encrypt](https://letsencrypt.org/) などで証明書を取得）。設定できたら、`settings/setting.json` の `web_base_url` を公開 URL（例: `https://compass.example.com`）に合わせます。

## ユーザーの追加

Compass Bot は「子ども（一般ユーザー）」と「親（管理者）」の2種類のユーザーを扱います。

**推奨の流れ**: 最初の親を1人だけ手作業で JSON 作成 → Bot と Web を起動 → その親で Web ダッシュボードにログイン → **あとは全員ブラウザの「＋ 子どもを追加」「＋ 親を追加」から追加**します。2人目以降は JSON を手で書く必要はありません。

### 最初の親を1人だけ用意する（唯一の手作業）

Web にログインできる管理者が1人も居ない状態からのブートストラップだけは、JSON を手で作ります。

```bash
cp settings/users/parents/parent.example.json settings/users/parents/おとうさん.json
```
```json
{
  "name": "おとうさん",
  "discord_user_id": 222222222222222222
}
```
- `discord_user_id`: Discord で開発者モードを有効にし、ユーザーを右クリック →「IDをコピー」で取得（17〜19桁）。
- `parents/` に置くだけで管理者になります（`setting.json` への追加は不要）。
- 作成後、Bot を起動します。この親で [Webダッシュボードの登録フロー](#登録フロー初回のみ) を済ませればログインできます。

### ブラウザから追加する（2人目以降・推奨）

ログイン後、「全ユーザー一覧」の上にある **「＋ 子どもを追加」「＋ 親を追加」** から、名前・Discord ID などを入力して追加します。既存ユーザーの編集も同じ画面の「設定」から行えます。

- **子どもを追加**しても再起動は不要です（すぐ会話できます）。
- **親を追加**した場合は、反映のため `sudo systemctl restart compass.service` が必要です。
- 名前や Discord ID が既存と重複する場合、同じ種類の Discord ID が重複する場合は、その場でエラー表示されます。

以降は、手作業で JSON を書きたい場合の参考です。通常はブラウザからの追加で十分です。

### （参考）子どもの JSON を手で作る

`settings/users/<名前>.json` を作ります（ファイル名は任意。`name` フィールドが実際のキーになります）。

```bash
cp settings/users/user.example.json settings/users/たろう.json
```

```json
{
  "name": "たろう",
  "discord_user_id": 111111111111111111,
  "age": 12,
  "gender": "male",
  "bot_personality": "sibling",
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

- `name`: 子どもの名前。Discord の会話・残高・ログのキーになる。**他のユーザーと重複させない**。
- `discord_user_id`: その子の Discord ユーザーID（17〜19桁の数値）。Discord で開発者モードを有効にし、ユーザーを右クリック →「IDをコピー」で取得。
- `age`: 年齢。会話の言葉づかい（漢字の量・文の長さ）を年齢に合わせる。
- `gender`: `male` / `female` / `unspecified`。
- `bot_personality`: 会話の口調。`sibling`（年上のきょうだい・既定）/ `friend` / `teacher` / `parent`。
- `fixed_allowance`: 毎月の固定お小遣い（円）。
- `temporary_max`: 1回の臨時査定で提案できる上限（円）。
- `fixed_increase_cap` / `penalty_cap`: 固定増額の上限 / 記録漏れ時の調整上限（円）。
- `keywords`: 査定時に用途を判断するための語。投資的/娯楽/危険の3分類。

親を手作業で足す場合は `settings/users/parents/<名前>.json` に `name` と `discord_user_id` の2つを書くだけです（上のブートストラップと同じ形式）。

> **親の追加・変更・削除は再起動が必要**: 親の判定は Bot 起動時に一度だけ読み込みます。反映するには `sudo systemctl restart compass.service` してください。子どもユーザーは毎回読み直すため再起動は不要です。

### 重要な前提: 親IDと子IDは重複させない

同じ Discord ユーザーIDを親と子の両方に登録しないでください。Bot は Discord ID で本人を判定するため、重複するとルーティングが壊れます（親の発話が弾かれる等）。動作確認用に自分を子として試したい場合は、テスト専用の別ユーザーを用意してください。

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

親ダッシュボードでは、全ユーザー一覧の各行にある `設定` からモーダルを開き、子ども別に以下を編集できます。
- `name`, `discord_user_id`, `age`, `gender`, `bot_personality`
- `fixed_allowance`, `temporary_max`, `fixed_increase_cap`, `penalty_cap`
- `keywords.investment`, `keywords.fun`, `keywords.danger`

親ユーザー設定も、同じ全ユーザー一覧の親行にある `設定` からモーダルで `name` と `discord_user_id` を編集できます。

全ユーザー一覧の並び順は、各行のドラッグハンドルで入れ替えて `並び順を保存` から保存できます。保存先は `settings/setting.json` の `web_dashboard.user_order` です。未保存時の初期表示は、設定ファイルの列挙順ではなく名前順で安定させます。ドラッグ中にポインタを取りこぼした場合も、ページ全体の `pointerup` / `blur` でドラッグ状態を解除します。

モーダルは画面外クリックでは閉じず、`Esc` または `キャンセル` で閉じます。未保存変更がある場合は破棄確認を表示し、`name` または `discord_user_id` の変更保存時は重要項目の保存確認を表示します。設定フォームまたは並び順に未保存変更がある状態でリロード・ページ離脱しようとした場合は、ブラウザの確認を表示します。

ダッシュボードのカード幅はPCで最大1440pxまで広げ、タブレットとスマホでは画面幅に合わせて余白とカードpaddingを縮めます。列数の多い全ユーザー一覧は、狭い画面でカード幅を壊さず横スクロールします。操作後の成功・失敗メッセージは画面下部のトーストで表示し、5秒後に自動で閉じます。トースト表示後はブラウザ履歴から `msg` / `error` クエリを削除するため、リロードしても同じ通知は再表示されません。

Discord ID は桁落ちを避けるため画面では文字列入力として扱い、保存時にサーバー側で整数へ変換します。`name` を変更した場合は、ウォレット状態、監査状態、Webユーザー名、学習支援状態など名前キーのデータを可能な範囲で移行します。

### `settings/setting.json`
- `web_base_url`: WebダッシュボードのベースURL（例: `https://compass.example.com`）
- `allow_channel_ids`: Botが反応するチャンネルID配列
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
- `child_income_report`: 子どもの自己申告入金（臨時入金）の上限設定
  - `max_amount`: 1回の自己申告で反映できる上限額（円）。既定 5000。0 以下で上限なし
  - 上限を超えた場合は残高を変えず、おうちの人に `残高調整` で記録してもらう案内を返す
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
  - 過去7日間に支出記録がないユーザーにのみ送信する。`allow_channel_ids` のメンバー情報またはチャンネル名から子ども別送信先を推定する
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
親は `はなの残高おしえて` のように子どもの名前を明示して確認することもできます。

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
親が `はなのお小遣い金額を変えたい` のように自然文で送った場合は、誤操作防止のためこの明示コマンド形式を案内します。

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

ブラウザから `https://compass.example.com` にアクセスしてお小遣いの状況を確認できます。
**親・子供の両方が利用可能**です。ログイン後の表示内容はユーザー種別で自動的に切り替わります。

### 登録フロー（初回のみ）

1. **アクセス申請**
   - `https://compass.example.com/compass-bot/register` を開く
   - ユーザー名を入力して申請
     - **親の場合**: `settings/users/*.json` の `name` フィールドと一致するユーザー名を入力
     - **子供の場合**: 同上（`name` フィールドと一致させること）

2. **親が Discord で承認**
   - Discord に申請通知が届く
   - 親が `web承認 [ユーザー名]` と送信
   - Discord に仮パスワードが通知される

3. **本パスワード設定**
   - `https://compass.example.com/compass-bot/set_password` を開く
   - 仮パスワードを入力 → 本パスワード（8文字以上）を設定

4. **ログイン**
   - `https://compass.example.com/compass-bot/login` またはトップページからログイン

### 表示内容

| ユーザー種別 | 表示内容 |
|---|---|
| 親（管理者） | 全ユーザーの残高・月額お小遣い・今月支出・低残高フラグ・貯金目標・残高報告状況、表示順保存、支給/残高調整、承認待ち申請一覧 |
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
- `runtime_diagnostics.jsonl`: 実運用診断ログ（会話処理の分岐や違和感の記録）
- `data/`: 残高・監査・リマインド・会話セッションなどの実行時状態

これらは実行時に自動生成され、リポジトリには含まれません。

## 環境変数（任意調整）

| 変数 | 既定 | 用途 |
|---|---|---|
| `COMPASS_CLAUDE_BIN` | `/usr/local/bin/claude` | claude CLI 実行ファイルのパス |

金額の安全弁（自己申告入金の回数/日次/月次上限、査定の各上限）は環境変数ではなく `settings/setting.json` と各ユーザー設定で管理します。

## 注意点

- `chat.require_mention=true` の場合はメンション必須です。
- `chat.require_mention=false` の自然会話モードでも、他ユーザー宛てメンションを含む発言には反応しません。
- `allow_channel_ids` を設定している場合、対象チャンネル以外では反応しません。
- 個人情報を含む実設定（`settings/setting.json`, `settings/users/*.json`, `settings/users/parents/*.json`）は `.gitignore` で除外し、テンプレート（`*.example.json`）のみ共有します。
