"""査定の承認・却下が、子への F/B（opener 生成入力）として届くことの回帰テスト。

背景（なぜこのテストが要るか）:
　2026/08/07 の実ログで、親が査定を却下したあと「伝えて」と頼んだのに、
　AI が「子に文章を届ける手段がない」と答えていた。当時は本当に無かった。
　その後 2026/08/08 に査定 F/B 基盤（parent_intent を子へ渡す配線）が入って解消したが、
　**この経路を検証するテストが1件も無かった**。
　直っていることをテストで固定し、次に壊れたら気づけるようにする。

守るべき不変条件:
  1. 承認・却下のどちらでも、子への F/B が積まれる（親の判断が子に届く）。
  2. `parent_intent`（親の意図を第三者視点へ翻訳したもの）が渡る。
  3. **生の親の一言（raw_note）を opener の材料として使わない**。
     親の言い方はきつい場合があり、そのまま子へ出すとコーチングにならない。
     raw_note は記録として持つが、子へ渡す入口は parent_intent に絞る。
  4. 同じ子に複数積まれたら、**最新の proposal_id の1件だけ**を返す
     （再提案→承認/却下が連続したとき、古い結果の opener を出さない）。
  5. 取り出したらキューは空になる（同じ F/B を二度送らない）。
"""
import json
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

_results = []


def _check(name, passed, detail=""):
    _results.append({"test": name, "passed": bool(passed), "detail": str(detail)[:220]})


def _setup(tmp):
    """本番データに触れないよう、残高も payout store も一時ディレクトリへ隔離する。"""
    (tmp / "logs").mkdir(exist_ok=True)
    json.dump({"users": {"たろう": {"expected_balance": 1000}}},
              open(tmp / "wallet_state.json", "w"))

    os.environ["COMPASS_PARENT_MODE"] = "1"
    os.environ["COMPASS_ALLOW_ADMIN_OPS"] = "1"

    from app import config, wallet_service
    # get_log_dir は両モジュールが個別に持つため、片方だけ差し替えると本番へ書きに行く
    config.get_log_dir = lambda *a, **k: tmp / "logs"
    wallet_service.get_log_dir = lambda *a, **k: tmp / "logs"

    from app import mcp_wallet as m
    m.PARENT_MODE = True
    m.ALLOW_ADMIN_OPS = True
    m._wallet.wallet_state_path = tmp / "wallet_state.json"

    # payout store も隔離する（査定の申請・承認はここに載る）。
    # _payout_store() は**呼ぶたびに新しいインスタンスを返す**ため、
    # 返り値のパスだけ差し替えても、承認/却下の中で作り直された別インスタンスは
    # 本番パスを見てしまう。ファクトリごと差し替えて、常に同じ隔離 store を返す。
    _store = m._payout_store()
    _store.payout_requests_path = tmp / "payout_requests.json"
    m._payout_store = lambda: _store
    m._resolve_parent_target = lambda n: {"name": "たろう"} if n == "たろう" else None
    # 承認は残高を動かすため、tool 層とは別に config.find_child_user_by_name で
    # ユーザー設定の実在を確認する。テスト用の子を返すよう差し替える
    config.find_child_user_by_name = lambda n: (
        {"name": "たろう", "fixed_allowance": 500} if n == "たろう" else None)
    return m, _store


def _put_request(m, store, proposal_id, total):
    """査定の申請を直接置く。

    子の propose_allowance はガードレール（4層）で自動的に減額・却下されるため、
    F/B 経路の検証には向かない。ここで見たいのは
    「親が承認/却下したとき子へ F/B が積まれるか」なので、申請は直接作る。
    """
    with m._payout_locked():
        doc = store._load_doc(store.payout_requests_path, "requests")
        doc.setdefault("requests", {})["たろう"] = {
            # status が "pending" でないと承認/却下は「承認待ちの査定は無い」で止まる
            "status": "pending",
            "proposal_id": proposal_id, "total": total,
            "fixed": 0, "temporary": total, "reason": "テスト用の申請",
        }
        store._save_doc(store.payout_requests_path, doc, "requests")


