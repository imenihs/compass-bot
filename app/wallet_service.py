import json
from pathlib import Path

from app.config import get_log_dir
from app.storage import append_jsonl, now_jst_iso

# 1ユーザーが登録できる貯金目標の上限数
MAX_SAVINGS_GOALS = 5


class WalletService:
    def __init__(self):
        root = Path(__file__).resolve().parents[1]
        self.wallet_state_path = root / "data" / "wallet_state.json"
        self.wallet_audit_state_path = root / "data" / "wallet_audit_state.json"

    # ------------------------------------------------------------------
    # 旧形式マイグレーション
    # ------------------------------------------------------------------

    @staticmethod
    def _migrate_savings_goals_if_needed(state: dict) -> bool:
        """旧形式(savings_goal 単数キー)を新形式(savings_goals リスト)に変換する。
        変換が発生した場合は True を返す。初回ロード時に一度だけ実行される。"""
        migrated = False
        for u in state.get("users", {}).values():
            # 旧キーがあり新キーが未設定の場合のみ移行する
            if "savings_goal" in u and "savings_goals" not in u:
                old = u.pop("savings_goal")
                # 旧目標が有効な dict なら id=1 を付与してリスト化する
                if isinstance(old, dict) and old.get("title"):
                    u["savings_goals"] = [{"id": 1, **old}]
                else:
                    u["savings_goals"] = []
                migrated = True
        return migrated

    # ------------------------------------------------------------------
    # 状態ファイル読み書き
    # ------------------------------------------------------------------

    def _load_wallet_state(self) -> dict:
        if not self.wallet_state_path.exists():
            return {"users": {}}
        try:
            with open(self.wallet_state_path, "r", encoding="utf-8") as f:
                data = json.load(f)
                if isinstance(data, dict) and isinstance(data.get("users"), dict):
                    # 旧形式データが残っていれば自動マイグレーションして保存する
                    if self._migrate_savings_goals_if_needed(data):
                        self._save_wallet_state(data)
                    return data
        except Exception:
            pass
        return {"users": {}}

    def _save_wallet_state(self, state: dict) -> None:
        self.wallet_state_path.parent.mkdir(parents=True, exist_ok=True)
        with open(self.wallet_state_path, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False, indent=2)

    # ------------------------------------------------------------------
    # 残高操作
    # ------------------------------------------------------------------

    def has_wallet(self, user_name: str) -> bool:
        state = self._load_wallet_state()
        return user_name in state.get("users", {})

    def get_balance(self, user_name: str) -> int:
        state = self._load_wallet_state()
        users = state.get("users", {})
        u = users.get(user_name, {})
        return int(u.get("expected_balance", 0))

    def set_balance(self, user_name: str, amount: int) -> None:
        state = self._load_wallet_state()
        users = state.setdefault("users", {})
        u = users.setdefault(user_name, {})
        u["expected_balance"] = int(amount)
        self._save_wallet_state(state)

    def update_balance(
        self,
        user_conf: dict,
        system_conf: dict,
        delta: int,
        action: str,
        note: str = "",
        extra: dict | None = None,
    ) -> tuple[int, list[dict]]:
        user_name = str(user_conf.get("name", "unknown"))
        # 更新前の残高を記録しておく（目標達成の前後比較に使う）
        before = self.get_balance(user_name)
        after = before + int(delta)
        self.set_balance(user_name, after)

        log_dir = get_log_dir(system_conf)
        ledger_path = log_dir / f"{user_name}_wallet_ledger.jsonl"
        record = {
            "ts": now_jst_iso(),
            "name": user_name,
            "action": action,
            "delta": int(delta),
            "balance_before": before,
            "balance_after": after,
            "note": note,
        }
        if extra:
            record["extra"] = extra
        append_jsonl(ledger_path, record)

        # 全貯金目標に対して達成チェックをする（残高増加時のみ判定する）
        achieved: list[dict] = []
        if delta > 0:
            for goal in self.get_savings_goals(user_name):
                target = int(goal.get("target_amount", 0))
                # 更新前は未達成かつ更新後に到達した瞬間のみ達成とみなす
                if before < target <= after:
                    achieved.append(goal)

        return after, achieved

    # ------------------------------------------------------------------
    # 監査・ペナルティ
    # ------------------------------------------------------------------

    def load_audit_state(self) -> dict:
        if not self.wallet_audit_state_path.exists():
            return {"pending_by_user": {}}
        try:
            with open(self.wallet_audit_state_path, "r", encoding="utf-8") as f:
                data = json.load(f)
                if isinstance(data, dict):
                    data.setdefault("pending_by_user", {})
                    return data
        except Exception:
            pass
        return {"pending_by_user": {}}

    def save_audit_state(self, state: dict) -> None:
        self.wallet_audit_state_path.parent.mkdir(parents=True, exist_ok=True)
        with open(self.wallet_audit_state_path, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False, indent=2)

    def apply_penalty(self, user_conf: dict, system_conf: dict, diff: int, wallet_audit_conf: dict) -> int:
        penalty = int(abs(diff) * float(wallet_audit_conf.get("penalty_rate", 1.0)))
        cap = user_conf.get("penalty_cap")
        if cap in ("", None):
            cap = None
        if cap is None:
            cap = int(user_conf.get("fixed_allowance", 0))
        if cap is not None:
            penalty = min(penalty, int(cap))
        if penalty <= 0:
            return 0
        # update_balance の戻り値（tuple）は使用しない（達成通知はペナルティ時不要）
        self.update_balance(
            user_conf=user_conf,
            system_conf=system_conf,
            delta=-penalty,
            action="penalty",
            note="wallet_mismatch",
            extra={"mismatch": int(diff)},
        )
        return penalty

    # ------------------------------------------------------------------
    # 貯金目標 CRUD（複数対応）
    # ------------------------------------------------------------------

    def get_savings_goals(self, user_name: str) -> list[dict]:
        """全貯金目標をリストで返す。未設定なら空リスト。"""
        state = self._load_wallet_state()
        u = state.get("users", {}).get(user_name, {})
        goals = u.get("savings_goals", [])
        # 壊れたデータに備えてリスト型以外は空リストに差し替える
        return goals if isinstance(goals, list) else []

    def add_savings_goal(self, user_name: str, title: str, target_amount: int) -> tuple[bool, str]:
        """貯金目標を追加する。同名タイトルが既存なら金額を更新する。
        上限(MAX_SAVINGS_GOALS)超過の場合は (False, エラーメッセージ) を返す。"""
        state = self._load_wallet_state()
        users = state.setdefault("users", {})
        u = users.setdefault(user_name, {})
        goals: list[dict] = u.get("savings_goals", [])
        if not isinstance(goals, list):
            goals = []

        # 同名タイトルが既存なら金額だけ更新して返す
        for g in goals:
            if g.get("title") == title:
                g["target_amount"] = int(target_amount)
                u["savings_goals"] = goals
                self._save_wallet_state(state)
                return True, "updated"

        # 上限チェック
        if len(goals) >= MAX_SAVINGS_GOALS:
            return False, f"目標は最大{MAX_SAVINGS_GOALS}件までだよ。まず不要な目標を削除してね。"

        # id は既存の最大値 + 1 で採番する（削除後の再利用は行わない）
        next_id = max((g.get("id", 0) for g in goals), default=0) + 1
        goals.append({"id": next_id, "title": title, "target_amount": int(target_amount)})
        u["savings_goals"] = goals
        self._save_wallet_state(state)
        return True, "added"

    def remove_savings_goal(self, user_name: str, title: str) -> bool:
        """指定タイトルの目標を削除する。見つかれば True を返す。"""
        state = self._load_wallet_state()
        users = state.get("users", {})
        u = users.get(user_name, {})
        goals = u.get("savings_goals", [])
        if not isinstance(goals, list):
            return False
        # タイトルが一致しない目標だけを残すフィルタリングをする
        new_goals = [g for g in goals if g.get("title") != title]
        if len(new_goals) == len(goals):
            return False  # 対象タイトルが見つからなかった
        u["savings_goals"] = new_goals
        self._save_wallet_state(state)
        return True

    def clear_all_savings_goals(self, user_name: str) -> None:
        """全貯金目標を削除する。"""
        state = self._load_wallet_state()
        users = state.get("users", {})
        if user_name in users:
            users[user_name]["savings_goals"] = []
            self._save_wallet_state(state)
