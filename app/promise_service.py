"""約束フォロー管理の保存層（N-11.18）。

親子の間で結ばれた条件つきの約束（例「10000円貸す代わりに10ヶ月毎月500円返す」
「ゲームは1日1時間まで」）を記録し、期限まで追い続ける。

社長の言葉が出発点である。「極論10000円貸してもいいんだけど、それを手抜きせず管理して
やり切ることが、最も人間ができないこと」。親が返済の約束を覚えていられずうやむやになると、
失われるのは金額ではなく「約束は守るもの」という学習機会である。
AI は忘れない・飽きない・感情的にならない。人間が続けられないことを代わりにやり切る。

設計上の要点:
  ・**flock で保護する**。履行カウンタは金銭に直結するため、load→変更→save を必ずロック内で行う。
    貯金目標（savings_goals）がロック無しでロストアップデートを起こしていた前例がある。
  ・**子ども名をキーに1件へ上書きしない**。約束は複数同時に持てる（返済＋ゲーム時間）。
    査定（payout_requests）のモデルは流用できないため独自の保存先を持つ。
  ・**親の承認で確定する**。子が勝手に約束を作れない。承認前は下書きであり追跡も通知もしない。
"""
import json
import os
import tempfile
import threading
import uuid
from datetime import datetime
from pathlib import Path

from app.storage import JST, now_jst_iso

ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = ROOT / "data"

# 1人の子が同時に持てる約束の上限。多すぎると子も親も追えなくなる
MAX_ACTIVE_PROMISES = 5

# 状態: draft=親の承認待ち / active=履行中 / done=完了 / closed=打ち切り（期限切れ含む）
STATUS_DRAFT = "draft"
STATUS_ACTIVE = "active"
STATUS_DONE = "done"
STATUS_CLOSED = "closed"


