"""
handlers_parent.py — 親専用コマンドハンドラ群

bot.py の肥大化防止のために分離。グローバル状態は init() で注入する。
"""

import re
from datetime import datetime

import json

import discord

from app.bot_utils import (
    _build_goal_achieved_message,
    _load_jsonl,
    _normalize_japanese_command,
    _usage_guide_text,
)
from app import config
from app.config import (
    find_user_by_name,
    find_child_user_by_name,
    get_allow_channel_ids,
    get_parent_ids,
    load_all_users,
    load_system,
    get_log_dir,
    update_user_field,
    get_web_base_url,
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


def _is_exact_command(content: str, *forms: str) -> bool:
    """引数の無い固定コマンドを**完全一致**で判定する（N-11.17）。

    従来は `"使い方の説明" in normalized` のような部分一致で判定しており、
    ①文中にその語があるだけで発火する ②「使い方の説明」と「使い方の説明と初期設定」が
    衝突するため否定条件（`"初期設定" not in ...`）で回避する、という状態だった。
    後者は **bot.py の呼び出し順に依存する暗黙のルール**で、
    並び順を変えると壊れる。順序依存を無くすには完全一致にするのが正しい。

    引数を取るコマンド（支給・残高調整など）はここでは扱わない。
    それらは言葉の解釈が要るため AI に構造化させる（A案）。

    Args:
        content: 生のメッセージ本文。
        *forms: 許可する表記（漢字・ひらがな等の表記ゆれを列挙する）。

    Returns:
        bool: いずれかの表記と完全一致すれば True。
    """
    mention_body = extract_input_from_mention((content or "").strip(), _client.user)
    body = mention_body if mention_body is not None else (content or "")
    normalized = _normalize_japanese_command(body).strip()
    return any(normalized == f for f in forms)


def _parent_op_key(message: discord.Message, action: str, target: str) -> str:
    """親のテキストコマンド用の冪等キーを作る。

    Discord のメッセージ ID はメッセージごとに一意なので、同じメッセージが再配信されても
    同じキーになり二重適用されない。親が意図して2回打った場合は別 ID になるため通る
    （「同じ操作の再送」だけを弾き、「本当に2回やりたい」は妨げない）。

    親テキスト経路はこれまで operation_key を渡しておらず、Discord の再送や親の連打で
    そのまま二重支給になっていた（有識者の反証で判明・2026/08/09）。AI 経路は
    _scoped_op_key で冪等化済みだったため、経路によって安全性が食い違っていた。

    Args:
        message: 親のメッセージ。
        action: 操作種別（allowance_manual_grant 等）。
        target: 対象児の名前。

    Returns:
        str: 冪等キー。
    """
    mid = int(getattr(message, "id", 0) or 0)
    return f"{target}:{action}:msg{mid}"


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

    # 設定語は文中どこにあっても拾う。
    # 「とりあえず軽めで」「今日から軽めにして」のように、
    # 親は設定語を文頭に置くとは限らないため（先頭限定にすると大量に取りこぼす）。
    # 質問・回想との切り分けは _looks_like_question と、
    # 呼び出し側の「変化が無ければ保存しない」で担保する。
    for needle, value in _FOLLOW_POLICY_STRENGTH_ALIASES.items():
        if needle in body or needle in normalized:
            updates["nudge_strength"] = value
            break

    for needle, value in _FOLLOW_POLICY_FREQUENCY_ALIASES.items():
        if needle in body or needle in normalized:
            updates["frequency"] = value
            break

    # 申し送り（parent_note）は、設定語を取り除いた**残りの文**とする。
    # 取り除かないと「軽め」だけ送ったときに parent_note が文字列 "軽め" で
    # 上書きされ、それまでの申し送りが壊れる（有識者反証で判明）。
    # 残りが空なら「申し送りの指定なし」として空文字を返し、
    # 呼び出し側が現在の申し送りを保つ。
    note = body
    note = re.sub(r"\b(enabled|focus|strength|frequency)\s*=\s*\S+", "", note, flags=re.IGNORECASE)
    for word in ("有効", "無効", "オン", "オフ"):
        note = note.replace(word, "")
    # 実際に採用した設定語だけを消す（別の意味で使われた語まで消さない）
    for alias_map, key in ((_FOLLOW_POLICY_STRENGTH_ALIASES, "nudge_strength"),
                           (_FOLLOW_POLICY_FREQUENCY_ALIASES, "frequency"),
                           (_FOLLOW_POLICY_FOCUS_ALIASES, "focus_area")):
        if key not in updates:
            continue
        for needle, value in alias_map.items():
            if value == updates[key] and needle in note:
                note = note.replace(needle, "", 1)
                break
    return updates, note.strip()


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

    # 完全一致で判定する（N-11.17）。部分一致だと文中にこの語があるだけで一斉送信が走る
    if not _is_exact_command(content, "使い方の説明と初期設定",
                             "つかいかたのせつめいとしょきせってい"):
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


async def maybe_handle_safety_setup_check(message: discord.Message, content: str) -> bool:
    """「安全設定チェック」で、危険信号の通知先が正しいかを実際に送信して確かめる（親のみ）。

    設定ミスは仕様やコードでは防げない。「親だけが見えるはずのチャンネル」が実は子にも
    見えている、という取り違えは、実際に送ってみて初めて分かる。
    自傷やいじめの通知が子に見えてしまう事故を、運用開始前に発見できるようにする。

    やること。
      ① 危険信号の通知先チャンネルを解決し、そこへ「確認用メッセージ」を送る。
      ② そのチャンネルに誰が入れるか（子が見えていないか）を親自身に目視確認してもらう。
      ③ 子ども用チャンネルにも確認用メッセージを送り、どの子のチャンネルかを取り違えていないか見せる。
    実際に送るのが要点で、設定値を表示するだけでは「見えるかどうか」は分からない。

    Args:
        message: 親からのメッセージ。
        content: 生の本文。

    Returns:
        bool: このコマンドとして処理したら True。
    """
    if not _is_parent(message.author.id):
        return False
    # 完全一致で判定する（N-11.17）
    if not _is_exact_command(content, "安全設定チェック", "あんぜんせっていちぇっく"):
        return False

    from app.config import get_safety_alert_setting, get_allowance_reminder_setting, load_all_users

    safety_conf = get_safety_alert_setting() or {}
    parent_conf = get_allowance_reminder_setting() or {}
    # 実際に使われる宛先を、本番と同じ解決順で求める（表示だけの確認にしない）
    target_id = safety_conf.get("channel_id") or parent_conf.get("channel_id")
    lines = ["**安全設定チェックを実行したよ。**", ""]

    if not safety_conf.get("enabled", True):
        lines.append("⚠️ `safety_alert.enabled` が false だよ。危険信号の通知は送られないよ。")
    if safety_conf.get("channel_id"):
        lines.append(f"・危険信号の通知先: 専用設定のチャンネル（ID {safety_conf['channel_id']}）")
    elif target_id:
        lines.append(f"・危険信号の通知先: 査定と同じ親チャンネル（ID {target_id}）へフォールバック中")
        lines.append("　→ 分けたいときは `safety_alert.channel_id` を設定してね。")
    else:
        lines.append("❌ 通知先が未設定だよ。危険信号を検知しても**どこにも通知できない**。")
        lines.append("　→ `safety_alert.channel_id` か `allowance_reminder.channel_id` を設定してね。")

    # ① 通知先へ実際に送る。届くかどうかは送ってみないと分からない
    delivered = False
    if target_id:
        try:
            ch = _client.get_channel(int(target_id))
            if ch is None:
                ch = await _client.fetch_channel(int(target_id))
            await ch.send(
                "🔔 **【安全設定チェック】ここは危険信号の通知先です**\n"
                "お子さんの安全に関わる連絡（いじめ・つらい気持ちの訴えなど）は、このチャンネルに届きます。\n"
                "**このメッセージがお子さんから見えていないか、必ず確認してください。**\n"
                "見えている場合は、チャンネルの権限設定を見直すか、"
                "`safety_alert.channel_id` に大人だけのチャンネルを指定してください。\n"
                "※ 家庭内の虐待が疑われる内容は、この経路では通知しません（お子さんへ公的窓口を案内します）。"
            )
            delivered = True
        except Exception as e:
            lines.append(f"❌ 通知先への送信に失敗したよ: {type(e).__name__}")
            lines.append("　→ チャンネルIDが正しいか、Botに送信権限があるか確認してね。")

    if delivered:
        lines.append("✅ 通知先へ確認用メッセージを送ったよ。**子どもに見えていないか確認してね。**")

    # ③ 子ども用チャンネルにも送り、どの子のチャンネルかの取り違えを見つける。
    #    宛先解決は査定 F/B と同じ _resolve_child_channels_strict を使う（入口ごとに解決方法を変えない）。
    #    候補が複数ある子（共有チャンネル）は送らず警告する。他の子に見える事故を防ぐため。
    sent_children = []
    try:
        channels, counts = await _resolve_child_channels_strict()
    except Exception as e:
        channels, counts = {}, {}
        lines.append(f"⚠️ 子ども用チャンネルの解決に失敗したよ: {type(e).__name__}")
    for user in load_all_users():
        name = str(user.get("name", ""))
        if not name:
            continue
        n = counts.get(name, 0)
        if n == 0:
            lines.append(f"⚠️ {name} さんのチャンネルが見つからないよ（発言がないと判定できないことがあるよ）。")
            continue
        if n > 1:
            lines.append(
                f"⚠️ {name} さんのチャンネル候補が {n} 件あるよ。"
                "1つのチャンネルに複数の子がいると、通知が他の子に見えてしまうよ。"
            )
            continue
        ch = channels.get(name)
        if ch is None:
            continue
        try:
            await ch.send(
                f"🔔 **【安全設定チェック】ここは {name} さん用のチャンネルです**\n"
                "名前が合っているか、おうちの人に教えてね。"
            )
            sent_children.append(name)
        except Exception as e:
            lines.append(f"⚠️ {name} さんのチャンネルへ送れなかったよ: {type(e).__name__}")

    if sent_children:
        lines.append(
            f"✅ 子ども用チャンネルへも確認用メッセージを送ったよ（{', '.join(sent_children)}）。\n"
            "　**表示された名前がそのチャンネルの子と合っているか確認してね。**"
        )
    lines.append("")
    lines.append("設定を直したら、Botを再起動してからもう一度このコマンドを実行してね。")
    await message.channel.send("\n".join(lines))
    return True


async def maybe_handle_parent_usage_single(message: discord.Message, content: str) -> bool:
    """「使い方の説明」コマンドでコマンドを送ったチャンネル1つだけに使い方を送信する（親のみ）。
    「使い方の説明と初期設定」（全チャンネル一斉）より後に判定すること。"""
    if not _is_parent(message.author.id):
        return False

    # 完全一致なので「使い方の説明と初期設定」とは自然に区別される（N-11.17）。
    # 従来は部分一致＋否定条件で除外しており、bot.py の呼び出し順に依存していた。
    if not _is_exact_command(content, "使い方の説明", "つかいかたのせつめい"):
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

    # 「全体確認」「ぜんたいかくにん」に**完全一致**したときだけ反応する（N-11.17）。
    # 部分一致だと「全体確認ってどうやるの？」のような疑問文でも発火する
    if not _is_exact_command(content, "全体確認", "ぜんたいかくにん"):
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


async def maybe_handle_user_setting_change(message: discord.Message, content: str) -> bool:
    """親が固定お小遣い・臨時上限に触れたとき、Web ダッシュボードへ案内する（親のみ）。

    **チャットで金額の設定を受け付けるのをやめた**（N-11.17）。
    固定お小遣いは毎月効き続ける設定で、桁を間違えると気づきにくい。
    Web には数値入力のフォームが既にあり、値が曖昧にならないうえ
    一覧で見比べられる。フォロー方針と同じ方針にそろえる。

    現在値の確認だけはその場で答える（見るだけなら曖昧さが無いため）。
    """
    if not _is_parent(message.author.id):
        return False
    body = _command_body(content)
    m = re.match(r"^設定変更(?:\s+(\S+))?", body)
    if not m:
        return False

    base_url = get_web_base_url().rstrip("/")
    target_name = (m.group(1) or "").strip()
    if target_name:
        target_conf = find_child_user_by_name(target_name)
        if target_conf is None:
            await message.channel.send(f"`{target_name}` は子どもユーザー設定に見つからなかったよ。")
            return True
        fixed = int(target_conf.get("fixed_allowance", 0) or 0)
        temp = int(target_conf.get("temporary_max", 0) or 0)
        await message.channel.send(
            f"{target_name}の今の設定だよ。"
            f"\n- 固定お小遣い: {fixed:,}円"
            f"\n- 臨時の上限: {temp:,}円"
            f"\n\n変更は Web から → {base_url}"
        )
        return True

    await message.channel.send(
        f"お小遣いの設定は Web からお願いね → {base_url}"
        "\n今の設定を見るなら「設定変更 <名前>」だよ。"
    )
    return True

async def maybe_handle_followup_policy(message: discord.Message, content: str) -> bool:
    """親が AI フォロー方針に触れたとき、Web ダッシュボードへ案内する（親のみ）。

    **チャットで細かい設定を受け付けるのをやめた**（N-11.17）。理由は3つ。

    1. 言葉から「指示」と「質問」を見分けるのは実務上できない。
       「軽めだっけ」で設定が変わる、逆に「とりあえず軽めで」が無視される、を
       語彙の調整で4周往復して、どちらかに必ず倒れることが分かった。
    2. 実ログでは親はこのコマンドをほぼ使わない（親の発話48件中0件）。
       親は自然文で話すか、Web を使う。
    3. Web ダッシュボードに同じ設定のフォームが既にある。
       選択式なので値が曖昧にならず、パースも要らない。

    設定の変更は確実な Web へ寄せ、チャットは案内に徹する。
    現在値の確認だけはその場で答える（見るだけなら曖昧さが無いため）。
    """
    body = _command_body(content)
    m = re.match(r"^(?:AI)?フォロー(?:方針|設定|強さ|頻度)(?:\s+(\S+))?", body, re.IGNORECASE)
    if not m:
        return False

    # 親以外は無視する
    if not _is_parent(message.author.id):
        await message.channel.send("AIフォロー方針の変更は親のみできるよ。")
        return True

    target_name = (m.group(1) or "").strip()
    base_url = get_web_base_url().rstrip("/")

    # 対象が分かるなら現在値を見せる（確認だけなら曖昧さが無い）
    if target_name:
        target_conf = find_user_by_name(target_name)
        if target_conf is None:
            await message.channel.send(f"`{target_name}` はユーザー設定に見つからなかったよ。")
            return True
        current_policy = _normalize_follow_policy(target_conf.get("ai_follow_policy"))
        await message.channel.send(
            _follow_policy_summary(target_name, current_policy)
            + f"\n\n変更は Web から → {base_url}"
        )
        return True

    await message.channel.send(
        f"AIフォロー方針は Web から設定してね → {base_url}"
        "\n今の設定を見るなら「フォロー方針 <名前>」だよ。"
    )
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


def _split_child_name(rest: str) -> tuple[str, str]:
    """『名前 ID <一言>』の先頭から、登録済み児童名を最長前方一致で切り出す。

    子名にスペースが含まれ得るため（例「山田 太郎」）、単純な `\\S+` 分割だと名前を壊す。登録名の中から
    rest の先頭に一致する最も長いものを名前として採り、残りを tail（ID＋一言）として返す（codex #5）。

    Args:
        rest: コマンドの『査定承認/却下 』以降の文字列。

    Returns:
        tuple[str, str]: (登録名, 残り)。名前が特定できなければ ("", rest)。
    """
    from app.config import load_all_users
    names = sorted(
        (str(u.get("name", "")).strip() for u in load_all_users()),
        key=len, reverse=True,  # 長い名前を先に試す（最長一致）
    )
    r = rest.strip()
    for nm in names:
        if not nm:
            continue
        if r == nm:
            return nm, ""
        if r.startswith(nm) and len(r) > len(nm) and r[len(nm)] in (" ", "　"):
            return nm, r[len(nm):].strip()
    # 登録名に一致しなければ、先頭トークンを名前とみなすフォールバック
    parts = r.split(maxsplit=1)
    if parts:
        return parts[0].strip(), (parts[1].strip() if len(parts) > 1 else "")
    return "", r


async def _resolve_child_channels_strict() -> tuple[dict, dict]:
    """子ごとに送信先チャンネルと候補数を返す。候補が1つの子だけ確実に送れる（誤送信防止）。

    reminder の `_child_channels` は先着で1つに畳むため複数候補を検知できない。ここでは
    `_channel_users` を全チャンネルに適用して子ごとの候補チャンネルを集め、数を数える。
    呼び出し側は「候補数==1」の子だけへ送る。

    Returns:
        tuple[dict, dict]: ({name: 代表チャンネル}, {name: 候補数})。解決不能時は空。
    """
    channel_by_name: dict = {}
    count_by_name: dict = {}
    try:
        rs = _reminder_service
        users = rs.load_all_users()
        user_by_discord_id = {int(u["discord_user_id"]): u for u in users if u.get("discord_user_id")}
        # 運用前提：チャンネルには親が必要最小限しか登録しない＝子チャンネルは基本その子専用（社長方針）。
        # よって opener は子チャンネルへ送る（親が見守れる。DM は親が把握できず不可）。子の特定は既存の
        # _channel_users（メンバー実体→取れなければチャンネル名一意一致）を使う。ただし保険として、
        # メンバー実体で「明確に複数の子」が検出できたチャンネルは共有とみなし送らない（誤登録事故の防止）。
        for channel_id in rs.allow_channel_ids:
            channel = rs.client.get_channel(channel_id)
            if channel is None:
                channel = await rs.client.fetch_channel(channel_id)
            # メンバー実体で複数の子が明確に見えたら共有チャンネル。opener を送らない（他の子に見える誤送信防止）
            member_ids = {
                int(getattr(m, "id", 0)) for m in getattr(channel, "members", []) if getattr(m, "id", None)
            }
            member_children = [user_by_discord_id[mid] for mid in member_ids if mid in user_by_discord_id]
            if len(member_children) >= 2:
                # 共有チャンネル：そこにいる子全員の候補数を増やし（送信先には採らない）、opener を出さない
                for user_conf in member_children:
                    nm = str(user_conf.get("name", "")).strip()
                    if nm:
                        count_by_name[nm] = count_by_name.get(nm, 0) + 1
                continue
            # 単独 or キャッシュ空：_channel_users（名前補完込み）で子を特定して送信先候補にする
            for user_conf in rs._channel_users(channel, users, user_by_discord_id):
                nm = str(user_conf.get("name", "")).strip()
                if not nm:
                    continue
                count_by_name[nm] = count_by_name.get(nm, 0) + 1
                channel_by_name.setdefault(nm, channel)
    except Exception:
        return {}, {}
    return channel_by_name, count_by_name


async def _drive_assessment_feedback() -> None:
    """承認/却下で積まれた子 F/B（opener）を取り出し、各子のチャンネルへ opener を生成・送信する。

    mcp_wallet が feedback_pending に積んだ入力を bot 側で取り出し（子ごと最新1件）、子チャンネルを
    reminder の既存機構で解決して generate_assessment_feedback を呼ぶ。子チャンネルが特定できなければ
    送らず診断ログに残す（誤送信防止）。テキストコマンド・親 AI 会話の両入口から同じ本関数を呼ぶ。
    """
    from app import mcp_wallet
    from app.config import find_child_user_by_name
    from app.conv.ai_conversation import generate_assessment_feedback
    try:
        feedbacks = mcp_wallet.take_pending_feedback()
    except Exception:
        feedbacks = []
    if not feedbacks:
        return
    # 子チャンネルを解決する。誤送信防止のため「その子が属すチャンネルがちょうど1つ」のときだけ送る。
    # _child_channels は先着で1つに畳むため（複数候補でも1つ返す）、ここで候補数を独立に数えて2つ以上なら送らない（codex 再現の修正）。
    child_channel, child_candidate_count = await _resolve_child_channels_strict()
    for fb in feedbacks:
        child = str(fb.get("name", "")).strip()
        conf = find_child_user_by_name(child)
        channel = child_channel.get(child)
        # 候補が2つ以上ある子は、どのチャンネルへ送るか一意でないため送らない（別の子の目に触れる誤送信を防ぐ）
        if child_candidate_count.get(child, 0) != 1:
            channel = None
        if conf is None or channel is None:
            # 子チャンネルが特定できないときは送らない（別の子へ誤送信しない）。診断へ残す
            try:
                _reminder_service._write_runtime_diagnostic({
                    "event": "assessment_feedback_no_child_channel",
                    "severity": "warn", "child": child,
                })
            except Exception:
                pass
            continue
        try:
            await generate_assessment_feedback(channel, conf, fb)
        except Exception:
            # 1件の失敗で後続を止めない（再enqueueはしない＝重複opener防止）
            pass


def _find_user_file_stem(discord_user_id: int, want_role: str) -> str | None:
    """Discord ID と役割から、設定ファイル名（拡張子なし）を引く。

    トークンのキーは user_key（`child:<ファイル名>` / `parent:<ファイル名>`）である。
    **discord_user_id はキーにできない**。実データで子「テスト」と親「とうちゃん」が
    同一 ID を持つ（兼務アカウント）ため、ID だけでは一意に定まらない。
    そこで「どのチャンネルで打たれたか」で役割を先に決め、この関数で絞り込む。

    Args:
        discord_user_id: 打った人の Discord ID。
        want_role: dashboard_token.ROLE_CHILD / ROLE_PARENT。

    Returns:
        str | None: 設定ファイル名。見つからなければ None。
    """
    from app import dashboard_token
    from app.config import CHILDREN_DIR, PARENTS_DIR

    base = (PARENTS_DIR if want_role == dashboard_token.ROLE_PARENT else CHILDREN_DIR)
    if not base.exists():
        return None
    for path in sorted(base.glob("*.json")):
        if path.name.endswith(".example.json"):
            continue
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, json.JSONDecodeError):
            continue
        if int(data.get("discord_user_id", 0) or 0) == int(discord_user_id):
            return path.stem
    return None


