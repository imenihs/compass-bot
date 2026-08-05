"""会話セッションと支給要請の保存・排他を一手に担う土台。

このモジュールが `data/conversation_sessions.json` と `data/payout_requests.json` の
2ファイルを**唯一**所有する。対話層の他モジュールはファイルを直接開かず、必ず
`SessionStore` を経由する。

排他は `asyncio.Lock` で行う。Web も Bot も単一のイベントループ上で動くため、
同一ループ上の2コルーチンが同じ臨界区間へ入ることを防ぐのが目的である。
`threading.RLock` は同一スレッド内で再入可能で、同一ループ上の2コルーチンが両方とも
取得に成功してしまい相互排他が黙って失われるため使わない。ロックは read → 変更 →
tmp+replace の一連を1区間として保持し、臨界区間の内側で `await` するのは
このモジュール自身のロック配下の I/O に限る。

保存は tmp+replace（`app/wallet_service.py:72-78` と同方式）で行い、
既存ファイルを空の状態で全消しする書き込みは拒否する。読み込みは失敗時に閉じ、
形式不正な JSON（`[]` や `null` 等）は空 default へ落とさず例外を上げる。

金額計算はここに置かない。支給要請の内訳計算や充当規則は第3段で対話層側が担い、
本モジュールは要請ドキュメントの保存・読み出し・排他だけを提供する。
"""

import asyncio
import copy
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from app.storage import append_jsonl, now_jst_iso

# JST。会話セッションの時刻表現は台帳・ログと揃える
JST = timezone(timedelta(hours=9))

# セッションの既定有効期限（分）。期限切れ判定の既定値であり、張る側は個別に上書きできる。
# 設定 setting.json の conversation_session.expiry_minutes と連動させるには、張る側が
# deps.conversation_session_setting()["expiry_minutes"] を open_session の ttl_minutes へ渡す。
# SessionStore 自身は依存を持たない設計を保つため、config の解決は呼び出し側に委ねる。
DEFAULT_SESSION_TTL_MINUTES = 30


