"""親モードAI主導tool の決定的テスト。実claude を起動せず tool 関数を直接呼ぶ。

親経路のAI主導化: 親会話でAIが親用tool(parent_grant/adjust/approve/reject/list/pending)を呼ぶ。
設計原則「AIに金額・対象を推測させない」を Python 境界で担保することを検証する:
  - 親モード(PARENT_MODE)でないと親用tool は拒否される
  - 対象児は子ディレクトリ実在のみ(親名・未登録は拒否)
  - 金額は明示値のみ(不正値は拒否)、operation_key で冪等
  - 支給/調整で実残高が正しく動く、承認/却下フロー、一覧・pending は残高を動かさない
隔離環境で実データに触れない。1行1 JSON。
"""
import json, os, sys, tempfile
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app import config

_results = []
def _check(n, p, d=""): _results.append({"name": n, "passed": bool(p), "detail": d})


def _setup(tmp: Path):
    (tmp / "settings" / "users" / "parents").mkdir(parents=True, exist_ok=True)
    # 子は children/ 配下に置く。config.CHILDREN_DIR の実配置に合わせる
    (tmp / "settings" / "users" / "children").mkdir(parents=True, exist_ok=True)
    (tmp / "data").mkdir(parents=True, exist_ok=True)
    (tmp / "settings" / "users" / "children" / "tarou.json").write_text(
        json.dumps({"name": "たろう", "age": 10, "discord_user_id": 111, "fixed_increase_cap": 100}, ensure_ascii=False), encoding="utf-8")
    (tmp / "settings" / "users" / "children" / "hana.json").write_text(
        json.dumps({"name": "はな", "age": 8, "discord_user_id": 222, "fixed_increase_cap": 100}, ensure_ascii=False), encoding="utf-8")
    (tmp / "settings" / "users" / "parents" / "oya.json").write_text(
        json.dumps({"name": "とうちゃん", "discord_user_id": 999}, ensure_ascii=False), encoding="utf-8")
    setting = {"assessment_guardrail": {"temporary_max": 1000, "monthly_total_max": 3000, "daily_count_max": 3},
               "child_income_report": {"max_amount": 5000}}
    (tmp / "settings" / "setting.json").write_text(json.dumps(setting, ensure_ascii=False), encoding="utf-8")
    (tmp / "settings" / "system.json").write_text(json.dumps({"log_dir": str(tmp / "data")}, ensure_ascii=False), encoding="utf-8")
    (tmp / "data" / "wallet_state.json").write_text(
        json.dumps({"users": {"たろう": {"expected_balance": 1000}, "はな": {"expected_balance": 2000}}, "applied_operation_keys": {}}, ensure_ascii=False), encoding="utf-8")
    config.SETTINGS_DIR = tmp / "settings"
    config.USERS_DIR = config.SETTINGS_DIR / "users"
    config.PARENTS_DIR = config.USERS_DIR / "parents"
    config.CHILDREN_DIR = config.USERS_DIR / "children"
    config.SYSTEM_PATH = config.SETTINGS_DIR / "system.json"
    config.SETTING_PATH = config.SETTINGS_DIR / "setting.json"


def _test_all_parent_tools_execute():
    """親用 tool が**実際に実行できる**こと（実行時エラーの検出）。

    `parent_list_overview` が `from app import wallet_service as _ws` と書いて
    **モジュールのまま** `_ws.load_audit_state()` を呼んでおり、
    親が「全体を見たい」と言うと必ず AttributeError で落ちていた（2026/08/11 発覚）。

    tool の登録有無を見るテストはあったが、**呼んでみるテストが無かった**ため
    素通りしていた。登録されているかではなく、動くかを見る。
    """
    import app.mcp_wallet as m

    orig = (m.PARENT_MODE, m.ALLOW_ADMIN_OPS)
    m.PARENT_MODE = True
    m.ALLOW_ADMIN_OPS = True
    try:
        # 引数なしで安全に呼べる参照系だけを対象にする（残高を動かすものは除く）
        readonly = [
            ("parent_list_overview", {}),
            ("parent_list_balances", {}),
            ("parent_get_pending", {}),
            ("parent_get_usage_guide", {}),
            ("parent_get_settings_info", {}),
            ("parent_list_promises", {}),
        ]
        for name, args in readonly:
            fn = m._HANDLERS.get(name)
            if fn is None:
                _check(f"parent_tool_runs::{name}", False, "未登録")
                continue
            try:
                result = fn(args)
                _check(f"parent_tool_runs::{name}", isinstance(result, str) and bool(result),
                       str(result)[:60])
            except Exception as exc:  # noqa: BLE001 - 実行時エラーを検出するのが目的
                _check(f"parent_tool_runs::{name}", False, f"{type(exc).__name__}: {exc}")
    finally:
        m.PARENT_MODE, m.ALLOW_ADMIN_OPS = orig


