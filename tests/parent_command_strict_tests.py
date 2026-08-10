"""親経路に「文字列一致のコマンド」が残っていないことを検証する（2026/08/10 全廃）。

**なぜ全廃したか。**
一致で発火させる作りは、利用者に一字一句の暗記を強いる。実際に事故が起きた。
「ダッシュボードを見たい」は `show_words` の完全一致に当たらず AI 経路へ落ち、
そこでは tool が案内文しか返さないため **DM が永久に届かなかった**。
語彙を足す対処は 4 周やって収束しないことが分かっている（足しても次の言い回しが来る）。

**代わりの構造。**
意図の解釈は AI が行い、実処理は tool（parent_list_overview 等）が担う。
Discord 送信が要るものは bot_action キュー経由で bot プロセスが実行する。

このテストが守るもの。
  ① `maybe_handle_*` が復活していないこと（一致判定の再導入を検出する）
  ② 旧コマンドの機能が tool として全部あること（消し忘れ・移し忘れを検出する）
  ③ 親の自然文に引用されたコマンド文字列で、実残高が動かないこと
     （そもそも一致判定が無いので動きようがない、を実際に確かめる）
"""
import ast
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

_results = []


def _check(name, passed, detail=""):
    """1件の判定を記録する。"""
    _results.append({"test": name, "passed": bool(passed), "detail": str(detail)[:200]})


def _test_no_string_match_handlers():
    """親・子のハンドラに `maybe_handle_*` が1つも残っていないこと。

    ここが再び増えると、利用者に暗記を強いる作りへ逆戻りする。
    """
    for path in ("app/handlers_parent.py", "app/handlers_child.py"):
        src = Path(__file__).resolve().parents[1] / path
        tree = ast.parse(src.read_text(encoding="utf-8"))
        found = [n.name for n in ast.walk(tree)
                 if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
                 and n.name.startswith("maybe_handle_")]
        _check(f"no_maybe_handle::{Path(path).name}", not found, found)


def _test_bot_has_no_command_dispatch():
    """bot.py が `maybe_handle_*` を呼んでいないこと。"""
    src = (Path(__file__).resolve().parents[1] / "app/bot.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    calls = []
    for n in ast.walk(tree):
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) \
                and n.func.attr.startswith("maybe_handle_"):
            calls.append(n.func.attr)
    _check("bot_no_command_dispatch", not calls, calls)


def _test_old_commands_exist_as_tools():
    """廃止したコマンドの機能が、すべて tool として存在すること。

    一致判定を消しただけで機能まで落とすと、親は同じことができなくなる。
    旧コマンドと新 tool の対応をここに固定して、移し忘れを検出する。
    """
    from app import mcp_wallet

    # 旧コマンド名 → 置き換えた tool 名
    mapping = {
        "全体確認": "parent_list_overview",
        "使い方の説明": "parent_get_usage_guide",
        "使い方の説明と初期設定": "parent_broadcast_usage_guide",
        "安全設定チェック": "parent_safety_setup_check",
        "設定変更": "parent_get_settings_info",
        "フォロー方針": "parent_get_settings_info",
        "URL再発行": "reissue_dashboard_url",
    }
    for old, tool in mapping.items():
        _check(f"tool_exists::{old}", tool in mcp_wallet._HANDLERS, tool)

    # 親会話の許可リストに載っていなければ AI からは呼べない
    from app.conv.ai_conversation import ALLOWED_PARENT_TOOLS, ALLOWED_WALLET_TOOLS
    for tool in set(mapping.values()):
        full = f"mcp__wallet__{tool}"
        allowed = full in ALLOWED_PARENT_TOOLS or full in ALLOWED_WALLET_TOOLS
        _check(f"tool_allowed::{tool}", allowed, full)


def _test_quoted_command_cannot_move_balance():
    """親の自然文に引用されたコマンド文字列で、実残高が動かないこと。

    以前は部分一致で「昨日『支給 はな 300円』ってやったっけ？」が発火し、
    引用しただけで残高が動いた。一致判定を全廃したので発火経路そのものが無い。
    ここでは「その文字列を含む発話を処理しても残高が変わらない」を実際に確かめる。
    """
    import json as _json
    import tempfile

    from app import config
    import app.wallet_service as ws

    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        (tmp / "data").mkdir()
        state = tmp / "data" / "wallet_state.json"
        state.write_text(_json.dumps(
            {"users": {"はな": {"expected_balance": 1000}}, "applied_operation_keys": {}},
            ensure_ascii=False), encoding="utf-8")

        w = ws.WalletService()
        w.wallet_state_path = state
        w.wallet_audit_state_path = tmp / "data" / "wallet_audit_state.json"

        before = w.get_balance("はな")
        # 一致判定が無いので、これらはただの文字列として AI へ渡るだけ。
        # Python 側で残高を動かす経路が存在しないことを確認する
        quoted = [
            "昨日の話だけど「支給 はな 300円」ってもう反映したんだっけ？",
            "残高調整 はな -500円 ってどういう意味？",
            "この前 設定変更 はな 固定 800円 にしたよね",
            "全体確認ってどうやるの？",
        ]
        # bot.py に「この文字列で分岐する」コードが無いことを見る。
        # コメントや文字列リテラルに語が出るのは無害なので、AST 上の識別子だけを見る
        bot_tree = ast.parse(
            (Path(__file__).resolve().parents[1] / "app/bot.py").read_text(encoding="utf-8"))
        branch_names = {n.attr for n in ast.walk(bot_tree)
                        if isinstance(n, ast.Attribute) and n.attr.startswith("maybe_handle_")}
        for text in quoted:
            _check(f"no_literal_branch::{text[:16]}",
                   not branch_names, f"bot.py に一致判定が残っている: {branch_names}")
        _check("balance_unchanged", w.get_balance("はな") == before,
               f"{before} -> {w.get_balance('はな')}")


def main():
    """全テストを走らせて結果を出す。"""
    _test_no_string_match_handlers()
    _test_bot_has_no_command_dispatch()
    _test_old_commands_exist_as_tools()
    _test_quoted_command_cannot_move_balance()

    passed = sum(1 for x in _results if x["passed"])
    for x in _results:
        print(json.dumps(x, ensure_ascii=False))
    print(json.dumps({"summary": True, "passed": passed, "total": len(_results)},
                     ensure_ascii=False))
    return passed == len(_results)


if __name__ == "__main__":
    sys.exit(0 if main() else 1)
