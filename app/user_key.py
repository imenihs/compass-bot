"""learning_support_state 等のファイル名に使う user_key を、全経路で一意に生成する共有関数。

会話層(deps)・リマインダ(reminder_service)・Web(server)・査定プロンプト(bot)の4経路が
同じ learning_support_state/{key}.json を読み書きする。以前はそれぞれ quote の safe 指定・
優先フィールド・文字数上限がバラバラで、子ども名に '.' '-' '_' 等の ASCII 記号が含まれると
ファイル名が食い違い、会話コーチング・能動伴走・Web の状態が別ファイルに分裂して同期しない
時限地雷になっていた。生成規則をここへ集約し、二度と分岐させない。

規則（1箇所で定義する）:
  - 優先フィールド: user_key → name → discord_user_id/discord_id → "unknown"
  - quote(safe="-_.")（URL 予約でない ASCII 記号は温存、それ以外はパーセントエンコード）
  - 先頭 120 文字に丸める（極端に長い名前でのファイル名爆発を防ぐ）
日本語名（現行の実児）は safe 指定に関わらず同一結果になるため、集約しても既存 state の
ファイル名は変わらない（後方互換）。
"""
from urllib.parse import quote

# ファイル名の長さ上限。極端に長い user_key でのファイル名爆発を防ぐ。
_MAX_KEY_LEN = 120


def canonical_user_key(user_conf: dict, fallback_name: str = "") -> str:
    """user_conf から learning_support_state 用の正規 user_key を生成する。

    Args:
        user_conf: 対象ユーザーの設定 dict（name / user_key / discord_user_id 等）。
        fallback_name: user_conf に有効なフィールドが無いときに使う名前（Web 経路用）。

    Returns:
        str: パーセントエンコード済みの user_key。全フィールドが空なら "unknown"。
    """
    if not isinstance(user_conf, dict):
        user_conf = {}
    # 優先フィールドを順に探す。user_key を最優先にし、無ければ name、次に fallback、最後に discord ID
    raw = (
        str(user_conf.get("user_key") or "").strip()
        or str(user_conf.get("name") or "").strip()
        or str(fallback_name or "").strip()
        or str(user_conf.get("discord_user_id") or user_conf.get("discord_id") or "").strip()
    )
    key = quote(raw, safe="-_.")[:_MAX_KEY_LEN]
    # 全フィールド空・丸め結果が空になった場合の保険
    return key or "unknown"
