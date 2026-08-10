"""
handlers_parent.py — 親専用コマンドハンドラ群

bot.py の肥大化防止のために分離。グローバル状態は init() で注入する。
"""

import re
from datetime import datetime

import json

import discord

from app.bot_utils import _usage_guide_text
from app import config
from app.config import (
    find_child_user_by_name,
    get_allow_channel_ids,
    get_parent_ids,
    load_all_users,
    load_system,
    get_log_dir,
    get_web_base_url,
)
from app.storage import append_jsonl, now_jst_iso

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


async def _drive_dashboard_dm() -> None:
    """tool が積んだ「URLをDMして」を取り出し、本人へ DM で届ける。

    **なぜキュー経由なのか**: tool（mcp_wallet）は別プロセスで動き Discord を持たない。
    そして AI の応答は必ずチャンネルへ出るため、tool に URL を返させると
    親チャンネルの相方にも自分専用 URL が見えてしまう。
    そこで tool は「送って」と積むだけにし、実際の DM はこのプロセスが行う。

    DM が拒否設定なら諦める（チャンネルへは絶対に出さない）。
    親チャンネルは夫婦2人が見ており、そこへ出すと相方に URL が渡るため。
    届かなかった旨だけ診断ログに残す。
    """
    from app import dashboard_token

    try:
        requests = dashboard_token.take_dm_requests()
    except Exception:  # noqa: BLE001 - キューの不調で会話を止めない
        return
    if not requests:
        return

    base_url = get_web_base_url().rstrip("/")
    for req in requests:
        uid = int(req.get("discord_user_id", 0) or 0)
        user_key = str(req.get("user_key", ""))
        if uid <= 0 or not user_key:
            continue
        token = dashboard_token.find_active_token(user_key)
        if not token:
            continue
        # 山括弧で囲むと Discord がリンクプレビューを作らない。
        # プレビューのために Discord 側が URL をクロールし、UUID が外部に出るのを避ける
        body = (
            "ダッシュボードのURLだよ。ひらいてブックマークしてね。\n"
            f"<{base_url}/compass-bot/d/{token}>\n"
            "（このURLは自分専用だよ。ほかの人に見せないでね）"
        )
        try:
            user = _client.get_user(uid) or await _client.fetch_user(uid)
            await user.send(body)
        except Exception as exc:  # noqa: BLE001 - 1人失敗しても他を止めない
            _log_dashboard_event("dashboard_url_dm_failed",
                                 {"discord_user_id": uid, "error": f"{type(exc).__name__}: {exc}"})


async def _drive_bot_actions() -> None:
    """tool が積んだ「bot にしかできない送信処理」を実行する。

    一斉通知と安全設定チェックは Discord への送信そのものが目的のため、
    別プロセスの tool では実行できない。ここで消化する。
    """
    from app import dashboard_token

    try:
        actions = dashboard_token.take_bot_actions()
    except Exception:  # noqa: BLE001 - キューの不調で会話を止めない
        return
    # 同じ種類の依頼は1回だけ実行する。
    # 一斉送信は全チャンネルへ飛ぶうえ取り消せないため、
    # 親が2回言っても2回配らない（request_dm が連打を畳むのと揃える）。
    # 低残高アラートは子ごとに内容が違うので、名前まで含めて重複判定する
    seen = set()
    deduped = []
    for action in actions:
        key = (str(action.get("kind", "")),
               str((action.get("payload") or {}).get("name", "")))
        if key in seen:
            continue
        seen.add(key)
        deduped.append(action)

    for action in deduped:
        kind = str(action.get("kind", ""))
        try:
            if kind == "broadcast_usage_guide":
                await _broadcast_usage_guide()
            elif kind == "safety_setup_check":
                await _run_safety_setup_check()
            elif kind == "low_balance_alert":
                # 閾値の判定は依頼を積む側で済んでいる。ここは送るだけ
                from app import handlers_child
                payload = action.get("payload", {}) or {}
                await handlers_child.send_low_balance_alert(
                    str(payload.get("name", "")),
                    int(payload.get("balance", 0) or 0),
                    int(payload.get("threshold", 0) or 0))
        except Exception as exc:  # noqa: BLE001 - 1件失敗しても他を止めない
            _log_dashboard_event("bot_action_failed",
                                 {"kind": kind, "error": f"{type(exc).__name__}: {exc}"})


