import json
import os
import re
from pathlib import Path
from typing import Optional

ROOT = Path(__file__).resolve().parents[1]
SETTINGS_DIR = ROOT / "settings"
USERS_DIR = SETTINGS_DIR / "users"
# 親ユーザーは子供と分けて管理する
PARENTS_DIR = USERS_DIR / "parents"
SYSTEM_PATH = SETTINGS_DIR / "system.json"
SETTING_PATH = SETTINGS_DIR / "setting.json"

def _load_json(path: Path) -> dict:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)

def load_system() -> dict:
    return _load_json(SYSTEM_PATH)

def load_setting() -> dict:
    if not SETTING_PATH.exists():
        return {}
    return _load_json(SETTING_PATH)

def load_all_users() -> list[dict]:
    """子供ユーザー一覧を返す。users/*.json を対象とし parents/ サブディレクトリは除外する"""
    users = []
    for p in USERS_DIR.glob("*.json"):
        # .example.json はサンプルファイルのため実ユーザーとして読み込まない
        if p.name.endswith(".example.json"):
            continue
        users.append(_load_json(p))
    return users

def load_all_parents() -> list[dict]:
    """親ユーザー一覧を返す。users/parents/*.json を対象とする"""
    parents = []
    if not PARENTS_DIR.exists():
        return parents
    for p in PARENTS_DIR.glob("*.json"):
        # .example.json はサンプルファイルのため除外する
        if p.name.endswith(".example.json"):
            continue
        parents.append(_load_json(p))
    return parents

def find_user_by_discord_id(discord_user_id: int) -> Optional[dict]:
    """discord_user_id でユーザーを検索する。子供→親の順で両ディレクトリを検索する"""
    for u in load_all_users() + load_all_parents():
        if int(u.get("discord_user_id", -1)) == int(discord_user_id):
            return u
    return None

def find_user_by_name(name: str) -> Optional[dict]:
    """名前でユーザーを検索する。子供→親の順で両ディレクトリを検索する"""
    target = (name or "").strip()
    if not target:
        return None
    for u in load_all_users() + load_all_parents():
        if str(u.get("name", "")).strip() == target:
            return u
    return None

def get_parent_ids() -> set[int]:
    """親ユーザーの Discord ID 集合を返す。users/parents/*.json から収集する"""
    ids: set[int] = set()
    for p in load_all_parents():
        uid = p.get("discord_user_id")
        if uid:
            ids.add(int(uid))
    return ids

def get_web_base_url() -> str:
    """WebダッシュボードのベースURL（URLのハードコードを避けるため設定から読む）"""
    setting = load_setting()
    return setting.get("web_base_url", "https://compass.rwc.0t0.jp")

def get_allow_channel_ids() -> set[int] | None:
    """
    ALLOW_CHANNEL_IDS が未設定なら None（制限なし）
    設定されていればカンマ区切りで複数許可
    """
    setting = load_setting()
    raw_list = setting.get("allow_channel_ids")
    if raw_list is None:
        pass
    elif isinstance(raw_list, list):
        return {int(x) for x in raw_list}
    elif isinstance(raw_list, str) and not raw_list.strip():
        return None

    raw = os.environ.get("ALLOW_CHANNEL_IDS", "").strip()
    if not raw:
        return None

    return {int(x.strip()) for x in raw.split(",") if x.strip()}

def get_gemini_model() -> str:
    setting = load_setting()
    model_name = (setting.get("gemini_model") or "").strip()
    if model_name:
        return model_name
    return os.environ.get("GEMINI_MODEL", "models/gemini-2.5-flash")

