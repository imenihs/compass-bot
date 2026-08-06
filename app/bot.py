import asyncio
import os
import json
import re
from pathlib import Path
from urllib.parse import quote

import discord
import uvicorn
from datetime import datetime

from app.bot_utils import (
    _assessment_history_message,
    _build_goal_achieved_message,
    _child_review_message,
    _contains_any_keyword,
    _contains_force_assess_keyword,
    _extract_keyword_hits,
    _is_same_month,
    _latest_assessed_amount,
    _ledger_history_message,
    _load_jsonl,
    _monthly_increase_stats,
    _normalize_assessed_amounts,
    _parse_fixed_delta_request,
    _parse_yen_amount,
    _progress_bar,
    _recent_conversation_history,
    _rough_word_count,
    _self_compare_message,
    _spending_analysis_for_user,
    _thinking_message,
    _usage_guide_text,
    _usage_guide_text_parent,
)
from app.config import (
    MAX_WALLET_INPUT_AMOUNT,
    find_parent_by_discord_id,
    find_user_by_discord_id,
    find_user_by_name,
    get_discord_id_conflicts,
    get_allow_channel_ids,
    get_assess_keyword,
    get_allowance_reminder_setting,
    get_chat_setting,
    get_child_income_report_setting,
    get_force_assess_test_keyword,
    get_gemini_model,
    get_log_dir,
    get_low_balance_alert_setting,
    get_monthly_summary_setting,
    get_parent_ids,
    get_pocket_journal_reminder_setting,
    get_proactive_child_nudge_setting,
    get_wallet_audit_setting,
    load_all_users,
    load_system,
    update_user_field,
)
from app.error_messages import (
    ai_failure_message,
    is_likely_transient_error,
    operation_failure_message,
    processing_failure_message,
)
from app.gemini_service import GeminiService, count_recent_allowance_requests
from app.message_parser import (
    contains_any_mention,
    extract_input_from_mention,
    parse_balance_report,
    parse_usage_report,
    parse_usage_report_flexible,
    parse_proxy_request,
)
from app.prompts import build_chat_prompt, build_prompt
from app.reflection_context import build_reflection_context
try:
    from app.learning_insights import build_learning_insights
except ImportError:
    try:
        from app.reflection_context import build_learning_insights
    except ImportError:
        build_learning_insights = None
from app.reminder_service import ReminderService
from app.storage import append_jsonl, now_jst_iso, JST
from app.wallet_service import WalletService
# 分割したハンドラモジュール（親向け・子供向け）を読み込む
from app import handlers_parent, handlers_child

DISCORD_BOT_TOKEN = os.environ.get("DISCORD_BOT_TOKEN")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")

PARENT_IDS = get_parent_ids()
ALLOW_CHANNEL_IDS = get_allow_channel_ids()
ALLOWANCE_REMINDER = get_allowance_reminder_setting()
WALLET_AUDIT = get_wallet_audit_setting()
CHAT_SETTING = get_chat_setting()
ASSESS_KEYWORD = get_assess_keyword()
FORCE_ASSESS_TEST_KEYWORD = get_force_assess_test_keyword()
LOW_BALANCE_ALERT = get_low_balance_alert_setting()
MONTHLY_SUMMARY = get_monthly_summary_setting()
POCKET_JOURNAL_REMINDER = get_pocket_journal_reminder_setting()
PROACTIVE_CHILD_NUDGE = get_proactive_child_nudge_setting()

# 初期設定・財布チェックで受け付ける現実的な財布上限（円）は config.MAX_WALLET_INPUT_AMOUNT。
# 定義は config へ移し（AI 主導層 mcp_wallet と共有するため）、上の import ブロックで取り込んでいる。
# Discord ID 等の誤入力を拒否する用途は従来どおり。