async def _broadcast_usage_guide() -> None:
    """使い方の説明を全チャンネルへ一斉送信し、結果を親チャンネルへ報告する。"""
    channel_ids = get_allow_channel_ids()
    if not channel_ids:
        await _notify_parent_channel(
            "`settings/setting.json` の `allow_channel_ids` が未設定なので一斉通知できないよ。")
        return

    text = _usage_guide_text()
    sent, failed = 0, []
    for cid in sorted(channel_ids):
        try:
            channel = _client.get_channel(int(cid)) or await _client.fetch_channel(int(cid))
            await channel.send(text)
            sent += 1
        except Exception as exc:  # noqa: BLE001 - 1チャンネル失敗しても続ける
            failed.append(f"{cid}({type(exc).__name__})")

    msg = f"使い方と初期設定のアナウンスを {sent}/{len(channel_ids)} チャネルに送信したよ。"
    if failed:
        msg += f"\n送信失敗: {', '.join(failed)}"
    await _notify_parent_channel(msg)


async def _notify_parent_channel(text: str) -> None:
    """親チャンネルへ報告を送る。解決できなければ診断ログだけ残す。

    tool 経由の依頼は「どのメッセージから来たか」を持たないため、
    報告先は設定で決まる親チャンネルに固定する。
    子チャンネルへ流すと、一斉通知の失敗内容などが子に見えてしまう。
    """
    from app.config import get_parent_channel_id

    cid = get_parent_channel_id()
    if cid:
        try:
            channel = _client.get_channel(int(cid)) or await _client.fetch_channel(int(cid))
            await channel.send(text)
            return
        except Exception as exc:  # noqa: BLE001 - 送信失敗は診断ログへ
            _log_dashboard_event("parent_channel_notify_failed",
                                 {"channel_id": int(cid), "error": f"{type(exc).__name__}: {exc}"})
            return
    _log_dashboard_event("parent_channel_unset", {"text": text[:100]})


async def _run_safety_setup_check() -> None:
    """危険信号の通知先が正しいかを、実際に送信して確かめる。

    設定ミスは仕様やコードでは防げない。「親だけが見えるはずのチャンネル」が実は子にも
    見えている、という取り違えは、実際に送ってみて初めて分かる。
    自傷やいじめの通知が子に見えてしまう事故を、運用開始前に発見できるようにする。

    やること。
      ① 危険信号の通知先チャンネルを解決し、そこへ「確認用メッセージ」を送る。
      ② そのチャンネルに誰が入れるかを親自身に目視確認してもらう。
      ③ 子ども用チャンネルにも送り、どの子のチャンネルかを取り違えていないか見せる。
    実際に送るのが要点で、設定値を表示するだけでは「見えるかどうか」は分からない。
    """
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
            ch = _client.get_channel(int(target_id)) or await _client.fetch_channel(int(target_id))
            await ch.send(
                "🔔 **【安全設定チェック】ここは危険信号の通知先です**\n"
                "お子さんの安全に関わる連絡（いじめ・つらい気持ちの訴えなど）は、このチャンネルに届きます。\n"
                "**このメッセージがお子さんから見えていないか、必ず確認してください。**\n"
                "見えている場合は、チャンネルの権限設定を見直すか、"
                "`safety_alert.channel_id` に大人だけのチャンネルを指定してください。\n"
                "※ 家庭内の虐待が疑われる内容は、この経路では通知しません（お子さんへ公的窓口を案内します）。"
            )
            delivered = True
        except Exception as e:  # noqa: BLE001 - 送信失敗を親へ伝えて続ける
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
    except Exception as e:  # noqa: BLE001 - 解決失敗でも通知先の確認結果は返す
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
        except Exception as e:  # noqa: BLE001 - 1人失敗しても他を続ける
            lines.append(f"⚠️ {name} さんのチャンネルへ送れなかったよ: {type(e).__name__}")

    if sent_children:
        lines.append(
            f"✅ 子ども用チャンネルへも確認用メッセージを送ったよ（{', '.join(sent_children)}）。\n"
            "　**表示された名前がそのチャンネルの子と合っているか確認してね。**"
        )
    lines.append("")
    lines.append("設定を直したら、Botを再起動してからもう一度たのんでね。")
    await _notify_parent_channel("\n".join(lines))


def _log_dashboard_event(event: str, detail: dict) -> None:
    """ダッシュボード・キュー処理の診断ログを残す。

    _log_parent_handler_error は message を必須とするが、tool 経由の依頼には
    元メッセージが無いため、こちらは message 抜きで書ける形にする。
    ログの失敗で本処理を落とさない。

    Args:
        event: 事象名。
        detail: 付随情報。
    """
    try:
        append_jsonl(get_log_dir(load_system()) / "runtime_diagnostics.jsonl", {
            "ts": now_jst_iso(),
            "event": event,
            "details": detail or {},
        })
    except Exception as log_error:  # noqa: BLE001 - ログ失敗は標準出力へ逃がす
        print(f"[handlers_parent] log failed: {type(log_error).__name__}: {log_error}")

