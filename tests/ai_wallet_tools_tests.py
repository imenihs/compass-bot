"""Phase N-11 AI 主導層の wallet tool ガードレールを検証する決定的テスト。

claude CLI を起動せず、mcp_wallet の tool 関数を直接呼んで金額処理の安全性を確かめる。
実残高を扱う核のため、正常系だけでなく「通ってはいけない入力」を必ず試す:
  - 子ども本人性の束縛（別の子の越境を拒否）
  - operation_key の冪等（二重適用しない）
  - 査定4層ガードレール（固定・臨時・月次累計・日次回数の各上限）
  - 親承認フロー（提案は残高を動かさない／承認で支給／却下で不変／二重承認しない）

隔離環境（一時ディレクトリ）で実データに触れない。結果は1行1 JSON で出力し、集計する。
"""
import json
import shutil
import os
import sys
import tempfile
from pathlib import Path

# リポジトリ直下を import パスに含める
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import config


def _setup(tmp: Path) -> None:
    """config のパスと WalletService/payout の保存先を隔離ディレクトリへ向ける。"""
    (tmp / "settings" / "users" / "children").mkdir(parents=True, exist_ok=True)
    (tmp / "data").mkdir(parents=True, exist_ok=True)
    # 子ども1人（たろう、固定増額上限100円）と、別の子（はな）を用意する
    (tmp / "settings" / "users" / "children" / "tarou.json").write_text(
        json.dumps({"name": "たろう", "age": 10, "discord_user_id": 111, "fixed_increase_cap": 100}, ensure_ascii=False),
        encoding="utf-8",
    )
    (tmp / "settings" / "users" / "children" / "hana.json").write_text(
        json.dumps({"name": "はな", "age": 8, "discord_user_id": 222, "fixed_increase_cap": 100}, ensure_ascii=False),
        encoding="utf-8",
    )
    # 親も置く（本人性束縛で親名が拒否されることの確認用）
    (tmp / "settings" / "users" / "parents").mkdir(parents=True, exist_ok=True)
    (tmp / "settings" / "users" / "parents" / "chichi.json").write_text(
        json.dumps({"name": "とうちゃん", "discord_user_id": 999}, ensure_ascii=False), encoding="utf-8",
    )
    setting = {
        "assessment_guardrail": {"temporary_max": 1000, "monthly_total_max": 3000, "daily_count_max": 3},
        "child_income_report": {"max_amount": 5000},
    }
    (tmp / "settings" / "setting.json").write_text(json.dumps(setting, ensure_ascii=False), encoding="utf-8")
    (tmp / "settings" / "system.json").write_text(json.dumps({"log_dir": str(tmp / "data")}, ensure_ascii=False), encoding="utf-8")
    (tmp / "data" / "wallet_state.json").write_text(
        json.dumps({"users": {"たろう": {"expected_balance": 1000}, "はな": {"expected_balance": 2000}}, "applied_operation_keys": {}}, ensure_ascii=False),
        encoding="utf-8",
    )
    # config のパスを差し替える
    config.SETTINGS_DIR = tmp / "settings"
    config.USERS_DIR = config.SETTINGS_DIR / "users"
    config.CHILDREN_DIR = config.USERS_DIR / "children"
    config.PARENTS_DIR = config.USERS_DIR / "parents"
    config.SYSTEM_PATH = config.SETTINGS_DIR / "system.json"
    config.SETTING_PATH = config.SETTINGS_DIR / "setting.json"


def _wallet_and_tools(tmp: Path):
    """隔離先を見る WalletService を作り、mcp_wallet に差し込んで返す。"""
    import app.wallet_service as ws
    from app import mcp_wallet
    from app.conv.session import SessionStore
    w = ws.WalletService()
    w.wallet_state_path = tmp / "data" / "wallet_state.json"
    w.wallet_audit_state_path = tmp / "data" / "wallet_audit_state.json"
    mcp_wallet._wallet = w
    # payout も隔離先へ
    mcp_wallet._payout_store = lambda: SessionStore(data_dir=tmp / "data")
    return w, mcp_wallet


# テスト結果を貯める
_results: list[dict] = []


def _check(name: str, passed: bool, detail: str = "") -> None:
    _results.append({"name": name, "passed": bool(passed), "detail": detail})


