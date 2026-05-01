"""
Compass Bot Webダッシュボード + ヘルスチェック FastAPI サーバー。
認証フロー: 申請 → Discord通知 → 親がweb承認 → 仮PW発行 → 本PW設定 → ダッシュボード
"""

import datetime
import json
import re
import uuid
from pathlib import Path
from typing import Any, Optional
from urllib.parse import quote

import discord
from fastapi import Cookie, FastAPI, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from app import web_auth
from app.config import (
    get_allow_channel_ids,
    get_allowance_reminder_setting,
    get_low_balance_alert_setting,
    get_web_base_url,
    find_user_by_name,
    load_all_users,
    load_system,
    update_user_field,
)
from app.storage import JST, now_jst_iso

try:
    from app.reflection_context import build_reflection_context
except ImportError:
    build_reflection_context = None

try:
    from app.learning_insights import build_learning_insights
except ImportError:
    build_learning_insights = None

# アプリケーションインスタンス
app = FastAPI()

# テンプレートディレクトリ
ROOT = Path(__file__).resolve().parents[1]
templates = Jinja2Templates(directory=str(ROOT / "templates"))
LEARNING_SUPPORT_STATE_DIR = ROOT / "data" / "learning_support_state"
GROWTH_PLANS_DIR = ROOT / "data" / "growth_plans"

# bot.py から on_ready で注入されるグローバル変数
_discord_client: Optional[discord.Client] = None
_wallet_service = None  # WalletService インスタンス

FOLLOW_FOCUS_CHOICES = [
    {"value": "record_habit", "label": "記録習慣"},
    {"value": "planning", "label": "買う前の計画"},
    {"value": "satisfaction_reflection", "label": "満足度の振り返り"},
    {"value": "saving_goal", "label": "貯金目標"},
    {"value": "impulse_spending", "label": "衝動買いの抑制"},
    {"value": "income_balance", "label": "収入と支出のバランス"},
]
FOLLOW_FOCUS_VALUES = {choice["value"] for choice in FOLLOW_FOCUS_CHOICES}

FOLLOW_STRENGTH_CHOICES = [
    {"value": "light", "label": "軽め"},
    {"value": "normal", "label": "標準"},
    {"value": "careful", "label": "慎重"},
]
FOLLOW_STRENGTH_VALUES = {choice["value"] for choice in FOLLOW_STRENGTH_CHOICES}

FOLLOW_FREQUENCY_CHOICES = [
    {"value": "low", "label": "必要なときだけ"},
    {"value": "normal", "label": "通常"},
]
FOLLOW_FREQUENCY_VALUES = {choice["value"] for choice in FOLLOW_FREQUENCY_CHOICES}

LEARNING_CARD_FEEDBACK_CHOICES = {
    "use_this_week": "今週使う",
    "suppress_week": "今週は出さない",
    "helpful": "役に立った",
}

CHILD_CHALLENGE_FEEDBACK_CHOICES = {
    "done": "やった",
    "later": "あとで",
    "different": "ちがう",
}

GROWTH_PLAN_STATUS_CHOICES = {"draft", "active", "done", "cancelled"}
GROWTH_PLAN_REQUEST_TYPES = {"allowance_increase", "extra_income", "saving_goal_support"}


def init(discord_client: discord.Client, wallet_service) -> None:
    """on_ready から依存オブジェクトを受け取って初期化する"""
    global _discord_client, _wallet_service
    _discord_client = discord_client
    _wallet_service = wallet_service


# ---------- ヘルスチェック ----------

@app.get("/health")
def health():
    """死活監視エンドポイント"""
    return {"status": "ok"}


# ---------- ユーティリティ ----------

async def _get_current_user(session_token: Optional[str]) -> Optional[str]:
    """セッショントークンからログイン中のユーザー名を取得する。未ログインは None を返す"""
    if not session_token:
        return None
    return await web_auth.get_session_user(session_token)


def _is_admin(username: str) -> bool:
    """ユーザー名が管理者（setting.json の parent_ids に登録済み）か確認する。
    Web ユーザーの is_admin フラグとの二段階判定で安全性を高める"""
    # web_users.json の is_admin フラグで確認する（parent との紐付けは承認時に設定）
    users_data = web_auth._read_json(web_auth.WEB_USERS_PATH)
    user = users_data.get(username, {})
    return bool(user.get("is_admin", False))


async def _notify_discord(message: str) -> bool:
    """Discord の allowance_reminder.channel_id にメッセージを送信する"""
    if _discord_client is None:
        return False
    try:
        # 通知先チャンネルを取得する（allowance_reminder → allow_channel_ids の順でフォールバック）
        reminder_conf = get_allowance_reminder_setting()
        channel_id = reminder_conf.get("channel_id")
        if not channel_id:
            allow_ids = get_allow_channel_ids()
            channel_id = next(iter(allow_ids), None) if allow_ids else None
        if not channel_id:
            return False
        channel = _discord_client.get_channel(int(channel_id))
        if channel:
            await channel.send(message)
            return True
    except Exception:
        pass
    return False


def _load_dashboard_jsonl(path: Path) -> list[dict]:
    """ダッシュボード表示用に JSONL を安全に読み込む"""
    rows: list[dict] = []
    if not path.exists():
        return rows
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(row, dict):
                rows.append(row)
    return rows


def _safe_int(value) -> Optional[int]:
    """整数化できる値だけ int にする"""
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _normalize_form_bool(value: str | bool | None) -> bool:
    """HTMLフォーム由来の真偽値を bool に寄せる"""
    if isinstance(value, bool):
        return value
    return str(value or "").strip().lower() in {"1", "true", "on", "yes", "enabled"}


def _first_focus_area(policy: dict) -> str:
    """保存済み方針から単一選択UIで扱う重視観点を取り出す"""
    raw_focus = policy.get("focus_area")
    if isinstance(raw_focus, str) and raw_focus in FOLLOW_FOCUS_VALUES:
        return raw_focus

    raw_focuses = policy.get("focus_areas")
    if isinstance(raw_focuses, list):
        for item in raw_focuses:
            item_text = str(item).strip()
            if item_text in FOLLOW_FOCUS_VALUES:
                return item_text

    return "record_habit"


def _normalize_follow_policy(user_conf: dict) -> dict:
    """ユーザー設定から AI フォロー方針をUI・プロンプト共通の形にする"""
    raw_policy = user_conf.get("ai_follow_policy")
    policy = raw_policy if isinstance(raw_policy, dict) else {}
    legacy_note = str(user_conf.get("parent_followup_note") or "").strip()
    if isinstance(raw_policy, dict) and "parent_note" in policy:
        parent_note = str(policy.get("parent_note") or "").strip()
    else:
        parent_note = legacy_note
    focus_area = _first_focus_area(policy)
    nudge_strength = str(policy.get("nudge_strength") or "light").strip()
    if nudge_strength not in FOLLOW_STRENGTH_VALUES:
        nudge_strength = "light"
    frequency = str(policy.get("frequency") or "low").strip()
    if frequency not in FOLLOW_FREQUENCY_VALUES:
        frequency = "low"

    return {
        "enabled": bool(policy.get("enabled", bool(parent_note))),
        "focus_area": focus_area,
        "focus_areas": [focus_area],
        "nudge_strength": nudge_strength,
        "frequency": frequency,
        "parent_note": parent_note,
        "updated_by_parent": str(policy.get("updated_by_parent") or policy.get("updated_by_parent_id") or ""),
        "updated_at": str(policy.get("updated_at") or ""),
    }


