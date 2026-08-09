import json
import os
import re
from pathlib import Path
from typing import Optional

# 金額入力の上限（円）。桁あふれ・異常値を弾く共通上限。bot.py と mcp_wallet.py が共有する。
# 従来は bot.py 内に定義していたが、AI 主導層（mcp_wallet）が bot.py を import せず参照できるよう
# config へ移した（bot.py の import で GeminiService 初期化が走る問題を避けるため）。
MAX_WALLET_INPUT_AMOUNT = 1_000_000

ROOT = Path(__file__).resolve().parents[1]
SETTINGS_DIR = ROOT / "settings"
USERS_DIR = SETTINGS_DIR / "users"
# 親ユーザーは子供と分けて管理する
PARENTS_DIR = USERS_DIR / "parents"
SYSTEM_PATH = SETTINGS_DIR / "system.json"
SETTING_PATH = SETTINGS_DIR / "setting.json"

def _log_config_error(path: Path, error: Exception, event: str = "config_load_error") -> None:
    """設定ファイル異常を最低限の診断ログへ残す。config内なので固定ログ先を使う。"""
    try:
        log_dir = ROOT / "data" / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        with open(log_dir / "runtime_diagnostics.jsonl", "a", encoding="utf-8") as f:
            f.write(json.dumps({
                "event": event,
                "path": str(path),
                "error_type": type(error).__name__,
                "error_message": str(error),
            }, ensure_ascii=False) + "\n")
    except Exception:
        pass


def _safe_int(value, default: int | None = None) -> int | None:
    """設定値を安全に int 化する。失敗時は default を返す。"""
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _load_json(path: Path, default: dict | None = None) -> dict:
    """JSON設定を安全に読む。破損時は診断ログを残して default を返す。"""
    fallback = {} if default is None else default
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else fallback
    except (OSError, json.JSONDecodeError) as e:
        _log_config_error(path, e)
        return fallback

def load_system() -> dict:
    return _load_json(SYSTEM_PATH, {"log_dir": "data/logs"})

def load_setting() -> dict:
    if not SETTING_PATH.exists():
        return {}
    return _load_json(SETTING_PATH)

def load_all_users() -> list[dict]:
    """子供ユーザー一覧を返す。users/*.json を対象とし parents/ サブディレクトリは除外する"""
    users = []
    for p in sorted(USERS_DIR.glob("*.json"), key=lambda path: path.name):
        # .example.json はサンプルファイルのため実ユーザーとして読み込まない
        if p.name.endswith(".example.json"):
            continue
        data = _load_json(p, {})
        if data:
            users.append(data)
    return users

def load_all_parents() -> list[dict]:
    """親ユーザー一覧を返す。users/parents/*.json を対象とする"""
    parents = []
    if not PARENTS_DIR.exists():
        return parents
    for p in sorted(PARENTS_DIR.glob("*.json"), key=lambda path: path.name):
        # .example.json はサンプルファイルのため除外する
        if p.name.endswith(".example.json"):
            continue
        data = _load_json(p, {})
        if data:
            parents.append(data)
    return parents

def find_child_user_by_discord_id(discord_user_id: int) -> Optional[dict]:
    """discord_user_id で子供ユーザーだけを検索する"""
    for u in load_all_users():
        if _safe_int(u.get("discord_user_id"), -1) == int(discord_user_id):
            return u
    return None

def find_parent_by_discord_id(discord_user_id: int) -> Optional[dict]:
    """discord_user_id で親ユーザーだけを検索する"""
    for u in load_all_parents():
        if _safe_int(u.get("discord_user_id"), -1) == int(discord_user_id):
            return u
    return None

def find_user_by_discord_id(discord_user_id: int) -> Optional[dict]:
    """discord_user_id でユーザーを検索する。親IDの誤作動防止のため親→子供の順で検索する"""
    parent = find_parent_by_discord_id(discord_user_id)
    if parent is not None:
        return parent
    return find_child_user_by_discord_id(discord_user_id)

def find_user_by_name(name: str) -> Optional[dict]:
    """名前でユーザーを検索する。子供→親の順で両ディレクトリを検索する"""
    target = (name or "").strip()
    if not target:
        return None
    for u in load_all_users() + load_all_parents():
        if str(u.get("name", "")).strip() == target:
            return u
    return None

def find_child_user_by_name(name: str) -> Optional[dict]:
    """名前で子ユーザーだけを検索する。親は一切対象にしない。

    金額を動かす操作（AI 主導層の wallet tool）の対象特定に使う。find_user_by_name は
    親も返すため、親名で残高操作されて親名義の偽の財布が実帳簿に混入するのを防ぐ。
    子ディレクトリ（load_all_users）のみを走査する。

    Args:
        name: 子ユーザー名。

    Returns:
        Optional[dict]: 一致する子ユーザー設定。子に無ければ（親名でも）None。
    """
    target = (name or "").strip()
    if not target:
        return None
    # 子ディレクトリのみ走査する。親は含めない
    for u in load_all_users():
        if str(u.get("name", "")).strip() == target:
            return u
    return None

