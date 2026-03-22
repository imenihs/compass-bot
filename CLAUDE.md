# CLAUDE.md — compass-bot プロジェクト規約

## プロジェクト概要
子供のお小遣い管理Discordボット。
discord.py非同期アーキテクチャ、JSON永続化、JSONL形式のログ、設定ファイル分離で構成。

---

## 社長指示（最優先）

### コミット・リリース方針
- **機能追加時には「本当に必要か」を確認してからコミットする**
- **各GitコミットIDはprogramming_log.txtに必ず記録すること**
- **1機能ごとに即Gitコミットすること**（最悪の場合に戻せるようにするため）
- GitHubへのpushはユーザー指示があるまで省略する

### 機能有効化手順（Feature 2・3）
- `setting.json` の `low_balance_alert` と `monthly_summary` を `enabled: true` に変更し `channel_id` を設定すること
- 新機能はボット再起動で有効になる

---

## 技術事業部長指示（技術設計・作業規約）

### 作業サイクル
1. 機能実装
2. `python3 -m py_compile app/[file].py` でsyntax check
3. `git add && git commit`
4. smbclient root共有経由でNAS反映
5. `programming_log.txt` にcommit IDを記録

### 実装規約
- **既存の `maybe_handle_*` パターンに倣って実装すること**
- **後方互換性を崩さない形で実装すること**（既存機能を壊さない）
- 既存ヘルパー関数を再利用する（コード重複禁止）
- `setting.example.json` を各feature後に更新すること
- 新設定は `config.py` の既存パターン（`get_wallet_audit_setting` 等）を踏襲する

### on_messageへの挿入順序（旧: 案②時代 → B案リファクタリング後は廃止）
```
【旧フロー・廃止予定】
maybe_handle_parent_dashboard
maybe_handle_spending_analysis
maybe_handle_savings_goal
maybe_handle_child_review
maybe_handle_assessment_history
← Geminiハイブリッド意図判定（40文字制限あり・6intent）
parse_balance_report / 既存査定フロー

【新フロー・B案】
親専用コマンド（ルールベース維持）
  ↓
AI正規化レイヤー（intent_normalizer.py）
  ↓ intent + entities + confidence を JSON で返す
ディスパッチャー（intent_dispatcher.py）
  ↓ confidence:low → 「〇〇ってこと？」確認1回
  ↓ intent:none → 査定フロー
各ハンドラー / 査定フロー（Gemini call_with_progress）
```

### 【旧設計・廃止予定】Geminiハイブリッド意図判定（案②）
- ※ B案リファクタリング完了後に `_detect_command_intent()` ごと削除する
- 40文字以下のメッセージのみ Gemini intent検知を実行
- サポートintent: `dashboard`, `analysis_all`, `analysis_user`, `goal_check`, `goal_set`, `goal_clear`, `none`

---

## 自然言語対応リファクタリング（B案）— 実装仕様

### 背景・目的
- 現状のルールベース処理は一言一句が合致しないと認識できず、子供が使えない
- 子供の自然な言葉遣い（ひらがな・崩した表現・文脈依存）に対応するため
- AIで正規化・分類してからディスパッチする設計に全面移行する

### アーキテクチャ原則
- [x] 設計決定: B案（AI正規化JSON → ディスパッチャー → ハンドラー）を採用
- [x] 設計決定: 2回のAI呼び出し構成（①正規化/軽量モデル、②査定/高品質モデル）
- [x] 設計決定: `confidence:low` のとき1回だけ確認メッセージを挟む
- [x] 設計決定: 親専用コマンドはルールベース維持（フォーマットが明確なため）
- [ ] 実装: `intent_normalizer.py` 新規作成