def _child_income_over_limit_message(amount: int, limit: int, user_name: str) -> str | None:
    """子供の自己申告入金が上限を超えていないか判定し、超えていれば案内文を返す。

    Args:
        amount: 子供が申告した金額。
        limit: 1回あたりの上限額。0 以下なら上限なしとして扱う。
        user_name: 対象の子供の名前。案内文に含める。

    Returns:
        str | None: 上限超過なら親へ依頼する案内文。範囲内なら None。
    """
    # 0 以下は「上限を設けない」の意味として扱う
    if limit <= 0 or int(amount) <= limit:
        return None
    return (
        f"{int(amount):,}円 は自分で記録できる上限（{limit:,}円）を超えてるよ。\n"
        "大きい金額は、おうちの人に記録してもらってね。\n"
        f"（おうちの人へ: `残高調整 {user_name} +{int(amount)}円` で反映できます）"
    )
# 貯金目標の補完入力で受け付ける上限。通常の子供向け目標として十分な範囲にする。
MAX_GOAL_INPUT_AMOUNT = 10_000_000
_thinking_sent_message_keys: set[tuple[str, int]] = set()


gemini_service = GeminiService(
    api_key=GEMINI_API_KEY,
    model_name=get_gemini_model(),
    assess_keyword=ASSESS_KEYWORD,
)
wallet_service = WalletService()

intents = discord.Intents.default()
intents.message_content = True
client = discord.Client(intents=intents)
reminder_service = ReminderService(
    client=client,
    allowance_reminder_conf=ALLOWANCE_REMINDER,
    wallet_audit_conf=WALLET_AUDIT,
    load_all_users_fn=load_all_users,
    wallet_service=wallet_service,
    allow_channel_ids=ALLOW_CHANNEL_IDS,
    monthly_summary_conf=MONTHLY_SUMMARY,
    pocket_journal_reminder_conf=POCKET_JOURNAL_REMINDER,
    proactive_child_nudge_conf=PROACTIVE_CHILD_NUDGE,
)


def is_parent(user_id: int) -> bool:
    """ユーザーIDが親（管理者）かどうかを判定する"""
    return user_id in PARENT_IDS

def _find_channel_child_user_conf(message: discord.Message) -> dict | None:
    """親が子ども用チャンネルで発言した場合の対象子ユーザーを推定する"""
    child_users = load_all_users()
    channel_name = str(getattr(message.channel, "name", "") or "")

    # チャンネル名に子どもの名前が1人だけ含まれる場合は、その子を優先する
    name_matches = [
        u for u in child_users
        if str(u.get("name", "")).strip()
        and str(u.get("name", "")).strip() in channel_name
    ]
    if len(name_matches) == 1:
        return name_matches[0]

    # チャンネルメンバーから登録済み子ユーザーを探す
    member_ids = {
        int(member.id)
        for member in getattr(message.channel, "members", [])
        if getattr(member, "id", None) is not None
    }
    if not member_ids:
        return None

    member_matches = [
        u for u in child_users
        if u.get("discord_user_id") and int(u.get("discord_user_id")) in member_ids
    ]
    if len(member_matches) == 1:
        return member_matches[0]

    # 複数候補または候補なしの場合は誤操作防止のため自動補正しない
    return None


def _extract_child_name_from_text(input_block: str) -> str | None:
    """本文に登録済み子ユーザー名が1人だけ含まれる場合、その名前を返す"""
    matches = [
        str(u.get("name", "")).strip()
        for u in load_all_users()
        if str(u.get("name", "")).strip()
        and _child_name_mentioned_in_text(input_block, str(u.get("name", "")).strip())
    ]
    unique_matches = sorted(set(matches))
    return unique_matches[0] if len(unique_matches) == 1 else None


def _find_child_names_in_text(input_block: str) -> list[str]:
    """本文に含まれる登録済み子ユーザー名を重複なしで返す"""
    matches = [
        str(u.get("name", "")).strip()
        for u in load_all_users()
        if str(u.get("name", "")).strip()
        and _child_name_mentioned_in_text(input_block, str(u.get("name", "")).strip())
    ]
    return sorted(set(matches))


def _child_name_mentioned_in_text(input_block: str, child_name: str) -> bool:
    """短い名前が普通の単語の一部に偶然入っただけの場合は名前扱いしない"""
    body = input_block or ""
    name = (child_name or "").strip()
    if not body or not name:
        return False
    prefix = r"(^|[\s　、。,.!！?？:：`'\"「」『』（）\(\)\[\]【】]|[はがをにへとで])"
    suffix = r"(?:さん|ちゃん|くん)?(?=$|[\s　、。,.!！?？:：`'\"「」『』（）\(\)\[\]【】]|[のはがをにへとで]|について)"
    return bool(re.search(prefix + re.escape(name) + suffix, body))