def get_allowance_reminder_setting() -> dict:
    """
    reminderの設定を返す。未設定時は安全なデフォルト。
    """
    setting = load_setting()
    rem = setting.get("allowance_reminder", {}) if isinstance(setting, dict) else {}
    if not isinstance(rem, dict):
        rem = {}

    enabled = bool(rem.get("enabled", False))
    channel_id = rem.get("channel_id")
    if channel_id in ("", None):
        channel_id = None
    elif channel_id is not None:
        channel_id = int(channel_id)

    payday_day = int(rem.get("payday_day", 1))
    payday_day = min(31, max(1, payday_day))

    notify_time = str(rem.get("notify_time", "20:00")).strip()
    if not re.match(r"^\d{2}:\d{2}$", notify_time):
        notify_time = "20:00"

    # notify_offset は文字列（単一 or カンマ区切り）または配列を受け付ける
    raw_offset = rem.get("notify_offset", "-7day")
    if isinstance(raw_offset, list):
        raw_offsets = raw_offset
    else:
        raw_offsets = [s.strip() for s in str(raw_offset).split(",") if s.strip()]

    before_days_list = []
    for o in raw_offsets:
        mo = re.match(r"^-?(\d+)day$", o.lower())
        if mo:
            before_days_list.append(int(mo.group(1)))
    if not before_days_list:
        before_days_list = [7]

    # 支給日当日（before_days=0）に全ユーザーの固定額を自動加算するか否かのフラグ
    auto_grant_on_payday = bool(rem.get("auto_grant_on_payday", False))

    return {
        "enabled": enabled,
        "channel_id": channel_id,
        "payday_day": payday_day,
        "notify_time": notify_time,
        "before_days_list": before_days_list,
        "auto_grant_on_payday": auto_grant_on_payday,
    }

def get_wallet_audit_setting() -> dict:
    """
    毎月の財布残高照合設定を返す。
    """
    setting = load_setting()
    audit = setting.get("wallet_audit", {}) if isinstance(setting, dict) else {}
    if not isinstance(audit, dict):
        audit = {}

    enabled = bool(audit.get("enabled", False))
    channel_id = audit.get("channel_id")
    if channel_id in ("", None):
        channel_id = None
    elif channel_id is not None:
        channel_id = int(channel_id)

    check_day = int(audit.get("check_day", 1))
    check_day = min(31, max(1, check_day))

    check_time = str(audit.get("check_time", "20:00")).strip()
    if not re.match(r"^\d{2}:\d{2}$", check_time):
        check_time = "20:00"

    penalty_rate = float(audit.get("penalty_rate", 1.0))
    if penalty_rate < 0:
        penalty_rate = 0.0

    return {
        "enabled": enabled,
        "channel_id": channel_id,
        "check_day": check_day,
        "check_time": check_time,
        "penalty_rate": penalty_rate,
    }

def get_chat_setting() -> dict:
    """
    会話入力のモード設定を返す。
    """
    setting = load_setting()
    chat = setting.get("chat", {}) if isinstance(setting, dict) else {}
    if not isinstance(chat, dict):
        chat = {}

    natural_chat_enabled = bool(chat.get("natural_chat_enabled", False))
    require_mention = bool(chat.get("require_mention", not natural_chat_enabled))

    return {
        "natural_chat_enabled": natural_chat_enabled,
        "require_mention": require_mention,
    }

def get_assess_keyword() -> str:
    """
    査定モード判定用のキーワードを返す（setting.json 単一ソース）。
    """
    setting = load_setting()
    raw = str(setting.get("assess_keyword", "")).strip() if isinstance(setting, dict) else ""
    if raw:
        return raw
    raise RuntimeError("settings/setting.json に assess_keyword を設定してください。")

def get_force_assess_test_keyword() -> str:
    """
    動作確認用: 入力にこのキーワードが含まれる場合、査定モードを強制する。
    """
    setting = load_setting()
    return str(setting.get("force_assess_test_keyword", "")).strip() if isinstance(setting, dict) else ""