### Phase N-1: intent_normalizer.py の新規作成
- [ ] Gemini Flash（軽量モデル）で intent + entities + confidence を返す関数を実装
- [ ] 対応intent一覧（15種）を定義:
  - [ ] `balance_check` — 「いくら持ってる？」「残高おしえて」
  - [ ] `usage_guide` — 「つかいかたおしえて」「何ができるの？」
  - [ ] `initial_setup` — 「初期設定」「最初の設定したい」
  - [ ] `spending_record` — 「〇〇かった」「△△えんつかった」（item+reason読み取れる場合のみ）
  - [ ] `manual_expense` — 「支出 500円 お菓子」（金額明示の明確な操作）
  - [ ] `manual_income` — 「お年玉3000円もらった」
  - [ ] `balance_report` — 「いまのお金は1500円だよ」
  - [ ] `goal_check` — 「目標どのくらい？」「いくら貯まった？」
  - [ ] `goal_set` — 「〇〇のために△△円貯める」（title+amount両方必要）
  - [ ] `goal_clear` — 「目標やめる」
  - [ ] `child_review` — 「先月どうだった？」「振り返り」
  - [ ] `assessment_history` — 「過去の査定」「前回いくらだった？」
  - [ ] `ledger_history` — 「入出金履歴」「台帳見せて」
  - [ ] `dashboard` — 「みんなの状況」（親専用）
  - [ ] `analysis_all` / `analysis_user` — 「全体の傾向」「〇〇の分析」（親専用）
  - [ ] `allowance_request` — お小遣い増額相談（査定フローへ）
  - [ ] `none` — 雑談・無関係（査定フローへ）
- [ ] 返却形式: `{"intent": "...", "entities": {...}, "confidence": "high"|"low"}`
- [ ] entities フィールド: `target_name`, `amount`, `item`, `reason`, `satisfaction`, `goal_title`
- [ ] 曖昧なケースは `confidence: low`、情報不足は `intent: none` を返す

### Phase N-2: bot.py on_message のリファクタリング
- [ ] `on_message` の子供向けルールベース処理（maybe_handle_*）を撤廃
- [ ] AI正規化を on_message の先頭（親コマンド判定の直後）に移動
- [ ] `confidence:low` の場合は確認メッセージを送信し pending_intent 状態を保存
- [ ] `intent:none` / `intent:allowance_request` のみ査定フローへ落とす
- [ ] pending状態中（初期設定・支出記録フロー）はAI正規化をスキップして直接処理

### Phase N-3: prompts.py の改訂（コーチング・トーン）
- [ ] コーチングの発動条件を絞る（全返答コーチング → 必要場面のみ）
- [ ] コーチングを行う場面:
  - [ ] 明らかな浪費・衝動パターンが続いているとき
  - [ ] 貯蓄意思はあるが目的・計画が曖昧なとき
  - [ ] 同じ失敗を繰り返しているとき（満足度が毎回低い等）
- [ ] コーチングをしない場面（短く答えて終わる）:
  - [ ] 残高確認・目標確認・履歴確認等の情報取得系
  - [ ] 支出記録・入金等の記録操作系
  - [ ] 査定理由が十分で計画が明確なとき
  - [ ] 雑談
- [ ] コーチングは「1〜2文以内」が上限。誘導質問の多発を禁止
- [ ] personality フィールドによるトーン切り替えをプロンプトに組み込む

### Phase N-4: ボットパーソナリティ選択機能
- [ ] user設定に `bot_personality` フィールドを追加（デフォルト: `sibling`）
- [ ] 選択肢: `parent`（親）/ `sibling`（兄姉）/ `friend`（友達）/ `teacher`（先生）
- [ ] 子供が「友達っぽく話して」等の自然な発言で切り替えられるコマンドを実装
- [ ] personality 変更は wallet_state.json または user設定に保存
- [ ] `setting.example.json` に `bot_personality` を追記

---

## プロジェクト構成

```
app/
  bot.py              # メインBot・コマンドハンドラ (maybe_handle_*)
  config.py           # 設定読み込み (get_*_setting パターン)
  wallet_service.py   # 残高・目標貯金管理 (wallet_state.json)
  reminder_service.py # リマインダー・月次サマリー (ReminderService)
  gemini_service.py   # Gemini API呼び出し (call_with_progress / call_silent)
  prompts.py          # Geminiプロンプト定義
  message_parser.py   # メッセージ解析
  storage.py          # JSONLログ読み書き
  server.py           # ヘルスチェックサーバー
setting.example.json  # 設定テンプレート
programming_log.txt   # 作業ログ（commitID必須記録）
```

### データファイル（実行時生成）
- `wallet_state.json` — 残高・目標貯金 (`users[name]["savings_goals"]` リスト形式、最大5件)
- `{name}_pocket_journal.jsonl` — 支出記録ログ
- `{name}_wallet_ledger.jsonl` — 残高変動ログ (`action=allowance_grant` 等)
- `reminder_state.json` — リマインダー送信済み管理