def _is_child_user_name(name: str) -> bool:
    """名前が登録済み子ユーザーかどうかを判定する"""
    target = (name or "").strip()
    return any(str(u.get("name", "")).strip() == target for u in load_all_users())


def _parent_natural_management_guide(input_block: str) -> str | None:
    """親の自然文による管理要求を明示コマンドへ誘導する文面を返す"""
    body = input_block or ""
    child_names = _find_child_names_in_text(body)
    if len(child_names) != 1:
        return None

    child_name = child_names[0]
    subject_keywords = [
        "お小遣い", "小遣い", "金額", "固定", "臨時", "上限", "支給額", "残高",
    ]
    action_keywords = [
        "変え", "変更", "設定", "増や", "減ら", "上げ", "下げ", "にして", "にする", "調整",
    ]
    if not (
        _contains_any_keyword(body, subject_keywords)
        and _contains_any_keyword(body, action_keywords)
    ):
        return None

    return (
        "親向けの金額変更は、誤操作防止のため明示コマンドで実行してね。\n"
        f"- 固定お小遣い変更: `設定変更 {child_name} 固定 300円`\n"
        f"- 臨時上限変更: `設定変更 {child_name} 臨時 1000円`\n"
        f"- 残高を直接増減: `残高調整 {child_name} +500円` / `残高調整 {child_name} -300円`"
    )


def _looks_like_parent_only_command(input_block: str) -> bool:
    """子どもの入力を親専用コマンドとして誤って査定/雑談へ流さないための判定"""
    body = (input_block or "").strip()
    if not body:
        return False
    parent_prefixes = [
        "支給", "残高調整", "設定変更", "一括支給", "アナウンス", "web承認",
        "全体確認", "全員の分析", "残高チェック送信", "月頭案内送信",
        "reminder test", "reminder-test", "リマインダーテスト",
    ]
    if any(body.lower().startswith(prefix.lower()) for prefix in parent_prefixes):
        return True
    return bool(re.match(r"^.+の分析\s*$", body))


def _short_log_text(value, limit: int = 1200) -> str:
    """診断ログ用に長すぎる文字列を切り詰める"""
    text = "" if value is None else str(value)
    if len(text) <= limit:
        return text
    return text[:limit] + "...(truncated)"


def _message_state_key(message: discord.Message) -> tuple[str, int]:
    """discord.Message は任意属性を持てないため、外部キーで一時状態を管理する。"""
    message_id = getattr(message, "id", None)
    if message_id is not None:
        try:
            return ("discord_message_id", int(message_id))
        except Exception:
            pass
    return ("object_id", id(message))


def _mark_thinking_sent(message: discord.Message, sent: bool) -> None:
    key = _message_state_key(message)
    if sent:
        _thinking_sent_message_keys.add(key)
    else:
        _thinking_sent_message_keys.discard(key)


def _was_thinking_sent(message: discord.Message) -> bool:
    return _message_state_key(message) in _thinking_sent_message_keys


def _compact_intent_result(intent_result: dict | None) -> dict:
    """intent_result を診断ログに保存しやすい形へ整える"""
    if not isinstance(intent_result, dict):
        return {}
    return {
        "intent": str(intent_result.get("intent", "")),
        "confidence": str(intent_result.get("confidence", "")),
        "entities": intent_result.get("entities") or {},
    }


