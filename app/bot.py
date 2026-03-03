import os
import json
import re

import discord
from datetime import datetime

from app.bot_utils import (
    _build_goal_achieved_message,
    _contains_any_keyword,
    _contains_force_assess_keyword,
    _extract_keyword_hits,
    _latest_assessed_amount,
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
    _usage_guide_text,
    _usage_guide_text_parent,
)
from app.config import (
    find_user_by_discord_id,
    find_user_by_name,
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
    get_wallet_audit_setting,
    load_all_users,
    load_system,
)
from app.gemini_service import GeminiService, count_recent_allowance_requests
from app.message_parser import (
    extract_input_from_mention,
    parse_balance_report,
    parse_usage_report,
    parse_usage_report_flexible,
    parse_proxy_request,
)
from app.prompts import build_prompt
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

# Geminiによる自然語コマンド意図判定プロンプト（案② ハイブリッド方式）
_INTENT_PROMPT_TEMPLATE = """\
以下のユーザーメッセージが次のコマンドのどれに相当するか判定してください。

コマンド一覧:
- dashboard: 全員の残高・状況を確認したい（例:「みんなの状況は？」「全員の残高教えて」）
- analysis_all: 全員の支出傾向を分析したい（例:「みんなの使い方の傾向は？」）
- analysis_user: 特定ユーザーの支出傾向を分析したい（例:「たろうの最近の支出は？」）
- goal_check: 貯金目標の進捗を確認したい（例:「目標どのくらい進んだ？」「いくら貯まった？」）
- goal_set: 貯金目標を設定したい（例:「ゲーム機のために30000円貯めたい」）
- goal_clear: 貯金目標を削除・キャンセルしたい（例:「目標をやめる」「貯金目標取り消して」）
- none: 上記のどれにも当てはまらない（お小遣い相談、雑談など）

メッセージ: {message}

JSON形式のみで回答してください（説明文不要）:
{{"intent": "<コマンド名>", "target_name": "<ユーザー名またはnull>", "goal_title": "<タイトルまたはnull>", "goal_amount": <金額の整数またはnull>}}
"""

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
)


def is_parent(user_id: int) -> bool:
    """ユーザーIDが親（管理者）かどうかを判定する"""
    return user_id in PARENT_IDS

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

async def maybe_handle_help_and_initial_setup(
    message: discord.Message,
    user_conf: dict,
    system_conf: dict,
    input_block: str,
) -> bool:
    user_name = str(user_conf.get("name", ""))
    if not user_name:
        return False

    setup_keywords = ["初期設定", "しょきせってい"]
    usage_keywords = ["使い方", "つかいかた"]
    balance_keywords = ["現在のお小遣い", "おこづかいいくら", "残高確認", "いまの残高", "今の残高"]
    spending_keywords = ["支出記録", "ししゅつきろく", "支出の記録", "ししゅつのきろく","使った記録","つかったきろく"]

    is_setup = _contains_any_keyword(input_block, setup_keywords)
    is_usage = _contains_any_keyword(input_block, usage_keywords)
    is_balance_check = _contains_any_keyword(input_block, balance_keywords)
    is_spending = _contains_any_keyword(input_block, spending_keywords)
    is_pending = _is_initial_setup_pending(user_name)

    if is_setup or is_pending:
        amount = _parse_yen_amount(input_block)
        if amount is None:
            _set_initial_setup_pending(user_name, True)
            await message.channel.send(
                "初期設定をはじめるよ。"
                "\nいまの所持金を `1234円` の形で送ってね。"
            )
            return True

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

    if is_usage:
        # 子供向けガイドを送信する
        await message.channel.send(_usage_guide_text())
        # 親の場合は親専用コマンド一覧も追加で送信する
        if is_parent(message.author.id):
            await message.channel.send(_usage_guide_text_parent())
        return True

    if is_balance_check:
        now_balance = wallet_service.get_balance(user_name)
        await message.channel.send(f"{user_name}さんの現在の所持金は {now_balance}円 だよ。")
        return True

    if is_spending:
        state = wallet_service.load_audit_state()
        pending = state.get("spending_record_pending_by_user", {})
        if not isinstance(pending, dict):
            pending = {}
        pending[user_name] = now_jst_iso()
        state["spending_record_pending_by_user"] = pending
        wallet_service.save_audit_state(state)
        await message.channel.send(
            "支出の記録をするよ。次のどちらかで送ってね。"
            "\n1. `使った物: ノート` `理由: テスト勉強のため` `満足度: 8`"
            "\n2. `ノート / テスト勉強のため / 8`"
        )
        return True

    return False


