"""親の金額操作の確認（N-11.17 の Python 境界）の回帰テスト。

守るべき不変条件:
  1. **確認文は Python が組み立てる**。AI に文面を作らせない。
     AI が対象や金額を取り違えていても、親が見るのは構造化データから起こした文なので
     食い違いがそのまま目に入る。これが人による最終チェックになる。
  2. **一度使った確認は消える**。同じ確認で二重に実行されない。
  3. **猶予を過ぎた同意は無効**。別の操作への返事を誤って実行しない。
  4. **条件つきの返事は同意にしない**。「はい、でも3000円で」を通すと確認の意味が無い。
"""
import json
import sys
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app import parent_confirm as pc
from app.storage import JST

_results = []


def _check(name, passed, detail=""):
    _results.append({"test": name, "passed": bool(passed), "detail": str(detail)[:200]})


def _test_confirmation_text_is_built_by_python():
    """確認文に、対象・操作・金額がそのまま出ること（親が食い違いに気づける）。"""
    body = pc.build_confirmation("grant", "たろう", 5000, extra="テストがんばったから")
    _check("confirm_shows_child", "たろう" in body, body[:80])
    _check("confirm_shows_amount", "5,000円" in body, body[:120])
    _check("confirm_shows_action", "支給" in body, body[:120])
    _check("confirm_shows_extra", "テストがんばったから" in body, body[:160])
    _check("confirm_asks_yes_no", "はい" in body and "いいえ" in body, body[-80:])

    # 調整は符号つきで見せる（減らす操作だと親が分かるように）
    adj = pc.build_confirmation("adjust", "はな", -300)
    _check("confirm_adjust_shows_sign", "-300円" in adj, adj[:120])


def _test_reply_classification():
    """同意・拒否の判定。条件つきの返事は同意にしない。"""
    for text, want in [("はい", "yes"), ("OK", "yes"), ("お願い", "yes"),
                       ("いいえ", "no"), ("キャンセル", "no"), ("やめる", "no"),
                       ("はい、でも3000円で", "other"), ("うーん", "other"), ("", "other")]:
        got = pc.classify_reply(text)
        _check(f"reply[{text[:14] or 'empty'}]", got == want, got)


def _test_pending_lifecycle():
    """確認待ちが1回だけ使え、猶予切れは無効で、新しい確認が古いものを破棄すること。"""
    now = datetime.now(JST)

    # 取り出したら消える（二重実行の防止）
    pc.put_pending(9001, "grant", {"name": "たろう", "amount": 5000}, now)
    rec = pc.take_pending(9001, now)
    _check("pending_taken_once", rec is not None and rec["args"]["amount"] == 5000, rec)
    _check("pending_cleared_after_take", pc.take_pending(9001, now) is None)

    # 猶予を過ぎたら無効（別の操作への返事を誤実行しない）
    pc.put_pending(9002, "adjust", {"name": "はな", "delta": -300}, now)
    expired = pc.take_pending(9002, now + timedelta(seconds=pc.CONFIRM_WAIT_SEC + 1))
    _check("pending_expires", expired is None, expired)

    # 猶予内なら有効
    pc.put_pending(9003, "grant", {"name": "ゆい", "amount": 100}, now)
    alive = pc.take_pending(9003, now + timedelta(seconds=pc.CONFIRM_WAIT_SEC - 10))
    _check("pending_valid_within_window", alive is not None, alive)

    # 新しい確認は古いものを破棄する（どれへの返事か曖昧にしない）
    pc.put_pending(9004, "grant", {"name": "A", "amount": 100}, now)
    pc.put_pending(9004, "grant", {"name": "B", "amount": 200}, now)
    latest = pc.take_pending(9004, now)
    _check("pending_overwritten_by_new",
           latest is not None and latest["args"]["name"] == "B", latest)

    # キャンセルで消える
    pc.put_pending(9005, "grant", {"name": "はな", "amount": 100}, now)
    pc.clear_pending(9005)
    _check("pending_cleared_by_cancel", pc.take_pending(9005, now) is None)

    # 親ごとに独立している（他の親の確認を取り違えない）
    pc.put_pending(9006, "grant", {"name": "X", "amount": 1}, now)
    pc.put_pending(9007, "grant", {"name": "Y", "amount": 2}, now)
    a = pc.take_pending(9006, now)
    b = pc.take_pending(9007, now)
    _check("pending_is_per_parent",
           a["args"]["name"] == "X" and b["args"]["name"] == "Y", f"{a} {b}")


def main():
    _test_confirmation_text_is_built_by_python()
    _test_reply_classification()
    _test_pending_lifecycle()
    passed = sum(1 for x in _results if x["passed"])
    for x in _results:
        print(json.dumps(x, ensure_ascii=False))
    print(json.dumps({"summary": True, "passed": passed, "total": len(_results)},
                     ensure_ascii=False))
    return passed == len(_results)


if __name__ == "__main__":
    sys.exit(0 if main() else 1)
