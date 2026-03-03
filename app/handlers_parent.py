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
from app.message_parser import extract_input_from_mention
from app.storage import JST

# モジュールレベルの依存オブジェクト — init() で bot.py から注入する
_wallet_service = None
_client = None
_reminder_service = None
_allowance_reminder_conf: dict = {}


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
        await message.channel.send(f"送信に失敗したよ。原因: {type(e).__name__}: {e}")
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
        await message.channel.send(f"テスト送信に失敗したよ。原因: {type(e).__name__}: {e}")
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
        await message.channel.send(f"{target_name}の設定ファイルの更新に失敗したよ。")
        return True

    await message.channel.send(
        f"{target_name}の{label}を変更したよ。"
        f"\n{old_value}円 → {amount}円"
    )
    return True


async def maybe_handle_bulk_grant(message: discord.Message, content: str) -> bool:
    """親が全ユーザーに固定お小遣いを一括支給するコマンドを処理する（親のみ）。
    全ユーザーの fixed_allowance を残高に加算し、結果を一覧表示する。"""
    if not _is_parent(message.author.id):
        return False
    # 誤作動を防ぐため完全一致で判定する
    if (content or "").strip() != "一括支給":
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