def _validate_follow_policy_note(note: str) -> Optional[str]:
    """親メモが比較・罰・人格評価に寄りすぎていないか確認する"""
    if len(note) > 300:
        return "AIフォロー方針は300文字以内で入力してください。"

    comparison_pattern = r"(兄弟|兄|姉|弟|妹|友達|他人|同級生).{0,12}(比べ|比較)|(?:比べ|比較).{0,12}(兄弟|兄|姉|弟|妹|友達|他人|同級生)"
    if re.search(comparison_pattern, note):
        return "兄弟・友達・他人と比べる方針は保存できません。過去の本人との比較に言い換えてください。"

    blocked_terms = [
        "罰",
        "罰金",
        "ペナルティ",
        "叱",
        "怒",
        "厳しく",
        "だらしない",
        "浪費家",
        "嘘つき",
        "問い詰め",
        "反省させ",
        "借金させ",
    ]
    for term in blocked_terms:
        if term in note:
            return "叱る・罰を与える・人格を評価する方針は保存できません。次の小さな行動を支える表現にしてください。"

    return None


def _extract_reflection_points(context: Optional[dict]) -> list[str]:
    """reflection_context の将来出力からダッシュボード向けの箇条書きを取り出す"""
    if not isinstance(context, dict):
        return []

    for key in ("dashboard_points", "summary_points", "prompt_points"):
        value = context.get(key)
        if isinstance(value, list):
            points = [str(x).strip() for x in value if str(x).strip()]
            if points:
                return points[:5]

    for key in ("dashboard_summary", "summary", "text"):
        value = context.get(key)
        if isinstance(value, str) and value.strip():
            points = [line.strip(" ・-") for line in value.splitlines() if line.strip(" ・-")]
            if points:
                return points[:5]
    return []


def _try_build_reflection_context(
    user_conf: dict,
    system_conf: dict,
    audit_state: Optional[dict] = None,
) -> Optional[dict]:
    """build_reflection_context があれば利用する"""
    if build_reflection_context is None:
        return None

    try:
        context = build_reflection_context(
            user_conf=user_conf,
            system_conf=system_conf,
            audit_state=audit_state,
        )
    except Exception:
        return None
    if isinstance(context, dict):
        return context
    return None


def _short_text(value: Any, max_len: int = 500) -> str:
    """画面・状態保存に使う短い文字列へ正規化する"""
    if type(value).__module__ == "fastapi.params" and hasattr(value, "default"):
        value = value.default
    text = str(value or "").replace("\r\n", "\n").strip()
    if len(text) > max_len:
        return text[:max_len].rstrip()
    return text


def _coerce_text_list(value: Any, max_items: int = 5, max_len: int = 240) -> list[str]:
    """文字列・配列・dict をテンプレート表示用の短い配列にする"""
    if value is None:
        return []
    if isinstance(value, dict):
        lines = []
        for key, item in value.items():
            if item in (None, "", [], {}):
                continue
            key_text = _short_text(key, 40)
            if isinstance(item, list):
                item_text = "・".join(_short_text(x, 80) for x in item if _short_text(x, 80))
            else:
                item_text = _short_text(item, max_len)
            if item_text:
                lines.append(f"{key_text}: {item_text}")
            if len(lines) >= max_items:
                break
        return lines
    if isinstance(value, list):
        lines = []
        for item in value:
            if isinstance(item, dict):
                lines.extend(_coerce_text_list(item, max_items=max_items - len(lines), max_len=max_len))
            else:
                text = _short_text(item, max_len)
                if text:
                    lines.append(text)
            if len(lines) >= max_items:
                break
        return lines[:max_items]
    text = _short_text(value, max_len)
    if not text:
        return []
    return [line.strip(" ・-") for line in text.splitlines() if line.strip(" ・-")][:max_items]


def _metric_label(key: str) -> str:
    labels = {
        "count": "記録件数",
        "total_amount": "支出合計",
        "average_satisfaction": "満足度平均",
        "completion_rate": "記録完全率",
        "allowance_ratio": "固定お小遣い比",
        "goal_impact": "目標影響",
    }
    return labels.get(key, key)


def _normalize_learning_metrics(metrics: Any) -> list[dict]:
    """learning_insights の metrics をテンプレート共通形式へ変換する"""
    normalized: list[dict] = []
    if isinstance(metrics, dict):
        iterable = [{"label": _metric_label(str(k)), "value": v} for k, v in metrics.items()]
    elif isinstance(metrics, list):
        iterable = metrics
    else:
        iterable = []

    for item in iterable:
        if isinstance(item, dict):
            label = _short_text(item.get("label") or item.get("name") or item.get("key"), 40)
            value = item.get("value")
            if value is None:
                value = item.get("text")
        else:
            label = "指標"
            value = item
        value_text = _short_text(value, 80)
        if label and value_text:
            normalized.append({"label": label, "value": value_text})
        if len(normalized) >= 6:
            break
    return normalized


def _insight_type_label(card_type: str) -> str:
    labels = {
        "low_satisfaction_high_amount": "高額低満足",
        "repeated_small_spending": "少額の反復支出",
        "positive_planned_purchase": "よい判断の言語化",
        "saving_goal_impact": "目標貯金への影響",
        "record_habit": "記録習慣",
        "income_balance": "収入と支出のバランス",
        "online_risk": "オンライン支出の確認",
    }
    return labels.get(card_type, "会話カード")


def _normalize_insight_card(raw_card: Any, index: int) -> dict:
    """親向け会話カードを、親画面で安全に扱う形へそろえる"""
    card = raw_card if isinstance(raw_card, dict) else {"title": raw_card}
    card_type = _short_text(card.get("type") or "record_habit", 80)
    card_id = _short_text(card.get("card_id") or card.get("id") or f"{card_type}-{index + 1}", 120)
    title = _short_text(card.get("title") or _insight_type_label(card_type), 80)
    parent_question = _short_text(
        card.get("parent_question") or card.get("question") or "次の買い物で、何を試すか一緒に1つだけ決めてみる？",
        220,
    )
    parent_action = _short_text(
        card.get("parent_action") or card.get("today_action") or card.get("next_action") or "5分だけ一緒に記録を見返す。",
        220,
    )
    child_action = _short_text(card.get("child_action") or card.get("action") or parent_action, 220)
    policy_match_raw = card.get("policy_match")
    if isinstance(policy_match_raw, bool):
        policy_match = "方針に合っています" if policy_match_raw else ""
    else:
        policy_match = _short_text(policy_match_raw, 80)

    return {
        "card_id": card_id,
        "type": card_type,
        "title": title,
        "evidence_lines": _coerce_text_list(card.get("evidence"), max_items=4),
        "skill": _short_text(card.get("skill") or "記録", 80),
        "parent_question": parent_question,
        "parent_action": parent_action,
        "child_action": child_action,
        "avoid": _short_text(card.get("avoid") or "叱責・人格評価・兄弟比較は避ける。", 180),
        "policy_match": policy_match,
        "next_observation": _short_text(card.get("next_observation"), 180),
    }


def _normalize_child_challenge(raw_challenge: Any, cards: list[dict]) -> dict:
    """子ども向けチャレンジだけを切り出す。親メモや内部方針は含めない"""
    if isinstance(raw_challenge, dict):
        challenge_id = _short_text(
            raw_challenge.get("challenge_id") or raw_challenge.get("card_id") or raw_challenge.get("id"),
            120,
        )
        title = _short_text(raw_challenge.get("title") or "次の小さなチャレンジ", 80)
        action = _short_text(
            raw_challenge.get("action") or raw_challenge.get("child_action") or raw_challenge.get("text"),
            220,
        )
        expected_time = _short_text(raw_challenge.get("expected_time") or raw_challenge.get("time") or "5分以内", 40)
        source_card_id = _short_text(raw_challenge.get("source_card_id") or raw_challenge.get("card_id"), 120)
    else:
        challenge_id = ""
        title = "次の小さなチャレンジ"
        action = _short_text(raw_challenge, 220)
        expected_time = "5分以内"
        source_card_id = ""

    if not action and cards:
        action = cards[0].get("child_action", "")
        source_card_id = cards[0].get("card_id", "")
    if not challenge_id:
        challenge_id = source_card_id or "child-challenge"
    if not action:
        action = "次の買い物を記録するとき、買った理由を一言だけ足してみよう。"

    return {
        "challenge_id": challenge_id,
        "title": title,
        "action": action,
        "expected_time": expected_time,
        "source_card_id": source_card_id,
    }


