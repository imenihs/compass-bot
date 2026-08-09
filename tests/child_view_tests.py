"""子ども向けダッシュボードの表示データの回帰テスト。

守るべき不変条件:
  1. **台帳の運用メモを子に見せない**。
     note には「親攻撃テスト復元」「restore_after_discord_id_amount_misparse」
     「gemini_assessed_total」のような開発者向けの文言が実在する。
  2. **extra を返さない**。Discord の実 ID が入っている行が実在する。
  3. **未知の action も内部用語を出さない**（安全側のラベルへ倒す）。
  4. 進捗は accumulated / target_amount で出す。総残高で出すと
     目標が2つあるとき同じお金が両方に計上される。
  5. 過去との比較は **goal_contribution / advance_repayment の合計**で見る。
     残高は支出でも減るため、頑張ったかどうかと連動しない。
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


def _write_ledger(tmp, name, rows):
    """台帳を一時ディレクトリへ作る（本番へは触れない）。"""
    path = tmp / f"{name}_wallet_ledger.jsonl"
    with open(path, "w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    return path


def _test_operational_notes_are_hidden():
    """運用メモ・内部用語・Discord ID が子の画面に出ないこと。"""
    from app import child_view as cv

    dangerous = [
        {"ts": "2026-08-03T10:00:00+09:00", "action": "balance_adjustment",
         "delta": -2700, "note": "検証で誤って動かした分の復元(parent_grant上限テスト・N-11.17)",
         "extra": {"adjusted_by": "111"}},
        {"ts": "2026-08-03T11:00:00+09:00", "action": "balance_adjustment",
         "delta": 100, "note": "親攻撃テスト復元"},
        {"ts": "2026-08-03T12:00:00+09:00", "action": "data_recovery",
         "delta": 0, "note": "restore_after_discord_id_amount_misparse"},
        {"ts": "2026-08-03T13:00:00+09:00", "action": "allowance_grant",
         "delta": 1000, "note": "gemini_assessed_total"},
    ]
    for row in dangerous:
        projected = cv.project_entry(row)
        raw_note = str(row.get("note", ""))
        _check(f"hides_note[{raw_note[:18]}]",
               projected["note"] == "" or projected["note"] != raw_note, projected)
        _check(f"no_extra[{raw_note[:18]}]", "extra" not in projected, projected)
        # 内部用語がラベルにも出ないこと
        _check(f"label_is_friendly[{raw_note[:18]}]",
               "_" not in projected["label"] and projected["label"], projected["label"])


def _test_child_notes_are_shown():
    """子自身の発話由来の note は見せること（何を買ったか等）。"""
    from app import child_view as cv

    shown = cv.project_entry({
        "ts": "2026-08-05T10:00:00+09:00", "action": "spending_record",
        "delta": -300, "note": "おかし"})
    _check("shows_own_item", shown["note"] == "おかし", shown)
    _check("shows_spend_label", shown["label"] == "つかった", shown)

    goal = cv.project_entry({
        "ts": "2026-08-05T11:00:00+09:00", "action": "advance_repayment",
        "delta": -500, "note": "パソコン代"})
    _check("shows_goal_title", goal["note"] == "パソコン代", goal)
    _check("shows_repay_label", goal["label"] == "かえした", goal)


def _test_unknown_action_falls_back():
    """未知の action でも内部用語を出さないこと。"""
    from app import child_view as cv

    projected = cv.project_entry({
        "ts": "2026-08-05T10:00:00+09:00", "action": "some_internal_action",
        "delta": -100, "note": "内部メモ"})
    _check("unknown_action_fallback", projected["label"] == "お金がうごいたよ", projected)
    _check("unknown_action_hides_note", projected["note"] == "", projected)


def _test_calendar_aggregates_by_day():
    """カレンダーが日ごとに入出金を合計すること。"""
    from app import child_view as cv

    tmp = Path(tempfile.mkdtemp())
    try:
        _write_ledger(tmp, "たろう", [
            {"ts": "2026-08-06T10:00:00+09:00", "action": "allowance_grant",
             "delta": 500, "note": ""},
            {"ts": "2026-08-06T18:00:00+09:00", "action": "spending_record",
             "delta": -200, "note": "おかし"},
            {"ts": "2026-08-07T10:00:00+09:00", "action": "spending_record",
             "delta": -300, "note": "ジュース"},
            # 別の月は混ざらない
            {"ts": "2026-07-06T10:00:00+09:00", "action": "spending_record",
             "delta": -999, "note": "先月分"},
        ])
        cal = cv.build_calendar(tmp, "たろう", 2026, 8)
        _check("calendar_month_filtered", set(cal["days"].keys()) == {6, 7}, cal["days"].keys())
        _check("calendar_sums_in", cal["days"][6]["in"] == 500, cal["days"][6])
        _check("calendar_sums_out", cal["days"][6]["out"] == 200, cal["days"][6])
        _check("calendar_day7", cal["days"][7]["out"] == 300, cal["days"][7])
        _check("calendar_has_entries", len(cal["days"][6]["entries"]) == 2,
               cal["days"][6]["entries"])
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _test_calendar_survives_broken_rows():
    """壊れた行があっても画面を落とさないこと。"""
    from app import child_view as cv

    tmp = Path(tempfile.mkdtemp())
    try:
        path = tmp / "たろう_wallet_ledger.jsonl"
        path.write_text(
            '{"ts": "2026-08-06T10:00:00+09:00", "action": "allowance_grant", "delta": 500}\n'
            'これはJSONではない\n'
            '{"ts": "こわれた日付", "action": "spending_record", "delta": -100}\n'
            '{"ts": "2026-08-06T12:00:00+09:00", "action": "spending_record", "delta": "文字"}\n',
            encoding="utf-8")
        try:
            cal = cv.build_calendar(tmp, "たろう", 2026, 8)
            crashed = False
        except Exception as exc:  # noqa: BLE001 - 落ちないことの確認が目的
            crashed = True
            cal = f"{type(exc).__name__}: {exc}"
        _check("calendar_no_crash_on_broken", not crashed, cal)
        if not crashed:
            _check("calendar_keeps_valid_rows", cal["days"].get(6, {}).get("in") == 500,
                   cal["days"])
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _test_goals_use_accumulated():
    """進捗が accumulated ベースで出ること（総残高ではない）。"""
    from app import child_view as cv

    goals = cv.build_goals([
        {"id": 1, "kind": "saving", "title": "パソコン",
         "target_amount": 150000, "accumulated": 30000, "status": "active"},
        {"id": 2, "kind": "advance", "title": "パソコン代",
         "target_amount": 3000, "accumulated": 3000, "status": "done"},
    ])
    _check("goal_pct_from_accumulated", goals[0]["pct"] == 20, goals[0])
    _check("goal_remaining", goals[0]["remaining"] == 120000, goals[0])
    _check("goal_saving_verb", goals[0]["verb"] == "ためた", goals[0])
    _check("goal_advance_verb", goals[1]["verb"] == "かえした", goals[1])
    _check("goal_done_status", goals[1]["status"] == "done", goals[1])
    _check("goal_pct_capped", goals[1]["pct"] == 100, goals[1])

    # 壊れたデータでも落ちない
    broken = cv.build_goals([None, "ごみ", {"target_amount": "×"}])
    _check("goals_survive_broken", isinstance(broken, list), broken)


def _test_history_counts_only_contributions():
    """過去との比較が、積立・返済の合計で出ること。

    残高は支出でも減るため、総残高で見ると「頑張ったか」と連動しない。
    """
    from app import child_view as cv

    tmp = Path(tempfile.mkdtemp())
    try:
        _write_ledger(tmp, "たろう", [
            {"ts": "2026-07-05T10:00:00+09:00", "action": "goal_contribution",
             "delta": -1000, "note": "パソコン"},
            {"ts": "2026-08-05T10:00:00+09:00", "action": "goal_contribution",
             "delta": -2000, "note": "パソコン"},
            {"ts": "2026-08-06T10:00:00+09:00", "action": "advance_repayment",
             "delta": -500, "note": "パソコン代"},
            # 支出・入金は比較に含めない
            {"ts": "2026-08-07T10:00:00+09:00", "action": "spending_record",
             "delta": -9999, "note": "おかし"},
            {"ts": "2026-08-08T10:00:00+09:00", "action": "allowance_grant",
             "delta": 5000, "note": ""},
        ])
        history = cv.monthly_saved_history(tmp, "たろう", months=6)
        by_month = {h["ym"]: h["amount"] for h in history}
        _check("history_july", by_month.get("2026-07") == 1000, by_month)
        _check("history_august", by_month.get("2026-08") == 2500, by_month)
        _check("history_excludes_spending", 9999 not in by_month.values(), by_month)
        _check("history_is_ordered",
               [h["ym"] for h in history] == sorted(h["ym"] for h in history), history)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def main():
    _test_operational_notes_are_hidden()
    _test_child_notes_are_shown()
    _test_unknown_action_falls_back()
    _test_calendar_aggregates_by_day()
    _test_calendar_survives_broken_rows()
    _test_goals_use_accumulated()
    _test_history_counts_only_contributions()

    passed = sum(1 for x in _results if x["passed"])
    for x in _results:
        print(json.dumps(x, ensure_ascii=False))
    print(json.dumps({"summary": True, "passed": passed, "total": len(_results)},
                     ensure_ascii=False))
    return passed == len(_results)


if __name__ == "__main__":
    sys.exit(0 if main() else 1)
