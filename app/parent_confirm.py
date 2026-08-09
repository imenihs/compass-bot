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
import json
import os
import tempfile
import threading
import uuid
from datetime import datetime
from pathlib import Path

from app.storage import JST

# 確認待ちの猶予（秒）。これを過ぎた同意は「別の操作への返事」の恐れがあるため無効にする。
# 親が席を外して戻ってきた程度は許容しつつ、取り違えは防ぐ長さにする。
CONFIRM_WAIT_SEC = 300

# 確認待ちの保存先。**ファイルに置く**。
# 確認を積むのは mcp_wallet（別プロセス）、読んで実行するのは bot プロセスであり、
# メモリ保持ではプロセスを跨げない（実測で bot 側から見えないことを確認済み）。
# 1人につき1件だけ保持する。複数を並べると「どれへの返事か」が曖昧になり取り違えるため、
# 新しい確認を出した時点で古いものは破棄する（親には新しい確認文だけが見えている）。
_LOCK = threading.RLock()
ROOT = Path(__file__).resolve().parents[1]
PENDING_PATH = ROOT / "data" / "parent_confirm_pending.json"


def _interprocess_lock():
    """プロセス間ロック。bot と mcp_wallet の両方が読み書きするため必須。"""
    from app.wallet_service import _interprocess_lock as _lk
    return _lk(PENDING_PATH.with_suffix(".json.lock"))


def _load() -> dict:
    """確認待ちを読む。壊れていても動作を止めず空で返す。"""
    try:
        if not PENDING_PATH.exists():
            return {}
        with open(PENDING_PATH, "r", encoding="utf-8") as f:
            doc = json.load(f)
        return doc if isinstance(doc, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def _save(doc: dict) -> None:
    """一時ファイル＋置換で保存する（書き込み途中の破損を避ける）。"""
    PENDING_PATH.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(PENDING_PATH.parent), suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(doc, f, ensure_ascii=False, indent=2)
            f.write("\n")
        Path(tmp).replace(PENDING_PATH)
    except Exception:
        Path(tmp).unlink(missing_ok=True)
        raise

# 同意・拒否とみなす返事。完全一致で見る（「はい、でも金額は…」のような曖昧な返事は同意にしない）
# 同意とみなす語。完全一致で見る。
# 「そう」は相槌・フィラーにもなる弱い同意なので**入れない**
# （「そう、、、」のような迷いが金額操作の実行になってしまう）。命令形の「そうして」だけ受ける。
_YES = ("はい", "OK", "ok", "Ok", "ＯＫ", "yes", "Yes", "YES", "オッケー", "うん",
        "実行", "実行して", "お願い", "おねがい", "お願いします", "おねがいします",
        "いいよ", "了解", "了解です", "りょうかい", "わかった", "そうして")
_NO = ("いいえ", "no", "No", "やめる", "やめて", "キャンセル", "cancel",
       "ちがう", "違う", "だめ", "ダメ", "取り消し", "とりけし")


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
                now: datetime | None = None) -> tuple[str, dict | None]:
    """確認待ちを登録する。1人につき1件だけ保持する。

    Args:
        parent_id: 親の Discord ID。
        action: 実行しようとしている操作。
        args: 実行に必要な引数（Python が検証済みのもの）。
        now: 現在時刻（テスト用）。

    Returns:
        tuple[str, dict | None]: (確認 ID, 破棄した古い確認)。
            古い確認があった場合、呼び出し側は親へ「前の確認は取り消した」と伝える。
    """
    stamp = (now or datetime.now(JST)).timestamp()
    token = uuid.uuid4().hex[:8]
    with _LOCK, _interprocess_lock():
        doc = _load()
        # 古い確認は破棄する。複数保持すると「どれへの返事か」が曖昧になる。
        # 破棄したものは呼び出し側へ返し、**親に知らせられるようにする**。
        # Discord では古い確認文も画面に残るため、黙って捨てると
        # 親が上の確認に「はい」と答えたつもりで別の操作が走る（有識者の反証で判明）。
        superseded = doc.get(str(int(parent_id)))
        doc[str(int(parent_id))] = {
            "token": token, "action": action, "args": dict(args or {}), "ts": stamp,
        }
        _save(doc)
    return token, superseded


def classify_reply(text: str) -> str:
    """親の返事を yes / no / other に分類する。

    完全一致で見る。「はい、でも金額は3000円で」のような条件つきの返事を
    同意とみなすと、確認の意味が無くなるため。
    """
    t = (text or "").strip()
    # 読点や三点リーダの連続は「迷い」の表れなので、同意とみなさない。
    # 「うん、、、」「はい…」を yes にすると、ためらいのまま金額操作が走る
    if any(mark in t for mark in ("、、", "。。", "…", "..", "・・")):
        return "other"
    # 末尾の句読点・感嘆符は落とす。「はい。」「はい!」を other にすると
    # 確認が保持されたまま残り、親が後で別の文脈で言った「はい」で実行されうる
    t = t.rstrip("。．.!！、,　 ")
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
    with _LOCK, _interprocess_lock():
        doc = _load()
        rec = doc.pop(str(int(parent_id)), None)
        if rec is not None:
            _save(doc)
    if not rec:
        return None
    if (cur - float(rec.get("ts", 0))) > CONFIRM_WAIT_SEC:
        # 古すぎる。別の操作への返事の恐れがあるため無効にする
        return None
    return rec


def peek_pending(parent_id: int) -> dict | None:
    """確認待ちの有無だけを見る（消さない）。"""
    with _LOCK, _interprocess_lock():
        rec = _load().get(str(int(parent_id)))
    return dict(rec) if rec else None


def clear_pending(parent_id: int) -> None:
    """確認待ちを破棄する（キャンセル時）。"""
    with _LOCK, _interprocess_lock():
        doc = _load()
        if doc.pop(str(int(parent_id)), None) is not None:
            _save(doc)


# 操作名 → 親へ見せる日本語。破棄した確認を伝えるときに使う
_ACTION_LABELS = {
    "parent_grant": "支給",
    "parent_adjust_balance": "残高の調整",
    "bulk_grant": "全員への一括支給",
}


def describe_superseded(superseded: dict | None) -> str:
    """破棄した古い確認を親へ伝える一文を作る。

    Discord では古い確認文も画面に残るため、黙って捨てると
    親が上の確認に「はい」と答えたつもりで別の操作が走る。
    「前のは取り消した」と明示して、どれに答えているかを一意にする。

    Args:
        superseded: put_pending が返した、破棄された古い確認（無ければ None）。

    Returns:
        str: 親へ添える一文。破棄が無ければ空文字。
    """
    if not superseded:
        return ""
    action = str(superseded.get("action", ""))
    label = _ACTION_LABELS.get(action, action)
    args = superseded.get("args") or {}
    name = str(args.get("name", "")).strip()
    who = f"{name}への" if name else ""
    return f"※ さっきの「{who}{label}」の確認は取り消したよ。こっちに答えてね。\n\n"


def clear_all_pending() -> int:
    """確認待ちを全て破棄する（起動時に呼ぶ）。

    再起動をまたいだ確認は、親がもう覚えていないうえ、
    保存形式が変わっている可能性もある（旧形式に「はい」と答えると噛み合わない）。
    消しても親はコマンドを打ち直せばよいだけなので、安全側に倒す。

    Returns:
        int: 破棄した件数。
    """
    with _LOCK, _interprocess_lock():
        doc = _load()
        n = len(doc)
        if n:
            _save({})
    return n