def _diagnostic_issue_tags(
    input_block: str,
    intent_result: dict | None = None,
    reply: str | None = None,
    selected_user_source: str | None = None,
    author_is_parent: bool = False,
) -> list[str]:
    """後で運用課題を探しやすいように会話上の違和感タグを付ける"""
    tags: list[str] = []
    intent = str((intent_result or {}).get("intent", ""))
    confidence = str((intent_result or {}).get("confidence", ""))
    money_keywords = [
        "残高", "ざんだか", "所持金", "お金", "おこづかい", "お小遣い",
        "買った", "かった", "使った", "つかった", "支出", "入金", "もらった",
        "財布", "貯金", "目標", "支給", "金額",
    ]
    clarification_keywords = [
        "どういうこと", "もう少し詳しく", "教えてくれる", "教えてね",
        "どんなこと", "どちら", "かな？", "かな", "一緒に考え",
    ]

    if confidence == "low":
        tags.append("gemini_low_confidence")
    if intent == "none" and _contains_any_keyword(input_block, money_keywords):
        tags.append("money_related_but_intent_none")
    if reply and _contains_any_keyword(reply, clarification_keywords):
        tags.append("reply_asks_clarification")
    if author_is_parent and selected_user_source == "author_discord_id":
        tags.append("parent_message_used_author_context")
    if selected_user_source == "parent_channel_context":
        tags.append("parent_message_used_child_channel_context")
    return tags


def _log_runtime_event(
    system_conf: dict | None,
    message: discord.Message,
    user_conf: dict | None,
    input_block: str,
    event: str,
    details: dict | None = None,
) -> None:
    """運用診断用のJSONLログを追記する"""
    try:
        conf = system_conf or load_system()
        log_dir = get_log_dir(conf)
        channel = getattr(message, "channel", None)
        record = {
            "ts": now_jst_iso(),
            "event": event,
            "discord_user_id": int(message.author.id),
            "author_is_parent": is_parent(message.author.id),
            "channel_id": int(getattr(channel, "id", 0) or 0),
            "channel_name": str(getattr(channel, "name", "") or ""),
            "selected_user": str((user_conf or {}).get("name", "")),
            "input": _short_log_text(input_block),
            "details": details or {},
        }
        append_jsonl(log_dir / "runtime_diagnostics.jsonl", record)
    except Exception as e:
        print(f"[runtime_diagnostics] log error: {type(e).__name__}: {e}")


def _log_system_diagnostic(event: str, details: dict | None = None) -> None:
    """メッセージに紐づかない設定診断ログを追記する"""
    try:
        system_conf = load_system()
        log_dir = get_log_dir(system_conf)
        append_jsonl(log_dir / "runtime_diagnostics.jsonl", {
            "ts": now_jst_iso(),
            "event": event,
            "discord_user_id": None,
            "author_is_parent": None,
            "channel_id": None,
            "channel_name": "",
            "selected_user": "",
            "input": "",
            "details": details or {},
        })
    except Exception as e:
        print(f"[runtime_diagnostics] system log error: {type(e).__name__}: {e}")


async def _send_processing_error_fallback(message: discord.Message, error: Exception) -> None:
    """メッセージ処理が落ちた場合、診断ログを残してユーザーへ最終応答を返す。"""
    content = str(getattr(message, "content", "") or "")
    try:
        system_conf = load_system()
    except Exception:
        system_conf = {}
    try:
        user_conf = find_user_by_discord_id(message.author.id)
    except Exception:
        user_conf = None
    _log_runtime_event(
        system_conf=system_conf,
        message=message,
        user_conf=user_conf,
        input_block=content,
        event="message_processing_unhandled_error",
        details={
            "error_type": type(error).__name__,
            "error": _short_log_text(error, limit=600),
            "user_action": "contact_admin",
        },
    )
    try:
        await message.channel.send(processing_failure_message())
    except Exception as send_error:
        print("fallback send error:", send_error)


def _should_send_unhandled_error_fallback(message: discord.Message) -> bool:
    """未捕捉例外時にユーザーへ返答すべきメッセージか判定する。"""
    if _was_thinking_sent(message):
        return True
    if bool(getattr(getattr(message, "author", None), "bot", False)):
        return False
    content = str(getattr(message, "content", "") or "").strip()
    if not content:
        return False
    try:
        if extract_input_from_mention(content, client.user) is not None:
            return True
    except Exception:
        pass
    if contains_any_mention(content):
        return False
    if CHAT_SETTING.get("natural_chat_enabled") and not CHAT_SETTING.get("require_mention"):
        return True
    direct_command_prefixes = [
        "使い方の説明", "つかいかたのせつめい", "支給", "残高調整", "設定変更", "一括支給",
        "アナウンス", "web承認", "全体確認", "全員の分析", "残高チェック送信", "月頭案内送信",
        "reminder test", "reminder-test", "リマインダーテスト", "フォロー方針", "フォロー強さ",
        "フォロー頻度",
    ]
    return any(content.lower().startswith(prefix.lower()) for prefix in direct_command_prefixes)


