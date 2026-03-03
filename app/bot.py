import os
import json
import re

import discord
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
    _load_jsonl,
    _monthly_increase_stats,
    _normalize_assessed_amounts,
    _normalize_japanese_command,
    _normalize_keyword,
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
    update_user_field,
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

async def maybe_handle_parent_broadcast_guide(message: discord.Message, content: str) -> bool:
    if not is_parent(message.author.id):
        return False

    mention_body = extract_input_from_mention((content or "").strip(), client.user)
    body = mention_body if mention_body is not None else (content or "")
    normalized = _normalize_japanese_command(body)
    is_cmd = (
        "使い方の説明と初期設定" in normalized
        or "つかいかたのせつめいとしょきせってい" in normalized
    )
    if not is_cmd:
        return False

    channel_ids = get_allow_channel_ids()
    if not channel_ids:
        await message.channel.send(
            "`settings/setting.json` の `allow_channel_ids` が未設定なので一斉通知できないよ。"
        )
        return True

    sent = 0
    failed: list[str] = []
    text = _usage_guide_text()
    for cid in sorted(channel_ids):
        try:
            channel = client.get_channel(int(cid))
            if channel is None:
                channel = await client.fetch_channel(int(cid))
            await channel.send(text)
            sent += 1
        except Exception as e:
            failed.append(f"{cid}({type(e).__name__})")

    msg = f"使い方と初期設定のアナウンスを {sent}/{len(channel_ids)} チャネルに送信したよ。"
    if failed:
        msg += f"\n送信失敗: {', '.join(str(x) for x in failed)}"
    await message.channel.send(msg)
    return True

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
    record = {
        "ts": now_jst_iso(),
        "discord_user_id": message.author.id,
        "name": user_conf.get("name"),
        "item": parsed["item"],
        "reason": parsed["reason"],
        "reason_word_count": reason_words,
        "satisfaction": parsed["satisfaction"],
    }
    append_jsonl(journal_path, record)
    compare_msg = _self_compare_message(log_dir, str(user_conf.get("name", "")), int(parsed["satisfaction"]))
    pending.pop(user_name, None)
    state["spending_record_pending_by_user"] = pending
    wallet_service.save_audit_state(state)
    await message.channel.send(
        "システムにお小遣い帳を記録したよ。"
        f"\n- 使った物: {parsed['item']}"
        f"\n- 理由: {parsed['reason']}"
        f"\n- 満足度: {parsed['satisfaction']}/10"
        f"\n- 比較: {compare_msg}"
    )
    return True

