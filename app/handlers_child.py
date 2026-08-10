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