def get_parent_ids() -> set[int]:
    """親ユーザーの Discord ID 集合を返す。users/parents/*.json から収集する"""
    ids: set[int] = set()
    for p in load_all_parents():
        uid = _safe_int(p.get("discord_user_id"))
        if uid is not None:
            ids.add(uid)
    return ids

def get_discord_id_conflicts() -> list[dict]:
    """子供・親設定間で discord_user_id が重複している組み合わせを返す"""
    conflicts: list[dict] = []
    children = load_all_users()
    parents = load_all_parents()
    for child in children:
        child_id = _safe_int(child.get("discord_user_id"))
        if child_id is None:
            continue
        for parent in parents:
            parent_id = _safe_int(parent.get("discord_user_id"))
            if parent_id is not None and child_id == parent_id:
                conflicts.append({
                    "discord_user_id": child_id,
                    "child_name": str(child.get("name", "")),
                    "parent_name": str(parent.get("name", "")),
                })
    return conflicts


def get_web_base_url() -> str:
    """WebダッシュボードのベースURL（URLのハードコードを避けるため設定から読む）"""
    setting = load_setting()
    return setting.get("web_base_url", "https://example.com")

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
        return {value for value in (_safe_int(x) for x in raw_list) if value is not None}
    elif isinstance(raw_list, str) and not raw_list.strip():
        return None

    raw = os.environ.get("ALLOW_CHANNEL_IDS", "").strip()
    if not raw:
        return None

    return {value for value in (_safe_int(x.strip()) for x in raw.split(",") if x.strip()) if value is not None}

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
        channel_id = _safe_int(channel_id)

    payday_day = _safe_int(rem.get("payday_day"), 1) or 1
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
        channel_id = _safe_int(channel_id)

    check_day = _safe_int(audit.get("check_day"), 1) or 1
    check_day = min(31, max(1, check_day))

    check_time = str(audit.get("check_time", "20:00")).strip()
    if not re.match(r"^\d{2}:\d{2}$", check_time):
        check_time = "20:00"

    try:
        penalty_rate = float(audit.get("penalty_rate", 1.0))
    except (TypeError, ValueError):
        penalty_rate = 1.0
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
        channel_id = _safe_int(channel_id)

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
        channel_id = _safe_int(channel_id)

    threshold = _safe_int(alert.get("threshold"), 500) or 500
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
    day_of_week = _safe_int(pjr.get("day_of_week"), 0) or 0
    day_of_week = max(0, min(6, day_of_week))

    notify_time = str(pjr.get("notify_time", "19:00")).strip()
    if not re.match(r"^\d{2}:\d{2}$", notify_time):
        notify_time = "19:00"

    return {
        "enabled": enabled,
        "day_of_week": day_of_week,
        "notify_time": notify_time,
    }