async def maybe_handle_savings_goal(
    message: discord.Message,
    user_conf: dict,
    input_block: str,
) -> bool:
    """貯金目標の追加・確認・削除コマンドを処理する（Feature 5 複数目標対応版）"""
    user_name = str(user_conf.get("name", ""))
    # ユーザー名が取得できない場合は処理不可のためスキップする
    if not user_name:
        return False

    # 「貯金目標 タイトル 金額円」パターン — 追加または同名タイトルの金額更新
    set_match = re.match(r"^貯金目標\s+(.+?)\s+(\d[\d,]*)\s*円?$", input_block.strip())
    # 「目標確認」パターン — 全目標をリスト表示する
    is_check = _contains_any_keyword(input_block, ["目標確認", "もくひょうかくにん"])
    # 「目標全削除」パターン — 全目標を一括削除する
    is_clear_all = _contains_any_keyword(input_block, ["目標全削除", "もくひょうぜんさくじょ"])
    # 「目標削除 タイトル」パターン — タイトル指定で1件削除する
    clear_title_match = re.match(r"^目標削除\s+(.+)$", input_block.strip())
    # 「目標削除」のみ（引数なし）— どれを削除するか案内する
    is_clear_bare = bool(re.match(r"^目標削除\s*$", input_block.strip()))

    # いずれにも該当しなければ次のハンドラへ委譲する
    if not set_match and not is_check and not is_clear_all and not clear_title_match and not is_clear_bare:
        return False

    current = wallet_service.get_balance(user_name)

    # 目標追加・更新: add_savings_goal は同名なら金額更新、新規なら追加する
    if set_match:
        title = set_match.group(1).strip()
        target_amount = int(set_match.group(2).replace(",", ""))
        success, result = wallet_service.add_savings_goal(user_name, title, target_amount)
        # 上限超過の場合は result にエラーメッセージが入っている
        if not success:
            await message.channel.send(result)
            return True
        bar = _progress_bar(current, target_amount)
        action_word = "更新" if result == "updated" else "追加"
        await message.channel.send(
            f"貯金目標を{action_word}したよ。\n"
            f"・目標: {title} {target_amount:,}円\n"
            f"・現在残高: {current:,}円\n"
            f"・進捗: {bar}"
        )
        return True

    # 全削除
    if is_clear_all:
        wallet_service.clear_all_savings_goals(user_name)
        await message.channel.send(f"{user_name}の貯金目標を全て削除したよ。")
        return True

    # タイトル指定削除
    if clear_title_match:
        title = clear_title_match.group(1).strip()
        found = wallet_service.remove_savings_goal(user_name, title)
        if found:
            await message.channel.send(f"目標「{title}」を削除したよ。")
        else:
            await message.channel.send(f"目標「{title}」は見つからなかったよ。")
        return True

    # 引数なし削除 — 一覧を見せてタイトル指定を促す
    if is_clear_bare:
        goals = wallet_service.get_savings_goals(user_name)
        if not goals:
            await message.channel.send("削除する目標がないよ。")
        else:
            goal_list = "\n".join(f"・{g['title']}" for g in goals)
            await message.channel.send(
                "どの目標を削除する？\n"
                f"{goal_list}\n"
                "「目標削除 タイトル」で削除できるよ。全部消す場合は「目標全削除」ね。"
            )
        return True

    # 目標確認: 全目標をプログレスバー付きで一覧表示する
    if is_check:
        goals = wallet_service.get_savings_goals(user_name)
        if not goals:
            await message.channel.send(
                "貯金目標がまだ設定されてないよ。\n"
                "`貯金目標 ゲーム機 30000円` の形で設定してね。"
            )
            return True
        lines = [f"【{user_name}の貯金目標（残高: {current:,}円）】"]
        for g in goals:
            title = str(g.get("title", ""))
            target_amount = int(g.get("target_amount", 0))
            bar = _progress_bar(current, target_amount)
            # 残り金額が負にならないよう 0 でクランプする
            remaining = max(target_amount - current, 0)
            lines.append(
                f"\n・{title}: {target_amount:,}円\n"
                f"  進捗: {bar}\n"
                f"  あと: {remaining:,}円"
            )
        await message.channel.send("\n".join(lines))
        return True

    return False


async def maybe_handle_child_review(
    message: discord.Message,
    user_conf: dict,
    system_conf: dict,
    input_block: str,
) -> bool:
    """子供向け今月の振り返りコマンドを処理する（Feature 6）。
    「振り返り」「今月の振り返り」などで起動し、当月の支出記録サマリーを返す。"""
    # 対応キーワードを列挙する（ひらがな表記も受け付ける）
    review_keywords = ["振り返り", "ふりかえり", "今月の振り返り", "こんげつのふりかえり"]
    if not _contains_any_keyword(input_block, review_keywords):
        return False

    user_name = str(user_conf.get("name", ""))
    if not user_name:
        return False

    now = datetime.now(JST)
    log_dir = get_log_dir(system_conf)
    # 当月の pocket_journal を読み込んでフィルタリングする
    journal_path = log_dir / f"{user_name}_pocket_journal.jsonl"
    all_rows = _load_jsonl(journal_path)
    month_rows = [
        r for r in all_rows
        if _is_same_month(r.get("ts"), now.year, now.month)
    ]
    # ウォレット残高を取得して振り返りメッセージに添える
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


async def maybe_handle_assessment_history(
    message: discord.Message,
    user_conf: dict,
    system_conf: dict,
    input_block: str,
) -> bool:
    """査定履歴確認コマンドを処理する（Tier 2-E）。
    「査定履歴」で直近5件の査定金額（固定・臨時・合計）を一覧表示する。"""
    history_keywords = ["査定履歴", "さていれきし"]
    if not _contains_any_keyword(input_block, history_keywords):
        return False

    user_name = str(user_conf.get("name", ""))
    if not user_name:
        return False

    log_dir = get_log_dir(system_conf)
    # allowance_amounts.jsonl から全件読み込んで末尾5件を取得する
    amounts_path = log_dir / f"{user_name}_allowance_amounts.jsonl"
    all_rows = _load_jsonl(amounts_path)
    # 新しい順に並べるため末尾から取得し、表示は新→旧の順にする
    recent = list(reversed(all_rows[-5:]))
    msg = _assessment_history_message(user_conf=user_conf, rows=recent)
    await message.channel.send(msg)
    return True


