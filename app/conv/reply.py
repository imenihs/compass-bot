"""応答の唯一の出口。送信・会話ログ記録・1900文字分割をここへ集約する。

新対話層（app/conv/**）が子どもへ返す応答は、すべて本モジュールの `send_reply` を通す。
直接 `channel.send` を呼ぶことは新層の内側で禁止する（実装仕様.md 第1段「応答の出口を1つにする」）。
例外は応答ではない6つの送信（考え中表示・未捕捉例外フォールバック・親チャンネル宛アラート／査定変更通知・
一斉使い方通知・一斉アナウンス）に限り、それらは本モジュールを経由しない。

本モジュールが担う3つの責務:
  1. 送信      — Discord の1メッセージ上限（2000文字）を超えないよう 1900 文字で分割して送る。
  2. 会話ログ  — `{name}_conversation.jsonl`（events.jsonl とは別系統）へ入力と応答を1行ずつ記録する。
                 記録は storage.append_conversation を通す唯一の書き手であり、書き込み後に rotate で切り詰める。
  3. 完全内容  — 送った応答の完全な文字列を返す。テストとログはこの戻り値で判定できる。

外部依存（log_dir・会話ログ設定）は deps 経由で呼び出し時に解決する。app.config 等を直接 import しない
（実装仕様.md 第1段「外部依存を deps.py の1箇所へ集約し、import 時に束縛しない」）。
"""

from typing import Any

from app import storage
from app.conv import deps

# Discord の1メッセージ文字数上限は2000。余白を取り1900文字ごとに分割する（既存 app/bot.py:2528 と同値）
SPLIT_SIZE = 1900

# 会話ログの role 値。プロンプトへ渡す履歴はこの値で発話者を区別する
ROLE_CHILD = "child"  # 子どもの入力
ROLE_BOT = "bot"      # ボットの応答


def _conversation_path(user_name: str) -> Any:
    """児童の会話ログ（{name}_conversation.jsonl）の絶対パスを返す。

    events.jsonl と同じ log_dir 配下へ置く。log_dir は deps 経由で現在値を解決する。

    Args:
        user_name: 対象児童名。

    Returns:
        Path: 会話ログファイルのパス。
    """
    # log_dir は events.jsonl 等と同じ出力先。会話ログもここへ集約する
    log_dir = deps.get_log_dir()
    return log_dir / f"{user_name}_conversation.jsonl"


def _rotate_conversation(path: Any) -> None:
    """会話ログを保持方針（行数上限・保持日数）に沿って切り詰める。

    追記のたびに呼ぶ。ローテーションは臨界区間の外で行う前提であり、本関数はロックを持たない
    （実装仕様.md 第1段「ローテーションは臨界区間の中では実行しない」）。応答の出口は session.py の
    ロック配下ではないため、ここで rotate してよい。設定読み込みや退避に失敗しても本処理は
    続行させる（記録済みの応答は既に届いており、切り詰め失敗で応答経路を止めない）。

    Args:
        path: live 会話ログのパス。
    """
    try:
        # 保持日数・行数上限は deps 経由で解決する（テスト差し替え対象外の設定）
        conf = deps.conversation_log_setting()
        storage.rotate(
            path,
            max_lines=conf.get("max_lines", 2000),
            retention_days=conf.get("retention_days", 90),
        )
    except Exception:
        # 切り詰めの失敗は応答経路を止めない。次ターンの追記時に再試行される
        pass


def record_incoming(
    user_name: str,
    text: str,
    *,
    kind: str = "chat",
    meta: dict | None = None,
) -> None:
    """子どもの入力を会話ログへ1行記録する。送信は行わない。

    応答（send_reply）とは別に、入力側も同じ会話ログへ残す。これにより1ターンにつき
    「入力1行・応答1行」が {name}_conversation.jsonl に並び、査定・雑談のプロンプトが
    文脈として読める（実装仕様.md 第1段③e）。会話ログの書き手を本モジュールへ一本化するため、
    入力の記録もここに置く。

    Args:
        user_name: 対象児童名。
        text: 子どもが送った入力本文。
        kind: 会話種別（雑談・査定相談など、対話層が定義する文字列）。プロンプトの履歴フィルタに使う。
        meta: 追加の任意メタ情報（intent 名など）。無ければ記録しない。
    """
    path = _conversation_path(user_name)
    # role で発話者を、kind で会話種別を区別する。ts は append_conversation が補う
    record = {"role": ROLE_CHILD, "kind": kind, "text": text}
    if meta:
        # メタは任意。存在するときだけ持たせ、無い行を空 dict で膨らませない
        record["meta"] = meta
    storage.append_conversation(path, record)
    # 入力追記でも行数上限を超えうるため、応答と同じく切り詰めを走らせる
    _rotate_conversation(path)


async def send_reply(
    channel: Any,
    content: str,
    *,
    user_name: str,
    kind: str = "chat",
    meta: dict | None = None,
) -> str:
    """応答を送信し、会話ログへ記録し、送った完全な内容を返す。新層の応答の唯一の出口。

    1900文字を超える応答は分割して複数メッセージで送る。会話ログには分割前の完全な1応答を
    1行として記録する（分割は Discord の送信都合であり、文脈としては1応答であるため）。
    直接 channel.send を呼ばず必ず本関数を通すことで、送信・記録・分割の変更を1箇所へ閉じ込める。

    Args:
        channel: 送信先の Discord チャンネル（テストでは FakeChannel）。
        content: 送る応答本文。空文字は送らず記録もしない。
        user_name: 会話ログの記録先を決める児童名。
        kind: 会話種別（雑談・査定相談など）。入力側の record_incoming と揃える。
        meta: 追加の任意メタ情報。無ければ記録しない。

    Returns:
        str: 送った応答の完全な内容（分割前）。空応答なら空文字。
    """
    # 空応答は送信も記録もしない。空行で会話ログを埋めない
    if not content:
        return ""

    # まず送信する。上限超過は 1900 文字ごとに分割し、順序を保って送る
    if len(content) > SPLIT_SIZE:
        for i in range(0, len(content), SPLIT_SIZE):
            await channel.send(content[i:i + SPLIT_SIZE])
    else:
        await channel.send(content)

    # 会話ログへは分割前の完全な1応答を1行で記録する。role=bot で応答と分かる
    path = _conversation_path(user_name)
    record = {"role": ROLE_BOT, "kind": kind, "text": content}
    if meta:
        # メタは任意。存在するときだけ持たせる
        record["meta"] = meta
    storage.append_conversation(path, record)
    # 追記後に保持方針で切り詰める。臨界区間の外なのでここで rotate してよい
    _rotate_conversation(path)

    # 完全な内容を返す。テストは戻り値で送信内容を、会話ログで記録を検証できる
    return content