async def maybe_handle_spending_record_flow(
    message: discord.Message,
    user_conf: dict,
    system_conf: dict,
    input_block: str,
) -> bool:
    user_name = str(user_conf.get("name", ""))
    if not user_name:
        return False

    state = wallet_service.load_audit_state()
    pending = state.get("spending_record_pending_by_user", {})
    if not isinstance(pending, dict):
        pending = {}

    is_pending = user_name in pending
    parsed = parse_usage_report(input_block) or parse_usage_report_flexible(input_block)

    if parsed is None and not is_pending:
        return False

    if parsed is None and is_pending:
        await message.channel.send(
            "支出の記録を続けるよ。"
            "\n`使った物 / 理由 / 満足度(0-10)` の形で送ってね。"
        )
        return True

    reason_words = _rough_word_count(parsed["reason"])
    if reason_words < 3:
        await message.channel.send(
            "理由は3語以上で書いてね。例: `理由: 英語 の テスト 対策`"
        )
        return True

    log_dir = get_log_dir(system_conf)
    journal_path = log_dir / f"{user_conf['name']}_pocket_journal.jsonl"
    # amount フィールドは任意（省略時は None）— 後方互換性あり
    amount = parsed.get("amount")
    record = {
        "ts": now_jst_iso(),
        "discord_user_id": message.author.id,
        "name": user_conf.get("name"),
        "item": parsed["item"],
        "reason": parsed["reason"],
        "reason_word_count": reason_words,
        "satisfaction": parsed["satisfaction"],
        "amount": amount,
    }
    append_jsonl(journal_path, record)
    compare_msg = _self_compare_message(log_dir, str(user_conf.get("name", "")), int(parsed["satisfaction"]))
    pending.pop(user_name, None)
    state["spending_record_pending_by_user"] = pending
    wallet_service.save_audit_state(state)
    # 金額が入力されている場合は記録内容に表示する
    amount_line = f"\n- 金額: {amount:,}円" if amount is not None else ""
    await message.channel.send(
        "システムにお小遣い帳を記録したよ。"
        f"\n- 使った物: {parsed['item']}"
        f"\n- 理由: {parsed['reason']}"
        f"\n- 満足度: {parsed['satisfaction']}/10"
        f"{amount_line}"
        f"\n- 比較: {compare_msg}"
    )
    return True


async def _detect_command_intent(input_block: str) -> dict | None:
    """
    自然語メッセージからコマンド意図をGeminiで判定する（案② ハイブリッド方式）。
    40文字超の場合はお小遣い相談の可能性が高いためスキップする。
    戻り値: {"intent": "...", "target_name": ..., "goal_title": ..., "goal_amount": ...} or None
    """
    # 長文はコマンドではなく相談と判断してAPI呼び出しをスキップする（コスト節約）
    if len(input_block) > 40:
        return None

    prompt = _INTENT_PROMPT_TEMPLATE.format(message=input_block)
    try:
        # call_silent は「考え中...」を表示しない軽量呼び出しである
        raw = await gemini_service.call_silent(prompt)
        # Geminiの返答からJSONブロックだけを正規表現で抽出する
        m = re.search(r'\{[^{}]*\}', raw, re.DOTALL)
        if m:
            result = json.loads(m.group())
            # intent フィールドが存在する場合のみ有効な結果として返す
            if isinstance(result, dict) and result.get("intent"):
                return result
    except Exception as e:
        # intent検知の失敗はボット動作を止めないためログ出力のみとする
        print(f"Intent detection error: {e}")
    return None


