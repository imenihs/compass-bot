import json
from pathlib import Path

from app.config import get_log_dir
from app.storage import append_jsonl, now_jst_iso


class WalletService:
    def __init__(self):
        root = Path(__file__).resolve().parents[1]
        self.wallet_state_path = root / "data" / "wallet_state.json"
        self.wallet_audit_state_path = root / "data" / "wallet_audit_state.json"

    def _load_wallet_state(self) -> dict:
        if not self.wallet_state_path.exists():
            return {"users": {}}
        try:
            with open(self.wallet_state_path, "r", encoding="utf-8") as f:
                data = json.load(f)
                if isinstance(data, dict) and isinstance(data.get("users"), dict):
                    return data
        except Exception:
            pass
        return {"users": {}}

    def _save_wallet_state(self, state: dict) -> None:
        self.wallet_state_path.parent.mkdir(parents=True, exist_ok=True)
        with open(self.wallet_state_path, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False, indent=2)

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
    ) -> int:
        user_name = str(user_conf.get("name", "unknown"))
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
        return after

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
        self.update_balance(
            user_conf=user_conf,
            system_conf=system_conf,
            delta=-penalty,
            action="penalty",
            note="wallet_mismatch",
            extra={"mismatch": int(diff)},
        )
        return penalty