def _sanitize_child_challenge(challenge: dict, user_conf: dict) -> dict:
    """子ども画面へ渡す文面から親メモ・内部方針らしさを排除する"""
    sanitized = dict(challenge)
    policy = _normalize_follow_policy(user_conf or {})
    parent_note = _short_text(policy.get("parent_note"), 300)
    blocked_fragments = [parent_note] if len(parent_note) >= 4 else []
    blocked_terms = (
        "親メモ",
        "内部方針",
        "AIフォロー方針",
        "ai_follow_policy",
        "parent_note",
        "focus_area",
        "nudge_strength",
        "frequency",
    )

    for key, fallback in (
        ("title", "次の小さなチャレンジ"),
        ("action", "次の買い物を記録するとき、買った理由を一言だけ足してみよう。"),
    ):
        text = _short_text(sanitized.get(key), 220)
        has_blocked_fragment = any(fragment and fragment in text for fragment in blocked_fragments)
        has_blocked_term = any(term in text for term in blocked_terms)
        sanitized[key] = fallback if has_blocked_fragment or has_blocked_term else text
    return sanitized


def _try_build_learning_insights(
    user_conf: dict,
    system_conf: dict,
    audit_state: Optional[dict] = None,
) -> Optional[dict]:
    """提供された learning_insights があれば呼び出す"""
    if build_learning_insights is None:
        return None
    try:
        name = str(user_conf.get("name", "")).strip()
        analysis_state = dict(audit_state or {})
        analysis_state["learning_support_state"] = _load_learning_support_state(user_conf, name)
        insights = build_learning_insights(
            user_conf=user_conf,
            system_conf=system_conf,
            audit_state=analysis_state,
            days=90,
        )
    except Exception:
        return None
    if isinstance(insights, dict):
        return insights
    return None


def _build_fallback_learning_insights(
    name: str,
    system_conf: dict,
    user_conf: dict,
    journal_rows: Optional[list[dict]] = None,
    audit_state: Optional[dict] = None,
) -> dict:
    """learning_insights 未提供時のSSRフォールバック。実ログは書き換えない"""
    summary = _build_learning_support_summary(name, system_conf, user_conf, journal_rows, audit_state)
    parent_hint = summary["parent_hints"][0] if summary.get("parent_hints") else "買い物の理由を1つだけ聞く。"
    child_hint = summary["child_hints"][0] if summary.get("child_hints") else "買った理由を一言だけ記録する。"
    signal = summary["signals"][0] if summary.get("signals") else "記録を増やすと会話カードが具体的になります。"
    return {
        "summary_text": signal,
        "metrics": summary.get("metrics", []),
        "insight_cards": [
            {
                "card_id": "fallback-record-habit",
                "type": "record_habit",
                "title": "記録を会話につなげる",
                "evidence": [signal],
                "skill": "記録",
                "parent_question": "次の買い物で、何を選んだ理由だけ一緒に確認してみる？",
                "parent_action": parent_hint,
                "child_action": child_hint,
                "avoid": "責める言い方や、他の子との比較は避ける。",
                "policy_match": "",
                "next_observation": "理由と満足度が次の記録に残るかを見る。",
            }
        ],
        "child_challenge": {
            "challenge_id": "fallback-record-habit",
            "title": "記録に一言足す",
            "action": child_hint,
            "expected_time": "5分以内",
        },
        "source_notes": ["learning_insights が未提供のため簡易集計から表示しています。"],
        "_legacy_summary": summary,
    }


def _normalize_learning_insights(
    name: str,
    system_conf: dict,
    user_conf: dict,
    journal_rows: Optional[list[dict]] = None,
    audit_state: Optional[dict] = None,
) -> dict:
    """親カード・子どもチャレンジをSSRで扱いやすい形にする"""
    raw = _try_build_learning_insights(user_conf, system_conf, audit_state)
    source = "learning_insights" if raw is not None else "dashboard_fallback"
    if raw is None:
        raw = _build_fallback_learning_insights(name, system_conf, user_conf, journal_rows, audit_state)

    cards = [_normalize_insight_card(card, idx) for idx, card in enumerate(raw.get("insight_cards") or [])]
    if not cards and source == "learning_insights":
        fallback = _build_fallback_learning_insights(name, system_conf, user_conf, journal_rows, audit_state)
        cards = [_normalize_insight_card(card, idx) for idx, card in enumerate(fallback.get("insight_cards") or [])]

    metrics = _normalize_learning_metrics(raw.get("metrics"))
    if not metrics and isinstance(raw.get("_legacy_summary"), dict):
        metrics = _normalize_learning_metrics(raw["_legacy_summary"].get("metrics"))

    summary_text = _short_text(raw.get("summary_text"), 240)
    if not summary_text and cards:
        evidence = cards[0].get("evidence_lines") or []
        summary_text = evidence[0] if evidence else cards[0].get("title", "")

    child_challenge = _sanitize_child_challenge(
        _normalize_child_challenge(raw.get("child_challenge"), cards),
        user_conf,
    )
    legacy_summary = raw.get("_legacy_summary")
    if not isinstance(legacy_summary, dict):
        legacy_summary = {
            "source": source,
            "source_label": "会話カード" if source == "learning_insights" else "簡易集計",
            "metrics": metrics,
            "signals": [summary_text] if summary_text else [],
            "parent_hints": [card["parent_question"] for card in cards[:2] if card.get("parent_question")],
            "child_hints": [child_challenge["action"]] if child_challenge.get("action") else [],
        }

    return {
        "source": source,
        "summary_text": summary_text,
        "metrics": metrics,
        "insight_cards": cards[:2],
        "child_challenge": child_challenge,
        "source_notes": _coerce_text_list(raw.get("source_notes"), max_items=3),
        "legacy_summary": legacy_summary,
    }


def _user_key_for_storage(user_conf: dict, fallback_name: str) -> str:
    """ユーザー名をファイル名として安全なキーへ変換する"""
    raw_key = str(user_conf.get("user_key") or user_conf.get("name") or fallback_name or "").strip()
    key = quote(raw_key, safe="-_.")
    return key[:120] if key else "unknown"


def _read_json_file(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError):
        return default
    return data