async def _handle_detected_intent(
    message: discord.Message,
    user_conf: dict,
    system_conf: dict,
    intent: dict,
) -> bool:
    """Gemini が検出したコマンド意図に応じて適切なハンドラを呼び出す（案②）"""
    intent_name = str(intent.get("intent", "none")).strip()

    # --- dashboard: 全体確認ダッシュボード（親専用）---
    if intent_name == "dashboard":
        # 親以外からのアクセスは無視する
        if not is_parent(message.author.id):
            return False
        # maybe_handle_parent_dashboard と同じロジックでダッシュボードを構築する
        system_conf_l = load_system()
        log_dir = get_log_dir(system_conf_l)
        users = sorted(load_all_users(), key=lambda x: str(x.get("name", "")))
        audit_state = wallet_service.load_audit_state()
        pending_by_user = audit_state.get("pending_by_user", {})
        lines = ["【全体確認ダッシュボード】"]
        for u in users:
            name = str(u.get("name", ""))
            fixed = int(u.get("fixed_allowance", 0))
            balance = wallet_service.get_balance(name)
            report_status = "未報告" if name in pending_by_user else "報告済"
            # 支出記録の末尾レコードから最終支出日を取得する
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

    # --- analysis_all / analysis_user: 支出傾向分析（親専用）---
    if intent_name in ("analysis_all", "analysis_user"):
        if not is_parent(message.author.id):
            return False
        log_dir = get_log_dir(load_system())
        now_dt = datetime.now(JST)
        if intent_name == "analysis_all":
            # 全ユーザー分の分析テキストを結合して返す
            users = sorted(load_all_users(), key=lambda x: str(x.get("name", "")))
            parts = [_spending_analysis_for_user(log_dir, str(u.get("name", "")), now_dt) for u in users]
            reply = "\n\n".join(parts) if parts else "ユーザーが見つからないよ。"
        else:
            # Gemini が抽出した target_name が空の場合はエラーを返す
            target_name = str(intent.get("target_name") or "").strip()
            if not target_name:
                await message.channel.send("分析対象のユーザー名が分からなかったよ。`[名前]の分析` と送ってね。")
                return True
            reply = _spending_analysis_for_user(log_dir, target_name, now_dt)
        # 1900文字を超える場合は分割して送信する
        if len(reply) > 1900:
            for i in range(0, len(reply), 1900):
                await message.channel.send(reply[i: i + 1900])
        else:
            await message.channel.send(reply)
        return True

    # --- goal_check: 目標確認（複数対応）---
    if intent_name == "goal_check":
        user_name = str(user_conf.get("name", ""))
        goals = wallet_service.get_savings_goals(user_name)
        current = wallet_service.get_balance(user_name)
        # 目標未設定の場合は設定方法を案内する
        if not goals:
            await message.channel.send(
                "貯金目標がまだ設定されてないよ。\n`貯金目標 ゲーム機 30000円` の形で設定してね。"
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

    # --- goal_set: 目標追加（Gemini が title と amount を抽出する）---
    if intent_name == "goal_set":
        user_name = str(user_conf.get("name", ""))
        goal_title = str(intent.get("goal_title") or "").strip()
        goal_amount = intent.get("goal_amount")
        # タイトルまたは金額が取得できない場合は再入力を促す
        if not goal_title or not isinstance(goal_amount, (int, float)) or goal_amount <= 0:
            await message.channel.send(
                "目標のタイトルと金額が分からなかったよ。\n`貯金目標 ゲーム機 30000円` の形で送ってね。"
            )
            return True
        target_amount = int(goal_amount)
        success, result = wallet_service.add_savings_goal(user_name, goal_title, target_amount)
        if not success:
            await message.channel.send(result)
            return True
        current = wallet_service.get_balance(user_name)
        bar = _progress_bar(current, target_amount)
        action_word = "更新" if result == "updated" else "追加"
        await message.channel.send(
            f"貯金目標を{action_word}したよ。\n"
            f"・目標: {goal_title} {target_amount:,}円\n"
            f"・現在残高: {current:,}円\n"
            f"・進捗: {bar}"
        )
        return True

    # --- goal_clear: 全目標削除（Gemini検知は曖昧なケース → 全削除が妥当）---
    if intent_name == "goal_clear":
        user_name = str(user_conf.get("name", ""))
        wallet_service.clear_all_savings_goals(user_name)
        await message.channel.send(f"{user_name}の貯金目標を全て削除したよ。")
        return True

    # 上記のいずれにも該当しない intent は処理しない
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

    # 親による全ユーザー一括支給コマンド（「一括支給」）
    if await handlers_parent.maybe_handle_bulk_grant(message, content):
        return

    # 親による全チャンネル一斉アナウンス（「アナウンス [本文]」）
    if await handlers_parent.maybe_handle_parent_announce(message, content):
        return

    mention_input = extract_input_from_mention(content, client.user)
    if mention_input is None:
        if CHAT_SETTING.get("natural_chat_enabled") and not CHAT_SETTING.get("require_mention"):
            mention_input = content
        else:
            return

    system_conf = load_system()
    user_conf = find_user_by_discord_id(message.author.id)
    proxy_name, input_block = parse_proxy_request(mention_input)

    if not input_block:
        await message.channel.send("相談内容を本文に書いて送ってね。")
        return

    if proxy_name:
        if not is_parent(message.author.id):
            await message.channel.send("`代理登録` は親のみ使用できるよ。")
            return
        user_conf = find_user_by_name(proxy_name)
        if user_conf is None:
            await message.channel.send(
                f"`{proxy_name}` はユーザー設定に見つからなかったよ。`settings/users/*.json` の `name` を確認してね。"
            )
            return

    if user_conf is None:
        await message.channel.send("設定にあなたのDiscord IDが登録されてないみたい。親に `settings/users/*.json` を追加してもらってね。")
        return

    if await maybe_handle_help_and_initial_setup(
        message=message,
        user_conf=user_conf,
        system_conf=system_conf,
        input_block=input_block,
    ):
        return

    if await maybe_handle_spending_record_flow(
        message=message,
        user_conf=user_conf,
        system_conf=system_conf,
        input_block=input_block,
    ):
        return

    if await handlers_child.maybe_handle_savings_goal(
        message=message,
        user_conf=user_conf,
        input_block=input_block,
    ):
        return

    if await handlers_child.maybe_handle_child_review(
        message=message,
        user_conf=user_conf,
        system_conf=system_conf,
        input_block=input_block,
    ):
        return

    if await handlers_child.maybe_handle_assessment_history(
        message=message,
        user_conf=user_conf,
        system_conf=system_conf,
        input_block=input_block,
    ):
        return

    # 入出金台帳確認コマンド（「入出金履歴」「台帳確認」「たろうの台帳」）
    if await handlers_child.maybe_handle_ledger_history(
        message=message,
        user_conf=user_conf,
        system_conf=system_conf,
        input_block=input_block,
    ):
        return

    # 子供による手動支出コマンド（「支出 500円 お菓子」）
    if await handlers_child.maybe_handle_manual_expense(
        message=message,
        user_conf=user_conf,
        system_conf=system_conf,
        input_block=input_block,
    ):
        return

    # 子供による臨時入金コマンド（「入金 3000円 お年玉」）
    if await handlers_child.maybe_handle_manual_income(
        message=message,
        user_conf=user_conf,
        system_conf=system_conf,
        input_block=input_block,
    ):
        return

    # 案② ハイブリッドGemini意図判定: ルールベースでマッチしなかった短いメッセージのみ試みる
    detected_intent = await _detect_command_intent(input_block)
    if detected_intent and detected_intent.get("intent") != "none":
        if await _handle_detected_intent(message, user_conf, system_conf, intent=detected_intent):
            return

    reported_balance = parse_balance_report(input_block)
    if reported_balance is not None:
        expected = wallet_service.get_balance(str(user_conf.get("name", "")))
        diff = int(reported_balance) - int(expected)
        state = wallet_service.load_audit_state()
        state["pending_by_user"].pop(str(user_conf.get("name", "")), None)
        wallet_service.save_audit_state(state)

        if diff == 0:
            await message.channel.send(
                f"{user_conf.get('name')}の残高報告を記録したよ。"
                f"\n報告残高: {reported_balance}円 / 帳簿残高: {expected}円（差分0円）"
            )
            return

        penalty = wallet_service.apply_penalty(
            user_conf=user_conf,
            system_conf=system_conf,
            diff=diff,
            wallet_audit_conf=WALLET_AUDIT,
        )
        new_balance = wallet_service.get_balance(str(user_conf.get("name", "")))
        await message.channel.send(
            f"{user_conf.get('name')}の残高差分を検知したよ。"
            f"\n報告残高: {reported_balance}円 / 帳簿残高: {expected}円 / 差分: {diff}円"
            f"\nペナルティ減額: {penalty}円"
            f"\n減額後の帳簿残高: {new_balance}円"
        )
        return

    force_assess_mode = _contains_force_assess_keyword(input_block, FORCE_ASSESS_TEST_KEYWORD)
    await message.channel.send("考え中だよ...")

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
    )
    try:
        reply = await gemini_service.call_with_progress(message.channel, prompt)
    except Exception as e:
        print("Gemini error:", e)
        await message.channel.send(f"ごめん、査定でエラーが出たよ。原因: {type(e).__name__}: {e}")
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


if __name__ == "__main__":
    if not DISCORD_BOT_TOKEN or not GEMINI_API_KEY:
        raise RuntimeError("DISCORD_BOT_TOKEN / GEMINI_API_KEY が未設定")
    client.run(DISCORD_BOT_TOKEN)