def _test_reject_delivers_intent(m, store):
    """却下したとき、親の意図が子への F/B として積まれること。"""
    _put_request(m, store, "p001", 1000)
    intent = "金額ではなく、お手伝いで返す約束の中身が決まっていないから見送った"
    raw = "こんな雑な計画で金は出せん"

    m._do_parent_reject_assessment({
        "name": "たろう", "expected_proposal_id": "p001",
        "parent_intent": intent, "note": raw,
    })
    queue = m.take_pending_feedback()

    _check("reject_enqueues_feedback", len(queue) == 1, queue)
    if not queue:
        return
    entry = queue[0]
    _check("reject_kind_is_reject", entry.get("kind") == "reject", entry.get("kind"))
    _check("reject_targets_child", entry.get("name") == "たろう", entry.get("name"))
    _check("reject_carries_parent_intent", entry.get("parent_intent") == intent,
           entry.get("parent_intent"))
    # AI 経路（_do_parent_reject_assessment）は **生の親の一言を tool へ渡さない**。
    # 親の言い方はきつい場合があり、そのまま子へ届くとコーチングにならないため、
    # 翻訳済みの parent_intent だけを入口にする設計。
    # したがって raw_note は空であることが正しい（生の言葉がキューに載らない）。
    _check("reject_does_not_carry_raw_words",
           not str(entry.get("raw_note", "")).strip()
           and raw not in str(entry.get("parent_intent", "")),
           {"raw": entry.get("raw_note"), "intent": entry.get("parent_intent")})


def _test_approve_delivers_intent(m, store):
    """承認したときも、なぜ OK かの入口が子へ積まれること。"""
    _put_request(m, store, "p002", 300)
    intent = "使い道をきちんと説明できたから認めた"

    # 承認は operation_key 必須（二重支給を防ぐ冪等キー）。無いと手前で止まる
    m._do_parent_approve_assessment({
        "name": "たろう", "expected_proposal_id": "p002",
        "operation_key": "test-approve-p002",
        "parent_intent": intent, "note": "まあいいだろう",
    })
    queue = m.take_pending_feedback()

    _check("approve_enqueues_feedback", len(queue) == 1, queue)
    if not queue:
        return
    entry = queue[0]
    _check("approve_kind_is_approve", entry.get("kind") == "approve", entry.get("kind"))
    _check("approve_carries_parent_intent", entry.get("parent_intent") == intent,
           entry.get("parent_intent"))


def _test_only_latest_is_delivered(m, store):
    """同じ子に複数積まれたら、最新の1件だけ返すこと。

    再提案→承認/却下が続いたとき、古い結果の opener を子へ出すと混乱する。
    """
    _put_request(m, store, "p010", 500)
    m._do_parent_reject_assessment({
        "name": "たろう", "expected_proposal_id": "p010",
        "parent_intent": "ひとつ目の見送り", "note": "",
    })
    _put_request(m, store, "p011", 700)
    m._do_parent_reject_assessment({
        "name": "たろう", "expected_proposal_id": "p011",
        "parent_intent": "ふたつ目の見送り", "note": "",
    })

    queue = m.take_pending_feedback()
    _check("latest_only_one_entry", len(queue) == 1, queue)
    if queue:
        _check("latest_is_newest_proposal", queue[0].get("proposal_id") == "p011",
               queue[0].get("proposal_id"))


def _test_queue_is_emptied(m, store):
    """取り出したらキューが空になること（同じ F/B を二度送らない）。"""
    _put_request(m, store, "p020", 200)
    m._do_parent_reject_assessment({
        "name": "たろう", "expected_proposal_id": "p020",
        "parent_intent": "見送り", "note": "",
    })
    first = m.take_pending_feedback()
    second = m.take_pending_feedback()
    _check("queue_returns_once", len(first) == 1, first)
    _check("queue_empty_after_take", second == [], second)


def main():
    tmp = Path(tempfile.mkdtemp())
    try:
        m, store = _setup(tmp)
        # 前のテストの残りを持ち越さない
        m.take_pending_feedback()
        _test_reject_delivers_intent(m, store)
        _test_approve_delivers_intent(m, store)
        _test_only_latest_is_delivered(m, store)
        _test_queue_is_emptied(m, store)
    finally:
        import shutil
        shutil.rmtree(tmp, ignore_errors=True)

    passed = sum(1 for x in _results if x["passed"])
    for x in _results:
        print(json.dumps(x, ensure_ascii=False))
    print(json.dumps({"summary": True, "passed": passed, "total": len(_results)},
                     ensure_ascii=False))
    return passed == len(_results)


if __name__ == "__main__":
    sys.exit(0 if main() else 1)