def _write_json_file(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.write("\n")
    tmp_path.replace(path)


def _learning_support_state_path(user_conf: dict, fallback_name: str) -> Path:
    return LEARNING_SUPPORT_STATE_DIR / f"{_user_key_for_storage(user_conf, fallback_name)}.json"


def _growth_plans_path(user_conf: dict, fallback_name: str) -> Path:
    return GROWTH_PLANS_DIR / f"{_user_key_for_storage(user_conf, fallback_name)}.json"


def _default_learning_support_state(user_conf: dict, fallback_name: str) -> dict:
    return {
        "user_key": _user_key_for_storage(user_conf, fallback_name),
        "last_card_type": "",
        "last_card_id": "",
        "last_nudge_at": "",
        "last_parent_question": "",
        "last_child_action": "",
        "child_response": {},
        "suppressed_card_types": [],
        "feedback_events": [],
        "child_challenge_events": [],
        "active_growth_plan_id": "",
    }


def _load_learning_support_state(user_conf: dict, fallback_name: str) -> dict:
    default = _default_learning_support_state(user_conf, fallback_name)
    loaded = _read_json_file(_learning_support_state_path(user_conf, fallback_name), default)
    if not isinstance(loaded, dict):
        return default
    merged = {**default, **loaded}
    if not isinstance(merged.get("feedback_events"), list):
        merged["feedback_events"] = []
    if not isinstance(merged.get("child_challenge_events"), list):
        merged["child_challenge_events"] = []
    if not isinstance(merged.get("suppressed_card_types"), list):
        merged["suppressed_card_types"] = []
    if not isinstance(merged.get("child_response"), dict):
        merged["child_response"] = {}
    return merged


def _save_learning_support_state(user_conf: dict, fallback_name: str, state: dict) -> None:
    state["user_key"] = _user_key_for_storage(user_conf, fallback_name)
    state["updated_at"] = now_jst_iso()
    _write_json_file(_learning_support_state_path(user_conf, fallback_name), state)


def _load_growth_plans(user_conf: dict, fallback_name: str) -> dict:
    default = {"user_key": _user_key_for_storage(user_conf, fallback_name), "plans": []}
    loaded = _read_json_file(_growth_plans_path(user_conf, fallback_name), default)
    if isinstance(loaded, list):
        return {**default, "plans": loaded}
    if not isinstance(loaded, dict):
        return default
    plans = loaded.get("plans")
    if not isinstance(plans, list):
        plans = []
    return {**default, **loaded, "plans": plans}


def _save_growth_plans(user_conf: dict, fallback_name: str, data: dict) -> None:
    data["user_key"] = _user_key_for_storage(user_conf, fallback_name)
    data["updated_at"] = now_jst_iso()
    _write_json_file(_growth_plans_path(user_conf, fallback_name), data)


def _active_growth_plan(user_conf: dict, fallback_name: str) -> Optional[dict]:
    plans_data = _load_growth_plans(user_conf, fallback_name)
    for plan in plans_data.get("plans", []):
        if isinstance(plan, dict) and plan.get("status") == "active":
            return plan
    return None


def _append_capped_event(container: dict, key: str, event: dict, limit: int = 50) -> None:
    events = container.get(key)
    if not isinstance(events, list):
        events = []
    events.append(event)
    container[key] = events[-limit:]


def _find_child_user_conf(name: str) -> Optional[dict]:
    target_name = str(name or "").strip()
    if not target_name:
        return None
    return next(
        (u for u in load_all_users() if str(u.get("name", "")).strip() == target_name),
        None,
    )


def _build_learning_support_summary(
    name: str,
    system_conf: dict,
    user_conf: dict,
    journal_rows: Optional[list[dict]] = None,
    audit_state: Optional[dict] = None,
) -> dict:
    """学習支援サマリーを作成する。reflection_context が未実装なら簡易集計で補う"""
    from app.config import get_log_dir
    from collections import Counter

    log_dir = get_log_dir(system_conf)
    if journal_rows is None:
        journal_rows = _load_dashboard_jsonl(log_dir / f"{name}_pocket_journal.jsonl")

    now = datetime.datetime.now(JST)
    month_str = now.strftime("%Y-%m")
    rows = sorted((r for r in journal_rows if isinstance(r, dict)), key=lambda r: str(r.get("ts", "")))
    month_rows = [r for r in rows if str(r.get("ts", "")).startswith(month_str)]
    analysis_rows = month_rows if month_rows else rows[-20:]
    analysis_label = f"{now.month}月" if month_rows else "直近の記録"

    amount_values = [_safe_int(r.get("amount")) for r in analysis_rows]
    total_amount = sum(v for v in amount_values if v is not None)
    sat_values = []
    for r in analysis_rows:
        sat = _safe_int(r.get("satisfaction"))
        if sat is not None and 0 <= sat <= 10:
            sat_values.append(sat)
    avg_satisfaction = sum(sat_values) / len(sat_values) if sat_values else None
    reason_count = sum(1 for r in analysis_rows if str(r.get("reason", "")).strip())
    item_counter = Counter(str(r.get("item", "")).strip() for r in analysis_rows if r.get("item"))
    top_items = [item for item, _ in item_counter.most_common(3)]

    metrics = [
        {"label": "対象", "value": analysis_label},
        {"label": "記録件数", "value": f"{len(analysis_rows)}件"},
        {"label": "支出合計", "value": f"{total_amount:,}円" if total_amount else "記録なし"},
        {
            "label": "満足度平均",
            "value": f"{avg_satisfaction:.1f}/10" if avg_satisfaction is not None else "記録なし",
        },
    ]

    signals: list[str] = []
    parent_hints: list[str] = []
    child_hints: list[str] = []

    if not analysis_rows:
        signals.append("支出記録がまだないため、買ったもの・金額・感想を集めるところから始められます。")
        parent_hints.append("次に買い物があったとき、まず「何を買ったか」と「どうだったか」を短く聞くのがよさそうです。")
        child_hints.append("買ったものがあったら、名前・金額・どうだったかを1つずつ記録してみましょう。")
    else:
        if month_rows:
            signals.append(f"今月は{len(month_rows)}件の記録があり、支出合計は{total_amount:,}円です。")
        else:
            signals.append("今月の記録はまだありません。表示は直近の記録から作っています。")
        if top_items:
            signals.append(f"よく出ている品目は「{'・'.join(top_items)}」です。")
        if sat_values:
            high_rows = []
            low_rows = []
            for r in analysis_rows:
                sat = _safe_int(r.get("satisfaction"))
                if sat is None:
                    continue
                if sat >= 8:
                    high_rows.append(r)
                if sat <= 4:
                    low_rows.append(r)
            if high_rows:
                high_item = str(high_rows[-1].get("item", "")).strip() or "直近の支出"
                signals.append(f"満足度が高い記録があります。特に「{high_item}」は振り返り材料になります。")
            if low_rows:
                low_item = str(low_rows[-1].get("item", "")).strip() or "直近の支出"
                signals.append(f"満足度が低めの記録もあります。「{low_item}」は次の判断を一緒に考えやすいです。")
        else:
            signals.append("満足度の記録がまだ少ないため、買った後の納得感を確認すると学習材料が増えます。")

        if reason_count < max(1, len(analysis_rows) // 2):
            signals.append("理由まで書けた記録が少なめです。買う前・買った後の一言理由が次の支援ポイントです。")
            parent_hints.append("次の買い物では「なぜそれを選んだの？」を1問だけ聞くと、責めずに振り返れます。")
            child_hints.append("次の記録では「なぜ買ったか」を一言だけ足してみましょう。")
        else:
            parent_hints.append("理由を書けている記録があります。よかった判断を本人の言葉で確認すると次につながります。")
            child_hints.append("理由を書けた記録は、次に同じような買い物をするときのヒントになります。")

    keywords = user_conf.get("keywords", {}) if isinstance(user_conf, dict) else {}
    keyword_labels = {
        "investment": "学び・目的につながる支出",
        "fun": "楽しみの支出",
        "danger": "注意して見たい支出",
    }
    for bucket, label in keyword_labels.items():
        terms = keywords.get(bucket, []) if isinstance(keywords, dict) else []
        if not isinstance(terms, list):
            continue
        hits = []
        for r in analysis_rows:
            text = f"{r.get('item', '')} {r.get('reason', '')}"
            for term in terms:
                term_text = str(term).strip()
                if term_text and term_text in text:
                    hits.append(term_text)
        if hits:
            unique_hits = list(dict.fromkeys(hits))[:3]
            signals.append(f"{label}: {'・'.join(unique_hits)}")
            if bucket == "investment":
                parent_hints.append("学びや目的につながった支出は、何が役に立ったかを具体的に聞くと次の判断材料になります。")
                child_hints.append("役に立った買い物は、何がよかったかをメモしておくと次に選びやすくなります。")
            elif bucket == "danger":
                parent_hints.append("注意して見たい支出は、禁止ではなく予算や回数を一緒に決める声かけが向いています。")
                child_hints.append("つい買いたくなるものは、先に予算を決めてから選ぶと安心です。")

    if not parent_hints:
        parent_hints.append("金額だけでなく、本人が納得できた理由と次に試したい工夫を確認するとよさそうです。")
    if not child_hints:
        child_hints.append("買ってよかった点と、次は変えたい点を1つずつ考えてみましょう。")

    reflection_context = _try_build_reflection_context(user_conf, system_conf, audit_state)
    reflection_points = _extract_reflection_points(reflection_context)
    if reflection_points:
        signals = reflection_points

    return {
        "source": "reflection_context" if reflection_points else "dashboard_fallback",
        "source_label": "振り返りシグナル" if reflection_points else "簡易集計",
        "metrics": metrics,
        "signals": signals[:6],
        "parent_hints": parent_hints[:4],
        "child_hints": child_hints[:4],
    }


def _build_user_stats(name: str, system_conf: dict, user_conf: Optional[dict] = None) -> dict:
    """ダッシュボード表示用のユーザー統計データを組み立てる"""
    from app.config import get_log_dir
    from app.storage import JST
    import datetime

    if user_conf is None:
        user_conf = find_user_by_name(name) or {}

    log_dir = get_log_dir(system_conf)
    now = datetime.datetime.now(JST)
    month_str = now.strftime("%Y-%m")

    # 今月の支出記録を集計する
    journal_path = log_dir / f"{name}_pocket_journal.jsonl"
    rows = _load_dashboard_jsonl(journal_path)
    month_spending = 0
    month_count = 0
    last_spent_date = None
    recent_items = []
    if rows:
        # 今月分のみフィルタする
        for r in rows:
            ts = r.get("ts", "")
            if ts.startswith(month_str):
                month_count += 1
                amount = r.get("amount")
                if amount is not None:
                    month_spending += int(amount)
                if last_spent_date is None:
                    last_spent_date = ts[:10]
        # 直近5件を recent_items に格納する（新しい順に逆引き）
        for r in reversed(rows[-10:]):
            recent_items.append({
                "date": r.get("ts", "")[:10],
                "item": r.get("item", ""),
                "satisfaction": r.get("satisfaction"),
                "amount": r.get("amount"),
            })
            if len(recent_items) >= 5:
                break

    # 残高を取得する
    balance = None
    has_wallet = False
    if _wallet_service:
        has_wallet = _wallet_service.has_wallet(name)
        if has_wallet:
            balance = _wallet_service.get_balance(name)

    # 低残高フラグを判定する
    low_balance_conf = get_low_balance_alert_setting()
    threshold = low_balance_conf.get("threshold", 500)
    low_balance = has_wallet and balance is not None and balance < threshold

    # 貯金目標を取得してパーセント計算する
    goals = []
    if _wallet_service and has_wallet:
        for g in _wallet_service.get_savings_goals(name):
            target = int(g.get("target_amount", 0))
            saved = balance if balance is not None else 0
            pct = int(saved / target * 100) if target > 0 else 0
            goals.append({
                "title": g.get("title", ""),
                "target": target,
                "saved": saved,
                "pct": min(pct, 100),
            })

    # 残高報告状態を確認する
    audit_reported = False
    audit_state = {}
    if _wallet_service:
        audit_state = _wallet_service.load_audit_state()
        pending = audit_state.get("pending_by_user", {})
        # pending に名前がなければ報告済みとみなす
        audit_reported = name not in pending

    follow_policy = _normalize_follow_policy(user_conf or {})
    learning_insights = _normalize_learning_insights(name, system_conf, user_conf or {}, rows, audit_state)

    return {
        "name": name,
        "fixed_allowance": int((user_conf or {}).get("fixed_allowance", 0)),
        "parent_followup_note": follow_policy["parent_note"],
        "ai_follow_policy": follow_policy,
        "learning_summary": learning_insights["legacy_summary"],
        "learning_insights": learning_insights,
        "child_challenge": learning_insights["child_challenge"],
        "active_growth_plan": _active_growth_plan(user_conf or {}, name),
        "balance": balance,
        "has_wallet": has_wallet,
        "low_balance": low_balance,
        "month_spending": month_spending,
        "month_count": month_count,
        "last_spent_date": last_spent_date,
        "audit_reported": audit_reported,
        "goals": goals,
        "recent_items": recent_items,
    }


# ---------- 認証ルート ----------

@app.get("/compass-bot/register", response_class=HTMLResponse)
async def get_register(request: Request):
    """アクセス申請ページを表示する"""
    return templates.TemplateResponse("register.html", {
        "request": request,
        "username": None,
        "error": None,
        "success": None,
    })


@app.post("/compass-bot/register", response_class=HTMLResponse)
async def post_register(request: Request, username: str = Form(...)):
    """アクセス申請を受け付けてDiscordに通知する"""
    username = username.strip()
    if not username:
        return templates.TemplateResponse("register.html", {
            "request": request,
            "username": None,
            "error": "ユーザー名を入力してください。",
            "success": None,
        })

    # すでに登録済みのユーザーは申請不要
    if await web_auth.user_exists(username):
        return templates.TemplateResponse("register.html", {
            "request": request,
            "username": None,
            "error": f"「{username}」はすでに登録済みです。ログインしてください。",
            "success": None,
        })

    # 申請を登録する
    app_id = await web_auth.create_application(username)

    # Discord に承認依頼を通知する
    msg = (
        f"🌐 **Webダッシュボード アクセス申請**\n"
        f"ユーザー名: **{username}**\n"
        f"承認するには Discord で `web承認 {username}` と送信してください。"
    )
    await _notify_discord(msg)

    return templates.TemplateResponse("register.html", {
        "request": request,
        "username": None,
        "error": None,
        "success": f"申請を受け付けました（ID: {app_id}）。管理者の承認をお待ちください。",
    })


@app.get("/compass-bot/login", response_class=HTMLResponse)
async def get_login(request: Request):
    """ログインページを表示する"""
    return templates.TemplateResponse("login.html", {
        "request": request,
        "username": None,
        "error": None,
    })


@app.post("/compass-bot/login", response_class=HTMLResponse)
async def post_login(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
):
    """ログイン認証を行い、成功時はセッションを発行してダッシュボードへリダイレクトする"""
    username = username.strip()
    ok = await web_auth.verify_password(username, password)
    if not ok:
        return templates.TemplateResponse("login.html", {
            "request": request,
            "username": None,
            "error": "ユーザー名またはパスワードが正しくありません。",
        })
    # セッションを発行する
    token = await web_auth.create_session(username)
    response = RedirectResponse(url="/compass-bot/dashboard", status_code=303)
    # httponly=True でJavaScriptからの読み取りを防ぐ
    response.set_cookie("session_token", token, httponly=True, samesite="lax", max_age=7*24*3600)
    return response


@app.get("/compass-bot/logout")
async def logout(session_token: Optional[str] = Cookie(default=None)):
    """セッションを削除してログインページへリダイレクトする"""
    if session_token:
        await web_auth.delete_session(session_token)
    response = RedirectResponse(url="/compass-bot/login", status_code=303)
    response.delete_cookie("session_token")
    return response


@app.get("/compass-bot/set_password", response_class=HTMLResponse)
async def get_set_password(request: Request, username: Optional[str] = None):
    """仮パスワード入力ページを表示する"""
    return templates.TemplateResponse("set_password.html", {
        "request": request,
        "step": "temp",
        "username": None,
        "username_hint": username,
        "token": None,
        "error": None,
    })


@app.post("/compass-bot/set_password", response_class=HTMLResponse)
async def post_set_password(
    request: Request,
    step: str = Form(...),
    username: str = Form(default=""),
    temp_password: str = Form(default=""),
    password: str = Form(default=""),
    password_confirm: str = Form(default=""),
    token: str = Form(default=""),
):
    """仮PW検証（step=temp）と本PW設定（step=set）の2段階フォーム処理を行う"""
    username = username.strip()

    if step == "temp":
        # 仮パスワードを検証する
        ok = await web_auth.consume_temp_password(username, temp_password.strip())
        if not ok:
            return templates.TemplateResponse("set_password.html", {
                "request": request,
                "step": "temp",
                "username": None,
                "username_hint": username,
                "token": None,
                "error": "仮パスワードが正しくありません。",
            })
        # 仮PW確認済み → 本PW設定フォームへ（one-time token を発行してCSRF対策とする）
        import secrets
        set_token = secrets.token_urlsafe(16)
        # 本PW設定待ち状態はweb_auth_state.json で管理済み（consume_temp_password で移行済み）
        return templates.TemplateResponse("set_password.html", {
            "request": request,
            "step": "set",
            "username": username,
            "username_hint": None,
            "token": set_token,
            "error": None,
        })

    elif step == "set":
        # パスワードの強度・一致チェックをする
        if len(password) < 8:
            return templates.TemplateResponse("set_password.html", {
                "request": request,
                "step": "set",
                "username": username,
                "username_hint": None,
                "token": token,
                "error": "パスワードは8文字以上にしてください。",
            })
        if password != password_confirm:
            return templates.TemplateResponse("set_password.html", {
                "request": request,
                "step": "set",
                "username": username,
                "username_hint": None,
                "token": token,
                "error": "パスワードが一致しません。",
            })
        # パスワード設定完了 → pw_setting 状態であることを確認してから保存する
        if not await web_auth.is_pw_setting_mode(username):
            return templates.TemplateResponse("set_password.html", {
                "request": request,
                "step": "temp",
                "username": None,
                "username_hint": username,
                "token": None,
                "error": "セッションが無効です。仮パスワードから再入力してください。",
            })
        await web_auth.set_password(username, password)
        # 設定完了後は自動ログインする
        session_token = await web_auth.create_session(username)
        response = RedirectResponse(url="/compass-bot/dashboard", status_code=303)
        response.set_cookie("session_token", session_token, httponly=True, samesite="lax", max_age=7*24*3600)
        return response

    # 不正なステップ値は login に戻す
    return RedirectResponse(url="/compass-bot/login", status_code=303)


# ---------- ダッシュボード ----------

@app.get("/compass-bot/dashboard", response_class=HTMLResponse)
async def get_dashboard(
    request: Request,
    session_token: Optional[str] = Cookie(default=None),
    msg: str = "",
    error: str = "",
):
    """ダッシュボードページを表示する。未ログインはログインページへリダイレクトする"""
    username = await _get_current_user(session_token)
    if not username:
        return RedirectResponse(url="/compass-bot/login", status_code=303)

    is_admin = _is_admin(username)
    system_conf = load_system()

    if is_admin:
        # 管理者: 全ユーザーの統計を収集する
        all_users = load_all_users()
        user_stats = []
        for u in all_users:
            name = u.get("name", "")
            if name:
                stats = _build_user_stats(name, system_conf, u)
                user_stats.append(stats)

        # 承認待ち申請一覧を取得する
        pending_apps = await web_auth.list_pending_applications()
        for app in pending_apps:
            ts = app.get("requested_at", 0)
            app["requested_at_str"] = datetime.datetime.fromtimestamp(ts, tz=JST).strftime("%Y-%m-%d %H:%M") if ts else "—"

        return templates.TemplateResponse("dashboard.html", {
            "request": request,
            "username": username,
            "is_admin": True,
            "users": user_stats,
            "pending_apps": pending_apps,
            "follow_focus_choices": FOLLOW_FOCUS_CHOICES,
            "follow_strength_choices": FOLLOW_STRENGTH_CHOICES,
            "follow_frequency_choices": FOLLOW_FREQUENCY_CHOICES,
            "flash_msg": msg,
            "flash_error": error,
        })
    else:
        # 一般ユーザー（子供）: 自分のデータのみ表示する
        # app/users/*.json に登録されている名前と Web ユーザー名を紐付ける
        # （Web申請時のユーザー名 == users/*.json の name フィールドを前提とする）
        stats = _build_user_stats(username, system_conf)
        return templates.TemplateResponse("dashboard.html", {
            "request": request,
            "username": username,
            "is_admin": False,
            "my_child_challenge": stats["child_challenge"],
            "my_balance": stats["balance"],
            "my_low_balance": stats["low_balance"],
            "my_goals": stats["goals"],
            "my_month_spending": stats["month_spending"],
            "my_month_count": stats["month_count"],
            "my_recent_items": stats["recent_items"],
            "flash_msg": msg,
            "flash_error": error,
        })


# ---------- 管理者操作 ----------

@app.post("/compass-bot/admin/approve", response_class=HTMLResponse)
async def admin_approve(
    request: Request,
    session_token: Optional[str] = Cookie(default=None),
    username: str = Form(...),
):
    """管理者がWeb申請を承認する。Webフォームからの操作用エンドポイント"""
    current_user = await _get_current_user(session_token)
    if not current_user or not _is_admin(current_user):
        return RedirectResponse(url="/compass-bot/login", status_code=303)

    username = username.strip()
    temp_pw = await web_auth.approve_application(username)
    if temp_pw:
        # Discord に仮パスワードを通知する
        base_url = get_web_base_url()
        msg = (
            f"✅ **Webアクセスを承認しました**\n"
            f"ユーザー: **{username}**\n"
            f"仮パスワード: `{temp_pw}`\n"
            f"下記URLからパスワードを設定してください:\n"
            f"{base_url}/compass-bot/set_password?username={username}"
        )
        await _notify_discord(msg)

    # ダッシュボードへ戻る
    return RedirectResponse(url="/compass-bot/dashboard", status_code=303)


# ---------- 親操作エンドポイント（Phase A） ----------

def _op_redirect(msg: str = "", error: str = "") -> RedirectResponse:
    """操作後にダッシュボードへリダイレクトする。結果メッセージをクエリパラメータで渡す"""
    from urllib.parse import quote
    if error:
        return RedirectResponse(url=f"/compass-bot/dashboard?error={quote(error)}", status_code=303)
    return RedirectResponse(url=f"/compass-bot/dashboard?msg={quote(msg)}", status_code=303)


@app.post("/compass-bot/op/grant")
async def op_grant(
    session_token: Optional[str] = Cookie(default=None),
    target: str = Form(...),
    amount: str = Form(...),
):
    """親が特定ユーザーへ手動支給する"""
    current_user = await _get_current_user(session_token)
    if not current_user or not _is_admin(current_user):
        return RedirectResponse(url="/compass-bot/login", status_code=303)

    # 金額を整数に変換する（カンマ・円記号を除去）
    try:
        amt = int(amount.replace(",", "").replace("円", "").strip())
    except ValueError:
        return _op_redirect(error="金額が正しくありません。")
    if amt <= 0:
        return _op_redirect(error="金額は1円以上を入力してください。")

    target_conf = find_user_by_name(target)
    if target_conf is None:
        return _op_redirect(error=f"「{target}」は見つかりませんでした。")
    if not _wallet_service or not _wallet_service.has_wallet(target):
        return _op_redirect(error=f"「{target}」のウォレットが未設定です。")

    system_conf = load_system()
    before = _wallet_service.get_balance(target)
    new_balance, _ = _wallet_service.update_balance(
        user_conf=target_conf,
        system_conf=system_conf,
        delta=amt,
        action="allowance_manual_grant",
        note="manual_grant_by_parent_web",
        extra={"granted_by": current_user},
    )
    return _op_redirect(msg=f"{target}に{amt:,}円を支給しました（{before:,}円 → {new_balance:,}円）")


@app.post("/compass-bot/op/bulk_grant")
async def op_bulk_grant(
    session_token: Optional[str] = Cookie(default=None),
):
    """親が全子供ユーザーに固定お小遣いを一括支給する"""
    current_user = await _get_current_user(session_token)
    if not current_user or not _is_admin(current_user):
        return RedirectResponse(url="/compass-bot/login", status_code=303)

    if not _wallet_service:
        return _op_redirect(error="ウォレットサービスが未初期化です。")

    system_conf = load_system()
    results = []
    # 子供ユーザー（load_all_users = 子供のみ）を一括処理する
    for user_conf in sorted(load_all_users(), key=lambda x: str(x.get("name", ""))):
        name = user_conf.get("name", "")
        fixed = int(user_conf.get("fixed_allowance", 0))
        if not name or fixed <= 0:
            continue
        if not _wallet_service.has_wallet(name):
            continue
        before = _wallet_service.get_balance(name)
        new_bal, _ = _wallet_service.update_balance(
            user_conf=user_conf,
            system_conf=system_conf,
            delta=fixed,
            action="allowance_grant",
            note="bulk_grant_by_parent_web",
            extra={"granted_by": current_user},
        )
        results.append(f"{name}: {before:,}→{new_bal:,}円")

    if not results:
        return _op_redirect(error="支給対象のユーザーが見つかりませんでした。")
    return _op_redirect(msg="一括支給完了 / " + " | ".join(results))


@app.post("/compass-bot/op/fixed_allowance")
async def op_fixed_allowance(
    session_token: Optional[str] = Cookie(default=None),
    target: str = Form(...),
    amount: str = Form(...),
):
    """親がユーザーの月額固定お小遣いを変更する"""
    current_user = await _get_current_user(session_token)
    if not current_user or not _is_admin(current_user):
        return RedirectResponse(url="/compass-bot/login", status_code=303)

    try:
        amt = int(amount.replace(",", "").replace("円", "").strip())
    except ValueError:
        return _op_redirect(error="月額が正しくありません。")
    if amt < 0:
        return _op_redirect(error="月額は0円以上を入力してください。")

    target_name = target.strip()
    target_conf = next(
        (u for u in load_all_users() if str(u.get("name", "")).strip() == target_name),
        None,
    )
    if target_conf is None:
        return _op_redirect(error=f"「{target_name}」は子供ユーザー設定に見つかりませんでした。")

    old_value = int(target_conf.get("fixed_allowance", 0))
    if not update_user_field(target_name, "fixed_allowance", amt):
        return _op_redirect(error=f"「{target_name}」の月額変更に失敗しました。")

    return _op_redirect(msg=f"{target_name}の月額お小遣いを変更しました（{old_value:,}円 → {amt:,}円）")


@app.post("/compass-bot/op/followup_policy")
async def op_followup_policy(
    session_token: Optional[str] = Cookie(default=None),
    target: str = Form(...),
    enabled: str = Form(default=""),
    focus_area: str = Form(default="record_habit"),
    nudge_strength: str = Form(default="light"),
    frequency: str = Form(default="low"),
    parent_note: str = Form(default=""),
):
    """親がユーザー別のAIフォロー方針を保存する"""
    current_user = await _get_current_user(session_token)
    if not current_user or not _is_admin(current_user):
        return RedirectResponse(url="/compass-bot/login", status_code=303)

    target_name = target.strip()
    target_conf = next(
        (u for u in load_all_users() if str(u.get("name", "")).strip() == target_name),
        None,
    )
    if target_conf is None:
        return _op_redirect(error=f"「{target_name}」は子供ユーザー設定に見つかりませんでした。")

    focus_area = str(focus_area or "record_habit").strip()
    if focus_area not in FOLLOW_FOCUS_VALUES:
        return _op_redirect(error="AIフォローの重視観点が正しくありません。")

    nudge_strength = str(nudge_strength or "light").strip()
    if nudge_strength not in FOLLOW_STRENGTH_VALUES:
        return _op_redirect(error="AIフォローの強さが正しくありません。")

    frequency = str(frequency or "low").strip()
    if frequency not in FOLLOW_FREQUENCY_VALUES:
        return _op_redirect(error="AIフォローの頻度が正しくありません。")

    note = str(parent_note or "").replace("\r\n", "\n").strip()
    note_error = _validate_follow_policy_note(note)
    if note_error:
        return _op_redirect(error=note_error)

    policy = {
        "enabled": _normalize_form_bool(enabled),
        "focus_area": focus_area,
        "focus_areas": [focus_area],
        "nudge_strength": nudge_strength,
        "frequency": frequency,
        "parent_note": note,
        "updated_by_parent": current_user,
        "updated_at": now_jst_iso(),
    }

    if not update_user_field(target_name, "ai_follow_policy", policy):
        return _op_redirect(error=f"「{target_name}」のAIフォロー方針の保存に失敗しました。")

    state_text = "有効" if policy["enabled"] else "無効"
    return _op_redirect(msg=f"{target_name}のAIフォロー方針を保存しました（{state_text}）。")


@app.post("/compass-bot/op/followup_note")
async def op_followup_note(
    session_token: Optional[str] = Cookie(default=None),
    target: str = Form(...),
    parent_followup_note: str = Form(default=""),
):
    """旧フォーム互換: 親メモのみを現在の AI フォロー方針に変換して保存する"""
    target_conf = next(
        (u for u in load_all_users() if str(u.get("name", "")).strip() == target.strip()),
        None,
    )
    current_policy = _normalize_follow_policy(target_conf or {})
    return await op_followup_policy(
        session_token=session_token,
        target=target,
        enabled="on" if str(parent_followup_note or "").strip() else "",
        focus_area=current_policy["focus_area"],
        nudge_strength=current_policy["nudge_strength"],
        frequency=current_policy["frequency"],
        parent_note=parent_followup_note,
    )


@app.post("/compass-bot/op/learning_card_feedback")
async def op_learning_card_feedback(
    session_token: Optional[str] = Cookie(default=None),
    target: str = Form(...),
    card_id: str = Form(...),
    feedback: str = Form(...),
    card_type: str = Form(default=""),
    parent_question: str = Form(default=""),
    child_action: str = Form(default=""),
):
    """親が今週の会話カードへの反応を保存する"""
    current_user = await _get_current_user(session_token)
    if not current_user or not _is_admin(current_user):
        return RedirectResponse(url="/compass-bot/login", status_code=303)

    target_name = target.strip()
    target_conf = _find_child_user_conf(target_name)
    if target_conf is None:
        return _op_redirect(error=f"「{target_name}」は子供ユーザー設定に見つかりませんでした。")

    feedback_value = _short_text(feedback, 40)
    if feedback_value not in LEARNING_CARD_FEEDBACK_CHOICES:
        return _op_redirect(error="会話カードの反応が正しくありません。")

    now = now_jst_iso()
    state = _load_learning_support_state(target_conf, target_name)
    event = {
        "ts": now,
        "actor": current_user,
        "feedback": feedback_value,
        "card_id": _short_text(card_id, 120),
        "card_type": _short_text(card_type, 80),
    }
    _append_capped_event(state, "feedback_events", event)

    if feedback_value == "use_this_week":
        state["last_card_id"] = event["card_id"]
        state["last_card_type"] = event["card_type"]
        state["last_nudge_at"] = now
        state["last_parent_question"] = _short_text(parent_question, 220)
        state["last_child_action"] = _short_text(child_action, 220)
    elif feedback_value == "suppress_week":
        suppressed = state.get("suppressed_card_types")
        if not isinstance(suppressed, list):
            suppressed = []
        suppressed.append({
            "card_id": event["card_id"],
            "card_type": event["card_type"],
            "ts": now,
        })
        state["suppressed_card_types"] = suppressed[-20:]

    _save_learning_support_state(target_conf, target_name, state)
    label = LEARNING_CARD_FEEDBACK_CHOICES[feedback_value]
    return _op_redirect(msg=f"{target_name}の会話カード反応を保存しました（{label}）。")


@app.post("/compass-bot/op/child_challenge_feedback")
async def op_child_challenge_feedback(
    session_token: Optional[str] = Cookie(default=None),
    challenge_id: str = Form(...),
    feedback: str = Form(...),
    target: str = Form(default=""),
    child_action: str = Form(default=""),
):
    """子どものチャレンジ反応を保存する。親メモや内部方針はレスポンスに含めない"""
    current_user = await _get_current_user(session_token)
    if not current_user:
        return RedirectResponse(url="/compass-bot/login", status_code=303)

    is_admin = _is_admin(current_user)
    target_value = _short_text(target, 120)
    target_name = target_value if is_admin and target_value else current_user
    if target_value and target_value != current_user and not is_admin:
        return _op_redirect(error="自分以外のチャレンジ反応は保存できません。")

    target_conf = _find_child_user_conf(target_name)
    if target_conf is None:
        return _op_redirect(error=f"「{target_name}」は子供ユーザー設定に見つかりませんでした。")

    feedback_value = _short_text(feedback, 40)
    if feedback_value not in CHILD_CHALLENGE_FEEDBACK_CHOICES:
        return _op_redirect(error="チャレンジの反応が正しくありません。")

    now = now_jst_iso()
    state = _load_learning_support_state(target_conf, target_name)
    event = {
        "ts": now,
        "actor": current_user,
        "feedback": feedback_value,
        "challenge_id": _short_text(challenge_id, 120),
        "child_action": _short_text(child_action, 220),
    }
    _append_capped_event(state, "child_challenge_events", event)
    state["child_response"] = {
        "challenge_id": event["challenge_id"],
        "feedback": feedback_value,
        "responded_at": now,
    }
    if event["child_action"]:
        state["last_child_action"] = event["child_action"]

    _save_learning_support_state(target_conf, target_name, state)
    label = CHILD_CHALLENGE_FEEDBACK_CHOICES[feedback_value]
    return _op_redirect(msg=f"チャレンジの反応を保存しました（{label}）。")


@app.post("/compass-bot/op/growth_plan")
async def op_growth_plan(
    session_token: Optional[str] = Cookie(default=None),
    target: str = Form(...),
    plan_id: str = Form(default=""),
    action: str = Form(default="save"),
    status: str = Form(default="active"),
    request_type: str = Form(default="allowance_increase"),
    child_reason: str = Form(default=""),
    parent_condition: str = Form(default=""),
    agreed_action: str = Form(default=""),
    review_at: str = Form(default=""),
    reward_amount: str = Form(default=""),
    notes: str = Form(default=""),
):
    """親が成長行動プランを作成・更新・終了する"""
    current_user = await _get_current_user(session_token)
    if not current_user or not _is_admin(current_user):
        return RedirectResponse(url="/compass-bot/login", status_code=303)

    target_name = target.strip()
    target_conf = _find_child_user_conf(target_name)
    if target_conf is None:
        return _op_redirect(error=f"「{target_name}」は子供ユーザー設定に見つかりませんでした。")

    action_value = _short_text(action, 40)
    status_value = _short_text(status, 40)
    if action_value in {"done", "cancelled", "cancel"}:
        status_value = "cancelled" if action_value == "cancel" else action_value
    if status_value not in GROWTH_PLAN_STATUS_CHOICES:
        return _op_redirect(error="成長行動プランの状態が正しくありません。")

    request_type_value = _short_text(request_type, 80)
    if request_type_value not in GROWTH_PLAN_REQUEST_TYPES:
        return _op_redirect(error="成長行動プランの種類が正しくありません。")

    reward_text = _short_text(reward_amount, 40).replace(",", "").replace("円", "").strip()
    reward_value = None
    if reward_text:
        try:
            reward_value = int(reward_text)
        except ValueError:
            return _op_redirect(error="報酬額が正しくありません。")
        if reward_value < 0:
            return _op_redirect(error="報酬額は0円以上を入力してください。")

    now = now_jst_iso()
    plans_data = _load_growth_plans(target_conf, target_name)
    plans = [p for p in plans_data.get("plans", []) if isinstance(p, dict)]
    target_plan_id = _short_text(plan_id, 120)
    existing_index = next(
        (idx for idx, plan in enumerate(plans) if str(plan.get("plan_id", "")) == target_plan_id),
        None,
    )

    if existing_index is None:
        target_plan_id = target_plan_id or f"gp-{uuid.uuid4().hex[:12]}"
        plan = {"plan_id": target_plan_id, "created_at": now, "created_by": current_user}
        plans.append(plan)
    else:
        plan = plans[existing_index]

    plan.update({
        "plan_id": target_plan_id,
        "status": status_value,
        "request_type": request_type_value,
        "child_reason": _short_text(child_reason, 300),
        "parent_condition": _short_text(parent_condition, 300),
        "agreed_action": _short_text(agreed_action, 300),
        "review_at": _short_text(review_at, 40),
        "reward_amount": reward_value,
        "notes": _short_text(notes, 500),
        "updated_at": now,
        "updated_by": current_user,
    })

    plans_data["plans"] = plans
    _save_growth_plans(target_conf, target_name, plans_data)

    state = _load_learning_support_state(target_conf, target_name)
    if status_value == "active":
        state["active_growth_plan_id"] = target_plan_id
    elif state.get("active_growth_plan_id") == target_plan_id:
        state["active_growth_plan_id"] = ""
    _save_learning_support_state(target_conf, target_name, state)

    return _op_redirect(msg=f"{target_name}の成長行動プランを保存しました。")


@app.post("/compass-bot/op/adjust")
async def op_adjust(
    session_token: Optional[str] = Cookie(default=None),
    target: str = Form(...),
    amount: str = Form(...),
    direction: str = Form(...),  # "plus" or "minus"
):
    """親が残高を手動調整する（加算・減算）"""
    current_user = await _get_current_user(session_token)
    if not current_user or not _is_admin(current_user):
        return RedirectResponse(url="/compass-bot/login", status_code=303)

    try:
        amt = int(amount.replace(",", "").replace("円", "").strip())
    except ValueError:
        return _op_redirect(error="金額が正しくありません。")
    if amt <= 0:
        return _op_redirect(error="金額は1円以上を入力してください。")

    delta = amt if direction == "plus" else -amt

    target_conf = find_user_by_name(target)
    if target_conf is None:
        return _op_redirect(error=f"「{target}」は見つかりませんでした。")
    if not _wallet_service or not _wallet_service.has_wallet(target):
        return _op_redirect(error=f"「{target}」のウォレットが未設定です。")

    system_conf = load_system()
    before = _wallet_service.get_balance(target)
    new_balance, _ = _wallet_service.update_balance(
        user_conf=target_conf,
        system_conf=system_conf,
        delta=delta,
        action="balance_adjustment",
        note="manual_adjustment_by_parent_web",
        extra={"adjusted_by": current_user},
    )
    label = "加算" if delta >= 0 else "減算"
    return _op_redirect(msg=f"{target}の残高を{label}しました（{before:,}円 → {new_balance:,}円）")


# ---------- エントリーポイント ----------

@app.get("/", response_class=HTMLResponse)
@app.get("/compass-bot", response_class=HTMLResponse)
@app.get("/compass-bot/", response_class=HTMLResponse)
async def index(session_token: Optional[str] = Cookie(default=None)):
    """ルートアクセス: ログイン済みならダッシュボード、未ログインはログインページへ"""
    username = await _get_current_user(session_token)
    if username:
        return RedirectResponse(url="/compass-bot/dashboard", status_code=303)
    return RedirectResponse(url="/compass-bot/login", status_code=303)
