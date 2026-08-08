import fcntl
import json
import sys
import threading
import uuid
from contextlib import contextmanager
from pathlib import Path

from app.config import get_log_dir
from app.storage import append_jsonl, now_jst_iso


@contextmanager
def _interprocess_lock(lock_path: Path):
    """複数プロセス間で残高ファイルの read-modify-write を直列化するファイルロック。

    threading.RLock は同一プロセス内しか排他できない。claude が起動する mcp_wallet 子プロセスと、
    親承認を実行する bot プロセスが同じ wallet_state.json / payout_requests.json を別々に
    read→変更→save するとロストアップデート（片方の更新が上書きで消える）が起きる。fcntl.flock で
    OS レベルの排他ロックを張り、両プロセスが同じロックファイルを取ることで直列化する。

    Args:
        lock_path: ロック用ファイルのパス（対象ファイル名 + ".lock"）。
    """
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    f = open(lock_path, "w")
    try:
        # 排他ロックを取る。他プロセスが保持中ならブロックして待つ
        fcntl.flock(f.fileno(), fcntl.LOCK_EX)
        yield
    finally:
        try:
            fcntl.flock(f.fileno(), fcntl.LOCK_UN)
        finally:
            f.close()

# 1ユーザーが登録できる貯金目標の上限数
MAX_SAVINGS_GOALS = 5


class _PrecheckRejected(Exception):
    """update_balance のロック内 precheck が書き込みを拒否したことを表す。

    上限超過などで「書いてはいけない」と判定した場合に送出する。呼び出し側はこれを捕まえて
    子ども向けの文面へ変換する。ロックを保持したまま判定するため、判定と書き込みの間に
    他プロセスが割り込む余地が無い（TOCTOU の解消）。
    """


