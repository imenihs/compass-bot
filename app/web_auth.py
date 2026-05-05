"""
Webダッシュボード認証モジュール。
申請・承認・パスワード設定・セッション管理を担う。
"""

import asyncio
import hashlib
import json
import os
import secrets
import traceback
from pathlib import Path
from typing import Optional

# データファイルのパス定義
ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = ROOT / "data"
WEB_USERS_PATH = DATA_DIR / "web_users.json"      # ユーザー情報（PW ハッシュ等）
WEB_SESSIONS_PATH = DATA_DIR / "web_sessions.json" # セッショントークン管理
WEB_AUTH_STATE_PATH = DATA_DIR / "web_auth_state.json"  # 申請・仮PW状態管理

# セッション有効期間（秒）: 7日間
SESSION_TTL_SECONDS = 7 * 24 * 3600

# ファイルI/O の競合防止用ロック
_lock = asyncio.Lock()


# ---------- 内部ユーティリティ ----------

def _read_json(path: Path) -> dict:
    """JSONファイルを読み込む。存在しない場合は空dictを返す"""
    if not path.exists():
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError) as e:
        _log_web_auth_error("web_auth_json_read_error", path, e)
        return {}


def _write_json(path: Path, data: dict) -> None:
    """JSONファイルに書き込む。ディレクトリが存在しない場合は作成する"""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.write("\n")
    tmp_path.replace(path)


def _log_web_auth_error(event: str, path: Path, error: Exception) -> None:
    """Web認証状態ファイルの異常を診断ログに残す。"""
    try:
        log_path = DATA_DIR / "logs" / "runtime_diagnostics.jsonl"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        with open(log_path, "a", encoding="utf-8") as f:
            f.write(json.dumps({
                "event": event,
                "path": str(path),
                "error_type": type(error).__name__,
                "error_message": str(error),
                "traceback": "".join(
                    traceback.format_exception(type(error), error, error.__traceback__, limit=4)
                )[:2000],
            }, ensure_ascii=False) + "\n")
    except Exception:
        pass


def _hash_password(password: str, salt: str) -> str:
    """SHA-256 + salt でパスワードをハッシュ化する（家庭用途）"""
    return hashlib.sha256(f"{salt}{password}".encode("utf-8")).hexdigest()


def _now_timestamp() -> float:
    """現在時刻を Unix timestamp で返す"""
    import time
    return time.time()


# ---------- 申請管理 ----------

async def create_application(username: str, discord_user_id: Optional[int] = None) -> str:
    """
    Webアクセス申請を登録する。
    重複申請の場合は既存の申請トークンを返す。
    戻り値: 申請ID（Discord 通知用）
    """
    async with _lock:
        state = _read_json(WEB_AUTH_STATE_PATH)
        # 既存申請チェック（username 重複）
        apps = state.get("applications", {})
        for app_id, app in apps.items():
            if app.get("username") == username:
                return app_id
        # 新規申請を登録する
        app_id = secrets.token_urlsafe(8)
        apps[app_id] = {
            "username": username,
            "discord_user_id": discord_user_id,
            "status": "pending",       # pending / approved / rejected
            "requested_at": _now_timestamp(),
        }
        state["applications"] = apps
        _write_json(WEB_AUTH_STATE_PATH, state)
        return app_id


async def approve_application(username: str) -> Optional[str]:
    """
    申請を承認して仮パスワードを発行する。
    戻り値: 仮パスワード（6桁英数字）。対象が見つからなければ None を返す
    """
    async with _lock:
        state = _read_json(WEB_AUTH_STATE_PATH)
        apps = state.get("applications", {})
        # username で申請を検索する（pending または approved＝PW未設定 も再承認可能）
        target_id = None
        for app_id, app in apps.items():
            if app.get("username") == username and app.get("status") in ("pending", "approved"):
                target_id = app_id
                break
        if target_id is None:
            return None
        # 仮パスワードを生成して状態を更新する
        temp_password = secrets.token_urlsafe(6)[:8]
        apps[target_id]["status"] = "approved"
        apps[target_id]["temp_password"] = temp_password
        apps[target_id]["approved_at"] = _now_timestamp()
        state["applications"] = apps
        _write_json(WEB_AUTH_STATE_PATH, state)
        return temp_password


async def get_temp_password(username: str) -> Optional[str]:
    """承認済みの仮パスワードを取得する。未承認・未登録なら None を返す"""
    state = _read_json(WEB_AUTH_STATE_PATH)
    apps = state.get("applications", {})
    for app in apps.values():
        if app.get("username") == username and app.get("status") == "approved":
            return app.get("temp_password")
    return None