def _build_learning_context_for_prompt(user_conf: dict, system_conf: dict, audit_state: dict) -> dict:
    """学習支援エンジンの出力を査定プロンプト用に取得する。"""
    if callable(build_learning_insights):
        try:
            analysis_state = dict(audit_state or {})
            analysis_state["learning_support_state"] = _load_learning_support_state_for_prompt(user_conf)
            context = build_learning_insights(
                user_conf=user_conf,
                system_conf=system_conf,
                audit_state=analysis_state,
            )
        except TypeError:
            context = build_learning_insights(user_conf, system_conf, audit_state)
        return context if isinstance(context, dict) else {}

    context = build_reflection_context(
        user_conf=user_conf,
        system_conf=system_conf,
        audit_state=audit_state,
    )
    return context if isinstance(context, dict) else {}


def _load_learning_support_state_for_prompt(user_conf: dict) -> dict:
    """Webで保存された会話カード状態をDiscord査定プロンプトにも反映する"""
    name = str((user_conf or {}).get("name") or "").strip()
    user_id = str((user_conf or {}).get("discord_user_id") or "").strip()
    key = quote(name or user_id or "unknown", safe="")
    path = Path(__file__).resolve().parents[1] / "data" / "learning_support_state" / f"{key}.json"
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


@client.event
async def on_ready():
    try:
        await _on_ready_impl()
    except Exception as e:
        print("on_ready error:", e)
        _log_system_diagnostic(
            "on_ready_unhandled_error",
            {
                "error_type": type(e).__name__,
                "error": _short_log_text(e, limit=600),
            },
        )


async def _on_ready_impl():
    print(f"Compass logged in as {client.user}")
    conflicts = get_discord_id_conflicts()
    for conflict in conflicts:
        print(
            "[config_warning] discord_user_id duplicated between child and parent: "
            f"{conflict}"
        )
    if conflicts:
        _log_system_diagnostic(
            "config_duplicate_discord_id",
            {
                "conflicts": conflicts,
                "policy": "parent_lookup_takes_precedence; use proxy or channel context for child operations",
            },
        )
    # 分割したハンドラモジュールに依存オブジェクトを注入する
    handlers_parent.init(
        wallet_service=wallet_service,
        client=client,
        reminder_service=reminder_service,
        allowance_reminder_conf=ALLOWANCE_REMINDER,
    )
    handlers_child.init(
        wallet_service=wallet_service,
        client=client,
        low_balance_alert_conf=LOW_BALANCE_ALERT,
    )
    # Webダッシュボード用サーバーに Discord client と wallet_service を注入する
    from app import server as web_server
    web_server.init(discord_client=client, wallet_service=wallet_service)
    reminder_service.start_loop_if_needed()
    print("Allowance reminder loop started")


@client.event
async def on_message(message: discord.Message):
    try:
        await _on_message_impl(message)
    except Exception as e:
        print("on_message error:", e)
        if _should_send_unhandled_error_fallback(message):
            await _send_processing_error_fallback(message, e)
    finally:
        _mark_thinking_sent(message, False)