async def maybe_handle_parent_dashboard(message: discord.Message, content: str) -> bool:
    """親向けダッシュボード: 全ユーザーの残高・状況を一覧表示する（Feature 1）"""
    # 親以外はこのコマンドを使えない
    if not is_parent(message.author.id):
        return False

    # メンション部分を除去してコマンド本文を取得する
    mention_body = extract_input_from_mention((content or "").strip(), client.user)
    body = mention_body if mention_body is not None else (content or "")
    normalized = _normalize_japanese_command(body)
    # 「全体確認」「ぜんたいかくにん」のどちらでも反応する
    if "全体確認" not in normalized and "ぜんたいかくにん" not in normalized:
        return False

    # ユーザー一覧・残高監査状態・ログディレクトリを取得する
    system_conf = load_system()
    log_dir = get_log_dir(system_conf)
    # ユーザーを名前順にソートして表示順を安定させる
    users = sorted(load_all_users(), key=lambda x: str(x.get("name", "")))
    audit_state = wallet_service.load_audit_state()
    # pending_by_user に名前があれば残高報告が未完了である
    pending_by_user = audit_state.get("pending_by_user", {})

    lines = ["【全体確認ダッシュボード】"]
    for u in users:
        name = str(u.get("name", ""))
        fixed = int(u.get("fixed_allowance", 0))
        balance = wallet_service.get_balance(name)
        # 監査の pending 状態で報告済/未報告を判定する
        report_status = "未報告" if name in pending_by_user else "報告済"

        # 支出記録JSONL の末尾レコードから最終支出日を取得する
        journal_path = log_dir / f"{name}_pocket_journal.jsonl"
        journal_rows = _load_jsonl(journal_path)
        last_spending_date = "なし"
        if journal_rows:
            last_ts = journal_rows[-1].get("ts")
            if last_ts:
                try:
                    dt = datetime.fromisoformat(str(last_ts))
                    # 月/日の形式で表示する（年は省略）
                    last_spending_date = dt.strftime("%m/%d")
                except Exception:
                    pass

        lines.append(
            f"・{name}: 固定{fixed}円 / 残高{balance}円 / 残高報告:{report_status} / 最終支出:{last_spending_date}"
        )

    await message.channel.send("\n".join(lines))
    return True


async def maybe_handle_spending_analysis(message: discord.Message, content: str) -> bool:
    """支出傾向分析コマンドを処理する: 「[name]の分析」または「全員の分析」（Feature 4、親専用）"""
    # 親以外のアクセスは無視する
    if not is_parent(message.author.id):
        return False

    # メンション除去後の本文を取得する
    mention_body = extract_input_from_mention((content or "").strip(), client.user)
    body = mention_body if mention_body is not None else (content or "")
    body_stripped = body.strip()

    # 「全員の分析」パターン — 正規化後の表記でも反応する
    all_match = "全員の分析" in body_stripped or "ぜんいんのぶんせき" in _normalize_japanese_command(body_stripped)
    # 「[name]の分析」パターン — 名前部分を正規表現で抽出する
    name_match = re.search(r"(.+)の分析", body_stripped)

    # いずれのパターンにも該当しない場合は次のハンドラへ委譲する
    if not all_match and not name_match:
        return False

    system_conf = load_system()
    log_dir = get_log_dir(system_conf)
    # 過去3ヶ月の集計基準日として現在時刻を使用する
    now_dt = datetime.now(JST)

    if all_match:
        # 全ユーザーの分析テキストをまとめて生成する
        users = sorted(load_all_users(), key=lambda x: str(x.get("name", "")))
        parts = [_spending_analysis_for_user(log_dir, str(u.get("name", "")), now_dt) for u in users]
        reply = "\n\n".join(parts) if parts else "ユーザーが見つからないよ。"
    else:
        # 特定ユーザー名を正規表現グループから取得する
        target_name = name_match.group(1).strip()
        reply = _spending_analysis_for_user(log_dir, target_name, now_dt)

    # Discord の1メッセージ文字数上限（2000文字）を超えないよう 1900 文字で分割する
    if len(reply) > 1900:
        for i in range(0, len(reply), 1900):
            await message.channel.send(reply[i: i + 1900])
    else:
        await message.channel.send(reply)
    return True