class WalletService:
    def __init__(self):
        root = Path(__file__).resolve().parents[1]
        self.wallet_state_path = root / "data" / "wallet_state.json"
        self.wallet_audit_state_path = root / "data" / "wallet_audit_state.json"
        self._lock = threading.RLock()

    @staticmethod
    def new_entry_id(prefix: str = "expense") -> str:
        """支出・台帳をひも付けるための短い一意IDを生成する。"""
        safe_prefix = "".join(ch for ch in str(prefix or "entry") if ch.isalnum() or ch in {"_", "-"})
        return f"{safe_prefix or 'entry'}_{uuid.uuid4().hex}"

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
        except Exception as e:
            self._log_wallet_error("wallet_state_load_error", e, {"path": str(self.wallet_state_path)})
            raise RuntimeError("wallet_state.json の読み込みに失敗しました") from e
        self._log_wallet_error(
            "wallet_state_invalid_error",
            ValueError("wallet_state.json schema is invalid"),
            {"path": str(self.wallet_state_path)},
        )
        raise RuntimeError("wallet_state.json の形式が不正です")

    def _save_wallet_state(self, state: dict) -> None:
        self.wallet_state_path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = self.wallet_state_path.with_suffix(self.wallet_state_path.suffix + ".tmp")
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False, indent=2)
            f.write("\n")
        tmp_path.replace(self.wallet_state_path)

    def _log_wallet_error(self, event: str, error: Exception, details: dict | None = None) -> None:
        """ウォレット状態の異常を診断ログへ残す。ログ失敗は標準エラーへ逃がす。"""
        try:
            root = Path(__file__).resolve().parents[1]
            log_path = root / "data" / "logs" / "runtime_diagnostics.jsonl"
            append_jsonl(log_path, {
                "ts": now_jst_iso(),
                "event": event,
                "error_type": type(error).__name__,
                "error_message": str(error),
                "details": details or {},
            })
        except Exception as log_error:
            # stderr へ出す。MCP サーバ(mcp_wallet)から共有されるため、stdout に書くと
            # JSON-RPC ストリームを汚して claude CLI のパースを壊す。stderr なら安全。
            print(f"[wallet_diagnostics] log error: {type(log_error).__name__}: {log_error}", file=sys.stderr)

    # ------------------------------------------------------------------
    # 残高操作
    # ------------------------------------------------------------------

    def has_wallet(self, user_name: str) -> bool:
        state = self._load_wallet_state()
        return user_name in state.get("users", {})

    def is_operation_applied(self, operation_key: str) -> bool:
        """指定の operation_key が既に適用済みかを返す。

        冪等スキップを呼び出し側が事前に判別するために使う。update_balance は既適用キーを
        黙ってスキップして (before, []) を返すため、成功時と区別がつかない。AI 主導層の
        wallet tool が「もう反映済み」と正しく伝えられるよう、呼ぶ前に本メソッドで確認する。

        Args:
            operation_key: 判定する冪等キー。空文字なら常に False。

        Returns:
            bool: 既に適用済みなら True。
        """
        key = str(operation_key or "").strip()
        if not key:
            return False
        state = self._load_wallet_state()
        return key in state.get("applied_operation_keys", {})

    def is_recent_dup_applied(self, dup_key: str, window_sec: int) -> bool:
        """内容キー(自然キー)が window_sec 以内に適用済みかを返す（言い直し二重適用の事前判定）。

        update_balance の窓判定と同じロジックを、wallet tool が「もう反映済み」と正しく伝えるための
        事前チェックに使う。存在するだけでなく ts が window 以内のときだけ True にすることで、
        窓を超えた「別の支出」を誤って既適用と報告しない。ts 解釈不能は安全側で True（弾く）。

        Args:
            dup_key: _natural_dup_key が返す内容キー。
            window_sec: この秒数以内の適用を「言い直し」とみなす。0 以下なら存在チェックに倒す。

        Returns:
            bool: window 以内に適用済みなら True。
        """
        key = str(dup_key or "").strip()
        if not key:
            return False
        state = self._load_wallet_state()
        meta = state.get("applied_operation_keys", {}).get(key)
        if not isinstance(meta, dict):
            return False
        if window_sec <= 0:
            return True  # 窓未指定なら存在で弾く
        ts_raw = meta.get("ts")
        if not ts_raw:
            return True
        from datetime import datetime as _dt, timedelta as _td
        try:
            applied_at = _dt.fromisoformat(str(ts_raw))
            now_dt = _dt.fromisoformat(now_jst_iso())
            if applied_at.tzinfo is None:
                applied_at = applied_at.replace(tzinfo=now_dt.tzinfo)
            return applied_at > now_dt - _td(seconds=window_sec)
        except ValueError:
            return True  # 解釈不能は安全側で弾く

    def get_balance(self, user_name: str) -> int:
        state = self._load_wallet_state()
        users = state.get("users", {})
        u = users.get(user_name, {})
        return int(u.get("expected_balance", 0))

    def set_balance(self, user_name: str, amount: int) -> None:
        with self._lock:
            state = self._load_wallet_state()
            users = state.setdefault("users", {})
            u = users.setdefault(user_name, {})
            u["expected_balance"] = int(amount)
            self._save_wallet_state(state)

    def _prune_aux_operation_keys(self, applied_keys: dict, max_age_days: int = 2) -> None:
        """applied_operation_keys から、aux=True かつ ts が max_age_days より古い自然キーを削る。

        自然キー(dup:)は2分窓の言い直し検知専用で長期保持の意味がない。主キー(aux が無い/False)は
        監査の正本なので触らない。ts が解釈できないキーは安全側で残す。呼び出しは update_balance の
        flock 内に限る(applied_keys の read-modify-write を直列化するため)。

        Args:
            applied_keys: applied_operation_keys 本体（in-place で削る）。
            max_age_days: この日数より古い aux キーを削除する。
        """
        from datetime import datetime, timedelta
        try:
            cutoff = datetime.fromisoformat(now_jst_iso()) - timedelta(days=max_age_days)
        except ValueError:
            return
        to_delete = []
        for key, meta in applied_keys.items():
            if not isinstance(meta, dict) or not meta.get("aux"):
                continue  # 主キーは監査の正本。残す
            ts_raw = meta.get("ts")
            if not ts_raw:
                continue  # ts 不明は安全側で残す
            try:
                ts = datetime.fromisoformat(str(ts_raw))
            except ValueError:
                continue
            # TZ 有無を揃えて比較する（cutoff は now_jst_iso 由来で aware）
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=cutoff.tzinfo)
            if ts < cutoff:
                to_delete.append(key)
        for key in to_delete:
            applied_keys.pop(key, None)

    def update_balance(
        self,
        user_conf: dict,
        system_conf: dict,
        delta: int,
        action: str,
        note: str = "",
        extra: dict | None = None,
        operation_key: str | None = None,
        aux_operation_keys: list[str] | None = None,
        aux_dedup_window_sec: int = 0,
        precheck=None,
    ) -> tuple[int, list[dict]]:
        # プロセス内(RLock)に加え、プロセス間(flock)でも直列化する。mcp_wallet 子プロセスと
        # bot プロセスが同じ wallet_state.json を read-modify-write するロストアップデートを防ぐ。
        #
        # aux_operation_keys: 主 operation_key に加えて冪等判定・記録する補助キー。会話の言い直し
        # (tool後にタイムアウト/失敗し、子が同じ発話を繰り返すと AI が別の生キーを選び主キー冪等を
        # すり抜ける)を弾くための自然キー(子:action:金額:品目:時刻窓)を mcp_wallet が渡す。主キーと
        # 同じ flock 内で登録し、二重適用防止をアトミックにする。
        lock_path = self.wallet_state_path.with_suffix(self.wallet_state_path.suffix + ".lock")
        with self._lock, _interprocess_lock(lock_path):
            user_name = str(user_conf.get("name", "unknown"))
            state = self._load_wallet_state()
            users = state.setdefault("users", {})
            user_state = users.setdefault(user_name, {})
            before = int(user_state.get("expected_balance", 0))
            applied_keys = state.setdefault("applied_operation_keys", {})
            # aux 自然キー(dup:)は2分窓の言い直し検知にしか使わないため無期限に残す必要がない。主キー
            # (grant/expense等)は監査の正本なので残すが、aux キーだけは ts が古いものを剪定して
            # applied_operation_keys の単調肥大(=毎回の tmp+replace 書込コスト・ロック保持時間の悪化)を防ぐ。
            self._prune_aux_operation_keys(applied_keys)
            safe_operation_key = str(operation_key or "").strip()
            if safe_operation_key and safe_operation_key in applied_keys:
                return before, []
            # 補助キー(内容キー)は経過秒で判定する。同じ内容キーが dedup_window_sec 以内に適用済みなら
            # 言い直しによる二重適用とみなしスキップする。窓を超えていれば「別の支出」として通し、後で ts を
            # 更新する。分バケットでなく経過秒にすることで分境界の漏れ(12:00:59 と 12:02:00 が重ならない)を防ぎ、
            # 品目空も内容キーで弾ける。window は呼び出し側(mcp_wallet)が渡す。
            from datetime import datetime as _dt, timedelta as _td
            safe_aux_keys = [str(k or "").strip() for k in (aux_operation_keys or []) if str(k or "").strip()]
            window = int(aux_dedup_window_sec or 0)
            for aux_key in safe_aux_keys:
                meta = applied_keys.get(aux_key)
                if not isinstance(meta, dict):
                    continue
                # ts が window 以内なら二重適用とみなす。ts 解釈不能や window<=0 なら従来どおり存在で弾く
                ts_raw = meta.get("ts")
                if window > 0 and ts_raw:
                    try:
                        applied_at = _dt.fromisoformat(str(ts_raw))
                        now_dt = _dt.fromisoformat(now_jst_iso())
                        if applied_at.tzinfo is None:
                            applied_at = applied_at.replace(tzinfo=now_dt.tzinfo)
                        if applied_at > now_dt - _td(seconds=window):
                            return before, []  # 窓内=言い直し。二重適用しない
                        # 窓外=別支出。この aux_key は下で新 ts に上書きされる
                        continue
                    except ValueError:
                        return before, []  # ts 解釈不能は安全側で弾く
                # window 未指定 or ts 無し: 存在するだけで弾く(従来互換)
                return before, []
            # 上限チェックをこの flock の内側で行う（TOCTOU の根本対処・2026/08/09）。
            # 以前は呼び出し側が「台帳を集計 → 上限判定 → update_balance」と別々に行っており、
            # 集計と書き込みの間にロックが無かった。複数プロセス（親の支給と子の入金、複数の
            # claude subprocess）が同時に走ると全員が「まだ余裕あり」を読んでから順に書けてしまい、
            # 実測で日次上限5000円に対し10000円の入金、月次上限3000円に対し4000円の支給が通った。
            # precheck はロックを保持したまま台帳を再集計して判定する。拒否理由を返せば書き込まない。
            if precheck is not None:
                reason = precheck()
                if reason:
                    # 拒否は「適用0件」で返す。呼び出し側は reason を見て文面を出す
                    raise _PrecheckRejected(str(reason))
            after = before + int(delta)

            log_dir = get_log_dir(system_conf)
            ledger_path = log_dir / f"{user_name}_wallet_ledger.jsonl"
            ts = now_jst_iso()
            record = {
                "ts": ts,
                "name": user_name,
                "action": action,
                "delta": int(delta),
                "balance_before": before,
                "balance_after": after,
                "note": note,
            }
            if safe_operation_key:
                record["operation_key"] = safe_operation_key
            if extra:
                record["extra"] = extra
            append_jsonl(ledger_path, record)

            user_state["expected_balance"] = after
            if safe_operation_key:
                applied_keys[safe_operation_key] = {
                    "ts": ts,
                    "name": user_name,
                    "action": action,
                    "delta": int(delta),
                    "balance_after": after,
                }
            # 補助キー(自然キー)も同じ flock 内で applied 登録する。次の言い直しターンで別の主キーでも
            # この自然キーが既適用に当たり二重適用を弾ける。値は主キーと同形式で追跡可能にする。
            for aux_key in safe_aux_keys:
                applied_keys[aux_key] = {
                    "ts": ts,
                    "name": user_name,
                    "action": action,
                    "delta": int(delta),
                    "balance_after": after,
                    "aux": True,
                }
            self._save_wallet_state(state)

            achieved: list[dict] = []
            if delta > 0:
                goals = user_state.get("savings_goals", [])
                for goal in goals if isinstance(goals, list) else []:
                    try:
                        target = int(goal.get("target_amount", 0))
                    except (TypeError, ValueError):
                        continue
                    if before < target <= after:
                        achieved.append(goal)

            return after, achieved

    # ------------------------------------------------------------------
    # 監査・ペナルティ
    # ------------------------------------------------------------------

    def load_audit_state(self) -> dict:
        """監査状態を読み込む。読めない・形式が不正な場合は例外を上げて処理を止める。

        呼び出し側は「読み込み → 変更 → save_audit_state」を1区間で行うため、
        ここで空dictを返すと壊れたファイルをそのまま書き戻し、全児童の pending と
        wallet_check_penalties を恒久的に消す。wallet_state.json 側の _load_wallet_state と
        同じく「形式不正なら失敗させる」方針に揃える。

        Returns:
            dict: 監査状態。ファイル未作成の場合のみ初回起動として空の状態を返す。

        Raises:
            RuntimeError: ファイルは在るが読めない、または dict でない場合。
        """
        # ファイルが無いのは初回起動の正当な空状態であり、異常ではない
        if not self.wallet_audit_state_path.exists():
            return {"pending_by_user": {}}
        try:
            with open(self.wallet_audit_state_path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception as e:
            # 破損JSONやI/O失敗を握りつぶすと、後段の save が空の状態で上書きしてしまう
            self._log_wallet_error(
                "wallet_audit_state_load_error", e, {"path": str(self.wallet_audit_state_path)}
            )
            raise RuntimeError("wallet_audit_state.json の読み込みに失敗しました") from e
        # [] や null は json.load が例外を出さないため、ここで明示的に弾く
        if not isinstance(data, dict):
            self._log_wallet_error(
                "wallet_audit_state_invalid_error",
                ValueError("wallet_audit_state.json schema is invalid"),
                {"path": str(self.wallet_audit_state_path), "loaded_type": type(data).__name__},
            )
            raise RuntimeError("wallet_audit_state.json の形式が不正です")
        data.setdefault("pending_by_user", {})
        return data

    def save_audit_state(self, state: dict) -> None:
        """監査状態を保存する。既存データを空の状態で全消しする書き込みは拒否する。

        Args:
            state: 保存する監査状態。load_audit_state で読んだ dict を変更したものを渡す。

        Raises:
            RuntimeError: 既存ファイルが在るのに、キーを1つも持たない状態を書こうとした場合。
        """
        # 正常な呼び出しは必ず load 済みで pending_by_user を持つため、完全に空の state は異常とみなす
        if not state and self.wallet_audit_state_path.exists():
            self._log_wallet_error(
                "wallet_audit_state_wipe_blocked",
                ValueError("refused to overwrite existing audit state with an empty state"),
                {"path": str(self.wallet_audit_state_path)},
            )
            raise RuntimeError("wallet_audit_state.json を空の状態で上書きしようとしました")
        self.wallet_audit_state_path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = self.wallet_audit_state_path.with_suffix(self.wallet_audit_state_path.suffix + ".tmp")
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False, indent=2)
            f.write("\n")
        tmp_path.replace(self.wallet_audit_state_path)

    # apply_penalty は削除した（2026/08/09）。
    # 残高不一致に対して子の残高を減額する処理だったが、呼び出し元がゼロの到達不能コードであった。
    # 現行の運用は「罰でなく記録の抜けを一緒に確認する」であり（実装仕様: 支出感想・残高不一致の
    # あるべき扱い）、減額は行わない。到達不能なまま残すと、将来誰かが「使える機能」と誤認して
    # 呼び出し、子の残高を黙って減らす事故になりうる。仕様と実装の乖離源にもなるため削除する。
    # 復活させる場合は、段階的な影響・文言・親への通知を仕様から設計し直すこと。

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
