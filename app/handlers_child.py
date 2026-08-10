"""
handlers_child.py — 子供向けコマンドハンドラ群

bot.py の肥大化防止のために分離。グローバル状態は init() で注入する。
"""

import re
from datetime import datetime


from app.config import get_parent_ids

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

async def send_low_balance_alert(name: str, new_balance: int, threshold: int) -> None:
    """残高が閾値を下回ったとき親チャンネルへアラートを送る（Feature 2）。

    **呼び出しは tool の依頼キュー経由**（2026/08/10）。
    以前は到達不能になっていた文字列一致ハンドラの中から呼ばれており、
    設定を有効にしても実際には一度も飛ばない状態だった。
    支出を記録する経路が tool へ移ったので、そちらから依頼を積んでもらう。

    閾値の判定は依頼を積む側（mcp_wallet）で済んでいる。ここは送るだけ。

    Args:
        name: 子どもの名前。
        new_balance: 支出後の残高。
        threshold: 設定された閾値（文面に出す）。
    """
    cfg = _low_balance_alert_conf
    channel_id = cfg.get("channel_id")
    # 送信先チャンネルが未設定の場合はスキップする
    if not channel_id:
        return
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