def _test_expense_writes_pocket_journal():
    """record_expense が **お小遣い帳にも** 記録すること（2026/08/10 の回帰）。

    文字列一致ハンドラを intent 方式へ移した際に journal への書き込みが失われ、
    **2〜5か月ぶん記録が空**になっていた。その結果:
      ・親ダッシュボードの「今月支出」が常に 0円（0件）
      ・「最終支出日」が常に「—」
      ・支出傾向分析が「記録なし」
      ・learning_insights の会話カード（満足度を軸に判定）が出ない
    残高（wallet_ledger）だけは正しく動いていたため、
    「お金は減っているのに支出0円」という食い違いが画面に出ていた。
    """
    import datetime
    import tempfile

    from app import config, mcp_wallet as m
    import app.wallet_service as ws

    tmp = Path(tempfile.mkdtemp())
    (tmp / "logs").mkdir()
    (tmp / "settings").mkdir()
    (tmp / "settings" / "system.json").write_text(
        json.dumps({"log_dir": str(tmp / "logs")}), encoding="utf-8")
    (tmp / "settings" / "setting.json").write_text("{}", encoding="utf-8")
    state = tmp / "wallet_state.json"
    state.write_text(json.dumps(
        {"users": {"たろう": {"expected_balance": 3000}}, "applied_operation_keys": {}},
        ensure_ascii=False), encoding="utf-8")

    orig = (config.SYSTEM_PATH, config.SETTING_PATH, m._wallet, m._resolve_child)
    config.SYSTEM_PATH = tmp / "settings" / "system.json"
    config.SETTING_PATH = tmp / "settings" / "setting.json"
    w = ws.WalletService()
    w.wallet_state_path = state
    w.wallet_audit_state_path = tmp / "audit.json"
    m._wallet = w
    m._resolve_child = lambda n=None: {"name": "たろう"}
    try:
        m._do_record_expense({"name": "たろう", "amount": 300,
                              "item": "おかし", "operation_key": "k1"})

        journal = tmp / "logs" / "たろう_pocket_journal.jsonl"
        _check("journal_file_created", journal.exists(), str(journal))
        if not journal.exists():
            return
        rows = [json.loads(x) for x in journal.read_text(encoding="utf-8").splitlines() if x.strip()]
        _check("journal_has_one_row", len(rows) == 1, len(rows))
        row = rows[0] if rows else {}
        _check("journal_amount", row.get("amount") == 300, row.get("amount"))
        _check("journal_item", row.get("item") == "おかし", row.get("item"))
        # 満足度は会話の中で後から supplement される想定なので、この時点では空でよい
        _check("journal_satisfaction_empty", row.get("satisfaction") is None, row.get("satisfaction"))
        # 実 Discord ID をログへ増やさない（規約）
        _check("journal_has_no_discord_id", "discord_user_id" not in row, sorted(row.keys()))

        # ダッシュボードの当月集計が 0 にならないこと（これが表に出ていた症状）
        month = datetime.datetime.now().strftime("%Y-%m")
        total = sum(int(r.get("amount") or 0) for r in rows
                    if str(r.get("ts", "")).startswith(month))
        _check("dashboard_month_spending_not_zero", total == 300, total)

        # 残高側（ledger）は従来どおり動く
        _check("balance_still_moves", w.get_balance("たろう") == 2700, w.get_balance("たろう"))
    finally:
        config.SYSTEM_PATH, config.SETTING_PATH, m._wallet, m._resolve_child = orig
        shutil.rmtree(tmp, ignore_errors=True)


