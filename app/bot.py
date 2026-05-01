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
    find_parent_by_discord_id,
    find_user_by_discord_id,
    find_user_by_name,
    get_discord_id_conflicts,
    get_allow_channel_ids,
    get_assess_keyword,
    get_allowance_reminder_setting,
    get_chat_setting,
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
from app import intent_normalizer
from app.gemini_service import GeminiService, count_recent_allowance_requests
from app.message_parser import (
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

# 初期設定・財布チェックで受け付ける現実的な財布上限。Discord ID等の誤入力を拒否する。
MAX_WALLET_INPUT_AMOUNT = 1_000_000
# 貯金目標の補完入力で受け付ける上限。通常の子供向け目標として十分な範囲にする。
MAX_GOAL_INPUT_AMOUNT = 10_000_000


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


def _coerce_positive_amount(value, max_amount: int) -> int | None:
    """Gemini等から来た金額を正の整数かつ上限内に丸める"""
    if value is None:
        return None
    try:
        amount = int(value)
    except Exception:
        return None
    if amount <= 0 or amount > int(max_amount):
        return None
    return amount


def _looks_uncertain_money_statement(input_block: str) -> bool:
    """金額が概算・未確定に見える入力かどうかを判定する"""
    compact = re.sub(r"\s+", "", input_block or "").lower()
    if not compact:
        return False
    uncertain_keywords = [
        "くらい", "ぐらい", "位", "くらいかな", "ぐらいかな", "たぶん", "多分",
        "かも", "かもしれない", "気がする", "きがする", "だと思う", "約",
        "およそ", "だいたい", "大体", "わからない", "分からない", "わかんない",
        "忘れた", "あとで",
    ]
    return any(k.lower() in compact for k in uncertain_keywords)


def _extract_confirmed_yen_amount(input_block: str, max_amount: int) -> int | None:
    """残高を変更してよい、単位付きかつ未確定表現でない金額だけを返す"""
    if _looks_uncertain_money_statement(input_block):
        return None
    return _parse_yen_amount(input_block, require_yen=True, max_amount=max_amount)


def _looks_like_bare_amount_reply(input_block: str) -> bool:
    """数字はあるが円/えん/万円の単位がない返答かどうか"""
    body = input_block or ""
    return bool(re.search(r"\d", body)) and _parse_yen_amount(body, require_yen=True) is None


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


def _is_initial_setup_pending(user_name: str) -> bool:
    state = wallet_service.load_audit_state()
    pending = state.get("initial_setup_pending_by_user", {})
    return bool(isinstance(pending, dict) and user_name in pending)

def _set_initial_setup_pending(user_name: str, enabled: bool) -> None:
    state = wallet_service.load_audit_state()
    pending = state.get("initial_setup_pending_by_user")
    if not isinstance(pending, dict):
        pending = {}
    if enabled:
        pending[user_name] = now_jst_iso()
    else:
        pending.pop(user_name, None)
    state["initial_setup_pending_by_user"] = pending
    wallet_service.save_audit_state(state)

async def _handle_initial_setup_pending(
    message: discord.Message,
    user_conf: dict,
    system_conf: dict,
    input_block: str,
) -> bool:
    """初期設定フローの pending 状態を処理する（AI正規化の前に呼ぶ）。
    pending 状態でない場合は False を返して次の処理へ委譲する。"""
    user_name = str(user_conf.get("name", ""))
    if not user_name or not _is_initial_setup_pending(user_name):
        return False

    # pending 状態: 金額入力を待っている
    amount = _extract_confirmed_yen_amount(input_block, MAX_WALLET_INPUT_AMOUNT)
    if amount is None:
        await message.channel.send(
            "初期設定を続けるよ。\nいまの所持金を `1234円` の形で送ってね。（円まで書いてね）"
        )
        return True

    # 金額が入力されたので残高を設定する
    before = wallet_service.get_balance(user_name)
    delta = int(amount) - int(before)
    wallet_service.update_balance(
        user_conf=user_conf,
        system_conf=system_conf,
        delta=delta,
        action="initial_setup",
        note="set_current_wallet_balance",
        extra={"discord_user_id": int(message.author.id)},
    )
    _set_initial_setup_pending(user_name, False)
    await message.channel.send(
        "初期設定を反映したよ。"
        f"\n対象: {user_name}"
        f"\n所持金: {before}円 → {amount}円"
    )
    return True


# NOTE: maybe_handle_help_and_initial_setup は B案移行に伴い
#       _handle_initial_setup_pending に置き換えた（N-2）。
#       トリガー（初期設定・使い方・残高確認・支出記録の起動）は
#       AI正規化 → _dispatch_by_intent で処理する。

# NOTE: 支出記録フロー（pending 状態 + 直接フォーマット入力）は
#       maybe_handle_spending_record_flow で継続して処理する。
#       起動トリガー（自然言語での「支出記録したい」等）は
#       AI正規化 → _dispatch_by_intent の spending_record ブランチで処理する。


async def _extract_expense_info(text: str, gemini_service) -> dict:
    """Gemini で自然言語から支出情報を抽出する。
    戻り値: {"item": str|None, "amount": int|None, "reason": str|None, "satisfaction": int|None}"""
    extract_prompt = (
        "以下のメッセージから支出情報を抽出してJSONのみ返してください（説明不要）。\n"
        '{"item": "買ったもの(必須)", "amount": 金額の整数またはnull, '
        '"reason": "理由またはnull", "satisfaction": 0〜10の整数またはnull}\n'
        "【注意】\n"
        "- item は具体的な商品・サービス名が必要。「なんか」「もの」「あれ」「これ」など曖昧な場合は null。\n"
        "- reason は購入の動機・感想・目的のみ。「残高教えて」「いくらある？」などコマンド的な文章から reason を推測しないこと。\n"
        "- 支出の話をしていないメッセージには全フィールド null を返すこと。\n\n"
        f"メッセージ: {text}"
    )
    if not gemini_service:
        return {}
    try:
        raw = await gemini_service.call_silent(extract_prompt)
        m = re.search(r'\{[\s\S]*\}', raw)
        if m:
            result = json.loads(m.group())
            # satisfaction は 0〜10 の範囲に丸める
            sat = result.get("satisfaction")
            if sat is not None:
                try:
                    result["satisfaction"] = max(0, min(10, int(sat)))
                except Exception:
                    result["satisfaction"] = None
            return result
    except Exception as e:
        print(f"[expense_extract] error: {e}")
    return {}


def _save_spending_pending(
    user_name: str,
    item,
    amount,
    reason,
    satisfaction,
    asked_optional: bool = False,
    entry_id: str | None = None,
) -> None:
    """支出記録フローの partial データを pending 状態として保存する。
    asked_optional=True の場合は記録済みで optional 待ちの soft pending を示す。"""
    state = wallet_service.load_audit_state()
    pending = state.get("spending_record_pending_by_user", {})
    if not isinstance(pending, dict):
        pending = {}
    pending[user_name] = {
        "ts": now_jst_iso(),
        "item": item,
        "amount": amount,
        "reason": reason,
        "satisfaction": satisfaction,
        "asked_optional": asked_optional,
        "entry_id": entry_id,
    }
    state["spending_record_pending_by_user"] = pending
    wallet_service.save_audit_state(state)


def _clear_spending_pending(user_name: str) -> None:
    """spending_record の pending 状態を解除する"""
    state = wallet_service.load_audit_state()
    pending = state.get("spending_record_pending_by_user", {})
    if isinstance(pending, dict):
        pending.pop(user_name, None)
    state["spending_record_pending_by_user"] = pending
    wallet_service.save_audit_state(state)


def _merge_expense_optional_into_entry(journal_path, entry_id: str, reason, satisfaction) -> bool:
    """entry_id が一致する支出行へ理由・満足度を追記する。"""
    if not entry_id or not journal_path.exists():
        return False

    try:
        raw_lines = journal_path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return False

    changed = False
    rewritten: list[str] = []
    for line in raw_lines:
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            rewritten.append(line)
            continue

        if isinstance(row, dict) and str(row.get("entry_id") or "") == str(entry_id):
            if reason:
                row["reason"] = reason
                row["reason_word_count"] = _rough_word_count(reason)
            if satisfaction is not None:
                row["satisfaction"] = int(satisfaction)
            row["supplemented_at"] = now_jst_iso()
            changed = True
        rewritten.append(json.dumps(row, ensure_ascii=False) if isinstance(row, dict) else line)

    if not changed:
        return False

    try:
        journal_path.write_text("\n".join(rewritten) + "\n", encoding="utf-8")
    except OSError:
        return False
    return True


def _write_expense_optional(
    user_conf: dict, system_conf: dict, item, reason, satisfaction, entry_id: str | None = None
) -> None:
    """optional 情報（理由・満足度）を元支出へ紐づける。"""
    user_name = str(user_conf.get("name", ""))
    log_dir = get_log_dir(system_conf)
    journal_path = log_dir / f"{user_name}_pocket_journal.jsonl"
    if entry_id and _merge_expense_optional_into_entry(journal_path, entry_id, reason, satisfaction):
        return

    # 旧pendingなど entry_id が取れない場合だけ互換用の補足行を残す。
    append_jsonl(journal_path, {
        "ts": now_jst_iso(),
        "entry_id": entry_id,
        "linked_entry_id": entry_id,
        "discord_user_id": None,
        "name": user_name,
        "action": "expense_supplement",
        "source": "supplement",
        "item": item or "",
        "reason": reason or "",
        "reason_word_count": _rough_word_count(reason or ""),
        "satisfaction": int(satisfaction) if satisfaction is not None else None,
        "amount": None,  # 補足エントリは残高変更なし
    })


async def _commit_expense_record(
    message: discord.Message,
    user_conf: dict,
    system_conf: dict,
    item: str,
    amount,
    reason,
    satisfaction,
) -> tuple[bool, str]:
    """支出情報を pocket_journal に記録し、金額があれば残高を更新して完了メッセージを送信する。
    成功フラグと entry_id を返す。残高不足でも記録行には entry_id を残す。"""
    user_name = str(user_conf.get("name", ""))
    log_dir = get_log_dir(system_conf)
    journal_path = log_dir / f"{user_name}_pocket_journal.jsonl"
    entry_id = wallet_service.new_entry_id("expense")
    reason_words = _rough_word_count(reason or "")
    append_jsonl(journal_path, {
        "ts": now_jst_iso(),
        "entry_id": entry_id,
        "discord_user_id": message.author.id,
        "name": user_name,
        "action": "spending_record",
        "source": "normal_record",
        "item": item,
        "reason": reason or "",
        "reason_word_count": reason_words,
        "satisfaction": int(satisfaction) if satisfaction is not None else None,
        "amount": int(amount) if amount is not None else None,
    })
    # 金額がある場合は残高を更新する
    balance_line = ""
    if amount:
        before = wallet_service.get_balance(user_name)
        if int(before) - int(amount) < 0:
            # 残高不足: pocket_journal には記録するが残高は変更しない
            await message.channel.send(
                "お小遣い帳には記録したよ。\n"
                f"でも残高が足りないよ！今の残高は {before}円 で、{int(amount):,}円 は使えないよ。\n"
                "残高の変更はしなかったよ。"
            )
            return False, entry_id
        new_balance, _ = wallet_service.update_balance(
            user_conf=user_conf,
            system_conf=system_conf,
            delta=-int(amount),
            action="spending_record",
            note=item,
            extra={"entry_id": entry_id, "discord_user_id": int(message.author.id)},
        )
        balance_line = f"\n残高: {before}円 → {new_balance}円"
        await handlers_child.maybe_send_low_balance_alert(user_conf=user_conf, new_balance=new_balance)
    # 満足度がある場合は自己比較メッセージを追加する
    compare_msg = ""
    if satisfaction is not None:
        compare_msg = "\n" + _self_compare_message(log_dir, user_name, int(satisfaction))
    amount_line = f"\n- 金額: {int(amount):,}円" if amount else ""
    reason_line = f"\n- 理由: {reason}" if reason else ""
    satisfaction_line = f"\n- 満足度: {satisfaction}/10" if satisfaction is not None else ""
    # optional 不足の場合は「今教えてくれると嬉しい」招待文を添える（soft pending と連動する）
    opt_hint = ""
    if not reason and satisfaction is None:
        opt_hint = "\nよかったら理由と満足度（0〜10）も教えてくれると振り返りに役立つよ！（スキップしてもOKだよ）"
    elif not reason:
        opt_hint = "\nよかったら理由も教えてくれると振り返りに役立つよ！（スキップしてもOKだよ）"
    elif satisfaction is None:
        opt_hint = "\nよかったら満足度（0〜10）も教えてくれると振り返りに役立つよ！（スキップしてもOKだよ）"
    await message.channel.send(
        "お小遣い帳に記録したよ！"
        f"\n- 買ったもの: {item}"
        f"{amount_line}"
        f"{reason_line}"
        f"{satisfaction_line}"
        f"{balance_line}"
        f"{compare_msg}"
        f"{opt_hint}"
    )
    # 記録成功を呼び出し元に伝える
    return True, entry_id


async def _process_expense_flow(
    message: discord.Message,
    user_conf: dict,
    system_conf: dict,
    item,
    amount,
    reason,
    satisfaction,
    gemini_service,
) -> None:
    """支出記録フローの共通ロジック。
    item/amount が揃っていない → 必須項目を要求して pending にする。
    item+amount が揃っていれば即記録し、optional 不足なら soft pending を設定する。"""
    user_name = str(user_conf.get("name", ""))

    # 必須: item がない
    if not item:
        suffix = "と金額" if amount is None else ""
        _save_spending_pending(user_name, item, amount, reason, satisfaction)
        await message.channel.send(f"何を買ったか{suffix}教えてね！（「やめる」でキャンセルもできるよ）")
        return

    # 必須: amount がない
    if amount is None:
        _save_spending_pending(user_name, item, amount, reason, satisfaction)
        await message.channel.send(f"{item}を買ったんだね！いくらだった？（「やめる」でキャンセルもできるよ）")
        return

    # item + amount 揃い → 即記録する
    success, entry_id = await _commit_expense_record(
        message, user_conf, system_conf, item, amount, reason, satisfaction
    )

    if success and (not reason or satisfaction is None):
        # optional が不足している場合は soft pending を設定する（次メッセージで受け付ける）
        # ブロッキングではないので別トピックが来たら自動解除される
        _save_spending_pending(
            user_name, item, amount, reason, satisfaction, asked_optional=True, entry_id=entry_id
        )
    else:
        # optional 込みで完了 or 残高不足の場合は pending を解除する
        _clear_spending_pending(user_name)


async def maybe_handle_spending_record_flow(
    message: discord.Message,
    user_conf: dict,
    system_conf: dict,
    input_block: str,
    gemini_service=None,
) -> bool:
    """支出記録フロー（pending 状態のみ処理する）。
    pending 中の自然言語入力を Gemini で構造化してフローを継続する。"""
    user_name = str(user_conf.get("name", ""))
    if not user_name:
        return False

    state = wallet_service.load_audit_state()
    pending = state.get("spending_record_pending_by_user", {})
    if not isinstance(pending, dict):
        pending = {}

    # pending 状態でなければこのハンドラーは処理しない
    if user_name not in pending:
        return False

    # 保存済み partial データを取得する
    partial = pending.get(user_name) or {}
    stored_item = partial.get("item") if isinstance(partial, dict) else None
    stored_amount = partial.get("amount") if isinstance(partial, dict) else None
    stored_reason = partial.get("reason") if isinstance(partial, dict) else None
    stored_sat = partial.get("satisfaction") if isinstance(partial, dict) else None
    asked_optional = partial.get("asked_optional", False) if isinstance(partial, dict) else False
    stored_entry_id = str(partial.get("entry_id") or "") if isinstance(partial, dict) else ""

    # --- soft pending（記録済み・optional 待ち）の場合 ---
    if asked_optional:
        # スキップワードなら pending を解除して終了する
        if intent_normalizer.is_no_reply(input_block.strip()):
            _clear_spending_pending(user_name)
            await message.channel.send("OK！また何か買ったら教えてね！")
            return True

        # optional 情報（理由・満足度）を抽出する
        extracted = await _extract_expense_info(input_block, gemini_service) if gemini_service else {}
        reason = extracted.get("reason") or stored_reason
        sat = extracted.get("satisfaction")
        satisfaction = sat if sat is not None else stored_sat

        if reason or satisfaction is not None:
            # optional 情報が取れた → 元の支出 entry_id に紐づけて完了する
            _write_expense_optional(
                user_conf, system_conf, stored_item, reason, satisfaction, entry_id=stored_entry_id or None
            )
            _clear_spending_pending(user_name)
            await message.channel.send("教えてくれてありがとう！記録に追加したよ！")
            return True

        # optional 情報もスキップワードもない → normalize_intent で別トピック判定する
        norm = await intent_normalizer.normalize_intent(input_block, gemini_service) if gemini_service else {"intent": "none"}
        if norm.get("intent", "none") != "none":
            # 新しいトピックと判断 → soft pending を解除して通常処理に戻す
            _clear_spending_pending(user_name)
            return False  # dispatcher に処理させる

        # intent:none で曖昧 → 一言促す
        await message.channel.send("ごめん、よくわからなかったよ。「なし」でスキップもできるよ！")
        return True

    # --- 通常 pending（必須項目待ち）の場合 ---
    # キャンセルワードなら pending を解除して終了する
    if intent_normalizer.is_no_reply(input_block.strip()):
        _clear_spending_pending(user_name)
        await message.channel.send("支出記録をキャンセルしたよ！")
        return True

    # 現メッセージから追加情報を抽出してマージする
    extracted = await _extract_expense_info(input_block, gemini_service) if gemini_service else {}
    item = extracted.get("item") or stored_item
    input_amount = _extract_confirmed_yen_amount(input_block, MAX_WALLET_INPUT_AMOUNT)
    amount = input_amount if input_amount is not None else stored_amount
    reason = extracted.get("reason") or stored_reason
    sat = extracted.get("satisfaction")
    satisfaction = sat if sat is not None else stored_sat

    if stored_amount is None and input_amount is None and re.search(r"\d", input_block or ""):
        item_label = item or stored_item or "買ったもの"
        await message.channel.send(
            f"金額は `100円` のように円まで書いてね。{item_label}はいくらだった？"
        )
        return True

    # item も amount も進展がない場合（関係ない話題 or 曖昧入力の可能性）
    if item == stored_item and amount == stored_amount:
        item_label = stored_item or "買ったもの"
        # 別トピックかどうかを intent で判定する
        norm = await intent_normalizer.normalize_intent(input_block, gemini_service) if gemini_service else {"intent": "none"}
        if norm.get("intent", "none") != "none":
            # 別の用件と判断 → 今は対応できないことを伝えて pending を継続する
            await message.channel.send(
                f"今 **{item_label}** の金額を確認してる途中だよ！\n"
                f"いくらだった？（「やめる」で記録をキャンセルもできるよ）"
            )
        else:
            # 曖昧な入力 → 再促する
            await message.channel.send(
                f"ごめん、読み取れなかったよ。{item_label}はいくらだった？\n"
                f"（「やめる」でキャンセルもできるよ）"
            )
        return True

    await _process_expense_flow(
        message, user_conf, system_conf, item, amount, reason, satisfaction, gemini_service
    )
    return True


# ------------------------------------------------------------------
# pending_intent 状態管理（confidence:low 確認フロー用）
# ------------------------------------------------------------------

def _get_pending_intent(user_name: str) -> dict | None:
    """pending_intent 状態を取得する。なければ None を返す"""
    state = wallet_service.load_audit_state()
    pending = state.get("pending_intent_by_user", {})
    return pending.get(user_name) if isinstance(pending, dict) else None


def _pop_wallet_check_penalty(user_name: str) -> dict | None:
    """財布チェックペナルティノートを取得して削除する（査定時に1回だけ使用する）"""
    state = wallet_service.load_audit_state()
    penalties = state.get("wallet_check_penalties", {})
    if not isinstance(penalties, dict):
        return None
    note = penalties.pop(user_name, None)
    if note:
        state["wallet_check_penalties"] = penalties
        wallet_service.save_audit_state(state)
    return note


async def _do_wallet_check(
    message: discord.Message,
    user_conf: dict,
    system_conf: dict,
    reported: int,
) -> None:
    """財布チェックの照合・帳簿修正・ペナルティ記録の共通処理。
    1. 帳簿を財布に合わせる（update_balance で差分を修正する）
    2. 差分があれば次回査定用にペナルティノートを保存する
    3. 子供にメッセージを送る"""
    user_name = str(user_conf.get("name", ""))
    expected = wallet_service.get_balance(user_name)
    diff = int(reported) - int(expected)

    # 報告済みフラグをクリアする
    state = wallet_service.load_audit_state()
    state.get("pending_by_user", {}).pop(user_name, None)
    state.get("wallet_check_pending_by_user", {}).pop(user_name, None)

    if diff == 0:
        wallet_service.save_audit_state(state)
        await message.channel.send(
            f"財布チェックOK！ぴったり合ってたよ！\n"
            f"財布: {reported}円 / 帳簿: {expected}円"
        )
        return

    # 帳簿を財布の金額に合わせる（update_balance で差分を適用する）
    wallet_service.update_balance(
        user_conf=user_conf,
        system_conf=system_conf,
        delta=diff,
        action="wallet_check_correction",
        note=f"財布チェック修正 差分{diff}円",
    )
    new_balance = wallet_service.get_balance(user_name)

    # 次回査定用にペナルティノートを保存する（差分がある場合は方向に関わらず記録する）
    # diff < 0（支出漏れ）= 重いペナルティ / diff > 0（収入漏れ）= 軽いペナルティ
    penalty_note = None
    penalties = state.get("wallet_check_penalties", {})
    if not isinstance(penalties, dict):
        penalties = {}
    if diff < 0:
        # 財布が帳簿より少ない = 記録されていない支出がある（重大な管理不備）
        penalty_type = "spending_leak"
        penalty_note = "⚠️ 次のお小遣い相談のときに、記録されていない支出があったことが考慮されるよ。"
    else:
        # 財布が帳簿より多い = 記録されていない収入がある（収入管理の不備）
        penalty_type = "income_leak"
        penalty_note = "📝 もらったお金が記録されていないよ。入金コマンドで記録しておこう。次のお小遣い相談でも触れるよ。"
    penalties[user_name] = {
        "ts": now_jst_iso(),
        "type": penalty_type,
        "diff": diff,
        "reported": int(reported),
        "expected": int(expected),
    }
    state["wallet_check_penalties"] = penalties

    wallet_service.save_audit_state(state)

    # メッセージを送る
    diff_desc = f"{abs(diff)}円{'多い' if diff > 0 else '少ない'}"
    correction_note = (
        "記録漏れの入金があったみたいだね。帳簿に足したよ。"
        if diff > 0
        else "記録漏れの支出があったみたいだね。帳簿を修正したよ。"
    )
    msg = (
        f"財布と帳簿がずれてたよ。\n"
        f"財布: {reported}円 / 帳簿: {expected}円（{diff_desc}）\n"
        f"{correction_note}\n"
        f"修正後の帳簿残高: {new_balance}円"
    )
    if penalty_note:
        msg += f"\n\n{penalty_note}"
    await message.channel.send(msg)


async def _handle_wallet_check_pending(
    message: discord.Message,
    user_conf: dict,
    system_conf: dict,
    input_block: str,
) -> bool:
    """wallet_check pending 状態を処理する。財布の中身の金額入力を受け取って照合する。"""
    user_name = str(user_conf.get("name", ""))
    state = wallet_service.load_audit_state()
    wcp = state.get("wallet_check_pending_by_user", {})
    if not isinstance(wcp, dict) or user_name not in wcp:
        return False

    reported = _extract_confirmed_yen_amount(input_block, MAX_WALLET_INPUT_AMOUNT)
    if reported is None:
        await message.channel.send("金額が読み取れなかったよ。「1500円」のように円まで書いて送ってね。")
        return True

    await _do_wallet_check(message, user_conf, system_conf, reported)
    return True


def _save_manual_income_pending(user_name: str, item: str | None = None) -> None:
    """臨時入金の金額待ち状態を保存する"""
    state = wallet_service.load_audit_state()
    pending = state.get("manual_income_pending_by_user", {})
    if not isinstance(pending, dict):
        pending = {}
    pending[user_name] = {
        "ts": now_jst_iso(),
        "item": item or "",
    }
    state["manual_income_pending_by_user"] = pending
    wallet_service.save_audit_state(state)


def _clear_manual_income_pending(user_name: str) -> None:
    """臨時入金の金額待ち状態を解除する"""
    state = wallet_service.load_audit_state()
    pending = state.get("manual_income_pending_by_user", {})
    if isinstance(pending, dict):
        pending.pop(user_name, None)
    state["manual_income_pending_by_user"] = pending
    wallet_service.save_audit_state(state)


async def _handle_manual_income_pending(
    message: discord.Message,
    user_conf: dict,
    system_conf: dict,
    input_block: str,
) -> bool:
    """manual_income pending 状態を処理する。金額入力を受け取って入金記録する。"""
    user_name = str(user_conf.get("name", ""))
    state = wallet_service.load_audit_state()
    pending = state.get("manual_income_pending_by_user", {})
    if not isinstance(pending, dict) or user_name not in pending:
        return False

    if intent_normalizer.is_no_reply(input_block.strip()):
        _clear_manual_income_pending(user_name)
        await message.channel.send("入金記録をキャンセルしたよ。")
        return True

    amount = _extract_confirmed_yen_amount(input_block, MAX_WALLET_INPUT_AMOUNT)
    if amount is None:
        if _looks_like_bare_amount_reply(input_block):
            await message.channel.send("金額は `500円` のように円まで書いて送ってね。")
        else:
            await message.channel.send("いくらもらったか、`500円` の形で教えてね。（「やめる」でキャンセルもできるよ）")
        return True

    partial = pending.get(user_name) if isinstance(pending.get(user_name), dict) else {}
    item = str(partial.get("item") or "").strip()
    before = wallet_service.get_balance(user_name)
    new_balance, achieved_goals = wallet_service.update_balance(
        user_conf=user_conf,
        system_conf=system_conf,
        delta=int(amount),
        action="manual_income",
        note=item,
    )
    _clear_manual_income_pending(user_name)
    await message.channel.send(
        f"入金を記録したよ。"
        f"\n- 金額: {amount}円"
        f"\n- メモ: {item if item else 'なし'}"
        f"\n残高: {before}円 → {new_balance}円"
    )
    for achieved_goal in achieved_goals:
        await message.channel.send(
            _build_goal_achieved_message(user_conf=user_conf, goal=achieved_goal)
        )
    return True


def _looks_like_non_goal_title_input(input_block: str) -> bool:
    """貯金目標のタイトル待ち中に、別コマンドらしい文を目標名にしないための判定"""
    body = (input_block or "").strip()
    if not body:
        return True
    if re.fullmatch(r"[\d,\s万円えん]+", body):
        return True

    command_like_keywords = [
        "残高", "所持金", "財布", "お金", "いくら", "教えて", "確認", "見せて",
        "使い方", "ヘルプ", "初期設定", "支出", "買った", "使った", "入金",
        "支給", "設定変更", "残高調整", "一括支給", "ダッシュボード", "分析",
        "代理", "アナウンス", "web承認", "査定", "履歴", "はい", "ちがう",
        "違う", "うん", "そう", "ok", "OK", "わからない", "分からない",
        "わかんない", "あとで", "それで", "くらい", "ぐらい", "たぶん",
        "かも", "やっぱいい", "やめる", "キャンセル",
    ]
    return _contains_any_keyword(body, command_like_keywords)


async def _handle_goal_set_pending(
    message: discord.Message,
    user_conf: dict,
    system_conf: dict,
    input_block: str,
) -> bool:
    """goal_set pending 状態を処理する。タイトルまたは金額の補完入力を受け取って目標を設定する。"""
    user_name = str(user_conf.get("name", ""))
    state = wallet_service.load_audit_state()
    gp = state.get("goal_set_pending_by_user", {})
    if not isinstance(gp, dict) or user_name not in gp:
        return False

    partial = gp[user_name] if isinstance(gp.get(user_name), dict) else {}
    stored_title = partial.get("title")
    stored_amount = partial.get("amount")

    if intent_normalizer.is_no_reply(input_block.strip()) or _contains_any_keyword(input_block, ["やっぱいい", "やっぱりいい"]):
        gp.pop(user_name, None)
        state["goal_set_pending_by_user"] = gp
        wallet_service.save_audit_state(state)
        await message.channel.send("貯金目標の設定をキャンセルしたよ。")
        return True

    # 補完情報を input_block から抽出する
    new_amount = _extract_confirmed_yen_amount(input_block, MAX_GOAL_INPUT_AMOUNT)
    new_title = input_block.strip() if not new_amount else None
    if stored_amount and not stored_title and new_title and _looks_like_non_goal_title_input(new_title):
        _log_runtime_event(
            system_conf, message, user_conf, input_block,
            "goal_set_pending_title_rejected",
            {"reason": "looks_like_command_or_reply", "stored_amount": int(stored_amount)},
        )
        await message.channel.send(
            f"{int(stored_amount):,}円で何を貯めたいか、目標名だけ教えてね。（例: ゲーム機）"
        )
        return True

    title = stored_title or new_title
    amount = stored_amount or new_amount

    if not title or not amount:
        # まだ揃っていない → 足りない方を再度聞く
        if not title:
            await message.channel.send("何を貯めたいか教えてね！")
        else:
            await message.channel.send(f"「{title}」いくら貯めたい？")
        return True

    # 揃った → 目標を設定してクリアする
    success, result = wallet_service.add_savings_goal(user_name, str(title), int(amount))
    gp.pop(user_name, None)
    state["goal_set_pending_by_user"] = gp
    wallet_service.save_audit_state(state)
    if not success:
        await message.channel.send(result)
        return True
    current = wallet_service.get_balance(user_name)
    bar = _progress_bar(current, int(amount))
    action_word = "更新" if result == "updated" else "追加"
    await message.channel.send(
        f"貯金目標を{action_word}したよ。\n"
        f"・目標: {title} {int(amount):,}円\n"
        f"・現在残高: {current:,}円\n"
        f"・進捗: {bar}"
    )
    return True


def _set_pending_intent(user_name: str, intent_result: dict, original_input: str) -> None:
    """confidence:low の確認待ち intent を保存する"""
    state = wallet_service.load_audit_state()
    pending = state.get("pending_intent_by_user")
    if not isinstance(pending, dict):
        pending = {}
    # 元のメッセージと intent_result を保存して確認後にディスパッチできるようにする
    pending[user_name] = {
        "ts": now_jst_iso(),
        "intent_result": intent_result,
        "original_input": original_input,
    }
    state["pending_intent_by_user"] = pending
    wallet_service.save_audit_state(state)


def _clear_pending_intent(user_name: str) -> None:
    """pending_intent 状態を削除する"""
    state = wallet_service.load_audit_state()
    pending = state.get("pending_intent_by_user", {})
    if isinstance(pending, dict):
        pending.pop(user_name, None)
    state["pending_intent_by_user"] = pending
    wallet_service.save_audit_state(state)


async def _ask_intent_confirmation(
    message: discord.Message,
    user_conf: dict,
    intent_result: dict,
    original_input: str,
) -> None:
    """confidence:low の場合、確認メッセージを送信して pending_intent に保存する"""
    user_name = str(user_conf.get("name", ""))
    intent = intent_result.get("intent", "none")
    question = intent_normalizer.get_confirmation_question(intent)
    # pending_intent を保存して次回の返答で確認を受け取れるようにする
    _set_pending_intent(user_name, intent_result, original_input)
    await message.channel.send(f"{question}（「はい」か「ちがう」で答えてね）")


async def _handle_pending_intent_reply(
    message: discord.Message,
    user_conf: dict,
    system_conf: dict,
    input_block: str,
) -> bool:
    """pending_intent の確認返答を処理する（B案: 1回のみ確認フロー）。
    pending がなければ False を返して次の処理へ委譲する。"""
    user_name = str(user_conf.get("name", ""))
    pending = _get_pending_intent(user_name)
    if not pending:
        return False

    if intent_normalizer.is_yes_reply(input_block):
        # ユーザーが確認 → 保存された intent で改めてディスパッチする
        _clear_pending_intent(user_name)
        saved_intent = pending.get("intent_result", {})
        saved_input = pending.get("original_input", input_block)
        return await _dispatch_by_intent(message, user_conf, system_conf, saved_input, saved_intent)

    if intent_normalizer.is_no_reply(input_block):
        # ユーザーが否定 → pending を消して査定フローへ落とす
        _clear_pending_intent(user_name)
        return False

    # 明確な yes/no でない → 確認は1回だけのルール通り pending を消してフォールスルー
    _clear_pending_intent(user_name)
    return False


# ------------------------------------------------------------------
# B案ディスパッチャー — AI正規化済みの intent に応じてハンドラを呼ぶ
# ------------------------------------------------------------------

async def _dispatch_by_intent(
    message: discord.Message,
    user_conf: dict,
    system_conf: dict,
    input_block: str,
    intent_result: dict,
) -> bool:
    """AI正規化済みの intent に応じて適切な処理を呼び出すディスパッチャー。
    処理した場合 True、査定フローへ落とす場合 False を返す。"""
    intent = str(intent_result.get("intent", "none")).strip()
    entities = intent_result.get("entities") or {}
    user_name = str(user_conf.get("name", ""))

    # --- 残高確認 ---
    if intent == "balance_check":
        view_conf = user_conf
        raw_target_name = str(entities.get("target_name") or "").strip()
        text_child_name = _extract_child_name_from_text(input_block) or ""
        # 本文に登録済み子ども名があればそれを優先し、AIの過剰抽出を避ける
        if text_child_name:
            target_name = text_child_name
        elif raw_target_name and _is_child_user_name(raw_target_name):
            target_name = raw_target_name
        else:
            target_name = ""
            if raw_target_name:
                _log_runtime_event(
                    system_conf, message, user_conf, input_block,
                    "target_name_ignored",
                    {
                        "intent": intent,
                        "raw_target_name": raw_target_name,
                        "reason": "not_registered_user",
                    },
                )

        if is_parent(message.author.id) and not _is_child_user_name(user_name) and not target_name:
            await message.channel.send("どの子の残高を確認するか分からなかったよ。`りかの残高おしえて` のように子どもの名前を入れてね。")
            return True

        # 親だけが明示された別ユーザーの残高を確認できる
        if target_name and target_name != user_name:
            if not is_parent(message.author.id):
                await message.channel.send("他のユーザーの残高確認は親のみできるよ。")
                return True
            target_conf = find_user_by_name(target_name)
            if target_conf is None:
                await message.channel.send(f"`{target_name}` はユーザー設定に見つからなかったよ。")
                return True
            view_conf = target_conf

        view_name = str(view_conf.get("name", ""))
        balance = wallet_service.get_balance(view_name)
        await message.channel.send(f"{view_name}さんの現在の所持金は {balance}円 だよ。")
        return True

    # --- 使い方ガイド ---
    if intent == "usage_guide":
        base_guide = _usage_guide_text()
        age = user_conf.get("age")
        age_int = int(age) if isinstance(age, int) else (int(str(age)) if isinstance(age, str) and str(age).strip().isdigit() else None)
        if age_int is not None and age_int <= 12:
            # 年齢が設定されている子供向け: Gemini に年齢適応リライトを依頼する
            rewrite_prompt = (
                f"以下の使い方ガイドを{age_int}歳の子供が読める表現に書き直してください。\n"
                f"【漢字レベルの目安】小学校の学習漢字を基準にしてください。\n"
                f"  - 6〜7歳（1〜2年生）: ほぼ全てひらがな\n"
                f"  - 8〜9歳（3〜4年生）: 3〜4年生までの漢字はOK、それ以上はひらがな\n"
                f"  - 10〜12歳（5〜6年生）: 5〜6年生までの漢字はOK、中学漢字はひらがな\n"
                f"コマンド例も同じ基準で年齢に合った表現にしてください（正確なコマンドでなくてよい）。\n"
                f"余計な説明は不要です。ガイド本文のみ返してください。\n\n"
                f"{base_guide}"
            )
            try:
                adapted = await gemini_service.call_silent(rewrite_prompt)
            except Exception as e:
                _log_runtime_event(
                    system_conf, message, user_conf, input_block,
                    "usage_guide_age_rewrite_failed",
                    {"error": f"{type(e).__name__}: {e}"},
                )
                adapted = ""
            await message.channel.send(adapted if adapted else base_guide)
        else:
            # 年齢未設定または13歳以上: ベーステキストをそのまま送信する
            await message.channel.send(base_guide)
        # 親の場合は親専用コマンド一覧も追加で送信する
        if is_parent(message.author.id):
            await message.channel.send(_usage_guide_text_parent())
        return True

    # --- 初期設定（トリガー）---
    if intent == "initial_setup":
        # Discord IDなどの裸数字を防ぐため、Gemini抽出額ではなく本文の「円」付き金額だけ採用する
        amount = _extract_confirmed_yen_amount(input_block, MAX_WALLET_INPUT_AMOUNT)
        if amount is None:
            # 金額不明 → pending 状態にして入力を促す
            _set_initial_setup_pending(user_name, True)
            await message.channel.send(
                "初期設定をはじめるよ。\nいまの所持金を `1234円` の形で送ってね。（円まで書いてね）"
            )
        else:
            before = wallet_service.get_balance(user_name)
            delta = int(amount) - int(before)
            wallet_service.update_balance(
                user_conf=user_conf,
                system_conf=system_conf,
                delta=delta,
                action="initial_setup",
                note="set_current_wallet_balance",
                extra={"discord_user_id": int(message.author.id)},
            )
            _set_initial_setup_pending(user_name, False)
            await message.channel.send(
                "初期設定を反映したよ。"
                f"\n対象: {user_name}"
                f"\n所持金: {before}円 → {amount}円"
            )
        return True

    # --- 支出記録（spending_record / manual_expense を一本化）---
    if intent in ("spending_record", "manual_expense"):
        # entities から初期情報を取得してフローを開始する
        item = str(entities.get("item") or "").strip() or None
        amount = _extract_confirmed_yen_amount(input_block, MAX_WALLET_INPUT_AMOUNT)
        reason = str(entities.get("reason") or "").strip() or None
        satisfaction = entities.get("satisfaction")
        # satisfaction は 0〜10 の範囲に丸める
        if satisfaction is not None:
            try:
                satisfaction = max(0, min(10, int(satisfaction)))
            except Exception:
                satisfaction = None
        await _process_expense_flow(
            message, user_conf, system_conf, item, amount, reason, satisfaction,
            gemini_service=gemini_service
        )
        return True

    # --- 臨時入金 ---
    if intent == "manual_income":
        amount = _extract_confirmed_yen_amount(input_block, MAX_WALLET_INPUT_AMOUNT)
        # item・reason どちらかをメモとして使う
        item = str(entities.get("item") or entities.get("reason") or "").strip()
        if not amount:
            _save_manual_income_pending(user_name, item)
            await message.channel.send("いくらもらったか、`500円` の形で教えてくれる？（「やめる」でキャンセルもできるよ）")
            return True
        before = wallet_service.get_balance(user_name)
        new_balance, achieved_goals = wallet_service.update_balance(
            user_conf=user_conf,
            system_conf=system_conf,
            delta=int(amount),
            action="manual_income",
            note=item,
        )
        await message.channel.send(
            f"入金を記録したよ。"
            f"\n- 金額: {amount}円"
            f"\n- メモ: {item if item else 'なし'}"
            f"\n残高: {before}円 → {new_balance}円"
        )
        # 入金で目標が達成された場合は祝福メッセージを送る
        for achieved_goal in achieved_goals:
            await message.channel.send(
                _build_goal_achieved_message(user_conf=user_conf, goal=achieved_goal)
            )
        return True

    # --- 財布チェック（残高照合）---
    if intent == "balance_report":
        # Geminiが裸数字を amount として返しても、本文に「円」がなければ採用しない
        reported = _extract_confirmed_yen_amount(input_block, MAX_WALLET_INPUT_AMOUNT)
        if reported is None and not _looks_uncertain_money_statement(input_block):
            reported = parse_balance_report(input_block)
        if reported is None:
            # 金額がない → pending にして財布の中身を聞く
            state = wallet_service.load_audit_state()
            wcp = state.get("wallet_check_pending_by_user", {})
            if not isinstance(wcp, dict):
                wcp = {}
            wcp[user_name] = now_jst_iso()
            state["wallet_check_pending_by_user"] = wcp
            wallet_service.save_audit_state(state)
            await message.channel.send("今の財布の中身はいくら？")
            return True
        await _do_wallet_check(message, user_conf, system_conf, int(reported))
        return True

    # --- 貯金目標 確認 ---
    if intent == "goal_check":
        goals = wallet_service.get_savings_goals(user_name)
        current = wallet_service.get_balance(user_name)
        if not goals:
            await message.channel.send(
                "まだ貯金目標が設定されてないよ。\n何か貯めたいものがあったら教えてね！"
            )
        else:
            lines = [f"【{user_name}の貯金目標（残高: {current:,}円）】"]
            for g in goals:
                title = str(g.get("title", ""))
                target_amount = int(g.get("target_amount", 0))
                bar = _progress_bar(current, target_amount)
                remaining = max(target_amount - current, 0)
                lines.append(
                    f"\n・{title}: {target_amount:,}円\n"
                    f"  進捗: {bar}\n"
                    f"  あと: {remaining:,}円"
                )
            await message.channel.send("\n".join(lines))
        return True

    # --- 貯金目標 設定 ---
    if intent == "goal_set":
        goal_title = str(entities.get("goal_title") or "").strip() or None
        goal_amount = _extract_confirmed_yen_amount(input_block, MAX_GOAL_INPUT_AMOUNT)
        # タイトルと金額の両方が揃ったら設定する、どちらか欠けたら pending で補完する
        if goal_title and goal_amount:
            target_amount = int(goal_amount)
            success, result = wallet_service.add_savings_goal(user_name, goal_title, target_amount)
            if not success:
                await message.channel.send(result)
                return True
            # goal_set pending があればクリアする
            state = wallet_service.load_audit_state()
            state.get("goal_set_pending_by_user", {}).pop(user_name, None)
            wallet_service.save_audit_state(state)
            current = wallet_service.get_balance(user_name)
            bar = _progress_bar(current, target_amount)
            action_word = "更新" if result == "updated" else "追加"
            await message.channel.send(
                f"貯金目標を{action_word}したよ。\n"
                f"・目標: {goal_title} {target_amount:,}円\n"
                f"・現在残高: {current:,}円\n"
                f"・進捗: {bar}"
            )
        elif goal_title:
            # タイトルはある・金額がない → 保存して金額を聞く
            state = wallet_service.load_audit_state()
            gp = state.get("goal_set_pending_by_user", {})
            if not isinstance(gp, dict):
                gp = {}
            gp[user_name] = {"ts": now_jst_iso(), "title": goal_title}
            state["goal_set_pending_by_user"] = gp
            wallet_service.save_audit_state(state)
            await message.channel.send(f"「{goal_title}」いくら貯めたい？")
        elif goal_amount:
            # 金額はある・タイトルがない → 保存してタイトルを聞く
            state = wallet_service.load_audit_state()
            gp = state.get("goal_set_pending_by_user", {})
            if not isinstance(gp, dict):
                gp = {}
            gp[user_name] = {"ts": now_jst_iso(), "amount": int(goal_amount)}
            state["goal_set_pending_by_user"] = gp
            wallet_service.save_audit_state(state)
            await message.channel.send(f"{int(goal_amount):,}円で何を貯めたいの？")
        else:
            # どちらもない → 両方聞く
            await message.channel.send("何を、いくら貯めたいか教えてね！\n例: 「ゲーム機を30000円貯めたい」")
        return True

    # --- 貯金目標 削除 ---
    if intent == "goal_clear":
        goal_title = str(entities.get("goal_title") or "").strip()
        if goal_title:
            # タイトルが指定されている場合は1件だけ削除する
            found = wallet_service.remove_savings_goal(user_name, goal_title)
            if found:
                await message.channel.send(f"目標「{goal_title}」を削除したよ。")
            else:
                await message.channel.send(f"目標「{goal_title}」は見つからなかったよ。")
        else:
            if not _contains_any_keyword(input_block, ["全部", "全て", "すべて", "ぜんぶ", "全削除", "全消し"]):
                await message.channel.send("どの目標を削除するか分からなかったよ。`目標削除 ゲーム機` のように目標名を入れてね。全部消すなら `目標を全部削除` と送ってね。")
                return True
            # 明示的な全削除だけ実行する
            wallet_service.clear_all_savings_goals(user_name)
            await message.channel.send(f"{user_name}の貯金目標を全て削除したよ。")
        return True

    # --- 子供向け振り返り ---
    if intent == "child_review":
        now = datetime.now(JST)
        log_dir = get_log_dir(system_conf)
        journal_path = log_dir / f"{user_name}_pocket_journal.jsonl"
        all_rows = _load_jsonl(journal_path)
        # 当月の支出記録だけを絞り込む
        month_rows = [
            r for r in all_rows
            if _is_same_month(r.get("ts"), now.year, now.month)
        ]
        balance = wallet_service.get_balance(user_name)
        msg = _child_review_message(
            user_conf=user_conf,
            month_rows=month_rows,
            balance=balance,
            year=now.year,
            month=now.month,
        )
        await message.channel.send(msg)
        return True

    # --- 査定履歴確認 ---
    if intent == "assessment_history":
        log_dir = get_log_dir(system_conf)
        amounts_path = log_dir / f"{user_name}_allowance_amounts.jsonl"
        all_rows = _load_jsonl(amounts_path)
        # 新しい順に並べるため末尾5件を逆順で取得する
        recent = list(reversed(all_rows[-5:]))
        msg = _assessment_history_message(user_conf=user_conf, rows=recent)
        await message.channel.send(msg)
        return True

    # --- 入出金台帳履歴 ---
    if intent == "ledger_history":
        target_name = str(entities.get("target_name") or "").strip()
        if target_name and target_name != user_name:
            # 他ユーザーの台帳閲覧は親のみ許可する
            if not is_parent(message.author.id):
                await message.channel.send("他のユーザーの台帳確認は親のみできるよ。")
                return True
            target_conf = find_user_by_name(target_name)
            if target_conf is None:
                await message.channel.send(f"`{target_name}` はユーザー設定に見つからなかったよ。")
                return True
            view_conf = target_conf
        else:
            # target_name 未指定 → 自分の台帳を表示する
            view_conf = user_conf
        log_dir = get_log_dir(system_conf)
        ledger_path = log_dir / f"{view_conf.get('name', '')}_wallet_ledger.jsonl"
        rows = _load_jsonl(ledger_path)
        await message.channel.send(_ledger_history_message(view_conf, rows))
        return True

    # --- ダッシュボード（親専用）---
    if intent == "dashboard":
        if not is_parent(message.author.id):
            await message.channel.send("全体確認は親のみできるよ。")
            return True
        log_dir = get_log_dir(load_system())
        users = sorted(load_all_users(), key=lambda x: str(x.get("name", "")))
        audit_state = wallet_service.load_audit_state()
        pending_by_user = audit_state.get("pending_by_user", {})
        lines = ["【全体確認ダッシュボード】"]
        for u in users:
            name = str(u.get("name", ""))
            fixed = int(u.get("fixed_allowance", 0))
            balance = wallet_service.get_balance(name)
            report_status = "未報告" if name in pending_by_user else "報告済"
            # 最終支出日を pocket_journal の末尾レコードから取得する
            journal_rows = _load_jsonl(log_dir / f"{name}_pocket_journal.jsonl")
            last_spending_date = "なし"
            if journal_rows:
                last_ts = journal_rows[-1].get("ts")
                if last_ts:
                    try:
                        dt = datetime.fromisoformat(str(last_ts))
                        last_spending_date = dt.strftime("%m/%d")
                    except Exception:
                        pass
            lines.append(
                f"・{name}: 固定{fixed}円 / 残高{balance}円 / 残高報告:{report_status} / 最終支出:{last_spending_date}"
            )
        await message.channel.send("\n".join(lines))
        return True

    # --- 支出傾向分析（親専用）---
    if intent in ("analysis_all", "analysis_user"):
        if not is_parent(message.author.id):
            await message.channel.send("支出傾向分析は親のみできるよ。")
            return True
        log_dir = get_log_dir(load_system())
        now_dt = datetime.now(JST)
        if intent == "analysis_all":
            # 全ユーザー分の分析テキストを結合して返す
            users = sorted(load_all_users(), key=lambda x: str(x.get("name", "")))
            parts = [_spending_analysis_for_user(log_dir, str(u.get("name", "")), now_dt) for u in users]
            reply = "\n\n".join(parts) if parts else "ユーザーが見つからないよ。"
        else:
            # analysis_user: entities から対象ユーザー名を取得する
            target_name = str(entities.get("target_name") or "").strip()
            if not target_name:
                await message.channel.send("分析対象のユーザー名が分からなかったよ。`[名前]の分析` と送ってね。")
                return True
            reply = _spending_analysis_for_user(log_dir, target_name, now_dt)
        # 1900文字超の場合は分割して送信する
        if len(reply) > 1900:
            for i in range(0, len(reply), 1900):
                await message.channel.send(reply[i: i + 1900])
        else:
            await message.channel.send(reply)
        return True

    # --- ボットパーソナリティ変更（N-4）---
    if intent == "personality_change":
        personality = str(entities.get("personality") or "").strip()
        valid = {"parent", "sibling", "friend", "teacher"}
        labels = {
            "parent": "親っぽい口調",
            "sibling": "兄姉っぽい口調",
            "friend": "友達口調",
            "teacher": "先生口調",
        }
        # 認識できない personality は候補を提示して選んでもらう
        if personality not in valid:
            await message.channel.send(
                "どの話し方にする？\n"
                "・「親っぽく」→ 親口調\n"
                "・「兄姉っぽく」→ 兄姉口調（デフォルト）\n"
                "・「友達みたいに」→ 友達口調\n"
                "・「先生っぽく」→ 先生口調"
            )
            return True
        ok = update_user_field(user_name, "bot_personality", personality)
        label = labels[personality]
        if ok:
            await message.channel.send(f"話し方を「{label}」に変えたよ。")
        else:
            await message.channel.send("設定の変更に失敗したよ。ごめんね。")
        return True

    # none: 雑談プロンプトで楽しく会話する（査定フローには落とさない）
    if intent == "none":
        bot_personality = str(user_conf.get("bot_personality") or system_conf.get("bot_personality", "sibling"))
        chat_prompt = build_chat_prompt(
            user_conf=user_conf,
            input_text=input_block,
            bot_personality=bot_personality,
        )
        try:
            reply = await gemini_service.call_with_progress(
                message.channel,
                chat_prompt,
                timeout_reply="ごめん、今AIの返事が遅いみたい。少し短くしてもう一度送ってね。",
            )
        except Exception as e:
            print("Gemini chat error:", e)
            _log_runtime_event(
                system_conf, message, user_conf, input_block,
                "gemini_chat_error",
                {
                    "intent_result": _compact_intent_result(intent_result),
                    "error_type": type(e).__name__,
                    "error": _short_log_text(e, limit=600),
                },
            )
            await message.channel.send("ごめん、今AIの返事が不安定みたい。少し短くしてもう一度送ってね。")
            return True
        if reply:
            await message.channel.send(reply)
        _log_runtime_event(
            system_conf, message, user_conf, input_block,
            "gemini_chat_result",
            {
                "intent_result": _compact_intent_result(intent_result),
                "reply_excerpt": _short_log_text(reply, limit=1200),
                "issue_tags": _diagnostic_issue_tags(
                    input_block=input_block,
                    intent_result=intent_result,
                    reply=reply,
                    author_is_parent=is_parent(message.author.id),
                ),
            },
        )
        return True

    # allowance_request のみ査定フローへ落とす
    return False


async def send_assessment_change_notice(
    source_message: discord.Message,
    user_conf: dict,
    input_text: str,
    assessed: dict,
    previous_assessed: dict | None = None,
) -> tuple[bool, str]:
    """
    査定金額の変更があったとき、allowance_reminder.channel_id へ通知する。
    """
    channel_id = ALLOWANCE_REMINDER.get("channel_id")
    if not channel_id:
        return False, "allowance_reminder.channel_id が未設定"

    fixed_now = assessed.get("fixed")
    temporary_now = assessed.get("temporary")
    if not isinstance(fixed_now, int) or not isinstance(temporary_now, int):
        return False, "査定の固定/臨時が取得できない"

    prev = previous_assessed or {}
    fixed_prev = prev.get("fixed")
    temporary_prev = prev.get("temporary")
    if not isinstance(fixed_prev, int):
        fixed_prev = int(user_conf.get("fixed_allowance", 0))
    if not isinstance(temporary_prev, int):
        temporary_prev = 0

    lines = ["【お小遣い査定変更通知】", f"対象: {user_conf.get('name')}さん"]
    changed = False
    if temporary_now != temporary_prev:
        lines.append(f"臨時: {user_conf.get('name')}さん +{temporary_now}円（前回 +{temporary_prev}円）")
        changed = True
    if fixed_now != fixed_prev:
        lines.append(f"固定: {user_conf.get('name')}さん {fixed_prev}円 → {fixed_now}円")
        changed = True
    if not changed:
        return False, "変更なし"

    channel = client.get_channel(int(channel_id))
    if channel is None:
        channel = await client.fetch_channel(int(channel_id))

    lines.extend(
        [
            f"依頼者ID: {source_message.author.id}",
            f"入力内容: {input_text}",
        ]
    )
    await channel.send("\n".join(lines))
    return True, ""

@client.event
async def on_ready():
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
    if message.author.bot:
        return

    content = (message.content or "").strip()
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

    mention_input = extract_input_from_mention(content, client.user)
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

    # --- B案フロー: pending 状態の処理（AI正規化をスキップして直接処理する）---

    # 初期設定フローの pending 状態を処理する（金額待ち）
    if await _handle_initial_setup_pending(
        message=message,
        user_conf=user_conf,
        system_conf=system_conf,
        input_block=input_block,
    ):
        _log_runtime_event(
            system_conf, message, user_conf, input_block,
            "handled_by_pending",
            {"handler": "initial_setup_pending", "selected_user_source": selected_user_source},
        )
        return

    # 支出記録フロー（pending 状態のみ AI 前にキャッチする）
    if await maybe_handle_spending_record_flow(
        message=message,
        user_conf=user_conf,
        system_conf=system_conf,
        input_block=input_block,
        gemini_service=gemini_service,
    ):
        _log_runtime_event(
            system_conf, message, user_conf, input_block,
            "handled_by_pending",
            {"handler": "spending_record_pending", "selected_user_source": selected_user_source},
        )
        return

    # 臨時入金 pending（金額入力待ち）
    if await _handle_manual_income_pending(
        message=message,
        user_conf=user_conf,
        system_conf=system_conf,
        input_block=input_block,
    ):
        _log_runtime_event(
            system_conf, message, user_conf, input_block,
            "handled_by_pending",
            {"handler": "manual_income_pending", "selected_user_source": selected_user_source},
        )
        return

    # 財布チェック pending（金額入力待ち）
    if await _handle_wallet_check_pending(
        message=message,
        user_conf=user_conf,
        system_conf=system_conf,
        input_block=input_block,
    ):
        _log_runtime_event(
            system_conf, message, user_conf, input_block,
            "handled_by_pending",
            {"handler": "wallet_check_pending", "selected_user_source": selected_user_source},
        )
        return

    # 貯金目標設定 pending（タイトルまたは金額の補完待ち）
    if await _handle_goal_set_pending(
        message=message,
        user_conf=user_conf,
        system_conf=system_conf,
        input_block=input_block,
    ):
        _log_runtime_event(
            system_conf, message, user_conf, input_block,
            "handled_by_pending",
            {"handler": "goal_set_pending", "selected_user_source": selected_user_source},
        )
        return

    # pending_intent の確認返答を処理する（confidence:low 確認フロー）
    if await _handle_pending_intent_reply(
        message=message,
        user_conf=user_conf,
        system_conf=system_conf,
        input_block=input_block,
    ):
        _log_runtime_event(
            system_conf, message, user_conf, input_block,
            "handled_by_pending",
            {"handler": "pending_intent_reply", "selected_user_source": selected_user_source},
        )
        return

    # --- B案: AI正規化 → ディスパッチャー ---

    # AI 呼び出し前に「考え中」を送って無視されていないことを伝える
    age = user_conf.get("age")
    age_int_pre = int(age) if isinstance(age, int) else (int(str(age)) if isinstance(age, str) and str(age).strip().isdigit() else None)
    await message.channel.send(_thinking_message(age_int_pre))

    # Gemini 軽量モデルで intent + entities + confidence を取得する
    intent_result = await intent_normalizer.normalize_intent(input_block, gemini_service)
    intent_issue_tags = _diagnostic_issue_tags(
        input_block=input_block,
        intent_result=intent_result,
        selected_user_source=selected_user_source,
        author_is_parent=is_parent(message.author.id),
    )
    _log_runtime_event(
        system_conf=system_conf,
        message=message,
        user_conf=user_conf,
        input_block=input_block,
        event="gemini_intent_result",
        details={
            "intent_result": _compact_intent_result(intent_result),
            "selected_user_source": selected_user_source,
            "issue_tags": intent_issue_tags,
        },
    )

    # 低信頼度の場合は確認メッセージを送って終了する（確認は1回のみ）
    # intent:none は確認しても意味がないのでスキップする
    if intent_result.get("confidence") == "low" and intent_result.get("intent") != "none":
        await _ask_intent_confirmation(message, user_conf, intent_result, input_block)
        _log_runtime_event(
            system_conf, message, user_conf, input_block,
            "gemini_low_confidence_confirmation",
            {
                "intent_result": _compact_intent_result(intent_result),
                "selected_user_source": selected_user_source,
                "issue_tags": intent_issue_tags,
            },
        )
        return

    # ディスパッチャー（all child コマンドを intent ベースで処理する）
    if await _dispatch_by_intent(message, user_conf, system_conf, input_block, intent_result):
        _log_runtime_event(
            system_conf, message, user_conf, input_block,
            "handled_by_intent",
            {
                "intent_result": _compact_intent_result(intent_result),
                "selected_user_source": selected_user_source,
                "issue_tags": intent_issue_tags,
            },
        )
        return

    # none / allowance_request の場合は査定フローへ落ちる
    _log_runtime_event(
        system_conf, message, user_conf, input_block,
        "assessment_flow_started",
        {
            "intent_result": _compact_intent_result(intent_result),
            "selected_user_source": selected_user_source,
            "issue_tags": intent_issue_tags,
        },
    )
    force_assess_mode = _contains_force_assess_keyword(input_block, FORCE_ASSESS_TEST_KEYWORD)

    log_dir = get_log_dir(system_conf)
    recent_count = count_recent_allowance_requests(
        log_dir=log_dir,
        user_name=str(user_conf.get("name", "")),
        days=30,
    )
    increase_stats = _monthly_increase_stats(
        log_dir=log_dir,
        user_name=str(user_conf.get("name", "")),
        base_dt=datetime.now(JST),
    )
    now_dt = datetime.now(JST)
    current_month_text = f"{now_dt.month}月"
    next_month_num = 1 if now_dt.month == 12 else now_dt.month + 1
    next_month_text = f"{next_month_num}月"
    learning_context = {}
    try:
        learning_context = _build_learning_context_for_prompt(
            user_conf=user_conf,
            system_conf=system_conf,
            audit_state=wallet_service.load_audit_state(),
        )
    except Exception as e:
        _log_runtime_event(
            system_conf, message, user_conf, input_block,
            "learning_context_error",
            {
                "error_type": type(e).__name__,
                "error": _short_log_text(e, limit=300),
            },
        )
    wallet_check_penalty = _pop_wallet_check_penalty(str(user_conf.get("name", "")))
    prompt = build_prompt(
        user_conf=user_conf,
        system_conf=system_conf,
        input_text=input_block,
        recent_request_count=recent_count,
        recent_window_days=30,
        assess_keyword=ASSESS_KEYWORD,
        conversation_history=_recent_conversation_history(log_dir, str(user_conf.get("name", "")), limit=6),
        monthly_total_increase_count=increase_stats["total_increase_count"],
        monthly_total_increase_limit=2,
        last_total=increase_stats["last_total"],
        last_fixed=increase_stats["last_fixed"],
        keyword_hits=_extract_keyword_hits(user_conf, input_block),
        force_assess_test_keyword=FORCE_ASSESS_TEST_KEYWORD,
        is_force_assess_test=force_assess_mode,
        force_requested_fixed_delta=_parse_fixed_delta_request(input_block),
        runtime_now_text=now_dt.strftime("%Y-%m-%d %H:%M:%S %Z"),
        runtime_current_month_text=current_month_text,
        runtime_next_month_text=next_month_text,
        fixed_increase_cap=int(user_conf.get("fixed_increase_cap", 100)),
        months_since_last_fixed_increase=increase_stats["months_since_last_fixed_increase"],
        fixed_increase_count_this_year=increase_stats["fixed_increase_count_this_year"],
        # bot_personality は user_conf から読む（N-4）
        bot_personality=str(user_conf.get("bot_personality") or system_conf.get("bot_personality", "sibling")),
        # 財布チェックペナルティノート: あれば査定に影響させて使用後にクリアする
        wallet_check_penalty=wallet_check_penalty,
        reflection_context=learning_context,
        learning_insights=learning_context,
    )
    try:
        assessment_timeout_reply = (
            f"「{_short_log_text(input_block, limit=40)}」の相談は受け取ったよ。"
            "ごめん、今AIの応答が遅くて査定できなかったよ。少し短くしてもう一度送ってね。"
        )
        reply = await gemini_service.call_with_progress(
            message.channel,
            prompt,
            timeout_reply=assessment_timeout_reply,
        )
    except Exception as e:
        print("Gemini error:", e)
        _log_runtime_event(
            system_conf, message, user_conf, input_block,
            "gemini_assessment_error",
            {
                "error_type": type(e).__name__,
                "error": _short_log_text(e, limit=600),
            },
        )
        await message.channel.send(
            f"「{_short_log_text(input_block, limit=40)}」の相談は受け取ったよ。"
            "ごめん、今AIの応答が不安定で査定できなかったよ。少し短くしてもう一度送ってね。"
        )
        return

    if force_assess_mode and ASSESS_KEYWORD not in (reply or ""):
        reply = f"{ASSESS_KEYWORD}\n{reply}"
    assessed = gemini_service.extract_assessed_amounts(reply)
    if force_assess_mode and assessed is None:
        force_delta = _parse_fixed_delta_request(input_block)
        delta_rule = ""
        if force_delta is not None:
            delta_rule = f"`固定差分` は必ず {force_delta:+d}円 にすること。"
        repair_prompt = (
            "以下の回答を、査定フォーマットに厳密変換してください。"
            f"先頭行は必ず `{ASSESS_KEYWORD}`。"
            "次に【査定結果】で `固定：N円（翌月 N円 / 今月 N円 / 差分）` と `臨時：+N円（理由）` を整数で出すこと。"
            "`合計`は出力しない。"
            f"{delta_rule}"
            "説明文は短くてよい。\n\n"
            f"元の回答:\n{reply}"
        )
        try:
            repaired = await gemini_service.call_with_progress(message.channel, repair_prompt)
            if ASSESS_KEYWORD not in (repaired or ""):
                repaired = f"{ASSESS_KEYWORD}\n{repaired}"
            repaired_assessed = gemini_service.extract_assessed_amounts(repaired)
            if repaired_assessed is not None:
                reply = repaired
                assessed = repaired_assessed
        except Exception:
            pass

    previous_assessed = _latest_assessed_amount(log_dir, str(user_conf.get("name", "")))
    assessed = _normalize_assessed_amounts(user_conf=user_conf, assessed=assessed, previous_assessed=previous_assessed)
    _log_runtime_event(
        system_conf, message, user_conf, input_block,
        "gemini_assessment_result",
        {
            "assessed": assessed,
            "previous_assessed": previous_assessed,
            "reply_excerpt": _short_log_text(reply, limit=1200),
            "force_assess_mode": force_assess_mode,
            "issue_tags": _diagnostic_issue_tags(
                input_block=input_block,
                intent_result=intent_result,
                reply=reply,
                selected_user_source=selected_user_source,
                author_is_parent=is_parent(message.author.id),
            ),
        },
    )

    log_path = log_dir / f"{user_conf['name']}_events.jsonl"
    append_jsonl(
        log_path,
        {
            "ts": now_jst_iso(),
            "discord_user_id": message.author.id,
            "name": user_conf.get("name"),
            "age": user_conf.get("age"),
            "gender": user_conf.get("gender"),
            "input": input_block,
            "reply": reply,
            "assessed": assessed,
        },
    )

    if assessed is not None:
        amount_log_path = log_dir / f"{user_conf['name']}_allowance_amounts.jsonl"
        append_jsonl(
            amount_log_path,
            {
                "ts": now_jst_iso(),
                "discord_user_id": message.author.id,
                "name": user_conf.get("name"),
                "input": input_block,
                "fixed": assessed.get("fixed"),
                "temporary": assessed.get("temporary"),
                "total": assessed.get("total"),
            },
        )
        try:
            sent, reason = await send_assessment_change_notice(
                source_message=message,
                user_conf=user_conf,
                input_text=input_block,
                assessed=assessed,
                previous_assessed=previous_assessed,
            )
            if sent:
                await message.channel.send("査定変更を指定チャンネルに通知したよ。")
            elif reason not in {"変更なし"}:
                await message.channel.send(f"査定変更通知をスキップしたよ（{reason}）。")
        except Exception as e:
            print("assessment change notify error:", e)
            await message.channel.send(f"査定変更通知の送信に失敗したよ。原因: {type(e).__name__}: {e}")
        if assessed.get("total") is not None:
            # tuple で (更新後残高, 達成した目標リスト) が返る
            new_balance, achieved_goals = wallet_service.update_balance(
                user_conf=user_conf,
                system_conf=system_conf,
                delta=int(assessed["total"]),
                action="allowance_grant",
                note="gemini_assessed_total",
                extra={"discord_user_id": int(message.author.id)},
            )
            # 残高低下時は親へアラートを送信する（handlers_child に移管済み）
            await handlers_child.maybe_send_low_balance_alert(user_conf=user_conf, new_balance=new_balance)
            # 達成した目標ごとに祝福メッセージを送信する（複数同時達成もあり得る）
            for achieved_goal in achieved_goals:
                await message.channel.send(
                    _build_goal_achieved_message(user_conf=user_conf, goal=achieved_goal)
                )

    if len(reply) > 1900:
        for i in range(0, len(reply), 1900):
            await message.channel.send(reply[i : i + 1900])
    else:
        await message.channel.send(reply)


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
