"""
handlers_parent.py — 親専用コマンドハンドラ群

bot.py の肥大化防止のために分離。グローバル状態は init() で注入する。
"""

import re
from datetime import datetime

import discord

from app.bot_utils import (
    _build_goal_achieved_message,
    _load_jsonl,
    _normalize_japanese_command,
    _spending_analysis_for_user,
    _usage_guide_text,
)
from app.config import (
    find_user_by_name,
    get_allow_channel_ids,
    get_parent_ids,
    load_all_users,
    load_system,
    get_log_dir,
    update_user_field,
)
from app.error_messages import operation_failure_message
from app.message_parser import extract_input_from_mention
from app.storage import JST, append_jsonl, now_jst_iso

# モジュールレベルの依存オブジェクト — init() で bot.py から注入する
_wallet_service = None
_client = None
_reminder_service = None
_allowance_reminder_conf: dict = {}

_FOLLOW_POLICY_DEFAULT = {
    "enabled": True,
    "focus_area": "balanced",
    "nudge_strength": "light",
    "frequency": "low",
    "parent_note": "",
}

_FOLLOW_POLICY_FOCUS_LABELS = {
    "balanced": "バランス",
    "satisfaction_reflection": "満足度の振り返り",
    "impulse_spending": "買う前チェック",
    "saving_goal": "貯金目標",
    "record_habit": "記録習慣",
    "income_balance": "収入と支出のバランス",
}

_FOLLOW_POLICY_FOCUS_ALIASES = {
    "バランス": "balanced",
    "全体": "balanced",
    "満足度": "satisfaction_reflection",
    "振り返り": "satisfaction_reflection",
    "ふりかえり": "satisfaction_reflection",
    "衝動": "impulse_spending",
    "買う前": "impulse_spending",
    "一度待つ": "impulse_spending",
    "使いすぎ": "impulse_spending",
    "貯金": "saving_goal",
    "目標": "saving_goal",
    "記録": "record_habit",
    "記録習慣": "record_habit",
    "収入": "income_balance",
    "お小遣い増": "income_balance",
    "行動プラン": "income_balance",
}

_FOLLOW_POLICY_STRENGTH_ALIASES = {
    "軽め": "light",
    "やさしく": "light",
    "弱め": "light",
    "普通": "normal",
    "通常": "normal",
    "しっかり": "normal",
}

_FOLLOW_POLICY_FREQUENCY_ALIASES = {
    "必要なとき": "low",
    "少なめ": "low",
    "低め": "low",
    "ふつう": "normal",
    "普通": "normal",
    "毎回": "normal",
}

_FOLLOW_POLICY_UNSAFE_WORDS = (
    "兄弟と比べ",
    "姉妹と比べ",
    "比較して叱",
    "厳しく叱",
    "罰",
    "ペナルティ",
    "減額で脅",
    "だらしない",
    "浪費家",
    "嘘つき",
)


def init(wallet_service, client, reminder_service, allowance_reminder_conf: dict) -> None:
    """bot.py の起動時に依存オブジェクトを注入する。on_ready で呼ぶ。"""
    global _wallet_service, _client, _reminder_service, _allowance_reminder_conf
    _wallet_service = wallet_service
    _client = client
    _reminder_service = reminder_service
    _allowance_reminder_conf = allowance_reminder_conf


def _is_parent(user_id: int) -> bool:
    """Discord ユーザーIDが親（管理者）かどうかを判定する"""
    return user_id in get_parent_ids()


def _log_parent_handler_error(message: discord.Message, event: str, error: Exception, details: dict | None = None) -> None:
    """親ハンドラの失敗を診断ログへ残す。ログ失敗は標準出力に逃がす。"""
    try:
        system_conf = load_system()
        log_dir = get_log_dir(system_conf)
        append_jsonl(log_dir / "runtime_diagnostics.jsonl", {
            "ts": now_jst_iso(),
            "event": event,
            "discord_user_id": int(message.author.id),
            "channel_id": int(getattr(message.channel, "id", 0) or 0),
            "input": str(getattr(message, "content", "") or "")[:1200],
            "error_type": type(error).__name__,
            "error_message": str(error)[:600],
            "details": details or {},
        })
    except Exception as log_error:
        print(f"[parent_handler_diagnostics] log error: {type(log_error).__name__}: {log_error}")