async def maybe_handle_wallet_audit_send(message: discord.Message, content: str) -> bool:
    mention_body = extract_input_from_mention((content or "").strip(), client.user)
    if not (mention_body and mention_body.strip() in {"残高チェック送信", "月頭案内送信"}):
        return False

    if not is_parent(message.author.id):
        await message.channel.send("残高チェック送信は親のみ実行できるよ。")
        return True

    try:
        await reminder_service.send_wallet_audit()
        await message.channel.send("残高チェック案内を送信したよ。")
    except Exception as e:
        await message.channel.send(f"送信に失敗したよ。原因: {type(e).__name__}: {e}")
    return True


async def maybe_handle_reminder_test(message: discord.Message, content: str) -> bool:
    mention_body = extract_input_from_mention((content or "").strip(), client.user)
    is_test_cmd = bool(
        mention_body
        and mention_body.strip().lower()
        in {"reminder test", "reminder-test", "リマインダーテスト", "リマインダー テスト"}
    )
    if not is_test_cmd:
        return False

    if not is_parent(message.author.id):
        await message.channel.send("リマインダーテストは親のみ実行できるよ。")
        return True

    channel_id = ALLOWANCE_REMINDER.get("channel_id")
    if not channel_id:
        await message.channel.send("`settings/setting.json` の `allowance_reminder.channel_id` を設定してね。")
        return True

    payday = reminder_service.next_payday(today=datetime.now(JST).date(), payday_day=int(ALLOWANCE_REMINDER["payday_day"]))
    try:
        await reminder_service.send_allowance_reminder(payday=payday, channel_id=int(channel_id), is_test=True)
        await message.channel.send("リマインダーをテスト送信したよ。")
    except Exception as e:
        await message.channel.send(f"テスト送信に失敗したよ。原因: {type(e).__name__}: {e}")
    return True

async def maybe_handle_manual_grant(message: discord.Message, content: str) -> bool:
    """親が手動でお小遣いを支給するコマンドを処理する（親のみ）。
    「支給 たろう 700円」の形式にマッチする。"""
    # 親以外は無視する
    if not is_parent(message.author.id):
        return False
    # 先頭に「支給」を含む形式にマッチさせる（ユーザー名と金額が必須）
    m = re.search(r"支給\s+(\S+)\s+(\d[\d,]*)\s*円", (content or "").strip())
    if not m:
        return False

    target_name = m.group(1)
    amount = int(m.group(2).replace(",", ""))

    # 対象ユーザーを名前で検索する
    target_conf = find_user_by_name(target_name)
    if target_conf is None:
        await message.channel.send(f"`{target_name}` はユーザー設定に見つからなかったよ。")
        return True

    system_conf = load_system()
    before = wallet_service.get_balance(target_name)
    # allowance_grant（Gemini査定自動付与）と区別するため別のアクション名にする
    new_balance, achieved_goals = wallet_service.update_balance(
        user_conf=target_conf,
        system_conf=system_conf,
        delta=amount,
        action="allowance_manual_grant",
        note="manual_grant_by_parent",
        extra={"granted_by": str(message.author.id)},
    )
    await message.channel.send(
        f"{target_name}に支給したよ。"
        f"\n- 金額: {amount}円"
        f"\n残高: {before}円 → {new_balance}円"
    )
    # 支給により目標が達成された場合は祝福メッセージを送る
    for achieved_goal in achieved_goals:
        await message.channel.send(
            _build_goal_achieved_message(user_conf=target_conf, goal=achieved_goal)
        )
    return True


async def maybe_handle_balance_adjustment(message: discord.Message, content: str) -> bool:
    """親が残高を直接調整するコマンドを処理する（親のみ）。
    「残高調整 たろう +500円」「残高調整 たろう -300円」「残高調整 たろう 500円」の形式にマッチする。"""
    # 親以外は無視する
    if not is_parent(message.author.id):
        return False
    # 「残高調整」＋ユーザー名＋符号あり/なし金額の形式にマッチさせる
    m = re.search(r"残高調整\s+(\S+)\s+([+-]?\d[\d,]*)\s*円", (content or "").strip())
    if not m:
        return False

    target_name = m.group(1)
    # 符号なしの場合は加算（正）として扱う
    delta = int(m.group(2).replace(",", ""))

    target_conf = find_user_by_name(target_name)
    if target_conf is None:
        await message.channel.send(f"`{target_name}` はユーザー設定に見つからなかったよ。")
        return True

    system_conf = load_system()
    before = wallet_service.get_balance(target_name)
    new_balance, achieved_goals = wallet_service.update_balance(
        user_conf=target_conf,
        system_conf=system_conf,
        delta=delta,
        action="balance_adjustment",
        note="manual_adjustment_by_parent",
        extra={"adjusted_by": str(message.author.id)},
    )
    direction = "加算" if delta >= 0 else "減算"
    await message.channel.send(
        f"{target_name}の残高を調整したよ。"
        f"\n- {direction}: {abs(delta)}円（{delta:+d}円）"
        f"\n残高: {before}円 → {new_balance}円"
    )
    # 加算調整で目標が達成された場合は祝福メッセージを送る
    for achieved_goal in achieved_goals:
        await message.channel.send(
            _build_goal_achieved_message(user_conf=target_conf, goal=achieved_goal)
        )
    return True


