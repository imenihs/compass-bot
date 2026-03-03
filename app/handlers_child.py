"""
handlers_child.py — 子供向けコマンドハンドラ群

bot.py の肥大化防止のために分離。グローバル状態は init() で注入する。
"""

import re
from datetime import datetime

import discord

from app.bot_utils import (
    _assessment_history_message,
    _build_goal_achieved_message,
    _child_review_message,
    _contains_any_keyword,
    _is_same_month,
    _ledger_history_message,
    _load_jsonl,
    _progress_bar,
)
from app.config import (
    find_user_by_name,
    get_log_dir,
    get_parent_ids,
)
from app.storage import append_jsonl, now_jst_iso, JST

# モジュールレベルの依存オブジェクト — init() で bot.py から注入する
_wallet_service = None
_client = None
_low_balance_alert_conf: dict = {}


def init(wallet_service, client, low_balance_alert_conf: dict) -> None:
    """bot.py の起動時に依存オブジェクトを注入する。on_ready で呼ぶ。"""
    global _wallet_service, _client, _low_balance_alert_conf
    _wallet_service = wallet_service
    _client = client
    _low_balance_alert_conf = low_balance_alert_conf


def _is_parent(user_id: int) -> bool:
    """Discord ユーザーIDが親（管理者）かどうかを判定する"""
    return user_id in get_parent_ids()


# ------------------------------------------------------------------
# 低残高アラート（bot.py から移動）
# ------------------------------------------------------------------

async def maybe_send_low_balance_alert(user_conf: dict, new_balance: int) -> None:
    """残高が閾値を下回ったとき親チャンネルへアラートを送信する（Feature 2）"""
    cfg = _low_balance_alert_conf
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
        channel = _client.get_channel(int(channel_id))
        if channel is None:
            channel = await _client.fetch_channel(int(channel_id))
        await channel.send(
            f"【低残高アラート】{name}さんの残高が{new_balance}円になりました（閾値:{threshold}円）。"
        )
    except Exception as e:
        # アラート失敗はボット動作を止めないためログ出力のみとする
        print(f"Low balance alert error: {e}")


# ------------------------------------------------------------------
# 子供向けコマンドハンドラ
# ------------------------------------------------------------------

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

    current = _wallet_service.get_balance(user_name)

    # 目標追加・更新: add_savings_goal は同名なら金額更新、新規なら追加する
    if set_match:
        title = set_match.group(1).strip()
        target_amount = int(set_match.group(2).replace(",", ""))
        success, result = _wallet_service.add_savings_goal(user_name, title, target_amount)
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
        _wallet_service.clear_all_savings_goals(user_name)
        await message.channel.send(f"{user_name}の貯金目標を全て削除したよ。")
        return True

    # タイトル指定削除
    if clear_title_match:
        title = clear_title_match.group(1).strip()
        found = _wallet_service.remove_savings_goal(user_name, title)
        if found:
            await message.channel.send(f"目標「{title}」を削除したよ。")
        else:
            await message.channel.send(f"目標「{title}」は見つからなかったよ。")
        return True

    # 引数なし削除 — 一覧を見せてタイトル指定を促す
    if is_clear_bare:
        goals = _wallet_service.get_savings_goals(user_name)
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
        goals = _wallet_service.get_savings_goals(user_name)
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
    balance = _wallet_service.get_balance(user_name)
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


async def maybe_handle_ledger_history(
    message: discord.Message,
    user_conf: dict,
    system_conf: dict,
    input_block: str,
) -> bool:
    """入出金台帳の履歴を表示するコマンドを処理する。
    「入出金履歴」「台帳確認」→ 自分の履歴。親は「たろうの台帳」で他ユーザーも参照可能。"""
    body = (input_block or "").strip()

    # 「たろうの台帳」「たろうの入出金履歴」形式のパターン（親のみ）
    m_target = re.match(r"^(.+)の(?:台帳|入出金履歴)$", body)
    if m_target:
        target_name = m_target.group(1).strip()
        # 「全員の台帳」は対応しないので無視して通過させる（分析コマンドとの混在防止）
        if target_name == "全員":
            return False
        if not _is_parent(message.author.id):
            await message.channel.send("他のユーザーの台帳確認は親のみできるよ。")
            return True
        target_conf = find_user_by_name(target_name)
        if target_conf is None:
            await message.channel.send(f"`{target_name}` はユーザー設定に見つからなかったよ。")
            return True
        view_conf = target_conf
    elif body in {"入出金履歴", "台帳確認"}:
        # 自分の履歴を表示する
        view_conf = user_conf
    else:
        return False

    # wallet_ledger.jsonl を読み込んで直近 10 件を表示する
    log_dir = get_log_dir(system_conf)
    ledger_path = log_dir / f"{view_conf.get('name', '')}_wallet_ledger.jsonl"
    rows = _load_jsonl(ledger_path)
    await message.channel.send(_ledger_history_message(view_conf, rows))
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
    before = _wallet_service.get_balance(user_name)

    # delta を負にして残高を減算する
    new_balance, _ = _wallet_service.update_balance(
        user_conf=user_conf,
        system_conf=system_conf,
        delta=-amount,
        action="manual_expense",
        note=note,
    )
    # pocket_journal にも同時記録する（振り返り・分析で金額が見えるようにする）
    log_dir = get_log_dir(system_conf)
    journal_path = log_dir / f"{user_name}_pocket_journal.jsonl"
    append_jsonl(journal_path, {
        "ts": now_jst_iso(),
        "discord_user_id": message.author.id,
        "name": user_name,
        # メモがあればそれを品目とし、なければ「支出」と記録する
        "item": note if note else "支出",
        "reason": "手動支出",
        "reason_word_count": 0,
        "satisfaction": None,
        "amount": amount,
    })
    await message.channel.send(
        f"支出を記録したよ。"
        f"\n- 金額: {amount}円"
        f"\n- メモ: {note if note else 'なし'}"
        f"\n残高: {before}円 → {new_balance}円"
    )
    # 支出後に残高が低下した場合のアラートチェックをする
    await maybe_send_low_balance_alert(user_conf=user_conf, new_balance=new_balance)
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
    before = _wallet_service.get_balance(user_name)

    # delta を正にして残高を加算する
    new_balance, achieved_goals = _wallet_service.update_balance(
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
