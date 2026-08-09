"""親の金額操作を、実行前に Python が確認する仕組み（N-11.17 の Python 境界）。

子経路には `ACTIVE_CHILD` という「AI が破れない Python 境界」があり、
AI が別の子を指定しても tool 層で拒否される。
一方 **親経路にはこの対応物が無く、登録児全員が常に射程内**であった。

A 案（言葉の解釈を AI に任せる）へ移すにあたり、この非対称を埋める。
やり方は「AI を信用しない」ではなく「**人が確かめられるようにする**」である。

  ・確認文は **Python が組み立てる**。AI に文面を作らせない。
    AI が対象や金額を取り違えていても、親が見るのは Python が構造化データから
    起こした文なので、食い違いがそのまま目に入る。
  ・確認するのは **金額が動く操作だけ**。参照・一覧は即答する。
  ・確認は **AI が曖昧と判断したとき**にも使う（値の欠落・confident=false）。

実測（2026/08/09・110件）では対象児の取り違え0件・桁違い0件だったが、
「実測でゼロ」と「構造上起こり得ない」は別である。金銭は取り返しがつかないため、
人が捕まえられる経路を残す。
"""
import threading
import uuid
from datetime import datetime

from app.storage import JST

# 確認待ちの猶予（秒）。これを過ぎた同意は「別の操作への返事」の恐れがあるため無効にする。
# 親が席を外して戻ってきた程度は許容しつつ、取り違えは防ぐ長さにする。
CONFIRM_WAIT_SEC = 300

# 親ごとの確認待ち。{parent_id: {...}}
# 1人につき1件だけ保持する。複数を並べると「どれへの返事か」が曖昧になり取り違えるため、
# 新しい確認を出した時点で古いものは破棄する（親には新しい確認文だけが見えている）。
_PENDING: dict[int, dict] = {}
_LOCK = threading.RLock()

# 同意・拒否とみなす返事。完全一致で見る（「はい、でも金額は…」のような曖昧な返事は同意にしない）
_YES = ("はい", "OK", "ok", "オッケー", "うん", "そう", "実行", "お願い", "おねがい")
_NO = ("いいえ", "no", "やめる", "キャンセル", "cancel", "ちがう", "違う", "だめ", "ダメ")


def build_confirmation(action: str, child_name: str, amount, extra: str = "") -> str:
    """確認文を **Python が** 組み立てる（AI に作らせない）。

    AI が構造化した値をそのまま埋めるので、AI が取り違えていれば
    親の目にその食い違いが映る。これが人による最終チェックになる。

    Args:
        action: 操作の種類（grant / adjust など）。
        child_name: 対象児の名前。
        amount: 金額（符号つきでもよい）。
        extra: 補足（理由など）。

    Returns:
        str: 親へ見せる確認文。
    """
    label = {
        "grant": "支給",
        "adjust": "残高の調整",
        "bulk_grant": "全員への一括支給",
    }.get(action, action)
    lines = ["**この内容で実行していい？**", ""]
    if child_name:
        lines.append(f"・対象: **{child_name}**")
    lines.append(f"・操作: {label}")
    if amount is not None:
        try:
            n = int(amount)
            lines.append(f"・金額: **{n:+,}円**" if action == "adjust" else f"・金額: **{n:,}円**")
        except (TypeError, ValueError):
            lines.append(f"・金額: {amount}")
    if extra:
        lines.append(f"・内容: {extra}")
    lines.append("")
    lines.append("よければ「はい」、やめるなら「いいえ」と送ってね。")
    lines.append("（5分たつと自動でキャンセルされるよ）")
    return "\n".join(lines)


def put_pending(parent_id: int, action: str, args: dict,
                now: datetime | None = None) -> str:
    """確認待ちを登録する。1人につき1件だけ保持する。

    Args:
        parent_id: 親の Discord ID。
        action: 実行しようとしている操作。
        args: 実行に必要な引数（Python が検証済みのもの）。
        now: 現在時刻（テスト用）。

    Returns:
        str: 確認 ID。
    """
    stamp = (now or datetime.now(JST)).timestamp()
    token = uuid.uuid4().hex[:8]
    with _LOCK:
        # 古い確認は破棄する。複数保持すると「どれへの返事か」が曖昧になる
        _PENDING[int(parent_id)] = {
            "token": token, "action": action, "args": dict(args or {}), "ts": stamp,
        }
    return token


def classify_reply(text: str) -> str:
    """親の返事を yes / no / other に分類する。

    完全一致で見る。「はい、でも金額は3000円で」のような条件つきの返事を
    同意とみなすと、確認の意味が無くなるため。
    """
    t = (text or "").strip()
    if not t:
        return "other"
    if t in _YES:
        return "yes"
    if t in _NO:
        return "no"
    return "other"


def take_pending(parent_id: int, now: datetime | None = None) -> dict | None:
    """確認待ちを取り出して消す。猶予を過ぎていれば None（無効）。

    取り出したら必ず消す。同じ確認で二重に実行されるのを防ぐ。
    """
    cur = (now or datetime.now(JST)).timestamp()
    with _LOCK:
        rec = _PENDING.pop(int(parent_id), None)
    if not rec:
        return None
    if (cur - float(rec.get("ts", 0))) > CONFIRM_WAIT_SEC:
        # 古すぎる。別の操作への返事の恐れがあるため無効にする
        return None
    return rec


def peek_pending(parent_id: int) -> dict | None:
    """確認待ちの有無だけを見る（消さない）。"""
    with _LOCK:
        rec = _PENDING.get(int(parent_id))
    return dict(rec) if rec else None


def clear_pending(parent_id: int) -> None:
    """確認待ちを破棄する（キャンセル時）。"""
    with _LOCK:
        _PENDING.pop(int(parent_id), None)