def _test_wallet_check_clears_pending():
    """財布チェックが「報告済み」にすること（2026/08/11 の回帰）。

    `pending_by_user` は reminder_service が毎月積むが、**消す経路が失われていた**。
    財布チェックを受け取る実装が文字列一致ハンドラの中にあり、
    intent 方式への移行で到達不能になったあと削除されたため。
    結果、実児3人が全員「未報告」のまま固定され、何をしても変わらなかった。
    """
    import tempfile

    from app import config, mcp_wallet as m
    import app.wallet_service as ws

    tmp = Path(tempfile.mkdtemp())
    (tmp / "logs").mkdir()
    (tmp / "settings").mkdir()
    (tmp / "settings" / "system.json").write_text(
        json.dumps({"log_dir": str(tmp / "logs")}), encoding="utf-8")
    (tmp / "settings" / "setting.json").write_text("{}", encoding="utf-8")
    (tmp / "wallet.json").write_text(json.dumps(
        {"users": {"たろう": {"expected_balance": 3000}}, "applied_operation_keys": {}},
        ensure_ascii=False), encoding="utf-8")

    def reset_audit():
        (tmp / "audit.json").write_text(json.dumps(
            {"pending_by_user": {"たろう": "2026-08"},
             "wallet_check_pending_by_user": {"たろう": "2026-08"}},
            ensure_ascii=False), encoding="utf-8")

    orig = (config.SYSTEM_PATH, config.SETTING_PATH, m._wallet, m._resolve_child)
    config.SYSTEM_PATH = tmp / "settings" / "system.json"
    config.SETTING_PATH = tmp / "settings" / "setting.json"
    w = ws.WalletService()
    w.wallet_state_path = tmp / "wallet.json"
    w.wallet_audit_state_path = tmp / "audit.json"
    m._wallet = w
    m._resolve_child = lambda n=None: {"name": "たろう"}
    try:
        # ① ぴったり合っている: 報告済みになり、残高は動かない
        reset_audit()
        msg = m._do_report_wallet_balance({"name": "たろう", "amount": 3000, "operation_key": "k1"})
        st = w.load_audit_state()
        _check("check_ok_clears_pending", "たろう" not in st.get("pending_by_user", {}),
               list(st.get("pending_by_user", {}).keys()))
        _check("check_ok_clears_wallet_pending",
               "たろう" not in st.get("wallet_check_pending_by_user", {}), "残っている")
        _check("check_ok_keeps_balance", w.get_balance("たろう") == 3000, w.get_balance("たろう"))
        _check("check_ok_message", "ぴったり" in msg, msg[:40])

        # ② 財布が少ない: 帳簿を実額へ合わせ、記録漏れの支出としてペナルティを残す
        reset_audit()
        w.wallet_state_path.write_text(json.dumps(
            {"users": {"たろう": {"expected_balance": 3000}}, "applied_operation_keys": {}},
            ensure_ascii=False), encoding="utf-8")
        m._do_report_wallet_balance({"name": "たろう", "amount": 2500, "operation_key": "k2"})
        st = w.load_audit_state()
        _check("check_diff_clears_pending", "たろう" not in st.get("pending_by_user", {}),
               list(st.get("pending_by_user", {}).keys()))
        _check("check_diff_fixes_balance", w.get_balance("たろう") == 2500, w.get_balance("たろう"))
        pen = st.get("wallet_check_penalties", {}).get("たろう", {})
        _check("check_diff_penalty_type", pen.get("type") == "spending_leak", pen.get("type"))
        _check("check_diff_penalty_diff", pen.get("diff") == -500, pen.get("diff"))

        # ③ 冪等: 同じ operation_key では二度目は動かない
        before = w.get_balance("たろう")
        again = m._do_report_wallet_balance({"name": "たろう", "amount": 100, "operation_key": "k2"})
        _check("check_idempotent", w.get_balance("たろう") == before, w.get_balance("たろう"))
        _check("check_idempotent_message", "さっき受け取った" in again, again[:40])

        # ④ 0円（財布が空）も正当な報告として受ける
        reset_audit()
        zero = m._do_report_wallet_balance({"name": "たろう", "amount": 0, "operation_key": "k3"})
        _check("check_accepts_zero", "うまく読めなかった" not in zero, zero[:40])
    finally:
        config.SYSTEM_PATH, config.SETTING_PATH, m._wallet, m._resolve_child = orig
        shutil.rmtree(tmp, ignore_errors=True)