async def _send_dashboard_url(message: discord.Message, token: str, role: str) -> None:
    """発行した URL を本人へ届ける。**まず DM、届かなければそのチャンネルへ**。

    親チャンネルは夫婦2人が見ているため、そこへ流すと相手の URL も見えてしまう。
    DM なら打った本人にだけ届く。
    ただし DM は相手の設定次第で `discord.Forbidden` になるため、
    **届かなかったときはチャンネルへ出す**（親チャンネルは子から分離済みなので、
    最悪ここへ出しても子には見えない）。片方が失敗しても詰まらない構成にする。

    Args:
        message: コマンドを打ったメッセージ。
        token: 発行された UUID。
        role: child / parent。
    """
    base_url = get_web_base_url().rstrip("/")
    url = f"{base_url}/compass-bot/d/{token}"
    body = (
        "あたらしいダッシュボードのURLだよ。ひらいてブックマークしてね。\n"
        # 山括弧で囲むと Discord がリンクプレビューを作らない。
        # プレビューのために Discord 側が URL をクロールし、UUID がログに残るのを避ける
        f"<{url}>\n"
        "（このURLは自分専用だよ。ほかの人に見せないでね）"
    )
    try:
        await message.author.send(body)
        await message.channel.send("あたらしいURLをDMで送ったよ。")
        return
    except discord.Forbidden:
        # DM が拒否設定のとき。親チャンネル（子から分離済み）へ出す
        await message.channel.send(body)
    except Exception as exc:  # noqa: BLE001 - 送信失敗で処理を止めない
        _log_parent_handler_error(message, "dashboard_url_dm_failed", exc)
        await message.channel.send(body)


