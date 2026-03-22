import asyncio
import os
import json
import re

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
    amount = _parse_yen_amount(input_block)
    if amount is None:
        await message.channel.send(
            "初期設定を続けるよ。\nいまの所持金を `1234円` の形で送ってね。"
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


# ------------------------------------------------------------------
# pending_intent 状態管理（confidence:low 確認フロー用）
# ------------------------------------------------------------------

def _get_pending_intent(user_name: str) -> dict | None:
    """pending_intent 状態を取得する。なければ None を返す"""
    state = wallet_service.load_audit_state()
    pending = state.get("pending_intent_by_user", {})
    return pending.get(user_name) if isinstance(pending, dict) else None


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
        balance = wallet_service.get_balance(user_name)
        await message.channel.send(f"{user_name}さんの現在の所持金は {balance}円 だよ。")
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
            adapted = await gemini_service.call_silent(rewrite_prompt)
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
        # AI が金額を entities に持っている場合は即設定、なければフロー開始
        amount = entities.get("amount")
        if amount is None:
            amount = _parse_yen_amount(input_block)
        if amount is None:
            # 金額不明 → pending 状態にして入力を促す
            _set_initial_setup_pending(user_name, True)
            await message.channel.send(
                "初期設定をはじめるよ。\nいまの所持金を `1234円` の形で送ってね。"
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

    # --- 支出記録フロー開始（トリガー）---
    if intent == "spending_record":
        # pending 状態を立てて入力フォーマットを案内する
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

    # --- 手動支出（金額明示の即時記録）---
    if intent == "manual_expense":
        amount = entities.get("amount")
        item = str(entities.get("item") or "").strip()
        # entities に金額がない場合は input から regex で再試行する
        if amount is None:
            m = re.match(r"(\d[\d,]*)\s*円", input_block)
            if m:
                amount = int(m.group(1).replace(",", ""))
        if not amount:
            await message.channel.send(
                "金額が読み取れなかったよ。「支出 500円 お菓子」の形で送ってね。"
            )
            return True
        before = wallet_service.get_balance(user_name)
        new_balance, _ = wallet_service.update_balance(
            user_conf=user_conf,
            system_conf=system_conf,
            delta=-int(amount),
            action="manual_expense",
            note=item,
        )
        # pocket_journal にも記録して振り返り・分析で金額が見えるようにする
        log_dir = get_log_dir(system_conf)
        journal_path = log_dir / f"{user_name}_pocket_journal.jsonl"
        append_jsonl(journal_path, {
            "ts": now_jst_iso(),
            "discord_user_id": message.author.id,
            "name": user_name,
            "item": item if item else "支出",
            "reason": "手動支出",
            "reason_word_count": 0,
            "satisfaction": None,
            "amount": int(amount),
        })
        await message.channel.send(
            f"支出を記録したよ。"
            f"\n- 金額: {amount}円"
            f"\n- メモ: {item if item else 'なし'}"
            f"\n残高: {before}円 → {new_balance}円"
        )
        # 残高低下時は親へアラートを送信する
        await handlers_child.maybe_send_low_balance_alert(user_conf=user_conf, new_balance=new_balance)
        return True

    # --- 臨時入金 ---
    if intent == "manual_income":
        amount = entities.get("amount")
        # item・reason どちらかをメモとして使う
        item = str(entities.get("item") or entities.get("reason") or "").strip()
        if amount is None:
            m = re.match(r"(\d[\d,]*)\s*円", input_block)
            if m:
                amount = int(m.group(1).replace(",", ""))
        if not amount:
            await message.channel.send(
                "金額が読み取れなかったよ。「入金 3000円 お年玉」の形で送ってね。"
            )
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

    # --- 残高報告（照合）---
    if intent == "balance_report":
        # AI が抽出した amount を優先し、なければ regex で parse する
        reported = entities.get("amount")
        if reported is None:
            reported = parse_balance_report(input_block)
        if reported is None:
            # 金額が取れない場合は査定フローへ落とす
            return False
        expected = wallet_service.get_balance(user_name)
        diff = int(reported) - int(expected)
        state = wallet_service.load_audit_state()
        state["pending_by_user"].pop(user_name, None)
        wallet_service.save_audit_state(state)
        if diff == 0:
            await message.channel.send(
                f"{user_name}の残高報告を記録したよ。"
                f"\n報告残高: {reported}円 / 帳簿残高: {expected}円（差分0円）"
            )
            return True
        # 差分がある場合はペナルティを適用する
        penalty = wallet_service.apply_penalty(
            user_conf=user_conf,
            system_conf=system_conf,
            diff=diff,
            wallet_audit_conf=WALLET_AUDIT,
        )
        new_balance = wallet_service.get_balance(user_name)
        await message.channel.send(
            f"{user_name}の残高差分を検知したよ。"
            f"\n報告残高: {reported}円 / 帳簿残高: {expected}円 / 差分: {diff}円"
            f"\nペナルティ減額: {penalty}円"
            f"\n減額後の帳簿残高: {new_balance}円"
        )
        return True

    # --- 貯金目標 確認 ---
    if intent == "goal_check":
        goals = wallet_service.get_savings_goals(user_name)
        current = wallet_service.get_balance(user_name)
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

    # --- 貯金目標 設定 ---
    if intent == "goal_set":
        goal_title = str(entities.get("goal_title") or "").strip()
        goal_amount = entities.get("amount")
        # タイトルまたは金額が取れない場合は再入力を促す
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
            # タイトル不明 → 全削除
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
            return False
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
            return False
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
        personality = str(entities.get("personality") or "sibling").strip()
        # 有効な personality 値にフォールバックする
        valid = {"parent", "sibling", "friend", "teacher"}
        if personality not in valid:
            personality = "sibling"
        ok = update_user_field(user_name, "bot_personality", personality)
        labels = {
            "parent": "親っぽい口調",
            "sibling": "兄姉っぽい口調",
            "friend": "友達口調",
            "teacher": "先生口調",
        }
        label = labels.get(personality, personality)
        if ok:
            await message.channel.send(f"話し方を「{label}」に変えたよ。")
        else:
            await message.channel.send("設定の変更に失敗したよ。")
        return True

    # none / allowance_request は査定フローへ落とす
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

    # --- B案フロー: pending 状態の処理（AI正規化をスキップして直接処理する）---

    # 初期設定フローの pending 状態を処理する（金額待ち）
    if await _handle_initial_setup_pending(
        message=message,
        user_conf=user_conf,
        system_conf=system_conf,
        input_block=input_block,
    ):
        return

    # 支出記録フロー（pending 状態 + 直接フォーマット入力をAI前にキャッチする）
    if await maybe_handle_spending_record_flow(
        message=message,
        user_conf=user_conf,
        system_conf=system_conf,
        input_block=input_block,
    ):
        return

    # pending_intent の確認返答を処理する（confidence:low 確認フロー）
    if await _handle_pending_intent_reply(
        message=message,
        user_conf=user_conf,
        system_conf=system_conf,
        input_block=input_block,
    ):
        return

    # --- B案: AI正規化 → ディスパッチャー ---

    # Gemini 軽量モデルで intent + entities + confidence を取得する
    intent_result = await intent_normalizer.normalize_intent(input_block, gemini_service)

    # 低信頼度の場合は確認メッセージを送って終了する（確認は1回のみ）
    if intent_result.get("confidence") == "low":
        await _ask_intent_confirmation(message, user_conf, intent_result, input_block)
        return

    # ディスパッチャー（all child コマンドを intent ベースで処理する）
    if await _dispatch_by_intent(message, user_conf, system_conf, input_block, intent_result):
        return

    # none / allowance_request の場合は査定フローへ落ちる
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
        # bot_personality は user_conf から読む（N-4）
        bot_personality=str(user_conf.get("bot_personality", "sibling")),
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