class SessionStore:
    """会話セッションと支給要請の2ファイルを所有し、非同期に直列化するトランザクション層。

    会話セッションは「いま何の話をしているか」を児童ごとに1つだけ持つ状態である。
    張る・読む・抜ける・期限切れ判定をここへ共通化し、旧来6種に散っていた
    脱出手段のばらつきを1本化する土台とする。

    支給要請ファイルは第3段で本格利用するが、ファイルの所有と排他は本層に置く。
    第1段では保存・読み出し・全消しガードだけを提供し、内訳スキーマは持たない。
    """

    def __init__(self, data_dir: Path | None = None):
        """ストアを初期化する。

        Args:
            data_dir: 2ファイルを置くディレクトリ。省略時はリポジトリ直下の data/。
                      テストは一時ディレクトリを渡して実データを避けられる。
        """
        # 既定はリポジトリ直下の data/。wallet_service と同じ解決規則に揃える
        if data_dir is None:
            data_dir = Path(__file__).resolve().parents[2] / "data"
        self._data_dir = data_dir
        # 会話セッションの保存先。児童ごとに1状態を持つ
        self.sessions_path = data_dir / "conversation_sessions.json"
        # 支給要請の保存先。所有は本層だが利用は第3段
        self.payout_requests_path = data_dir / "payout_requests.json"
        # 同一イベントループ上の2コルーチンを直列化するロック。RLock は使わない
        self._lock = asyncio.Lock()

    # ------------------------------------------------------------------
    # 低レベル I/O（tmp+replace・fail-closed）
    # ------------------------------------------------------------------

    def _load_doc(self, path: Path, top_key: str) -> dict:
        """JSON ドキュメントを読む。失敗時は閉じ、初回のみ空 default を返す。

        `_load_wallet_state`（`app/wallet_service.py:51-70`）と同じ「形式不正なら失敗」
        方針に揃える。あらゆる例外で空 dict を返すと、後段の save が壊れたファイルを
        空で上書きし、全児童のセッション／要請を恒久的に消す。

        Args:
            path: 読み込むファイルパス。
            top_key: ドキュメント直下に必ず持たせるキー（"sessions" / "requests"）。

        Returns:
            dict: 読み込んだドキュメント。top_key を必ず持つ。

        Raises:
            RuntimeError: ファイルは在るが読めない、または dict でない場合。
        """
        # ファイルが無いのは初回起動の正当な空状態であり、異常ではない
        if not path.exists():
            return {top_key: {}}
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception as e:
            # 破損 JSON や I/O 失敗を握りつぶすと、後段の save が空で上書きしてしまう
            self._log_error("conversation_session_load_error", e, {"path": str(path)})
            raise RuntimeError(f"{path.name} の読み込みに失敗しました") from e
        # [] や null は json.load が例外を出さないため、ここで明示的に弾く
        if not isinstance(data, dict):
            self._log_error(
                "conversation_session_invalid_error",
                ValueError(f"{path.name} schema is invalid"),
                {"path": str(path), "loaded_type": type(data).__name__},
            )
            raise RuntimeError(f"{path.name} の形式が不正です")
        # 直下キーが欠けていても以降の処理が落ちないよう補う
        data.setdefault(top_key, {})
        # 直下キーが dict 以外なら壊れたデータとみなして空へ差し替える
        if not isinstance(data.get(top_key), dict):
            data[top_key] = {}
        return data

    def _save_doc(self, path: Path, doc: dict, top_key: str) -> None:
        """JSON ドキュメントを tmp+replace で保存する。top_key を欠く doc は拒否する。

        `_save_wallet_state`（`app/wallet_service.py:72-78`）と同じ原子的書き込みを行う。
        `_load_doc` は必ず top_key を補って返すため、正常な read→変更→save では doc は常に
        top_key を持つ。top_key を欠く doc は「load を経ていない／別物を組み立てた」異常であり、
        書けば直後の load が `setdefault` で空の top_key を補い、全児童のセッション／要請
        （＝実際のお金）を黙って消す。空 dict も top_key の typo も、この1条件でまとめて弾く。

        Args:
            path: 保存先ファイルパス。
            doc: 保存するドキュメント。load_doc で読んだものを変更して渡す。
            top_key: ドキュメント直下に必ず持たせるキー。

        Raises:
            RuntimeError: 既存ファイルが在るのに、top_key を持たない doc を書こうとした場合。
        """
        # top_key の有無で「load 由来か」を判定する。空 dict も別キーの doc も top_key を持たない
        # 既存ファイルを top_key 欠落の doc で上書きすると load 側が空 top_key を補い全件消える
        if top_key not in doc and path.exists():
            self._log_error(
                "conversation_session_wipe_blocked",
                ValueError(f"refused to overwrite existing file with a document missing top_key '{top_key}'"),
                {"path": str(path), "doc_keys": sorted(doc.keys())},
            )
            raise RuntimeError(f"{path.name} を不正な形（{top_key} キー欠落）で上書きしようとしました")
        path.parent.mkdir(parents=True, exist_ok=True)
        # 同一ディレクトリへ .tmp を書いてから replace することで、途中失敗でも本体を壊さない。
        # 同期は wallet_service（_save_wallet_state）に合わせ fsync しない。電源断直後の耐障害性を
        # 上げるなら flush+os.fsync と親ディレクトリの fsync を足すが、その際は wallet_service と
        # 同時に変更して両者の挙動を揃える前提とする。
        tmp_path = path.with_suffix(path.suffix + ".tmp")
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(doc, f, ensure_ascii=False, indent=2)
            f.write("\n")
        # replace は同一ファイルシステム上でアトミック。読み手は旧か新のどちらかしか見ない
        tmp_path.replace(path)

    def _log_error(self, event: str, error: Exception, details: dict | None = None) -> None:
        """セッション I/O の異常を診断ログへ残す。ログ失敗は標準出力へ逃がす。

        `WalletService._log_wallet_error`（`app/wallet_service.py:80-93`）と同じ経路の
        `runtime_diagnostics.jsonl` へ書く。会話層の I/O 異常も同じ場所で追える。
        """
        try:
            # 診断ログは data/logs 配下へ集約する。ストアの data_dir を基点にする
            log_path = self._data_dir / "logs" / "runtime_diagnostics.jsonl"
            append_jsonl(log_path, {
                "ts": now_jst_iso(),
                "event": event,
                "error_type": type(error).__name__,
                "error_message": str(error),
                "details": details or {},
            })
        except Exception as log_error:
            # 診断ログの失敗で本処理を巻き込まない
            print(f"[conversation_session_diagnostics] log error: {type(log_error).__name__}: {log_error}")

    # ------------------------------------------------------------------
    # 期限切れ判定（純関数・ロック不要）
    # ------------------------------------------------------------------

    @staticmethod
    def _now() -> datetime:
        """現在時刻を JST の aware datetime で返す。期限比較の基準にする。"""
        return datetime.now(JST)

    @classmethod
    def _is_expired(cls, session: dict, now: datetime | None = None) -> bool:
        """セッションが期限切れかを判定する。

        `expires_at` を持たない、または壊れて解釈できないセッションは、判定不能を
        「期限切れ」に倒して閉じ込めを防ぐ。読み込み・整理の両方から呼ぶ純関数とする。

        Args:
            session: 判定対象のセッション dict。
            now: 比較基準の現在時刻。省略時は JST の現在時刻。

        Returns:
            bool: 期限切れなら True。
        """
        if not isinstance(session, dict):
            # 壊れたエントリは閉じ込めを避けるため期限切れ扱いにする
            return True
        raw = session.get("expires_at")
        if not raw:
            # 期限が無いセッションは判定不能。閉じ込めを避け期限切れに倒す
            return True
        try:
            expires = datetime.fromisoformat(str(raw))
        except Exception:
            # 解釈できない期限は壊れているとみなし期限切れに倒す
            return True
        # naive な期限は JST とみなして aware 同士で比較する
        if expires.tzinfo is None:
            expires = expires.replace(tzinfo=JST)
        current = now or cls._now()
        return current >= expires

    # ------------------------------------------------------------------
    # 会話セッション：張る・読む・抜ける・整理（すべてロック配下）
    # ------------------------------------------------------------------

    async def open_session(
        self,
        user_name: str,
        kind: str,
        data: dict | None = None,
        ttl_minutes: int = DEFAULT_SESSION_TTL_MINUTES,
    ) -> dict:
        """会話セッションを張る（無ければ新規、有れば上書き）。

        児童ごとに状態は1つだけ持つ。同じ児童に既存セッションが有っても、
        新しい kind で上書きして常に最新の1状態へ収束させる。金額計算は行わない。

        Args:
            user_name: 対象児童名。
            kind: セッション種別（雑談・目標設定・査定相談など、対話層が定義する文字列）。
            data: セッションに紐づく任意ペイロード（partial 入力など）。金額の確定計算は持たない。
            ttl_minutes: 有効期限（分）。この時間を過ぎたセッションは期限切れになる。

        Returns:
            dict: 保存したセッションの完全な内容（コピー）。
        """
        async with self._lock:
            doc = self._load_doc(self.sessions_path, "sessions")
            sessions = doc["sessions"]
            now = self._now()
            # 期限を絶対時刻で持たせる。読み出し側は expires_at 単体で判定できる
            expires = now + timedelta(minutes=max(0, int(ttl_minutes)))
            existing = sessions.get(user_name)
            # 作成時刻は初回だけ固定し、更新のたびに created を動かさない
            created = existing.get("created") if isinstance(existing, dict) else None
            session = {
                "kind": kind,
                "data": data if isinstance(data, dict) else {},
                "created": created or now.isoformat(),
                "updated": now.isoformat(),
                "expires_at": expires.isoformat(),
            }
            sessions[user_name] = session
            self._save_doc(self.sessions_path, doc, "sessions")
            # deepcopy で返す。浅いコピーだと data の nested dict を共有し、呼び出し側の
            # in-place 変更が永続化されないのに参照だけ書き換わる罠になる（第3段で data に
            # 支給要請の金額内訳を載せる際に効く）。変更は必ず open_session で保存し直す
            return copy.deepcopy(session)

    async def get_session(self, user_name: str) -> dict | None:
        """児童のセッションを読む。期限切れは自動でクリアし None を返す。

        読み出しの時点で期限切れだったセッションは、その場で削除して閉じ込めを防ぐ。
        削除を伴うため read だけでなく write もあり得るが、いずれもロック配下で行う。

        Args:
            user_name: 対象児童名。

        Returns:
            dict | None: 有効なセッションの完全な内容（コピー）。無い／期限切れなら None。
        """
        async with self._lock:
            doc = self._load_doc(self.sessions_path, "sessions")
            sessions = doc["sessions"]
            session = sessions.get(user_name)
            if not isinstance(session, dict):
                # 未張り、または壊れたエントリは None として扱う
                return None
            if self._is_expired(session):
                # 期限切れはその場で消してから None を返す。次ターンで残骸を拾わない
                sessions.pop(user_name, None)
                self._save_doc(self.sessions_path, doc, "sessions")
                return None
            # deepcopy で返す。返り値 data の in-place 変更は永続化されないため、
            # 変更は必ず open_session で保存し直す（open_session と同じ約束）
            return copy.deepcopy(session)

    async def clear_session(self, user_name: str) -> bool:
        """児童のセッションを抜ける（削除する）。キャンセル語や完了時に呼ぶ。

        Args:
            user_name: 対象児童名。

        Returns:
            bool: 実際に削除したら True、元から無ければ False。
        """
        async with self._lock:
            doc = self._load_doc(self.sessions_path, "sessions")
            sessions = doc["sessions"]
            if user_name not in sessions:
                # 元から無ければ書き込みも起こさない
                return False
            sessions.pop(user_name, None)
            self._save_doc(self.sessions_path, doc, "sessions")
            return True

    async def prune_expired_sessions(self) -> list[str]:
        """期限切れの全セッションをまとめて削除する。定期整理から呼ぶ。

        個別の read で消しきれない放置セッションを一括で整理する。金額計算は行わない。

        Returns:
            list[str]: 削除した児童名の一覧。1件も無ければ空リスト。
        """
        async with self._lock:
            doc = self._load_doc(self.sessions_path, "sessions")
            sessions = doc["sessions"]
            now = self._now()
            # 反復中に辞書を変更しないよう、期限切れ名を先に集める
            expired = [name for name, s in sessions.items() if self._is_expired(s, now)]
            if not expired:
                # 削除対象が無ければ書き込みを起こさない
                return []
            for name in expired:
                sessions.pop(name, None)
            self._save_doc(self.sessions_path, doc, "sessions")
            return expired

    # ------------------------------------------------------------------
    # 支給要請ファイルの所有（第1段は保存・読み出しの土台のみ・内訳は持たない）
    # ------------------------------------------------------------------

    async def read_payout_requests(self) -> dict:
        """支給要請ドキュメント全体を読む。失敗時は閉じる。

        第3段で内訳・受領履歴を扱うが、ファイルの所有と排他は本層に置く。
        第1段では丸ごと読む土台だけを提供し、要請スキーマは解釈しない。

        Returns:
            dict: 要請ドキュメントの完全な内容。直下に "requests" を必ず持つ。
        """
        async with self._lock:
            # 完全な内容を返す。呼び出し側が要請スキーマを解釈する
            return self._load_doc(self.payout_requests_path, "requests")

    async def write_payout_requests(self, doc: dict) -> None:
        """支給要請ドキュメント全体を tmp+replace で保存する。空での全消しは拒否する。

        read_payout_requests で読んだ doc を変更して渡す。金額計算は呼び出し側が行い、
        本層は原子的な書き込みと全消しガードだけを担う。

        Args:
            doc: 保存する要請ドキュメント。直下に "requests" を持つ形を想定する。
        """
        async with self._lock:
            self._save_doc(self.payout_requests_path, doc, "requests")