class PromiseService:
    """約束の保存・更新を担う。ファイル I/O は必ず flock 配下で行う。"""

    def __init__(self, data_dir: Path | None = None):
        self.data_dir = Path(data_dir) if data_dir else DATA_DIR
        self.promises_path = self.data_dir / "promises.json"
        self._lock = threading.RLock()

    def _interprocess_lock(self):
        """プロセス間ロック。bot と mcp_wallet の両方から更新されるため必須。"""
        from app.wallet_service import _interprocess_lock
        lock_path = self.promises_path.with_suffix(".json.lock")
        return _interprocess_lock(lock_path)

    def _load(self) -> dict:
        """約束一覧を読む。壊れていても会話を止めず空で返す。"""
        try:
            if not self.promises_path.exists():
                return {"promises": []}
            with open(self.promises_path, "r", encoding="utf-8") as f:
                doc = json.load(f)
            if not isinstance(doc, dict):
                return {"promises": []}
            if not isinstance(doc.get("promises"), list):
                doc["promises"] = []
            return doc
        except (OSError, json.JSONDecodeError):
            return {"promises": []}

    def _save(self, doc: dict) -> None:
        """一時ファイル＋置換で保存する（書き込み途中の破損を避ける）。"""
        self.data_dir.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=str(self.data_dir), suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(doc, f, ensure_ascii=False, indent=2)
                f.write("\n")
            Path(tmp).replace(self.promises_path)
        except Exception:
            Path(tmp).unlink(missing_ok=True)
            raise

    # ------------------------------------------------------------------
    # 参照系（ロック不要。読むだけ）
    # ------------------------------------------------------------------

    def list_promises(self, child_name: str = "", status: str = "") -> list[dict]:
        """約束を返す。child_name / status で絞り込める。

        Args:
            child_name: 対象児で絞る（空なら全員）。
            status: 状態で絞る（空なら全状態）。

        Returns:
            list[dict]: 該当する約束のリスト。
        """
        doc = self._load()
        out = []
        for p in doc.get("promises", []):
            if not isinstance(p, dict):
                continue
            if child_name and str(p.get("child_name", "")) != child_name:
                continue
            if status and str(p.get("status", "")) != status:
                continue
            out.append(p)
        return out

    def get_promise(self, promise_id: str) -> dict | None:
        """ID で1件引く。無ければ None。"""
        for p in self._load().get("promises", []):
            if isinstance(p, dict) and str(p.get("id", "")) == str(promise_id):
                return p
        return None

    # ------------------------------------------------------------------
    # 更新系（必ず flock 配下）
    # ------------------------------------------------------------------

    def create_draft(self, child_name: str, title: str, detail: str,
                     total_times: int, note: str = "") -> tuple[bool, str, str]:
        """約束の下書きを作る。確定は親の承認を要する（子が勝手に作れない）。

        Args:
            child_name: 対象児。
            title: 約束の短い名前（例「パソコン代の返済」）。
            detail: 約束の中身（例「毎月500円ずつ10回返す」）。
            total_times: 履行の総回数（例10）。
            note: 結んだ経緯。後から意図が分かるように残す。

        Returns:
            tuple[bool, str, str]: (成功, promise_id, メッセージ)。
        """
        title = str(title or "").strip()
        detail = str(detail or "").strip()
        if not child_name or not title:
            return False, "", "約束の名前と対象のお子さんが必要だよ。"
        try:
            total = int(total_times)
        except (TypeError, ValueError):
            return False, "", "履行の回数が読み取れなかったよ。"
        if total <= 0 or total > 120:
            return False, "", "履行の回数は1〜120の範囲で決めてね。"

        with self._lock, self._interprocess_lock():
            doc = self._load()
            active = [p for p in doc["promises"]
                      if str(p.get("child_name", "")) == child_name
                      and str(p.get("status", "")) in (STATUS_DRAFT, STATUS_ACTIVE)]
            if len(active) >= MAX_ACTIVE_PROMISES:
                return False, "", (
                    f"{child_name} の約束はもう {MAX_ACTIVE_PROMISES} 件あるよ。"
                    "終わったものを片づけてから、新しい約束を作ってね。"
                )
            pid = uuid.uuid4().hex[:12]
            doc["promises"].append({
                "id": pid,
                "child_name": child_name,
                "title": title,
                "detail": detail,
                "note": str(note or "").strip(),
                "total_times": total,
                "done_times": 0,
                "status": STATUS_DRAFT,
                "created_at": now_jst_iso(),
                "approved_at": "",
                "last_reminded_at": "",
                "history": [],
            })
            self._save(doc)
        return True, pid, f"「{title}」の約束を下書きしたよ。おうちの人の承認で確定するよ。"

    def approve(self, promise_id: str, approved_by: str = "") -> tuple[bool, str]:
        """親が約束を承認して確定する。これで初めて追跡・通知の対象になる。"""
        with self._lock, self._interprocess_lock():
            doc = self._load()
            for p in doc["promises"]:
                if str(p.get("id", "")) != str(promise_id):
                    continue
                if str(p.get("status", "")) != STATUS_DRAFT:
                    return False, f"その約束はもう {p.get('status')} だよ。"
                p["status"] = STATUS_ACTIVE
                p["approved_at"] = now_jst_iso()
                p["approved_by"] = str(approved_by or "")
                self._save(doc)
                return True, f"「{p.get('title')}」の約束を確定したよ。ここから一緒に追いかけるね。"
        return False, "その約束は見つからなかったよ。"

    def record_progress(self, promise_id: str, note: str = "",
                        recorded_by: str = "") -> tuple[bool, str]:
        """履行を1回ぶん進める。親が申告して進む（台帳からの自動判定はしない）。

        自動判定をしない理由は仕様（N-11.18 ③障害1）のとおりで、台帳の
        action / delta / note からは「返済」と「同額の買い物」を区別できないためである。

        Returns:
            tuple[bool, str]: (成功, 子と親へ伝える文面)。
        """
        with self._lock, self._interprocess_lock():
            doc = self._load()
            for p in doc["promises"]:
                if str(p.get("id", "")) != str(promise_id):
                    continue
                if str(p.get("status", "")) != STATUS_ACTIVE:
                    return False, "その約束はいま進行中じゃないよ。"
                done = int(p.get("done_times", 0)) + 1
                total = int(p.get("total_times", 0))
                p["done_times"] = min(done, total)
                p.setdefault("history", []).append({
                    "ts": now_jst_iso(), "note": str(note or "").strip(),
                    "by": str(recorded_by or ""),
                })
                if p["done_times"] >= total:
                    p["status"] = STATUS_DONE
                    p["done_at"] = now_jst_iso()
                    self._save(doc)
                    return True, (
                        f"「{p.get('title')}」ぜんぶ終わったよ！{total}回、よくやりきったね。"
                    )
                self._save(doc)
                # 減点表示（あと何回）でなく積み上がり（何回できた）を前面に出す
                return True, (
                    f"「{p.get('title')}」{p['done_times']}回目、できたね。"
                    f"（{p['done_times']}/{total}）"
                )
        return False, "その約束は見つからなかったよ。"

    def close(self, promise_id: str, reason: str = "") -> tuple[bool, str]:
        """約束を打ち切る（期限切れ・見直し）。自動では消さず、必ず人が決める。"""
        with self._lock, self._interprocess_lock():
            doc = self._load()
            for p in doc["promises"]:
                if str(p.get("id", "")) != str(promise_id):
                    continue
                p["status"] = STATUS_CLOSED
                p["closed_at"] = now_jst_iso()
                p["closed_reason"] = str(reason or "").strip()
                self._save(doc)
                return True, f"「{p.get('title')}」の約束を終わりにしたよ。"
        return False, "その約束は見つからなかったよ。"

    def mark_reminded(self, promise_id: str, now: datetime | None = None) -> None:
        """リマインドを送った時刻を記録する（催促のしつこさを抑えるため）。"""
        stamp = (now or datetime.now(JST)).isoformat()
        with self._lock, self._interprocess_lock():
            doc = self._load()
            for p in doc["promises"]:
                if str(p.get("id", "")) == str(promise_id):
                    p["last_reminded_at"] = stamp
                    self._save(doc)
                    return
