"""Phase N-11 配信時に一度だけ実行する、旧 pending 状態の一掃マイグレーション。

AI 主導化で旧 pending ハンドラを on_message から撤去したため、wallet_audit_state.json に
残る旧 pending（配信前に永続化されたもの）は宙に浮く。放置しても新経路では発火しないが、
残骸として残さないよう空にする。残高は一切触らない（pending キーのみクリア）。

保存は tmp+replace で原子的に行い、実行前にバックアップを取る。
"""
import json
import shutil
import sys
from pathlib import Path

# クリア対象の pending キー。いずれも旧 intent/dispatch 方式の途中状態
_PENDING_KEYS = [
    "initial_setup_pending_by_user",
    "spending_record_pending_by_user",
    "manual_income_pending_by_user",
    "wallet_check_pending_by_user",
    "goal_set_pending_by_user",
    "pending_intent_by_user",
]


def main() -> None:
    root = Path(__file__).resolve().parents[1]
    path = root / "data" / "wallet_audit_state.json"
    if not path.exists():
        print(f"[skip] {path} が無い。クリア不要。")
        return

    # 実行前にバックアップを取る（切り戻し用）
    backup = path.with_suffix(".json.pre_n11_bak")
    shutil.copy2(path, backup)
    print(f"[backup] {backup}")

    with open(path, "r", encoding="utf-8") as f:
        state = json.load(f)

    cleared = {}
    for key in _PENDING_KEYS:
        v = state.get(key)
        if isinstance(v, dict) and v:
            cleared[key] = sorted(v.keys())
            state[key] = {}

    # 残高キー（wallet_check_penalties 等）は触らない。pending のみクリア
    tmp = path.with_suffix(".json.tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)
        f.write("\n")
    tmp.replace(path)

    if cleared:
        print("[cleared] 以下の pending を空にした:")
        for k, names in cleared.items():
            print(f"  - {k}: {names}")
    else:
        print("[cleared] クリア対象の pending は無かった。")


if __name__ == "__main__":
    main()