async def _on_message_impl(message: discord.Message):
    _mark_thinking_sent(message, False)
    if message.author.bot:
        return

    content = (message.content or "").strip()
    bot_mention_input = extract_input_from_mention(content, client.user)
    if bot_mention_input is None and contains_any_mention(content):
        return

    # 「使い方の説明と初期設定」は全チャンネルへの一斉通知のため最優先で処理する
    if await handlers_parent.maybe_handle_parent_broadcast_guide(message, content):
        return

    # 「使い方の説明」は単体チャンネルへの送信（一斉送信より後に判定する）
    if await handlers_parent.maybe_handle_parent_usage_single(message, content):
        return

    if ALLOW_CHANNEL_IDS is not None and message.channel.id not in ALLOW_CHANNEL_IDS:
        return

    if content.startswith("[#SH-"):
        await message.channel.send("`[#SH-xxx]`形式は非対応です。`@compass-bot 内容` で送ってね。")
        return

    if await handlers_parent.maybe_handle_parent_dashboard(message, content):
        return

    if await handlers_parent.maybe_handle_spending_analysis(message, content):
        return

    if await handlers_parent.maybe_handle_wallet_audit_send(message, content):
        return

    if await handlers_parent.maybe_handle_reminder_test(message, content):
        return

    # 親による支給コマンド（「支給 たろう 700円」）
    if await handlers_parent.maybe_handle_manual_grant(message, content):
        return

    # 親による残高調整コマンド（「残高調整 たろう +500円」）
    if await handlers_parent.maybe_handle_balance_adjustment(message, content):
        return

    # 親による設定変更コマンド（「設定変更 たろう 固定 800円」）
    if await handlers_parent.maybe_handle_user_setting_change(message, content):
        return

    # 親によるAIフォロー方針変更（「フォロー方針 たろう 記録習慣を重視」）
    if await handlers_parent.maybe_handle_followup_policy(message, content):
        return

    # 親による全ユーザー一括支給コマンド（「一括支給」）
    if await handlers_parent.maybe_handle_bulk_grant(message, content):
        return

    # 親による全チャンネル一斉アナウンス（「アナウンス [本文]」）
    if await handlers_parent.maybe_handle_parent_announce(message, content):
        return

    # 親によるWebダッシュボードアクセス申請の承認（「web承認 [ユーザー名]」）
    if await handlers_parent.maybe_handle_web_approve(message, content):
        return

    # 親による査定支給の承認/却下（「査定承認 [名前]」「査定却下 [名前]」）
    if await handlers_parent.maybe_handle_assessment_approve(message, content):
        return

    mention_input = bot_mention_input
    if mention_input is None:
        if CHAT_SETTING.get("natural_chat_enabled") and not CHAT_SETTING.get("require_mention"):
            mention_input = content
        else:
            return

    system_conf = load_system()
    user_conf = find_user_by_discord_id(message.author.id)
    selected_user_source = "author_discord_id" if user_conf is not None else "unresolved_author"
    proxy_name, input_block = parse_proxy_request(mention_input)

    if not input_block:
        await message.channel.send("相談内容を本文に書いて送ってね。")
        return

    if proxy_name:
        if not is_parent(message.author.id):
            await message.channel.send("`代理登録` は親のみ使用できるよ。")
            return
        user_conf = find_user_by_name(proxy_name)
        selected_user_source = "proxy"
        if user_conf is None:
            await message.channel.send(
                f"`{proxy_name}` はユーザー設定に見つからなかったよ。`settings/users/*.json` の `name` を確認してね。"
            )
            return
    elif is_parent(message.author.id):
        # 親が子ども用チャンネルで自然言語入力した場合は、そのチャンネルの子を対象にする
        channel_child_conf = _find_channel_child_user_conf(message)
        if channel_child_conf is not None:
            user_conf = channel_child_conf
            selected_user_source = "parent_channel_context"
        elif user_conf is not None:
            selected_user_source = "author_discord_id"

    if user_conf is None:
        await message.channel.send("設定にあなたのDiscord IDが登録されてないみたい。親に `settings/users/*.json` を追加してもらってね。")
        return

    if not is_parent(message.author.id) and _looks_like_parent_only_command(input_block):
        await message.channel.send("その操作は親のみできるよ。")
        _log_runtime_event(
            system_conf, message, user_conf, input_block,
            "parent_only_command_rejected",
            {"selected_user_source": selected_user_source},
        )
        return

    _log_runtime_event(
        system_conf=system_conf,
        message=message,
        user_conf=user_conf,
        input_block=input_block,
        event="message_context_resolved",
        details={
            "proxy_name": proxy_name,
            "selected_user_source": selected_user_source,
            "issue_tags": _diagnostic_issue_tags(
                input_block=input_block,
                selected_user_source=selected_user_source,
                author_is_parent=is_parent(message.author.id),
            ),
        },
    )

    if is_parent(message.author.id) and not proxy_name:
        parent_guide = _parent_natural_management_guide(input_block)
        if parent_guide:
            await message.channel.send(parent_guide)
            _log_runtime_event(
                system_conf, message, user_conf, input_block,
                "parent_natural_management_guided",
                {
                    "selected_user_source": selected_user_source,
                    "issue_tags": ["parent_natural_management_request"],
                },
            )
            return

    # --- AI 主導（Phase N-11）: claude CLI が会話を主導し、金額処理は wallet tool へ委譲する ---
    # 旧 B案（Gemini で intent 正規化 → _dispatch_by_intent → 査定フロー）は廃止し、handle_conversation へ一本化。
    # 雑談・残高・支出入金・目標・査定提案はすべて AI が会話の中で判断し、必要なら wallet tool を呼ぶ。
    # 会話文脈は claude session の --resume で継続する（現行で切れていた会話永続性の根本解決）。

    # 会話層は子ども本人の会話を前提とする。対象が子として実在しない（＝親が子チャンネル外で
    # 自然文を送った等）場合は会話層へ流さず案内する。wallet tool は本人性を env で束縛して親を
    # 弾くため実残高は元々安全だが、AI が親を子ども扱いして雑談する不自然さを手前で止める。
    from app.config import find_child_user_by_name
    if find_child_user_by_name(str(user_conf.get("name", ""))) is None:
        await message.channel.send(
            "お小遣いの相談は、お子さん本人のチャンネルか `名前の代理 〜` で話しかけてね。"
        )
        _mark_thinking_sent(message, True)
        return

    from app.conv.ai_conversation import handle_conversation
    await handle_conversation(message.channel, user_conf, input_block)
    _mark_thinking_sent(message, True)

    # 査定提案が出ていたら親へ通知する。propose_allowance は残高を動かさず pending を積むだけで、
    # 親が承認して初めて支給される。mcp_wallet は Discord を叩けないため、ここで未通知の提案を
    # 検知して発話チャンネルへ知らせる（承認/却下コマンドの書式も必ず添える）。通知漏れで支給が
    # 放置され子の頑張りが宙に浮くのを防ぐ。
    # 親を確実に呼ぶため、通知本文へ親IDメンションを付ける。親がそのチャンネルを常時見ていなくても
    # メンションで通知が飛ぶ。加えて未承認 pending は reminder_service の定期ループが親へ再通知する
    # （見逃し対策。ここでの1回きり通知に依存しない）。
    try:
        from app import mcp_wallet
        proposals = mcp_wallet.take_unnotified_proposals()
        if proposals:
            parent_mention = " ".join(f"<@{pid}>" for pid in sorted(get_parent_ids())) or "おうちの人"
            for proposal in proposals:
                child = str(proposal.get("name", ""))
                total = int(proposal.get("total", 0))
                reason = str(proposal.get("reason", ""))
                await message.channel.send(
                    f"🔔 {parent_mention} {child} さんの査定の提案が来ています。\n"
                    f"- 提案額: {total}円\n- 理由: {reason}\n"
                    f"承認するには `査定承認 {child}`、見送るには `査定却下 {child}` と送ってください。"
                )
    except Exception as e:
        # 通知の失敗で応答経路を壊さない。診断ログにだけ残す
        _log_runtime_event(
            system_conf, message, user_conf, input_block,
            "assessment_notify_error",
            {"error": f"{type(e).__name__}: {e}"},
        )
    return


async def _main():
    """Discordボットと uvicorn Webサーバーを同一プロセスで並列起動する"""
    from app.server import app as web_app
    # uvicorn を asyncio モードで起動する（ポート8765固定）
    uvicorn_config = uvicorn.Config(
        app=web_app,
        host="127.0.0.1",
        port=8765,
        loop="asyncio",
        log_level="warning",  # uvicornのアクセスログは warning 以上のみ表示
    )
    uvicorn_server = uvicorn.Server(uvicorn_config)
    # Discord クライアントと uvicorn を並列で実行する
    async with client:
        await asyncio.gather(
            client.start(DISCORD_BOT_TOKEN),
            uvicorn_server.serve(),
        )


if __name__ == "__main__":
    if not DISCORD_BOT_TOKEN or not GEMINI_API_KEY:
        raise RuntimeError("DISCORD_BOT_TOKEN / GEMINI_API_KEY が未設定")
    asyncio.run(_main())