async def maybe_handle_user_setting_change(message: discord.Message, content: str) -> bool:
    """親がユーザーの固定お小遣い・臨時上限を変更するコマンドを処理する（親のみ）。
    「設定変更 たろう 固定 800円」「設定変更 たろう 臨時 5000円」の形式にマッチする。"""
    # 親以外は無視する
    if not is_parent(message.author.id):
        return False
    # 「設定変更 ユーザー名 固定/臨時 金額円」の形式にマッチさせる
    m = re.search(r"設定変更\s+(\S+)\s+(固定|臨時)\s+(\d[\d,]*)\s*円", (content or "").strip())
    if not m:
        return False

    target_name = m.group(1)
    setting_type = m.group(2)  # "固定" または "臨時"
    amount = int(m.group(3).replace(",", ""))

    # 対象ユーザーを名前で検索する
    target_conf = find_user_by_name(target_name)
    if target_conf is None:
        await message.channel.send(f"`{target_name}` はユーザー設定に見つからなかったよ。")
        return True

    # 変更対象フィールドと表示ラベルを決定する
    if setting_type == "固定":
        field = "fixed_allowance"
        label = "固定お小遣い"
    else:
        field = "temporary_max"
        label = "臨時お小遣い上限"

    old_value = int(target_conf.get(field, 0))

    # users/*.json ファイルの対象フィールドを書き換える
    if not update_user_field(target_name, field, amount):
        await message.channel.send(f"{target_name}の設定ファイルの更新に失敗したよ。")
        return True

    await message.channel.send(
        f"{target_name}の{label}を変更したよ。"
        f"\n{old_value}円 → {amount}円"
    )
    return True


async def maybe_handle_manual_expense(
    message: discord.Message,
    user_conf: dict,
    system_conf: dict,
    input_block: str,
) -> bool:
    """子供（または親の代理）が手動で支出を記録するコマンドを処理する。
    「支出 500円 お菓子」の形式にマッチする。残高から差し引く。"""
    # 「支出 金額円 メモ」の形式にマッチさせる（メモは省略可）
    m = re.match(r"^支出\s+(\d[\d,]*)\s*円\s*(.*)?$", (input_block or "").strip())
    if not m:
        return False

    amount = int(m.group(1).replace(",", ""))
    note = (m.group(2) or "").strip()
    user_name = str(user_conf.get("name", ""))
    before = wallet_service.get_balance(user_name)

    # delta を負にして残高を減算する
    new_balance, _ = wallet_service.update_balance(
        user_conf=user_conf,
        system_conf=system_conf,
        delta=-amount,
        action="manual_expense",
        note=note,
    )
    await message.channel.send(
        f"支出を記録したよ。"
        f"\n- 金額: {amount}円"
        f"\n- メモ: {note if note else 'なし'}"
        f"\n残高: {before}円 → {new_balance}円"
    )
    # 支出後に残高が低下した場合のアラートチェックをする
    await _maybe_send_low_balance_alert(user_conf=user_conf, new_balance=new_balance)
    return True