def _run():
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        _setup(tmp)
        import importlib
        import app.wallet_service as ws
        # 親モード env を設定してから mcp_wallet を reload(モジュール変数 PARENT_MODE/ALLOW_ADMIN_OPS を反映)
        os.environ["COMPASS_PARENT_MODE"] = "1"
        os.environ["COMPASS_ALLOW_ADMIN_OPS"] = "1"
        os.environ.pop("COMPASS_ACTIVE_CHILD", None)
        import app.mcp_wallet as mcp
        importlib.reload(mcp)
        w = ws.WalletService()
        w.wallet_state_path = tmp / "data" / "wallet_state.json"
        w.wallet_audit_state_path = tmp / "data" / "wallet_audit_state.json"
        mcp._wallet = w
        from app.conv.session import SessionStore
        mcp._payout_store = lambda: SessionStore(data_dir=tmp / "data")

        def bal(n): return w.get_balance(n)

        # --- parent_grant: 明示額で支給 ---
        r = mcp._do_parent_grant({"name": "たろう", "amount": 300, "operation_key": "pg1"})
        _check("grant_applied", bal("たろう") == 1300 and "300円" in r, r)
        # 冪等
        r = mcp._do_parent_grant({"name": "たろう", "amount": 300, "operation_key": "pg1"})
        _check("grant_idempotent", bal("たろう") == 1300 and "すでに" in r, r)
        # 未登録の子は拒否
        r = mcp._do_parent_grant({"name": "だれか", "amount": 100, "operation_key": "pg2"})
        _check("grant_unknown_child_rejected", "見つからなかった" in r and bal("たろう") == 1300, r)
        # 親名を対象にできない(親の残高を動かせない)
        r = mcp._do_parent_grant({"name": "とうちゃん", "amount": 100, "operation_key": "pg3"})
        _check("grant_parent_name_rejected", "見つからなかった" in r, r)
        # 金額不正(推測させない: 金額が無ければ拒否)
        r = mcp._do_parent_grant({"name": "たろう", "amount": None, "operation_key": "pg4"})
        _check("grant_bad_amount_rejected", "金額が正しくない" in r and bal("たろう") == 1300, r)
        # --- parent_adjust_balance: 符号付き調整 ---
        # 1回あたりの上限(single_max)は撤回した（社長判断・2026/08/09）。
        # お小遣い管理であり、取り違えても台帳に日時・増減・理由が残って後から追える。
        # 桁ミスは AI の自己点検プロンプトで受け止める方針。
        r = mcp._do_parent_adjust_balance({"name": "はな", "delta": -500, "operation_key": "pa1"})
        _check("adjust_minus_applied", bal("はな") == 1500 and "減らした" in r, r)
        r = mcp._do_parent_adjust_balance({"name": "はな", "delta": 200, "operation_key": "pa2"})
        _check("adjust_plus_applied", bal("はな") == 1700 and "増やした" in r, r)
        r = mcp._do_parent_adjust_balance({"name": "はな", "delta": 0, "operation_key": "pa3"})
        _check("adjust_zero_noop", bal("はな") == 1700 and "変わらない" in r, r)

        # --- parent_list_balances / parent_get_pending は残高を動かさない ---
        before = (bal("たろう"), bal("はな"))
        r = mcp._do_parent_list_balances({})
        _check("list_balances_no_move", (bal("たろう"), bal("はな")) == before and "たろう" in r and "はな" in r, r)
        r = mcp._do_parent_get_pending({})
        _check("get_pending_empty", "承認待ちの査定はない" in r, r)

        # --- 親モードでないと拒否される(PARENT_MODEを外してreload) ---
        os.environ.pop("COMPASS_PARENT_MODE", None)
        os.environ.pop("COMPASS_ALLOW_ADMIN_OPS", None)
        importlib.reload(mcp)
        mcp._wallet = w
        # 絶対値ではなく「拒否されて残高が動かないこと」で見る
        # （上限テストを削除した際に期待値がずれた反省から、前後比較にする）
        _before = bal("たろう")
        r = mcp._do_parent_grant({"name": "たろう", "amount": 9999, "operation_key": "pgX"})
        _check("grant_rejected_without_parent_mode",
               "親のチャンネルからのみ" in r and bal("たろう") == _before, r)
        r = mcp._do_parent_adjust_balance({"name": "はな", "delta": 9999, "operation_key": "paX"})
        _check("adjust_rejected_without_parent_mode", "親のチャンネルからのみ" in r and bal("はな") == 1700, r)

        # 子モードでは親用toolが tool 一覧に現れない
        os.environ["COMPASS_ACTIVE_CHILD"] = "たろう"
        importlib.reload(mcp)
        names = [d["name"] for d in mcp._tool_defs()]
        _check("parent_tools_hidden_in_child_mode", not any(n.startswith("parent_") for n in names), str(names))

    _test_all_parent_tools_execute()
    passed = sum(1 for x in _results if x["passed"])
    for x in _results: print(json.dumps(x, ensure_ascii=False))
    print(json.dumps({"summary": True, "passed": passed, "total": len(_results)}, ensure_ascii=False))
    # **判定を終了コードへ返す**。返さないと落ちても PASS 扱いになり、
    # 実際に9件落ちたまま「全スイートPASS」と報告していた（2026/08/10 に是正）
    return passed == len(_results)


if __name__ == "__main__":
    sys.exit(0 if _run() else 1)