def get_proactive_child_nudge_setting() -> dict:
    """
    子どもへの能動的な伴走メッセージ設定を返す。
    setting.json の "proactive_child_nudge" セクションを読み込む。
    """
    setting = load_setting()
    raw = setting.get("proactive_child_nudge", {}) if isinstance(setting, dict) else {}
    if not isinstance(raw, dict):
        raw = {}

    enabled = bool(raw.get("enabled", False))
    notify_time = str(raw.get("notify_time", "18:30")).strip()
    if not re.match(r"^\d{2}:\d{2}$", notify_time):
        notify_time = "18:30"

    def bounded_int(key: str, default: int, minimum: int, maximum: int) -> int:
        try:
            value = int(raw.get(key, default))
        except (TypeError, ValueError):
            value = default
        return max(minimum, min(maximum, value))

    return {
        "enabled": enabled,
        "notify_time": notify_time,
        "no_record_days": bounded_int("no_record_days", 10, 3, 60),
        "challenge_stale_days": bounded_int("challenge_stale_days", 5, 1, 30),
        "growth_plan_review_days_before": bounded_int("growth_plan_review_days_before", 2, 0, 14),
        "min_days_between_nudges": bounded_int("min_days_between_nudges", 3, 1, 14),
        "max_per_run": bounded_int("max_per_run", 20, 1, 50),
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

def get_child_income_report_setting() -> dict:
    """
    子供の自己申告入金（臨時入金）の上限設定を返す。

    Returns:
        dict: {
            "max_amount": int,       # 1回の自己申告で反映できる上限額
            "daily_count_max": int,  # 1日に自己申告できる回数
            "daily_total_max": int,  # 1日に自己申告できる累計額
            "monthly_total_max": int,# 1ヶ月に自己申告できる累計額
        }
        0 以下が設定された場合、それぞれ安全側の既定値へ倒す（無制限にはしない）。
    """
    setting = load_setting()
    conf = setting.get("child_income_report", {}) if isinstance(setting, dict) else {}
    if not isinstance(conf, dict):
        conf = {}

    # 未設定でも安全側に倒したいので、既定値を入れて上限が必ず効くようにする
    max_amount = _safe_int(conf.get("max_amount"), 5000)
    if max_amount is None or max_amount <= 0:
        max_amount = 5000
    # 査定と同様、回数・累計の上限も持たせる。自己申告入金の連打で残高を無制限に膨らませられないようにする
    daily_count_max = _safe_int(conf.get("daily_count_max"), 5)
    if daily_count_max is None or daily_count_max <= 0:
        daily_count_max = 5
    daily_total_max = _safe_int(conf.get("daily_total_max"), 5000)
    if daily_total_max is None or daily_total_max <= 0:
        daily_total_max = 5000
    monthly_total_max = _safe_int(conf.get("monthly_total_max"), 20000)
    if monthly_total_max is None or monthly_total_max <= 0:
        monthly_total_max = 20000
    return {
        "max_amount": int(max_amount),
        "daily_count_max": int(daily_count_max),
        "daily_total_max": int(daily_total_max),
        "monthly_total_max": int(monthly_total_max),
    }


def get_conversation_session_setting() -> dict:
    """
    会話セッション（対話層が所有する「いま何の話をしているか」状態）の設定を返す。

    setting.json の "conversation_session": {"expiry_minutes": 30} を読み込む。
    期限切れの会話セッションはこの分数で失効させる。未設定でも安全に既定値が効く。

    Returns:
        dict: {"expiry_minutes": int} 形式。1分〜1440分（24時間）にクランプする。
    """
    setting = load_setting()
    conf = setting.get("conversation_session", {}) if isinstance(setting, dict) else {}
    # 型不正な値は空扱いにして既定へ倒す
    if not isinstance(conf, dict):
        conf = {}

    # 未設定でも期限が必ず効くよう既定30分を入れる
    expiry_minutes = _safe_int(conf.get("expiry_minutes"), 30)
    if expiry_minutes is None:
        expiry_minutes = 30
    # 短すぎ・長すぎを防ぐため 1〜1440 にクランプする
    expiry_minutes = max(1, min(1440, expiry_minutes))

    return {
        "expiry_minutes": expiry_minutes,
    }


def get_conversation_log_setting() -> dict:
    """
    会話ログ（{name}_conversation.jsonl）の保持方針の設定を返す。

    setting.json の "conversation_log": {"retention_days": 90, "max_lines": 2000} を読み込む。
    保持日数を超えたアーカイブは削除し、行数上限を超えた分はアーカイブへ退避する（社長決定 2026/08/05）。

    Returns:
        dict: {"retention_days": int, "max_lines": int} 形式。
              retention_days は 1〜3650 日、max_lines は 100〜1000000 行にクランプする。
    """
    setting = load_setting()
    conf = setting.get("conversation_log", {}) if isinstance(setting, dict) else {}
    # 型不正な値は空扱いにして既定へ倒す
    if not isinstance(conf, dict):
        conf = {}

    # 社長決定により保持日数の既定は90日
    retention_days = _safe_int(conf.get("retention_days"), 90)
    if retention_days is None:
        retention_days = 90
    # 0日以下や極端な長期を防ぐため 1〜3650 日にクランプする
    retention_days = max(1, min(3650, retention_days))

    # 行数上限の既定は2000行（1子あたりの会話ログを妥当な範囲に抑える）
    max_lines = _safe_int(conf.get("max_lines"), 2000)
    if max_lines is None:
        max_lines = 2000
    # 小さすぎる・大きすぎる値を防ぐため 100〜1000000 行にクランプする
    max_lines = max(100, min(1000000, max_lines))

    return {
        "retention_days": retention_days,
        "max_lines": max_lines,
    }


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


def get_assessment_guardrail_setting() -> dict:
    """
    査定支給のガードレール設定を返す。AI が査定で決めた支給額を Python 側で頭打ちにするための上限群。

    「何でもかんでも増額・追加支給しない」ため、AI の判断に委ねず tool 内でこれらの上限を強制する。
    1回の固定増額の上限は各児童の fixed_increase_cap（ユーザー設定）を使うため、ここには持たない。

    Returns:
        dict: {
            "temporary_max": int,      # 臨時支給の1回あたり上限（円）
            "monthly_total_max": int,  # 1ヶ月の査定支給の累計上限（円）
            "daily_count_max": int,    # 1日に査定支給できる回数
        }
    """
    setting = load_setting()
    conf = setting.get("assessment_guardrail", {}) if isinstance(setting, dict) else {}
    if not isinstance(conf, dict):
        conf = {}

    # 臨時支給の1回上限。既定1000円（fixed_increase_cap=100より大きめだが桁外れを防ぐ）
    temporary_max = _safe_int(conf.get("temporary_max"), 1000)
    if temporary_max is None or temporary_max <= 0:
        temporary_max = 1000

    # 月次累計上限。何度も査定して積み上げるのを防ぐ。既定3000円
    monthly_total_max = _safe_int(conf.get("monthly_total_max"), 3000)
    if monthly_total_max is None or monthly_total_max <= 0:
        monthly_total_max = 3000

    # 1日の査定回数上限。連打での支給を防ぐ。既定3回
    daily_count_max = _safe_int(conf.get("daily_count_max"), 3)
    if daily_count_max is None or daily_count_max <= 0:
        daily_count_max = 3

    return {
        "temporary_max": temporary_max,
        "monthly_total_max": monthly_total_max,
        "daily_count_max": daily_count_max,
    }


def get_parent_operation_setting() -> dict:
    """親が直接行う支給・残高調整の上限設定を返す（N-11.17 の前提）。

    親経路の tool（parent_grant / parent_adjust_balance）は、これまで
    「1〜MAX_WALLET_INPUT_AMOUNT（100万円）」しか検証しておらず、査定経路のような
    ガードレールを一切通していなかった。親は管理者だが、桁の打ち間違い（5000→50000）や
    AI が値を取り違えた場合に歯止めが無く、実残高が大きく動く。

    親の正当な操作を妨げない範囲で「明らかな桁違い」を止めるための上限を持つ。
    上限を超える場合は拒否し、親に金額の確認を促す（実行しない）。

    Returns:
        dict: {
            "single_max": int,  # 1回の支給・調整の上限（円）
        }
    """
    setting = load_setting()
    conf = setting.get("parent_operation", {}) if isinstance(setting, dict) else {}
    if not isinstance(conf, dict):
        conf = {}
    # 既定 50000円。お年玉・まとめ支給を想定しても十分で、桁違い（数十万）は止まる
    single_max = _safe_int(conf.get("single_max"), 50000)
    if single_max is None or single_max <= 0:
        single_max = 50000
    # 実行前に親へ確認を出す金額のしきい値。
    # これ未満は確認せずその場で実行する（お小遣い管理なので毎回の確認は煩わしいだけ）。
    # 取り違えても台帳（*_wallet_ledger.jsonl）に日時・増減・理由が残り、後から追える。
    # 確認は「桁を間違えた」「まとめて大きく動かす」ような、
    # 気づかないと困る額のときだけ出す。
    confirm_over = _safe_int(conf.get("confirm_over"), 10000)
    if confirm_over is None or confirm_over < 0:
        confirm_over = 10000
    return {"single_max": single_max, "confirm_over": confirm_over}


def get_safety_alert_setting() -> dict:
    """
    子の心身の安全に関わる危険信号の通知設定を返す（N-11.16）。

    査定通知（allowance_reminder.channel_id）に相乗りさせず専用キーを持つ。理由は2つ。
    ① 査定と安全は緊急度も宛先も異なる。査定通知を止めたい家庭でも安全通知は止めてはならない。
    ② 虐待の示唆は「親へ通知しない」という別経路が要るため、宛先の設計自体が査定と違う。

    虐待に関する通知先をここに持たないのは意図的である。加害者は同居の実親であることが多く、
    親チャンネルへ流すと加害者への情報還流になるため、子には外部の公的窓口を直接渡す（下記定数）。

    Returns:
        dict: {
            "enabled": bool,       # 危険信号の通知を行うか（既定 True。安全機能は既定で有効）
            "channel_id": int|None,# 通知先チャンネル。未設定なら通知できず診断へ記録する
        }
    """
    setting = load_setting()
    conf = setting.get("safety_alert", {}) if isinstance(setting, dict) else {}
    if not isinstance(conf, dict):
        conf = {}

    # 安全機能は既定で有効にする。明示的に false と書かれた時だけ無効
    enabled = conf.get("enabled")
    enabled = True if enabled is None else bool(enabled)

    # 通知先。未設定（None）でも会話は止めず、送信時に診断へ残して silent drop を避ける
    channel_id = _safe_int(conf.get("channel_id"), None)
    if channel_id is not None and channel_id <= 0:
        channel_id = None

    return {"enabled": enabled, "channel_id": channel_id}