async def maybe_handle_manual_income(
    message: discord.Message,
    user_conf: dict,
    system_conf: dict,
    input_block: str,
) -> bool:
    """子供（または親の代理）が臨時収入を記録するコマンドを処理する。
    「入金 3000円 お年玉」の形式にマッチする。残高に加算する。"""
    # 「入金 金額円 メモ」の形式にマッチさせる（メモは省略可）
    m = re.match(r"^入金\s+(\d[\d,]*)\s*円\s*(.*)?$", (input_block or "").strip())
    if not m:
        return False

    amount = int(m.group(1).replace(",", ""))
    note = (m.group(2) or "").strip()
    user_name = str(user_conf.get("name", ""))
    before = wallet_service.get_balance(user_name)

    # delta を正にして残高を加算する
    new_balance, achieved_goals = wallet_service.update_balance(
        user_conf=user_conf,
        system_conf=system_conf,
        delta=amount,
        action="manual_income",
        note=note,
    )
    await message.channel.send(
        f"入金を記録したよ。"
        f"\n- 金額: {amount}円"
        f"\n- メモ: {note if note else 'なし'}"
        f"\n残高: {before}円 → {new_balance}円"
    )
    # 入金により目標が達成された場合は祝福メッセージを送る
    for achieved_goal in achieved_goals:
        await message.channel.send(
            _build_goal_achieved_message(user_conf=user_conf, goal=achieved_goal)
        )
    return True


async def _maybe_send_low_balance_alert(user_conf: dict, new_balance: int) -> None:
    """残高が閾値を下回ったとき親チャンネルへアラートを送信する（Feature 2）"""
    cfg = LOW_BALANCE_ALERT
    # 機能が無効化されていれば何もしない
    if not cfg.get("enabled"):
        return
    channel_id = cfg.get("channel_id")
    # 送信先チャンネルが未設定の場合はスキップする
    if not channel_id:
        return
    threshold = int(cfg.get("threshold", 500))
    # 新残高が閾値以上であればアラート不要
    if new_balance >= threshold:
        return

    name = str(user_conf.get("name", ""))
    try:
        # キャッシュにチャンネルがない場合は API で取得する
        channel = client.get_channel(int(channel_id))
        if channel is None:
            channel = await client.fetch_channel(int(channel_id))
        await channel.send(
            f"【低残高アラート】{name}さんの残高が{new_balance}円になりました（閾値:{threshold}円）。"
        )
    except Exception as e:
        # アラート失敗はボット動作を止めないためログ出力のみとする
        print(f"Low balance alert error: {e}")


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
    reminder_service.start_loop_if_needed()
    print("Allowance reminder loop started")


@client.event
async def on_message(message: discord.Message):
    if message.author.bot:
        return

    content = (message.content or "").strip()
    if await maybe_handle_parent_broadcast_guide(message, content):
        return

    if ALLOW_CHANNEL_IDS is not None and message.channel.id not in ALLOW_CHANNEL_IDS:
        return

    if content.startswith("[#SH-"):
        await message.channel.send("`[#SH-xxx]`形式は非対応です。`@compass-bot 内容` で送ってね。")
        return

    if await maybe_handle_parent_dashboard(message, content):
        return

    if await maybe_handle_spending_analysis(message, content):
        return

    if await maybe_handle_wallet_audit_send(message, content):
        return

    if await maybe_handle_reminder_test(message, content):
        return

    # 親による支給コマンド（「支給 たろう 700円」）
    if await maybe_handle_manual_grant(message, content):
        return

    # 親による残高調整コマンド（「残高調整 たろう +500円」）
    if await maybe_handle_balance_adjustment(message, content):
        return

    # 親による設定変更コマンド（「設定変更 たろう 固定 800円」）
    if await maybe_handle_user_setting_change(message, content):
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

    if await maybe_handle_savings_goal(
        message=message,
        user_conf=user_conf,
        input_block=input_block,
    ):
        return

    if await maybe_handle_child_review(
        message=message,
        user_conf=user_conf,
        system_conf=system_conf,
        input_block=input_block,
    ):
        return

    if await maybe_handle_assessment_history(
        message=message,
        user_conf=user_conf,
        system_conf=system_conf,
        input_block=input_block,
    ):
        return

    # 子供による手動支出コマンド（「支出 500円 お菓子」）
    if await maybe_handle_manual_expense(
        message=message,
        user_conf=user_conf,
        system_conf=system_conf,
        input_block=input_block,
    ):
        return

    # 子供による臨時入金コマンド（「入金 3000円 お年玉」）
    if await maybe_handle_manual_income(
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
            # tuple で (更新後残高, 達成した目標 or None) が返る
            new_balance, achieved_goals = wallet_service.update_balance(
                user_conf=user_conf,
                system_conf=system_conf,
                delta=int(assessed["total"]),
                action="allowance_grant",
                note="gemini_assessed_total",
                extra={"discord_user_id": int(message.author.id)},
            )
            await _maybe_send_low_balance_alert(user_conf=user_conf, new_balance=new_balance)
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