def main() -> None:
    tmp = Path(tempfile.mkdtemp())
    _setup(tmp)
    w, mcp = _wallet_and_tools(tmp)

    # 発話者はたろうに束縛する（越境防止の検証）
    os.environ["COMPASS_ACTIVE_CHILD"] = "たろう"
    # ACTIVE_CHILD はモジュール読み込み時に評価されるため差し替える
    mcp.ACTIVE_CHILD = "たろう"

    # dispatcher 経由で tool を呼ぶ。_ChildMismatch 等は _handle_tool_call が拒否文へ変換する
    def call(tool: str, args: dict) -> str:
        out: list = []
        orig = mcp._send
        mcp._send = lambda m: out.append(m)
        try:
            mcp._handle_tool_call(1, {"name": tool, "arguments": args})
        finally:
            mcp._send = orig
        return out[0]["result"]["content"][0]["text"]

    # --- 本人性束縛: 別の子・親を拒否 ---
    r = call("get_balance", {"name": "はな"})
    _check("cross_child_balance_rejected", "操作できない" in r or "見つからなかった" in r, r)
    r = call("record_expense", {"name": "はな", "amount": 500, "operation_key": "x1"})
    _check("cross_child_expense_rejected", "操作できない" in r, r)
    _check("cross_child_hana_balance_unchanged", w.get_balance("はな") == 2000)
    r = call("get_balance", {"name": "とうちゃん"})
    _check("parent_rejected", "見つからなかった" in r or "操作できない" in r, r)

    # --- 自分の支出・冪等 ---
    r = call("record_expense", {"name": "たろう", "amount": 300, "operation_key": "e1"})
    _check("own_expense_applied", w.get_balance("たろう") == 700, r)
    r = call("record_expense", {"name": "たろう", "amount": 300, "operation_key": "e1"})
    _check("expense_idempotent", w.get_balance("たろう") == 700 and "さっき記録した" in r, r)

    # --- 自己申告入金の上限 ---
    r = call("record_income", {"name": "たろう", "amount": 6000, "operation_key": "i1"})
    _check("income_over_limit_rejected", "までだよ" in r and w.get_balance("たろう") == 700, r)
    r = call("record_income", {"name": "たろう", "amount": 200, "operation_key": "i2"})
    _check("income_within_limit_applied", w.get_balance("たろう") == 900, r)

    # --- 査定4層ガードレール（propose 経由。残高は動かさない）---
    # 固定200→100頭打ち + 臨時5000→1000頭打ち
    r = call("propose_allowance", {"name": "たろう", "fixed": 200, "temporary": 5000, "reason": "がんばった"})
    prop = mcp.read_pending_proposal("たろう")
    _check("propose_caps_fixed_and_temp", prop is not None and prop["fixed"] == 100 and prop["temporary"] == 1000, str(prop))
    _check("propose_no_balance_change", w.get_balance("たろう") == 900)

    # --- 親承認で実支給（900 + 1100 = 2000）---
    # 承認には pending の proposal_id を添える必要がある（古い通知での二重支給を防ぐため必須化した）。
    # テストも実際の運用と同じく、通知に載る ID を読んで渡す形にする。
    _pid = mcp.read_pending_proposal("たろう")["proposal_id"]
    r = mcp.approve_proposal("たろう", "appr1", expected_proposal_id=_pid)
    _check("approve_grants", w.get_balance("たろう") == 2000, r)
    _check("proposal_cleared_after_approve", mcp.read_pending_proposal("たろう") is None)
    # 二重承認しない（同じ ID を再度使っても残高は動かない）
    r = mcp.approve_proposal("たろう", "appr1", expected_proposal_id=_pid)
    _check("approve_no_double", w.get_balance("たろう") == 2000, r)

    # --- 日次回数上限: daily_count_max=3。既に1回承認済み。あと2回でその日は打ち止め ---
    call("propose_allowance", {"name": "たろう", "temporary": 100, "reason": "d2"})
    mcp.approve_proposal("たろう", "appr2",
                         expected_proposal_id=mcp.read_pending_proposal("たろう")["proposal_id"])  # 2回目
    call("propose_allowance", {"name": "たろう", "temporary": 100, "reason": "d3"})
    mcp.approve_proposal("たろう", "appr3",
                         expected_proposal_id=mcp.read_pending_proposal("たろう")["proposal_id"])  # 3回目
    bal_after_3 = w.get_balance("たろう")
    # 4回目の提案は日次回数上限で拒否される
    r = call("propose_allowance", {"name": "たろう", "temporary": 100, "reason": "d4"})
    _check("daily_count_blocks", ("回数" in r or "使いきった" in r) and w.get_balance("たろう") == bal_after_3, r)

    # --- 月次累計上限: 日次回数と独立に検証するため daily_count_max を一時的に大きくする ---
    # 別の子（はな）で、月次上限3000に対し臨時1000×3回=3000到達後の4回目が拒否されることを見る
    import app.config as _cfg
    _orig_guard = _cfg.get_assessment_guardrail_setting
    _cfg.get_assessment_guardrail_setting = lambda: {"temporary_max": 1000, "monthly_total_max": 3000, "daily_count_max": 99}
    mcp.ACTIVE_CHILD = "はな"
    os.environ["COMPASS_ACTIVE_CHILD"] = "はな"
    for i in range(3):
        call("propose_allowance", {"name": "はな", "temporary": 1000, "reason": f"m{i}"})
        mcp.approve_proposal("はな", f"hana-appr-{i}",
                             expected_proposal_id=mcp.read_pending_proposal("はな")["proposal_id"])  # 累計 1000,2000,3000
    bal_at_cap = w.get_balance("はな")
    r = call("propose_allowance", {"name": "はな", "temporary": 1000, "reason": "over"})
    _check("monthly_cap_blocks", "上限" in r and w.get_balance("はな") == bal_at_cap, r)
    _cfg.get_assessment_guardrail_setting = _orig_guard

    # --- operation_key のサーバ側名前空間化: 別の子が同一の生キーを使っても冪等衝突しない ---
    # たろうは既に生キー "e1" で支出済み（L113）。claude セッションは子ごとに完全分離されるため、
    # はなのセッションが独立に同じ低エントロピー生キー "e1" を選ぶことは構造的に起こりうる。
    # 名前空間化前は applied_operation_keys がフラット共有で、はなの本当に別の支出が「すでに記録済み」に
    # 化けて黙って消え、はなの実残高が乖離した。名前空間化（{child}:{action}:{key}）でこれを防ぐ。
    hana_before_ns = w.get_balance("はな")
    r = call("record_expense", {"name": "はな", "amount": 150, "operation_key": "e1"})
    _check(
        "cross_child_opkey_no_collision",
        w.get_balance("はな") == hana_before_ns - 150 and "記録したよ" in r,
        r,
    )
    # 同じ子・同じ生キーの二重適用は従来どおり冪等（名前空間化しても子内の冪等は維持される）
    r = call("record_expense", {"name": "はな", "amount": 150, "operation_key": "e1"})
    _check(
        "same_child_opkey_still_idempotent",
        w.get_balance("はな") == hana_before_ns - 150 and "さっき記録した" in r,
        r,
    )

    # --- 却下フロー（残高を動かさない）---
    # はなは月次上限に達しているので、上限を一時的に緩めて却下フローだけを検証する
    _cfg.get_assessment_guardrail_setting = lambda: {"temporary_max": 1000, "monthly_total_max": 999999, "daily_count_max": 99}
    before = w.get_balance("はな")
    call("propose_allowance", {"name": "はな", "temporary": 300, "reason": "test"})
    # 却下も承認と同じく proposal_id の一致を要求する（古い通知での取り違えを防ぐ）
    r = mcp.reject_proposal("はな", expected_proposal_id=mcp.read_pending_proposal("はな")["proposal_id"])
    # 文言は「却下」ではなく「見送った」。子に届く言い方として角を立てないため実装側でそう表現している
    _check("reject_no_balance_change", w.get_balance("はな") == before and "見送った" in r, r)
    _check("proposal_cleared_after_reject", mcp.read_pending_proposal("はな") is None)
    _cfg.get_assessment_guardrail_setting = _orig_guard

    # --- 自然キー冪等: 言い直し(別 op_key・同金額同品目)の二重適用を防ぐ / 別支出は通す ---
    # tool後失敗で子が「300円つかった」を言い直すと AI は別の生キーを選び op_key 冪等をすり抜ける。
    # 内容ベースの自然キー(子:action:金額:品目:分)で、直近2分の同一支出を二重適用しないことを検証する。
    mcp.ACTIVE_CHILD = "たろう"
    os.environ["COMPASS_ACTIVE_CHILD"] = "たろう"
    bal_before_dup = w.get_balance("たろう")
    # 1回目: 別 op_key で 80円のジュースを記録 → 適用される
    r = call("record_expense", {"name": "たろう", "amount": 80, "item": "ジュース", "operation_key": "say-once-1"})
    _check("dup_first_applied", w.get_balance("たろう") == bal_before_dup - 80 and "記録したよ" in r, r)
    # 2回目: 別 op_key(=AIの言い直し)でも同金額・同品目・同時刻窓 → 自然キーで弾かれ残高不変
    r = call("record_expense", {"name": "たろう", "amount": 80, "item": "ジュース", "operation_key": "say-once-2-different-key"})
    # 自然キー命中は「さっきも同じ…記録した」+ 別支出の逃げ道を示す文面（黙って落とさない）
    _check(
        "dup_restated_blocked",
        w.get_balance("たろう") == bal_before_dup - 80 and "さっきも同じ" in r and "べつの" in r,
        r,
    )
    # 別金額なら本当に別の支出とみなし通す（言い直しでない）
    bal_after_dup = w.get_balance("たろう")
    r = call("record_expense", {"name": "たろう", "amount": 120, "item": "ジュース", "operation_key": "real-second-1"})
    _check("dup_different_amount_applied", w.get_balance("たろう") == bal_after_dup - 120 and "記録したよ" in r, r)

    # 結果出力（1行1 JSON）
    _test_expense_writes_pocket_journal()

    _test_wallet_check_clears_pending()
    passed = sum(1 for x in _results if x["passed"])
    for x in _results:
        print(json.dumps(x, ensure_ascii=False))
    print(json.dumps({"summary": True, "passed": passed, "total": len(_results)}, ensure_ascii=False))
    # 集計を表示するだけだと失敗が exit 0 に埋もれ、CI でも手動実行でも見逃す
    # （実際にこのスイートは 16/22 のまま PASS 扱いで放置されていた）。終了コードへ必ず反映する。
    return passed == len(_results)


if __name__ == "__main__":
    sys.exit(0 if main() else 1)
