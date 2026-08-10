#!/bin/bash
# 本番のプロンプトと tool 定義そのままで、AI が正しい tool を選ぶかを検証する。
#
# **既存の scenario_runner.sh との違い**:
#   あちらは system prompt をシェル内にベタ書きしており、本番と別物になっていた。
#   （「◯円つかった」という古い形式のまま残っていた）
#   本番のプロンプトが変わっても気づけないので、こちらは app から直接読み込む。
#
# 検証の狙いは「決まった言い方でなくても呼べるか」。
# 2026/08/11 に、子が「今のお金56563」と書いても tool が呼ばれない事故があった。
# description が「『財布に3000円あった』のように」と例を並べていたのが原因。
# 例に無い言い回しを意図的に混ぜてある。
set -u
REPO=${REPO:-/web/compass-bot}
SB=${SB:-/tmp/compass-verify-$$}
RUNS=${RUNS:-1}

rm -rf "$SB"; mkdir -p "$SB/settings/users/children" "$SB/settings/users/parents" "$SB/data"
cp "$REPO/tests/ai_behavior/rec_mcp.py" "$SB/rec_mcp.py"

cat > "$SB/settings/users/children/tarou.json" <<'EOF'
{"name":"たろう","age":10,"discord_user_id":111,"fixed_allowance":1000,"temporary_max":3000}
EOF
cp "$REPO/settings/setting.example.json" "$SB/settings/setting.json"
cat > "$SB/settings/system.json" <<EOF
{"log_dir":"$SB/data"}
EOF
echo '{"users":{"たろう":{"expected_balance":5000}},"applied_operation_keys":{}}' > "$SB/data/wallet_state.json"

# 本番の system prompt をそのまま取り出す
SYS=$(cd "$REPO" && python3 -c "
import sys; sys.path.insert(0,'.')
from app.conv.ai_conversation import _build_system_prompt
print(_build_system_prompt({'name':'たろう','age':10,'fixed_allowance':1000}, 5000))
")
# 本番の許可リストをそのまま使う
ALLOWED=$(cd "$REPO" && python3 -c "
import sys; sys.path.insert(0,'.')
from app.conv.ai_conversation import ALLOWED_WALLET_TOOLS
print(','.join(ALLOWED_WALLET_TOOLS))
")

cat > "$SB/mcp.json" <<EOF
{"mcpServers":{"wallet":{"command":"python3","args":["$SB/rec_mcp.py"],
"env":{"COMPASS_REC_LOG":"$SB/rec.jsonl","COMPASS_ACTIVE_CHILD":"たろう",
"COMPASS_TEST_ROOT":"$SB"}}}}
EOF

# 発話|期待するtool（空=呼ばないのが正解 / |? = 呼んでも呼ばなくてもよい）
SCENARIOS=(
  # --- 例に無い言い回しで残高報告できるか（今回の事故そのもの）---
  "今のお金5000|report_wallet_balance"
  "かぞえたら4800円だった|report_wallet_balance"
  "さいふの中これだけ 5000円|report_wallet_balance"
  # --- 聞いてきた場合は get_balance（取り違えないか）---
  "いまいくらある？|get_balance"
  "のこりいくら|get_balance"
  # --- 支出・収入 ---
  "300円のおかしを買ったよ|record_expense"
  "きのう文房具に120円つかった|record_expense"
  "おばあちゃんに500円もらった|record_income"
  # --- 買う前の相談では記録しない ---
  "500円のおかし買おうかな|"
  # --- 目標。**呼ばずに聞き返すのが正解**（お金が動かないので急がなくてよい／
  #     「ゲーム機」だけでは何を買うか曖昧なので確認する）。
  #     期待値を set_savings_goal にしていたのは、こちらの誤りだった ---
  "ゲーム機を30000円ためたい|"
  "スイッチを30000円ためたい|?"
  # --- 取られた場合は支出にしない（被害者を二重に損させない）---
  "友だちに500円取られた|record_money_safety_concern"
  # --- 雑談ではtoolを呼ばない ---
  "今日は学校でリレー走ったよ|"
)

PASS=0; TOTAL=0; FAILED=()
echo "=== 本番プロンプトでの tool 選択検証（各${RUNS}回）==="
for entry in "${SCENARIOS[@]}"; do
  utt="${entry%%|*}"; expect="${entry#*|}"
  for run in $(seq 1 "$RUNS"); do
    TOTAL=$((TOTAL+1))
    rm -f "$SB/rec.jsonl"; touch "$SB/rec.jsonl"
    echo '{"users":{"たろう":{"expected_balance":5000}},"applied_operation_keys":{}}' > "$SB/data/wallet_state.json"
    timeout 120 claude -p "$utt" --append-system-prompt "$SYS" \
      --strict-mcp-config --mcp-config "$SB/mcp.json" --allowedTools "$ALLOWED" \
      --output-format json < /dev/null > "$SB/out.json" 2>/dev/null
    called=$(python3 -c "
import json
tools=[]
try:
    for line in open('$SB/rec.jsonl'):
        line=line.strip()
        if line: tools.append(json.loads(line)['tool'])
except Exception: pass
print(','.join(tools) if tools else '(none)')
")
    if [ "$expect" = "?" ]; then
      # 呼んでも聞き返してもよい（どちらも妥当な応答）
      ok=1
    elif [ -z "$expect" ]; then
      [ "$called" = "(none)" ] && ok=1 || ok=0
    else
      case ",$called," in *",$expect,"*) ok=1;; *) ok=0;; esac
    fi
    if [ "$ok" = "1" ]; then
      PASS=$((PASS+1)); mark="OK  "
    else
      mark="NG  "; FAILED+=("$utt → 期待:${expect:-なし} 実際:$called")
    fi
    printf "  %s %-28s 期待:%-28s 実際:%s\n" "$mark" "$utt" "${expect:-（呼ばない）}" "$called"
  done
done

echo
echo "結果: $PASS / $TOTAL"
if [ ${#FAILED[@]} -gt 0 ]; then
  echo "--- 失敗 ---"
  for f in "${FAILED[@]}"; do echo "  $f"; done
fi
rm -rf "$SB"
[ "$PASS" = "$TOTAL" ]
