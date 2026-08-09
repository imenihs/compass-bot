"""貯金の積立・立て替え返済（contribute_to_goal）の回帰テスト。

背景:
　貯金と立て替え返済は「ある数字が target_amount へ向かって増える」同じ構造なので、
　`savings_goals` に `kind`（saving / advance）を持たせて同じ関数で扱う（2026/08/10）。

守るべき不変条件:
  1. **残高の減算と accumulated の加算が1回で行われる**。
     別々に書くと「残高は減ったのに返済が記録されない（子が損する）」または
     「返済は記録されたのに残高が減らない（返済がタダになる）」が起きる。
  2. **同じ operation_key の再送では何も起きない**。applied=0 / closed=False を返す。
     closed に True を返すと**完済のお祝いが二度出る**。
  3. **過払いは残額へ丸める**。残り200円に500円返済なら200円だけ引いて完了。
     残高不足の判定も**丸めた後の額**で行う。
  4. 完了済みの目標へは積めない。
  5. **移行は3ケースに耐える**。この関数の前提となる savings_goals の形を保証する。
     移行は `_load_wallet_state`（全操作の入口）で走るため、
     ここで例外を出すと残高操作を含む wallet 全機能が停止する。
"""
import json
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

_results = []


def _check(name, passed, detail=""):
    _results.append({"test": name, "passed": bool(passed), "detail": str(detail)[:220]})


def _setup(tmp, goals=None, balance=10000):
    """本番データに触れないよう、残高もログも一時ディレクトリへ隔離する。"""
    (tmp / "logs").mkdir(exist_ok=True)
    json.dump(
        {"users": {"たろう": {"expected_balance": balance,
                            "savings_goals": goals if goals is not None else []}}},
        open(tmp / "wallet_state.json", "w"),
    )
    from app import config, wallet_service
    # get_log_dir は両モジュールが個別に持つため、片方だけ差し替えると本番へ書きに行く
    config.get_log_dir = lambda *a, **k: tmp / "logs"
    wallet_service.get_log_dir = lambda *a, **k: tmp / "logs"
    ws = wallet_service.WalletService()
    ws.wallet_state_path = tmp / "wallet_state.json"
    return ws, wallet_service


def _goal(gid, kind, title, target, accumulated=0, status="active"):
    return {"id": gid, "kind": kind, "title": title,
            "target_amount": target, "accumulated": accumulated, "status": status}