def get_monthly_summary_setting() -> dict:
    """
    月次サマリーレポートの設定を返す。
    setting.json の "monthly_summary": {"enabled": true, "channel_id": ..., "send_time": "09:00"}
    """
    setting = load_setting()
    ms = setting.get("monthly_summary", {}) if isinstance(setting, dict) else {}
    if not isinstance(ms, dict):
        ms = {}

    enabled = bool(ms.get("enabled", False))
    channel_id = ms.get("channel_id")
    if channel_id in ("", None):
        channel_id = None
    elif channel_id is not None:
        channel_id = int(channel_id)

    send_time = str(ms.get("send_time", "09:00")).strip()
    if not re.match(r"^\d{2}:\d{2}$", send_time):
        send_time = "09:00"

    return {
        "enabled": enabled,
        "channel_id": channel_id,
        "send_time": send_time,
    }


def get_low_balance_alert_setting() -> dict:
    """
    低残高アラート設定を返す。
    setting.json の "low_balance_alert": {"enabled": true, "threshold": 500, "channel_id": ...}
    """
    setting = load_setting()
    alert = setting.get("low_balance_alert", {}) if isinstance(setting, dict) else {}
    if not isinstance(alert, dict):
        alert = {}

    enabled = bool(alert.get("enabled", False))
    channel_id = alert.get("channel_id")
    if channel_id in ("", None):
        channel_id = None
    elif channel_id is not None:
        channel_id = int(channel_id)

    threshold = int(alert.get("threshold", 500))
    if threshold < 0:
        threshold = 0

    return {
        "enabled": enabled,
        "channel_id": channel_id,
        "threshold": threshold,
    }


def get_pocket_journal_reminder_setting() -> dict:
    """
    週次支出記録リマインドの設定を返す。
    setting.json の "pocket_journal_reminder" セクションを読み込む。
    day_of_week は Python の weekday() 準拠（0=月曜〜6=日曜）。
    """
    setting = load_setting()
    pjr = setting.get("pocket_journal_reminder", {}) if isinstance(setting, dict) else {}
    if not isinstance(pjr, dict):
        pjr = {}

    enabled = bool(pjr.get("enabled", False))

    # 0〜6 の範囲にクランプする
    day_of_week = int(pjr.get("day_of_week", 0))
    day_of_week = max(0, min(6, day_of_week))

    notify_time = str(pjr.get("notify_time", "19:00")).strip()
    if not re.match(r"^\d{2}:\d{2}$", notify_time):
        notify_time = "19:00"

    return {
        "enabled": enabled,
        "day_of_week": day_of_week,
        "notify_time": notify_time,
    }


def find_user_json_path_by_name(name: str) -> Path | None:
    """ユーザー名に対応する users/*.json のファイルパスを返す。見つからなければ None を返す"""
    target = (name or "").strip()
    if not target:
        return None
    # 全ユーザーファイルを走査して name フィールドが一致するパスを返す
    for p in USERS_DIR.glob("*.json"):
        # .example.json はサンプルファイルのためスキップする
        if p.name.endswith(".example.json"):
            continue
        try:
            data = _load_json(p)
            if str(data.get("name", "")).strip() == target:
                return p
        except Exception:
            continue
    return None


def update_user_field(name: str, field: str, value) -> bool:
    """ユーザーの設定ファイルの指定フィールドを更新して保存する。成功すれば True を返す"""
    path = find_user_json_path_by_name(name)
    if path is None:
        return False
    try:
        data = _load_json(path)
        # 指定フィールドを上書きして保存する
        data[field] = value
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        return True
    except Exception:
        return False


def get_log_dir(system_conf: dict) -> Path:
    rel = system_conf.get("log_dir", "data/logs")
    return ROOT / rel

def find_user_by_key(key: str) -> dict | None:
    """
    key は settings/users/*.json のファイル名（拡張子なし）
    例: yuu → settings/users/yuu.json
    """
    from pathlib import Path
    import json

    key = (key or "").strip()
    if not key:
        return None

    path = USERS_DIR / f"{key}.json"
    if not path.exists():
        return None

    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)