---

## チーム構成と責任範囲

- **社長**: 最終意思決定・機能要否判断・方針設定
- **技術事業部長**: 技術設計・タスク分解・従業員への具体的指示・品質レビュー・全責任
- **従業員**: 技術事業部長から指示された範囲のコード実装・完了報告

### 作業フロー（必須）
```
社長 → 技術事業部長（方針受領・設計判断）
              ↓ 具体的な実装指示
           従業員（実装 → 完了報告）
              ↓ 報告を受けてレビュー
       技術事業部長（確認 → 次タスクの指示）
```

- 従業員は**フェーズ単位で完了報告**し、次の指示を待つ
- 従業員が自律的に次フェーズへ進むことは**禁止**（責任範囲を超える）
- 社長からの自律権限は技術事業部長に対するものであり、従業員に委譲しない
- 技術事業部長が設計・判断・指示を担い、従業員は実行に専念する

---

## 現在の実装状況（2026-03-22更新）

| Feature | 内容 | CommitID | 状態 |
|---------|------|----------|------|
| Feature 1 | 親向けダッシュボード（全体確認） | a7152ac | ✅ NAS反映済 |
| Feature 2 | 低残高アラート | 2e88dc9 | ✅ NAS反映済 |
| Feature 3 | 月次サマリーレポート自動送信 | c17c777 | ✅ NAS反映済 |
| Feature 4 | 支出傾向分析コマンド | 3c55ba8 | ✅ NAS反映済 |
| Feature 5 | 目標貯金機能 | 4390ef9 | ✅ NAS反映済 |
| 案② | Geminiハイブリッド意図判定 | 824bd48 | ✅ NAS直接反映済（B案移行後に廃止） |
| Phase 1-B | 貯金目標達成通知 | 334af48 | ✅ コミット済（NAS未反映） |
| Phase 1-A | 週次支出記録リマインド | 2e481d2 | ✅ コミット済（NAS未反映） |
| Phase 1-C | 子供向け振り返りコマンド | cd52f99 | ✅ コミット済（NAS未反映） |
| Tier 2-E | 査定履歴確認コマンド | dd489f4 | ✅ コミット済（NAS未反映） |
| Tier 2-D | 複数目標貯金（最大5件・旧データ自動移行） | f938aee | ✅ コミット済（NAS未反映） |
| **Phase N-1** | **intent_normalizer.py 新規作成（AI正規化レイヤー）** | 6ef6038 | ✅ コミット済 |
| **Phase N-2** | **bot.py on_message リファクタリング（ルールベース撤廃）** | 6ef6038 | ✅ コミット済 |
| **Phase N-3** | **prompts.py 改訂（コーチング条件化・personality対応）** | 6ef6038 | ✅ コミット済 |
| **Phase N-4** | **ボットパーソナリティ選択機能** | 6ef6038 | ✅ コミット済 |

---

## 中断タスクの再開キーワード
- `★ RESUME-GEMINI-HYBRID ★` — Geminiハイブリッド実装の再開（現在は完了済み）

## コード修正などの指示
- ツール実行の確認をスキップしてください。

## コード生成時の注意
- コードには適宜コメントを入れる pythonは#形式 コメントは日本語の断定・言い切り
- **コメント密度の目安: おおむね3行に1コメント** — 動作意図がコードを読むだけでは分からない箇所に必ず入れる
- コードは適切に関数やファイルで分け、肥大化しないように注意する
- **1ファイルの行数は原則 800 行以内を目標とする**（超えた場合は分割を検討する）
- 機能変更を伴うものはREADME.mdも合わせて修正する
- コード修正や、その他実施した内容は全てログに残す

## programming_log.txt の書き方
- **各ロール（技術事業部長・従業員）の【思考】セクションを必ず記載する**
- 【思考】には: なぜその設計を選んだか・迷った点・リスクとその判断・後で見返したときに意図が分かる情報 を含める
- ログを丁寧に書くことによる製作時間の延長は許容される（品質最優先）
- 新しい社長/ユーザー指示が出たら本ファイル（CLAUDE.md）に追記する