def _test_normal_contribution():
    """積立が残高と accumulated の両方へ、1回で反映されること。"""
    tmp = Path(tempfile.mkdtemp())
    try:
        ws, _ = _setup(tmp, [_goal(1, "saving", "パソコン", 150000)])
        conf = {"name": "たろう"}
        applied, balance, goal, closed = ws.contribute_to_goal(
            conf, {}, 1, 3000, "k1", aux_dedup_window_sec=120)
        _check("contribute_applies_amount", applied == 3000, applied)
        _check("contribute_reduces_balance", balance == 7000, balance)
        _check("contribute_increases_accumulated", goal["accumulated"] == 3000, goal)
        _check("contribute_not_closed_yet", closed is False, closed)
        # 保存後に読み直しても両方が反映されていること（＝1回の保存で確定している）
        _check("contribute_persisted_balance", ws.get_balance("たろう") == 7000,
               ws.get_balance("たろう"))
        saved = ws.get_savings_goals("たろう")
        _check("contribute_persisted_accumulated",
               saved and saved[0].get("accumulated") == 3000, saved)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _test_idempotent_resend():
    """同じ operation_key の再送では何も起きないこと。

    closed に True を返すと完済のお祝いが二度出るため、必ず False を返す。
    """
    tmp = Path(tempfile.mkdtemp())
    try:
        ws, _ = _setup(tmp, [_goal(1, "saving", "パソコン", 150000)])
        conf = {"name": "たろう"}
        ws.contribute_to_goal(conf, {}, 1, 3000, "same-key", aux_dedup_window_sec=120)
        applied, balance, goal, closed = ws.contribute_to_goal(
            conf, {}, 1, 3000, "same-key", aux_dedup_window_sec=120)
        _check("resend_applies_nothing", applied == 0, applied)
        _check("resend_keeps_balance", balance == 7000, balance)
        _check("resend_keeps_accumulated", goal["accumulated"] == 3000, goal)
        _check("resend_not_closed", closed is False, closed)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _test_overpay_is_rounded():
    """過払いは残額へ丸められ、その額で完了すること。"""
    tmp = Path(tempfile.mkdtemp())
    try:
        ws, _ = _setup(tmp, [_goal(2, "advance", "パソコン代", 3000, accumulated=2800)])
        conf = {"name": "たろう"}
        applied, balance, goal, closed = ws.contribute_to_goal(
            conf, {}, 2, 500, "k2", aux_dedup_window_sec=120)
        _check("overpay_rounded_to_remaining", applied == 200, applied)
        _check("overpay_balance_by_applied", balance == 9800, balance)
        _check("overpay_reaches_target", goal["accumulated"] == 3000, goal)
        _check("overpay_marks_done", goal["status"] == "done", goal)
        _check("overpay_reports_closed", closed is True, closed)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _test_rejections():
    """積めない条件で正しく拒否されること。"""
    tmp = Path(tempfile.mkdtemp())
    try:
        ws, wallet_service = _setup(
            tmp, [_goal(1, "saving", "パソコン", 150000),
                  _goal(2, "advance", "終わった分", 1000, accumulated=1000, status="done")],
            balance=500)
        conf = {"name": "たろう"}

        # 完了済みには積めない
        try:
            ws.contribute_to_goal(conf, {}, 2, 100, "r1", aux_dedup_window_sec=120)
            _check("reject_done_goal", False, "例外が出なかった")
        except ValueError as e:
            _check("reject_done_goal", True, str(e))

        # 存在しない目標
        try:
            ws.contribute_to_goal(conf, {}, 999, 100, "r2", aux_dedup_window_sec=120)
            _check("reject_missing_goal", False, "例外が出なかった")
        except ValueError as e:
            _check("reject_missing_goal", True, str(e))

        # operation_key が空
        try:
            ws.contribute_to_goal(conf, {}, 1, 100, "", aux_dedup_window_sec=120)
            _check("reject_empty_op_key", False, "例外が出なかった")
        except ValueError as e:
            _check("reject_empty_op_key", True, str(e))

        # 残高不足（丸めた後の額で判定される）
        before = ws.get_balance("たろう")
        try:
            ws.contribute_to_goal(conf, {}, 1, 999999, "r3", aux_dedup_window_sec=120)
            _check("reject_insufficient_balance", False, "例外が出なかった")
        except wallet_service._PrecheckRejected as e:
            _check("reject_insufficient_balance", True, str(e))
        _check("reject_keeps_balance", ws.get_balance("たろう") == before,
               ws.get_balance("たろう"))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _test_ledger_record():
    """台帳に、親が見て何の操作か分かる形で残ること。"""
    tmp = Path(tempfile.mkdtemp())
    try:
        ws, _ = _setup(tmp, [_goal(1, "saving", "パソコン", 150000),
                             _goal(2, "advance", "パソコン代", 30000)])
        conf = {"name": "たろう"}
        ws.contribute_to_goal(conf, {}, 1, 1000, "L1", aux_dedup_window_sec=120)
        ws.contribute_to_goal(conf, {}, 2, 500, "L2", aux_dedup_window_sec=120)

        rows = [json.loads(x) for x in
                open(tmp / "logs" / "たろう_wallet_ledger.jsonl", encoding="utf-8")]
        _check("ledger_has_two_rows", len(rows) == 2, len(rows))
        # kind によって action を分ける（貯金と返済を親が見分けられるように）
        _check("ledger_saving_action",
               rows[0]["action"] == "goal_contribution", rows[0].get("action"))
        _check("ledger_advance_action",
               rows[1]["action"] == "advance_repayment", rows[1].get("action"))
        _check("ledger_delta_is_negative",
               rows[0]["delta"] == -1000 and rows[1]["delta"] == -500,
               [r["delta"] for r in rows])
        _check("ledger_has_goal_id",
               rows[0]["extra"]["goal_id"] == 1 and rows[1]["extra"]["goal_id"] == 2,
               [r["extra"] for r in rows])
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _test_migration_three_cases():
    """移行が3ケースすべてに耐えること。

    移行は `_load_wallet_state`（全操作の入口）で走るため、
    ここで例外を出すと**残高操作を含む wallet 全機能が停止する**。
    """
    from app.wallet_service import WalletService

    state = {"users": {
        # ケース1: キー自体が無い（実データの4人中3人がこれ）
        "キー無し": {"expected_balance": 1000},
        # ケース2: リストだが kind/accumulated が無い（旧形式）
        "旧形式": {"expected_balance": 2000,
                 "savings_goals": [{"id": 1, "title": "パソコン", "target_amount": 150000}]},
        # ケース3: リストでない（既存コードが3箇所で isinstance ガードしている＝過去に踏んだ形）
        "None": {"expected_balance": 3000, "savings_goals": None},
        "文字列": {"expected_balance": 4000, "savings_goals": "こわれた"},
        # 旧単数キー（既存の移行対象）
        "旧単数": {"expected_balance": 5000,
                 "savings_goal": {"title": "自転車", "target_amount": 30000}},
    }}

    try:
        WalletService._migrate_savings_goals_if_needed(state)
        crashed = False
    except Exception as exc:  # noqa: BLE001 - 例外が出ないことの確認が目的
        crashed = True
        _check("migration_no_crash", False, f"{type(exc).__name__}: {exc}")
    if crashed:
        return
    _check("migration_no_crash", True, "")

    users = state["users"]
    _check("migration_missing_key_to_list",
           users["キー無し"]["savings_goals"] == [], users["キー無し"])
    _check("migration_none_to_list",
           users["None"]["savings_goals"] == [], users["None"])
    _check("migration_string_to_list",
           users["文字列"]["savings_goals"] == [], users["文字列"])

    old = users["旧形式"]["savings_goals"][0]
    _check("migration_fills_kind", old.get("kind") == "saving", old)
    _check("migration_fills_accumulated", old.get("accumulated") == 0, old)
    _check("migration_fills_status", old.get("status") == "active", old)
    _check("migration_keeps_existing_fields",
           old.get("title") == "パソコン" and old.get("target_amount") == 150000, old)

    single = users["旧単数"]["savings_goals"]
    _check("migration_legacy_single_key",
           len(single) == 1 and single[0].get("kind") == "saving", single)