def _command_body(content: str) -> str:
    """メンションあり/なしの親コマンド本文を返す"""
    mention_body = extract_input_from_mention((content or "").strip(), _client.user)
    return (mention_body if mention_body is not None else (content or "")).strip()


def _normalize_follow_policy(raw_policy: dict | None) -> dict:
    """子ども別AIフォロー方針を保存可能な形にそろえる"""
    policy = dict(_FOLLOW_POLICY_DEFAULT)
    if isinstance(raw_policy, dict):
        policy.update({k: raw_policy.get(k, v) for k, v in policy.items()})
    policy["enabled"] = bool(policy.get("enabled", True))
    if policy.get("focus_area") not in _FOLLOW_POLICY_FOCUS_LABELS:
        policy["focus_area"] = "balanced"
    if policy.get("nudge_strength") not in {"light", "normal"}:
        policy["nudge_strength"] = "light"
    if policy.get("frequency") not in {"low", "normal"}:
        policy["frequency"] = "low"
    policy["parent_note"] = str(policy.get("parent_note") or "").strip()[:300]
    return policy


def _follow_policy_note_error(note: str) -> str | None:
    """親メモが罰・比較・人格評価に寄りすぎていないか確認する"""
    text = (note or "").strip()
    if len(text) > 300:
        return "AIフォロー方針は300文字以内で入力してね。"
    if any(word in text for word in _FOLLOW_POLICY_UNSAFE_WORDS):
        return "叱責・兄弟比較・罰を前提にした方針は保存しないよ。買う前チェック、記録習慣、親子で一緒に確認する表現に直してね。"
    return None


def _parse_follow_policy_updates(text: str) -> tuple[dict, str]:
    """自然文に近い親コマンドから方針フィールドを抽出する"""
    body = (text or "").strip()
    normalized = _normalize_japanese_command(body)
    updates: dict = {}

    if any(token in normalized for token in ("無効", "オフ", "off", "OFF")):
        updates["enabled"] = False
    elif any(token in normalized for token in ("有効", "オン", "on", "ON")):
        updates["enabled"] = True

    for needle, value in _FOLLOW_POLICY_FOCUS_ALIASES.items():
        if needle in body or needle in normalized:
            updates["focus_area"] = value
            break

    for needle, value in _FOLLOW_POLICY_STRENGTH_ALIASES.items():
        if needle in body or needle in normalized:
            updates["nudge_strength"] = value
            break

    for needle, value in _FOLLOW_POLICY_FREQUENCY_ALIASES.items():
        if needle in body or needle in normalized:
            updates["frequency"] = value
            break

    note = body
    note = re.sub(r"\b(enabled|focus|strength|frequency)\s*=\s*\S+", "", note, flags=re.IGNORECASE)
    note = note.replace("有効", "").replace("無効", "").replace("オン", "").replace("オフ", "").strip()
    return updates, note


def _follow_policy_summary(name: str, policy: dict) -> str:
    state = "有効" if policy["enabled"] else "無効"
    focus = _FOLLOW_POLICY_FOCUS_LABELS.get(policy["focus_area"], "バランス")
    strength = "軽め" if policy["nudge_strength"] == "light" else "通常"
    frequency = "必要なときだけ" if policy["frequency"] == "low" else "通常"
    note = policy["parent_note"] or "なし"
    return (
        f"{name}のAIフォロー方針: {state}\n"
        f"- 重視: {focus}\n"
        f"- 強さ: {strength}\n"
        f"- 頻度: {frequency}\n"
        f"- 親メモ: {note}"
    )


# ------------------------------------------------------------------
# 親専用コマンドハンドラ
# ------------------------------------------------------------------

async def maybe_handle_parent_broadcast_guide(message: discord.Message, content: str) -> bool:
    """「使い方の説明と初期設定」コマンドで全チャンネルに使い方を一斉通知する（親のみ）"""
    if not _is_parent(message.author.id):
        return False

    mention_body = extract_input_from_mention((content or "").strip(), _client.user)
    body = mention_body if mention_body is not None else (content or "")
    normalized = _normalize_japanese_command(body)
    # 「と初期設定」付きを先に判定して単体送信と区別する
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
            channel = _client.get_channel(int(cid))
            if channel is None:
                channel = await _client.fetch_channel(int(cid))
            await channel.send(text)
            sent += 1
        except Exception as e:
            failed.append(f"{cid}({type(e).__name__})")

    msg = f"使い方と初期設定のアナウンスを {sent}/{len(channel_ids)} チャネルに送信したよ。"
    if failed:
        msg += f"\n送信失敗: {', '.join(str(x) for x in failed)}"
    await message.channel.send(msg)
    return True


