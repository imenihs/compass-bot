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

### NAS反映方針
- NASのweb共有（force user=www-data）では `app/` への書き込みが拒否される
- **NAS反映は必ずroot共有経由で行うこと**
- NAS反映コマンド:
  ```
  smbclient //192.168.10.210/root -U AkiraShimizu%ashi1017 \
    -c "cd web/documents/compass-bot/[path]; put [local_file] [remote_file]"
  ```

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

### on_messageへの挿入順序（現在の順）
```
maybe_handle_parent_dashboard
maybe_handle_spending_analysis
maybe_handle_savings_goal
← Geminiハイブリッド意図判定
parse_balance_report / 既存査定フロー
```

### Geminiハイブリッド意図判定（案②）アーキテクチャ
- **40文字以下のメッセージのみ** Gemini intent検知を実行（長文はお小遣い相談と判断しスキップ）
- ルールベースでマッチしたコマンドは intent検知をスキップ（二重処理なし）
- `call_silent()` を使用（「考え中...」を表示しない軽量呼び出し）
- intent が `"none"` の場合は既存の査定フローへ落とす
- サポートintent: `dashboard`, `analysis_all`, `analysis_user`, `goal_check`, `goal_set`, `goal_clear`, `none`

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
- `wallet_state.json` — 残高・目標貯金 (`users[name]["savings_goal"]`)
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

## 現在の実装状況（2026-03-02）

| Feature | 内容 | CommitID | 状態 |
|---------|------|----------|------|
| Feature 1 | 親向けダッシュボード（全体確認） | a7152ac | ✅ NAS反映済 |
| Feature 2 | 低残高アラート | 2e88dc9 | ✅ NAS反映済 |
| Feature 3 | 月次サマリーレポート自動送信 | c17c777 | ✅ NAS反映済 |
| Feature 4 | 支出傾向分析コマンド | 3c55ba8 | ✅ NAS反映済 |
| Feature 5 | 目標貯金機能 | 4390ef9 | ✅ NAS反映済 |
| 案② | Geminiハイブリッド意図判定 | 824bd48 | ✅ NAS直接反映済 |

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