async def consume_temp_password(username: str, temp_pw: str) -> bool:
    """
    仮パスワードを検証し、一致すれば消費して True を返す。
    一度使ったら再利用不可にする（セキュリティ上）。
    """
    async with _lock:
        state = _read_json(WEB_AUTH_STATE_PATH)
        apps = state.get("applications", {})
        for app_id, app in apps.items():
            if (app.get("username") == username
                    and app.get("status") == "approved"
                    and app.get("temp_password") == temp_pw):
                # 仮PWを消費（本PW設定フローへ移行させる）
                apps[app_id]["temp_password"] = None
                apps[app_id]["status"] = "pw_setting"
                state["applications"] = apps
                _write_json(WEB_AUTH_STATE_PATH, state)
                return True
        return False


async def is_pw_setting_mode(username: str) -> bool:
    """本パスワード設定待ち状態かどうかを確認する"""
    state = _read_json(WEB_AUTH_STATE_PATH)
    apps = state.get("applications", {})
    for app in apps.values():
        if app.get("username") == username and app.get("status") == "pw_setting":
            return True
    return False


# ---------- ユーザー・パスワード管理 ----------

def _is_parent_by_name(username: str) -> bool:
    """ユーザー名が users/parents/ に登録された親かどうかを判定する。
    parents/*.json の name フィールドと照合するだけでよい。"""
    # 循環インポートを避けるため関数内でインポートする
    from app.config import load_all_parents
    for p in load_all_parents():
        if p.get("name") == username:
            return True
    return False


async def set_password(username: str, password: str) -> bool:
    """
    本パスワードを設定して web_users.json に保存する。
    users/*.json と parent_ids の照合で親なら is_admin=True を自動付与する。
    申請状態を completed に更新する。
    """
    async with _lock:
        # パスワードハッシュを生成する
        salt = secrets.token_hex(16)
        pw_hash = _hash_password(password, salt)

        # 親ユーザーかどうかを自動判定する（username == users/*.json の name かつ parent_ids に含まれる）
        is_admin = _is_parent_by_name(username)

        # ユーザー情報を保存する
        users = _read_json(WEB_USERS_PATH)
        users[username] = {
            "username": username,
            "salt": salt,
            "pw_hash": pw_hash,
            "created_at": _now_timestamp(),
            "is_admin": is_admin,  # 親なら True、子供なら False
        }
        _write_json(WEB_USERS_PATH, users)

        # 申請状態を completed に更新する
        state = _read_json(WEB_AUTH_STATE_PATH)
        apps = state.get("applications", {})
        for app_id, app in apps.items():
            if app.get("username") == username:
                apps[app_id]["status"] = "completed"
                break
        state["applications"] = apps
        _write_json(WEB_AUTH_STATE_PATH, state)
        return True


async def verify_password(username: str, password: str) -> bool:
    """ユーザー名とパスワードが一致するかを検証する"""
    users = _read_json(WEB_USERS_PATH)
    user = users.get(username)
    if not user:
        return False
    salt = user.get("salt", "")
    stored_hash = user.get("pw_hash", "")
    return _hash_password(password, salt) == stored_hash


async def get_web_user(username: str) -> Optional[dict]:
    """Webユーザー情報を取得する。存在しなければ None を返す"""
    users = _read_json(WEB_USERS_PATH)
    return users.get(username)


async def user_exists(username: str) -> bool:
    """Webユーザーが登録済みかどうかを確認する"""
    users = _read_json(WEB_USERS_PATH)
    return username in users


# ---------- セッション管理 ----------

async def create_session(username: str) -> str:
    """セッショントークンを発行して保存する。戻り値: トークン文字列"""
    async with _lock:
        token = secrets.token_urlsafe(32)
        sessions = _read_json(WEB_SESSIONS_PATH)
        sessions[token] = {
            "username": username,
            "created_at": _now_timestamp(),
        }
        _write_json(WEB_SESSIONS_PATH, sessions)
        return token


async def get_session_user(token: str) -> Optional[str]:
    """
    トークンからユーザー名を取得する。
    期限切れ・存在しないトークンは None を返す。
    """
    if not token:
        return None
    sessions = _read_json(WEB_SESSIONS_PATH)
    session = sessions.get(token)
    if not session:
        return None
    # セッション有効期限チェック
    elapsed = _now_timestamp() - session.get("created_at", 0)
    if elapsed > SESSION_TTL_SECONDS:
        # 期限切れセッションを削除する
        async with _lock:
            sessions.pop(token, None)
            _write_json(WEB_SESSIONS_PATH, sessions)
        return None
    return session.get("username")


async def delete_session(token: str) -> None:
    """セッションを削除する（ログアウト処理）"""
    async with _lock:
        sessions = _read_json(WEB_SESSIONS_PATH)
        sessions.pop(token, None)
        _write_json(WEB_SESSIONS_PATH, sessions)


# ---------- 申請一覧（管理者向け） ----------

async def list_pending_applications() -> list[dict]:
    """承認待ち申請の一覧を返す（管理者表示用）"""
    state = _read_json(WEB_AUTH_STATE_PATH)
    apps = state.get("applications", {})
    return [
        {"app_id": aid, **app}
        for aid, app in apps.items()
        if app.get("status") == "pending"
    ]
