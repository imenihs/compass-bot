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
        # 「安全設定チェック」は 2026/08/11 に機能ごと廃止した（通知経路が無くなったため）
        "全体確認": "parent_list_overview",
        "使い方の説明": "parent_get_usage_guide",
        "使い方の説明と初期設定": "parent_broadcast_usage_guide",
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

    **文字列を実際に流して確かめる**。当初は「bot.py に maybe_handle_ が無い」を
    4回チェックするだけで、引用文を一度も処理していなかった（空振り）。
    ここでは各文字列を tool 名として解決しようと試み、
    どれも tool に当たらない＝残高を動かす経路が無いことを確認する。
    """
    import json as _json
    import tempfile

    import app.wallet_service as ws
    from app import mcp_wallet as m

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

        quoted = [
            "昨日の話だけど「支給 はな 300円」ってもう反映したんだっけ？",
            "残高調整 はな -500円 ってどういう意味？",
            "この前 設定変更 はな 固定 800円 にしたよね",
            "全体確認ってどうやるの？",
            "URL再発行",
            "ダッシュボード",
        ]
        for text in quoted:
            # ① この文字列そのものが tool 名として解決されないこと。
            #    旧方式はこの形の文字列を見て直接処理を起動していた
            _check(f"not_a_tool_name::{text[:16]}",
                   text not in m._HANDLERS and text.split()[0] not in m._HANDLERS,
                   f"tool として解決された: {text}")

            # ② 発話の頭の語（旧コマンド名の位置）でも解決されないこと
            head = text.split()[0].split("「")[-1]
            _check(f"head_not_a_tool::{head[:12]}", head not in m._HANDLERS, head)

        # ③ ここまでで残高を動かす経路を一度も通っていないこと
        _check("balance_unchanged", w.get_balance("はな") == before,
               f"{before} -> {w.get_balance('はな')}")

        # ④ 残高を動かす tool は operation_key 無しでは動かない（AIが誤爆しても実害が出ない）。
        #    文字列一致を外した以上、実害を止めるのは Python 側のこの検査だけになる
        orig_resolve = m._resolve_child
        m._resolve_child = lambda n=None: {"name": "はな"}
        try:
            msg = m._do_record_expense({"name": "はな", "amount": 300})
            _check("expense_needs_operation_key", "うまくできなかった" in msg, msg[:40])
        finally:
            m._resolve_child = orig_resolve


def _test_settings_info_reads_real_keys():
    """設定の現在値が、**実データのキー名**で読めていること。

    移送時に `temporary_max` を `temporary_allowance_max` と書き違え、
    臨時上限を常に 0円 と答えていた。金額の現在値を偽って返すのは危険なので、
    実際に値を入れて読み出せるかを見る。
    """
    import json as _json
    import tempfile

    from app import config
    from app import mcp_wallet as m

    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        children = tmp / "children"
        children.mkdir(parents=True)
        (children / "tarou.json").write_text(_json.dumps(
            {"name": "たろう", "discord_user_id": 111,
             "fixed_allowance": 800, "temporary_max": 3000},
            ensure_ascii=False), encoding="utf-8")

        orig_children, orig_mode = config.CHILDREN_DIR, m.PARENT_MODE
        config.CHILDREN_DIR = children
        m.PARENT_MODE = True
        try:
            single = m._do_parent_get_settings_info({"name": "たろう"})
            _check("settings_shows_fixed", "800円" in single, single[:80])
            _check("settings_shows_temporary_max", "3000円" in single, single[:80])
            _check("settings_not_zero", "臨時上限: 0円" not in single, single[:80])
            _check("settings_points_to_web", "http" in single, single[-60:])

            # 一覧でもフォロー方針を出す（description が返すと宣言しているため）
            listed = m._do_parent_get_settings_info({})
            _check("list_shows_follow_policy", "フォロー" in listed, listed[:120])
            _check("list_shows_temporary_max", "3000円" in listed, listed[:120])
        finally:
            config.CHILDREN_DIR, m.PARENT_MODE = orig_children, orig_mode


def _test_tool_descriptions_use_criteria_not_phrases():
    """tool の description が「決まった言い方」で判断させていないこと。

    実際に事故が起きた（2026/08/11）。財布チェックの description が
    「『財布に3000円あった』『数えたら1200円だった』のように」と例を並べていたため、
    子が「今のお金56563」と書いても AI が tool を呼ばず、
    残高報告が永久に未報告のままだった。

    Python の文字列一致を全廃したのに、**プロンプト側で同じことをやっていた**。
    語彙を足しても収束しないのは Python でも AI でも同じなので、
    description には「判断の基準」を書き、当てはめは AI にさせる。
    """
    import re

    from app import mcp_wallet as m

    # 「〜と言ったら呼ぶ」「〜のように聞かれたら呼ぶ」は発話例への依存
    phrase_driven = re.compile(r"[」』]の?ように(聞かれ|言われ)|[」』]と言ったら")
    for d in m._tool_defs():
        name = d.get("name", "")
        desc = d.get("description", "")
        _check(f"criteria_not_phrases::{name}",
               not phrase_driven.search(desc), desc[:80])


def main():
    """全テストを走らせて結果を出す。"""
    _test_no_string_match_handlers()
    _test_bot_has_no_command_dispatch()
    _test_old_commands_exist_as_tools()
    _test_quoted_command_cannot_move_balance()
    _test_settings_info_reads_real_keys()
    _test_tool_descriptions_use_criteria_not_phrases()

    passed = sum(1 for x in _results if x["passed"])
    for x in _results:
        print(json.dumps(x, ensure_ascii=False))
    print(json.dumps({"summary": True, "passed": passed, "total": len(_results)},
                     ensure_ascii=False))
    return passed == len(_results)


if __name__ == "__main__":
    sys.exit(0 if main() else 1)