async def maybe_handle_url_reissue(message: discord.Message, content: str) -> bool:
    """「URL再発行」コマンド。**打った本人のダッシュボードURLだけ**を再発行する（親子共通）。

    再発行の入口を Web に置かない理由（docs/設計_UUID認証方式.md）:
      URL が漏れた場合、Web に入口があると**盗んだ側も同じ画面から再発行できる**。
      盗った側が先に再発行すると、正規の本人が締め出される。
      Discord のアカウントは本人確認済みの独立した経路なので、
      URL を盗んだだけの相手は再発行できず、正規の本人はいつでも取り戻せる。

    **他人の UUID は誰も再発行できない**（本人の Discord アカウントからのみ）。

    Args:
        message: Discord メッセージ。
        content: 発話本文。

    Returns:
        bool: このコマンドとして処理したら True。
    """
    from app import dashboard_token
    from app.config import is_parent_channel

    body = _command_body(content)
    if body.strip() not in {"URL再発行", "url再発行", "URLさいはっこう", "ダッシュボードURL"}:
        return False

    # 兼務アカウント（親IDが子としても登録されている）があるため、
    # **どのチャンネルで打たれたか**で役割を決める
    if is_parent_channel(message.channel.id):
        role = dashboard_token.ROLE_PARENT
    else:
        role = dashboard_token.ROLE_CHILD

    stem = _find_user_file_stem(message.author.id, role)
    if stem is None:
        await message.channel.send(
            "ごめん、あなたの登録が見つからなかったよ。おうちの人に伝えてね。"
        )
        return True

    user_key = dashboard_token.build_user_key(role, stem)
    token = dashboard_token.issue(user_key, role, issued_by=str(message.author.id))
    await _send_dashboard_url(message, token, role)
    return True