async def maybe_handle_parent_usage_single(message: discord.Message, content: str) -> bool:
    """「使い方の説明」コマンドでコマンドを送ったチャンネル1つだけに使い方を送信する（親のみ）。
    「使い方の説明と初期設定」（全チャンネル一斉）より後に判定すること。"""
    if not _is_parent(message.author.id):
        return False

    mention_body = extract_input_from_mention((content or "").strip(), _client.user)
    body = mention_body if mention_body is not None else (content or "")
    normalized = _normalize_japanese_command(body)
    # 「と初期設定」付きは一斉送信コマンドなのでここでは除外する
    is_cmd = (
        "使い方の説明" in normalized
        or "つかいかたのせつめい" in normalized
    ) and (
        "初期設定" not in normalized
        and "しょきせってい" not in normalized
    )
    if not is_cmd:
        return False

    # コマンドを送ったチャンネルに直接送信する
    await message.channel.send(_usage_guide_text())
    await message.channel.send("（このチャンネル単体への送信だよ。全チャンネルへ送る場合は「使い方の説明と初期設定」を使ってね）")
    return True


async def maybe_handle_parent_dashboard(message: discord.Message, content: str) -> bool:
    """親向けダッシュボード: 全ユーザーの残高・状況を一覧表示する（Feature 1）"""
    # 親以外はこのコマンドを使えない
    if not _is_parent(message.author.id):
        return False

    # メンション部分を除去してコマンド本文を取得する
    mention_body = extract_input_from_mention((content or "").strip(), _client.user)
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
    audit_state = _wallet_service.load_audit_state()
    # pending_by_user に名前があれば残高報告が未完了である
    pending_by_user = audit_state.get("pending_by_user", {})

    lines = ["【全体確認ダッシュボード】"]
    for u in users:
        name = str(u.get("name", ""))
        fixed = int(u.get("fixed_allowance", 0))
        balance = _wallet_service.get_balance(name)
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
    if not _is_parent(message.author.id):
        return False

    # メンション除去後の本文を取得する
    mention_body = extract_input_from_mention((content or "").strip(), _client.user)
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
    """「残高チェック送信」コマンドで今月の残高チェック案内を即時送信する（親のみ）"""
    mention_body = extract_input_from_mention((content or "").strip(), _client.user)
    if not (mention_body and mention_body.strip() in {"残高チェック送信", "月頭案内送信"}):
        return False

    if not _is_parent(message.author.id):
        await message.channel.send("残高チェック送信は親のみ実行できるよ。")
        return True

    try:
        await _reminder_service.send_wallet_audit()
        await message.channel.send("残高チェック案内を送信したよ。")
    except Exception as e:
        _log_parent_handler_error(message, "wallet_audit_send_error", e)
        await message.channel.send(operation_failure_message("残高チェック案内の送信"))
    return True


async def maybe_handle_reminder_test(message: discord.Message, content: str) -> bool:
    """「reminder test」コマンドでリマインダーをテスト送信する（親のみ）"""
    mention_body = extract_input_from_mention((content or "").strip(), _client.user)
    is_test_cmd = bool(
        mention_body
        and mention_body.strip().lower()
        in {"reminder test", "reminder-test", "リマインダーテスト", "リマインダー テスト"}
    )
    if not is_test_cmd:
        return False

    if not _is_parent(message.author.id):
        await message.channel.send("リマインダーテストは親のみ実行できるよ。")
        return True

    channel_id = _allowance_reminder_conf.get("channel_id")
    if not channel_id:
        await message.channel.send("`settings/setting.json` の `allowance_reminder.channel_id` を設定してね。")
        return True

    payday = _reminder_service.next_payday(
        today=datetime.now(JST).date(),
        payday_day=int(_allowance_reminder_conf["payday_day"]),
    )
    try:
        await _reminder_service.send_allowance_reminder(payday=payday, channel_id=int(channel_id), is_test=True)
        await message.channel.send("リマインダーをテスト送信したよ。")
    except Exception as e:
        _log_parent_handler_error(message, "reminder_test_send_error", e)
        await message.channel.send(operation_failure_message("リマインダーテスト送信"))
    return True


