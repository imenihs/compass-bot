#!/bin/bash
# AI振る舞いのシナリオ網羅テスト。子供の典型発話を実claudeへ投げ、
# 記録用MCPサーバが捕捉した tool 呼び出しを期待値と照合する。
# 非決定性を見るため各シナリオを N 回実行する。
set -u
REPO=${REPO:-/web/compass-bot}
# 作業用の隔離ディレクトリ。特定セッションのパスを固定すると他環境で動かないため、
# 既定は mktemp 相当の一時パスにし、必要なら env SB で上書きする。
SB=${SB:-/tmp/compass-scenario-$$}
REC=$SB/rec_mcp.py
RUNS=${RUNS:-1}

rm -rf "$SB"; mkdir -p "$SB/settings/users/parents" "$SB/data"
cp /web/compass-bot/tests/ai_behavior/rec_mcp.py "$REC"

# 隔離ユーザー: たろう(10歳, 固定増額cap100)
cat > "$SB/settings/users/tarou.json" <<'EOF'
{"name":"たろう","age":10,"discord_user_id":111,"bot_personality":"sibling","fixed_increase_cap":100}
EOF
cp "$REPO/settings/setting.example.json" "$SB/settings/setting.json"
cat > "$SB/settings/system.json" <<EOF
{"log_dir":"$SB/data"}
EOF
# 初期残高1000円
echo '{"users":{"たろう":{"expected_balance":1000}},"applied_operation_keys":{}}' > "$SB/data/wallet_state.json"

# MCP config(記録サーバを起動)
cat > "$SB/mcp.json" <<EOF
{"mcpServers":{"wallet":{"command":"$REPO/.venv/bin/python","args":["$REC"],"env":{"COMPASS_TEST_ROOT":"$SB","COMPASS_REC_LOG":"$SB/rec.jsonl","COMPASS_ACTIVE_CHILD":"たろう","COMPASS_TEST_ROOT":"$SB"}}}}
EOF

SYS='あなたは子ども「たろう」(10歳)のお小遣い管理を手伝うやさしい会話ボット。【最重要】子どもが「◯円つかった/買った」と言ったら雑談で流さず必ずrecord_expenseを呼ぶ。「◯円もらった」なら必ずrecord_income。「◯円ためたい/目標」なら必ずset_savings_goal。残高を聞かれたら必ずget_balance。お金の話は「ただの報告」と思っても例外なくツールを呼ぶ。ツールを呼ばずに「記録したよ」と言うのは禁止。査定の支給はpropose_allowanceで提案(親承認が要る)。自分で金額を計算しない。operation_keyは毎回ちがう一意な文字列。相手はたろう本人。お金がまったく出てこない雑談だけツールを使わず会話する。'
ALLOWED="mcp__wallet__get_balance,mcp__wallet__record_expense,mcp__wallet__record_income,mcp__wallet__get_savings_goals,mcp__wallet__set_savings_goal,mcp__wallet__propose_allowance"

# シナリオ: "発話|期待tool(空=tool呼ばない雑談)"
SCENARIOS=(
  "300円のおかしを買ったよ|record_expense"
  "おばあちゃんに500円もらった|record_income"
  "いまの残高いくら？|get_balance"
  "ゲーム機を30000円ためたい|set_savings_goal"
  "貯金の目標おしえて|get_savings_goals"
  "今日は学校でリレー走ったよ|"
  "おてつだいをたくさんがんばったからお小遣いあげて|propose_allowance"
  "アイスが好きなんだ|"
)

PASS=0; TOTAL=0
echo "=== AI振る舞いシナリオテスト (各${RUNS}回) ==="
for entry in "${SCENARIOS[@]}"; do
  utt="${entry%%|*}"; expect="${entry#*|}"
  for run in $(seq 1 "$RUNS"); do
    TOTAL=$((TOTAL+1))
    rm -f "$SB/rec.jsonl"; touch "$SB/rec.jsonl"
    # 残高を毎回リセット(シナリオ独立)
    echo '{"users":{"たろう":{"expected_balance":1000}},"applied_operation_keys":{}}' > "$SB/data/wallet_state.json"
    rm -f "$SB/data/payout_requests.json"
    timeout 90 claude -p "$utt" --append-system-prompt "$SYS" \
      --mcp-config "$SB/mcp.json" --allowedTools "$ALLOWED" \
      --output-format json < /dev/null > "$SB/out.json" 2>/dev/null
    # 記録されたtool
    called=$(python3 -c "
import json
tools=[]
try:
    for line in open('$SB/rec.jsonl'):
        line=line.strip()
        if line: tools.append(json.loads(line))
except: pass
def fmt(t):
    a=t.get('args',{})
    amt=a.get('amount') or a.get('target_amount') or ((a.get('fixed',0) or 0)+(a.get('temporary',0) or 0)) or ''
    return t['tool']+(f'({amt})' if amt else '')
print(','.join(fmt(t) for t in tools) if tools else '(none)')
")
    # 判定
    if [ -z "$expect" ]; then
      # 雑談期待: toolを呼ばないのが正解
      if [ "$called" = "(none)" ]; then ok=1; else ok=0; fi
    else
      # 期待toolが呼ばれていれば正解
      if echo "$called" | grep -q "$expect"; then ok=1; else ok=0; fi
    fi
    if [ "$ok" = 1 ]; then PASS=$((PASS+1)); mark="OK"; else mark="NG"; fi
    printf "  [%s] «%s» expect=%-18s called=%s\n" "$mark" "$utt" "${expect:-（雑談）}" "$called"
  done
done
echo "=== 結果: $PASS/$TOTAL ==="