def _test_tool_layer():
    """tool 層（子がチャットから使う経路）が既存の作法をすべて守ること。

    作法を1つでも落とすと穴になる:
      _resolve_child(別の子を弾く) / _parse_amount / operation_key必須 /
      _scoped_op_key(子ごとの名前空間) / _natural_dup_key(言い直し) / 残高不足の拒否
    """
    import os

    tmp = Path(tempfile.mkdtemp())
    try:
        (tmp / "logs").mkdir(exist_ok=True)
        json.dump({"users": {"たろう": {"expected_balance": 10000, "savings_goals": [
            _goal(1, "saving", "パソコン", 150000),
            _goal(2, "advance", "パソコン代", 3000, accumulated=2800),
        ]}}}, open(tmp / "wallet_state.json", "w"))

        from app import config, wallet_service
        config.get_log_dir = lambda *a, **k: tmp / "logs"
        wallet_service.get_log_dir = lambda *a, **k: tmp / "logs"
        from app import mcp_wallet as m
        m._wallet.wallet_state_path = tmp / "wallet_state.json"
        m.ACTIVE_CHILD = "たろう"
        m._resolve_child = lambda n=None: {"name": "たろう"}
        m._system_conf = lambda: {}

        r = m._do_contribute_to_goal(
            {"name": "たろう", "goal_id": 1, "amount": 3000, "operation_key": "c1"})
        _check("tool_contributes", "3000円貯めた" in r, r)
        _check("tool_shows_progress", "3000/150000" in r, r)

        # 冪等再送: 残高を動かさず、完済の祝いも出さない
        r = m._do_contribute_to_goal(
            {"name": "たろう", "goal_id": 1, "amount": 3000, "operation_key": "c1"})
        _check("tool_idempotent", "さっき記録した" in r, r)
        _check("tool_idempotent_balance", m._wallet.get_balance("たろう") == 7000,
               m._wallet.get_balance("たろう"))

        # 過払いは丸めて完済し、差額を子へ伝える
        r = m._do_contribute_to_goal(
            {"name": "たろう", "goal_id": 2, "amount": 500, "operation_key": "c2"})
        _check("tool_overpay_closes", "返しきった" in r, r)
        _check("tool_overpay_explains", "のこりは 200円" in r, r)

        # operation_key 無しは拒否（再送で二重に引かれるのを防ぐ）
        r = m._do_contribute_to_goal({"name": "たろう", "goal_id": 1, "amount": 100})
        _check("tool_requires_op_key", "うまくできなかった" in r, r)

        # goal_id 無しは聞き返す材料を返す
        r = m._do_contribute_to_goal(
            {"name": "たろう", "amount": 100, "operation_key": "c9"})
        _check("tool_requires_goal_id", "どの目標か" in r, r)

        # 存在しない目標・完済済み・残高不足
        r = m._do_contribute_to_goal(
            {"name": "たろう", "goal_id": 99, "amount": 100, "operation_key": "c10"})
        _check("tool_rejects_missing_goal", "見つからなかった" in r, r)
        r = m._do_contribute_to_goal(
            {"name": "たろう", "goal_id": 2, "amount": 100, "operation_key": "c11"})
        _check("tool_rejects_done_goal", "もう終わっている" in r, r)
        before = m._wallet.get_balance("たろう")
        r = m._do_contribute_to_goal(
            {"name": "たろう", "goal_id": 1, "amount": 999999, "operation_key": "c12"})
        _check("tool_rejects_insufficient", "足りない" in r, r)
        _check("tool_rejects_keeps_balance",
               m._wallet.get_balance("たろう") == before, m._wallet.get_balance("たろう"))

        # 一覧は accumulated ベースで出す（総残高ではない）
        listing = m._do_get_savings_goals({"name": "たろう"})
        _check("listing_uses_accumulated", "3000/150000" in listing, listing)
        _check("listing_marks_done", "達成ずみ" in listing, listing)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def main():
    _test_normal_contribution()
    _test_idempotent_resend()
    _test_overpay_is_rounded()
    _test_rejections()
    _test_ledger_record()
    _test_migration_three_cases()
    _test_tool_layer()

    passed = sum(1 for x in _results if x["passed"])
    for x in _results:
        print(json.dumps(x, ensure_ascii=False))
    print(json.dumps({"summary": True, "passed": passed, "total": len(_results)},
                     ensure_ascii=False))
    return passed == len(_results)


if __name__ == "__main__":
    sys.exit(0 if main() else 1)