async def maybe_handle_manual_grant(message: discord.Message, content: str) -> bool:
    """親が手動でお小遣いを支給するコマンドを処理する（親のみ）。
    「支給 たろう 700円」の形式にマッチする。"""
    # 親以外は無視する
    if not _is_parent(message.author.id):
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
    before = _wallet_service.get_balance(target_name)
    # allowance_grant（Gemini査定自動付与）と区別するため別のアクション名にする
    new_balance, achieved_goals = _wallet_service.update_balance(
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
    if not _is_parent(message.author.id):
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
    before = _wallet_service.get_balance(target_name)
    new_balance, achieved_goals = _wallet_service.update_balance(
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
    if not _is_parent(message.author.id):
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
        await message.channel.send(operation_failure_message(f"{target_name}の設定ファイル更新"))
        return True

    await message.channel.send(
        f"{target_name}の{label}を変更したよ。"
        f"\n{old_value}円 → {amount}円"
    )
    return True


async def maybe_handle_followup_policy(message: discord.Message, content: str) -> bool:
    """親がDiscordから子ども別AIフォロー方針を確認・変更する"""
    body = _command_body(content)
    m = re.match(r"^(?:AI)?フォロー(?:方針|設定)\s+(\S+)(?:\s+(.+))?$", body, re.IGNORECASE | re.DOTALL)
    strength_m = re.match(r"^(?:AI)?フォロー強さ\s+(\S+)(?:\s+(.+))?$", body, re.IGNORECASE | re.DOTALL)
    frequency_m = re.match(r"^(?:AI)?フォロー頻度\s+(\S+)(?:\s+(.+))?$", body, re.IGNORECASE | re.DOTALL)
    command_match = m or strength_m or frequency_m
    if not command_match:
        return False

    if not _is_parent(message.author.id):
        await message.channel.send("AIフォロー方針の変更は親のみできるよ。")
        return True

    target_name = command_match.group(1).strip()
    target_conf = find_user_by_name(target_name)
    if target_conf is None:
        await message.channel.send(f"`{target_name}` はユーザー設定に見つからなかったよ。")
        return True

    current_policy = _normalize_follow_policy(target_conf.get("ai_follow_policy"))
    rest = (command_match.group(2) or "").strip()
    if not rest:
        await message.channel.send(_follow_policy_summary(target_name, current_policy))
        return True

    updates, note = _parse_follow_policy_updates(rest)
    if strength_m:
        if "nudge_strength" not in updates:
            await message.channel.send("フォロー強さは `軽め` または `普通` で指定してね。")
            return True
        updates = {"nudge_strength": updates["nudge_strength"]}
        note = current_policy["parent_note"]
    if frequency_m:
        if "frequency" not in updates:
            await message.channel.send("フォロー頻度は `必要なときだけ` または `普通` で指定してね。")
            return True
        updates = {"frequency": updates["frequency"]}
        note = current_policy["parent_note"]

    note_error = _follow_policy_note_error(note)
    if note_error:
        await message.channel.send(note_error)
        return True

    new_policy = dict(current_policy)
    new_policy.update(updates)
    new_policy["parent_note"] = note
    new_policy = _normalize_follow_policy(new_policy)

    if not update_user_field(target_name, "ai_follow_policy", new_policy):
        await message.channel.send(operation_failure_message(f"{target_name}のAIフォロー方針保存"))
        return True

    await message.channel.send("AIフォロー方針を保存したよ。\n" + _follow_policy_summary(target_name, new_policy))
    return True


async def maybe_handle_bulk_grant(message: discord.Message, content: str) -> bool:
    """親が全ユーザーに固定お小遣いを一括支給するコマンドを処理する（親のみ）。
    全ユーザーの fixed_allowance を残高に加算し、結果を一覧表示する。"""
    if not _is_parent(message.author.id):
        return False
    # メンション付き（「@compass-bot 一括支給」）にも対応するためメンション除去後に判定する
    mention_body = extract_input_from_mention((content or "").strip(), _client.user)
    target = mention_body if mention_body is not None else (content or "")
    # 誤作動を防ぐため完全一致で判定する
    if target.strip() != "一括支給":
        return False

    users = sorted(load_all_users(), key=lambda x: str(x.get("name", "")))
    if not users:
        await message.channel.send("ユーザーが設定されていないよ。")
        return True

    system_conf = load_system()
    lines = ["【一括支給完了】"]
    # 全ユーザーを走査して fixed_allowance を残高に加算する
    for u in users:
        name = str(u.get("name", ""))
        amount = int(u.get("fixed_allowance", 0))
        # 固定お小遣いが設定されていないユーザーはスキップする
        if amount <= 0:
            lines.append(f"・{name}: スキップ（固定額未設定）")
            continue
        new_balance, achieved_goals = _wallet_service.update_balance(
            user_conf=u,
            system_conf=system_conf,
            delta=amount,
            action="allowance_monthly_auto_grant",
            note="bulk_grant_by_parent",
            extra={"granted_by": str(message.author.id)},
        )
        lines.append(f"・{name}: +{amount}円 → {new_balance}円")
        # 支給により目標が達成された場合は祝福メッセージを送る
        for achieved_goal in achieved_goals:
            await message.channel.send(
                _build_goal_achieved_message(user_conf=u, goal=achieved_goal)
            )

    await message.channel.send("\n".join(lines))
    return True


async def maybe_handle_parent_announce(message: discord.Message, content: str) -> bool:
    """親が任意メッセージを全 allow チャンネルに一斉送信するコマンドを処理する（親のみ）。
    「アナウンス [本文]」の形式にマッチする。メンションあり/なしどちらも対応する。"""
    if not _is_parent(message.author.id):
        return False

    body = (content or "").strip()
    # メンションが含まれる場合は除去した本文を使う
    mention_body = extract_input_from_mention(body, _client.user)
    target = mention_body if mention_body is not None else body

    # 「アナウンス 本文」の形式を抽出する（本文は複数行にも対応）
    m = re.match(r"^アナウンス\s+(.+)$", target.strip(), re.DOTALL)
    if not m:
        return False

    announce_text = m.group(1).strip()
    channel_ids = get_allow_channel_ids()
    if not channel_ids:
        await message.channel.send("`allow_channel_ids` が未設定なので一斉送信できないよ。")
        return True

    sent = 0
    failed: list[str] = []
    # 全 allow チャンネルに「【アナウンス】本文」を送信する
    for cid in sorted(channel_ids):
        try:
            channel = _client.get_channel(int(cid))
            if channel is None:
                channel = await _client.fetch_channel(int(cid))
            await channel.send(f"【アナウンス】\n{announce_text}")
            sent += 1
        except Exception:
            failed.append(str(cid))

    if failed:
        await message.channel.send(
            f"{sent}チャンネルに送信したよ。失敗チャンネル: {', '.join(failed)}"
        )
    else:
        await message.channel.send(f"{sent}チャンネルに送信したよ。")
    return True


async def maybe_handle_web_approve(message: discord.Message, content: str) -> bool:
    """「web承認 [ユーザー名]」コマンドで Web ダッシュボードアクセス申請を承認する（親のみ）。
    承認すると仮パスワードを発行して Discord 経由で通知する。"""
    if not _is_parent(message.author.id):
        return False

    body = (content or "").strip()
    # メンションを除去してからコマンド判定する
    mention_body = extract_input_from_mention(body, _client.user)
    target = mention_body if mention_body is not None else body

    # 「web承認 ユーザー名」の形式にマッチする
    m = re.match(r"^web承認\s+(.+)$", target.strip(), re.IGNORECASE)
    if not m:
        return False

    username = m.group(1).strip()
    # web_auth モジュールを経由して申請を承認する
    from app import web_auth
    temp_pw = await web_auth.approve_application(username)
    if temp_pw is None:
        await message.channel.send(
            f"「{username}」の承認待ち申請が見つからなかったよ。"
            f"申請ユーザー名を確認してね。"
        )
        return True

    # 仮パスワードを Discord で通知する（DM 送信は不可能なため同チャンネルに送信）
    from app.config import get_web_base_url
    base_url = get_web_base_url()
    await message.channel.send(
        f"✅ **{username}** のWebアクセスを承認したよ！\n"
        f"仮パスワード: `{temp_pw}`\n"
        f"下記URLでパスワードを設定してね:\n"
        f"{base_url}/compass-bot/set_password?username={username}"
    )
    return True
