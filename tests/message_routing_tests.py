"""発話の対象児（user_conf）解決の回帰テスト。

親子で同じ Discord ID を使う環境（家族共有端末・親がテスト用の子アカウントを兼ねる等）では、
find_user_by_discord_id が**親を優先**して返す。そのままでは親の発話が子として扱われないため、
子ども用チャンネルでの発話は「チャンネル文脈」で対象児へ解決し直す必要がある。

この解決は _on_message_impl の if/elif 連鎖（proxy → 親のチャンネル文脈）で行っている。
2026/08/09、安全判定のコードをこの連鎖の**途中**へ挿入して elif を分断し、
親が子チャンネルで会話できなくなる回帰を出した（親が「明示コマンドか代理で話しかけて」で蹴られる）。
全12スイートが PASS したまま壊れていたのは、この経路を通るテストが1本も無かったためである。

ここでは実際の解決関数と分岐構造の両方を固定し、同じ壊し方が再発しないようにする。
"""
import ast
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

_results = []


def _check(name, passed, detail=""):
    _results.append({"test": name, "passed": bool(passed), "detail": str(detail)[:200]})


class _FakeMember:
    def __init__(self, mid):
        self.id = mid


class _FakeChannel:
    def __init__(self, name="", members=()):
        self.name = name
        self.members = list(members)
        self.id = 999


class _FakeMessage:
    def __init__(self, channel):
        self.channel = channel


def _test_channel_context_resolves_child():
    """子ども用チャンネルでの発話が、チャンネル名から対象児へ解決されること。"""
    from app import bot as B
    from app.config import load_all_users

    users = load_all_users()
    child = next((u for u in users if str(u.get("name", "")).strip()), None)
    if child is None:
        _check("channel_context_setup", False, "子ユーザーが1人も無い")
        return
    name = str(child.get("name"))

    # チャンネル名に子の名前が含まれる（本番の compass-<名前> と同じ形）
    msg = _FakeMessage(_FakeChannel(name=f"compass-{name}"))
    got = B._find_channel_child_user_conf(msg)
    _check("channel_name_resolves_child",
           got is not None and str(got.get("name")) == name,
           f"got={(got or {}).get('name')} want={name}")

    # メンバーに子の Discord ID がいる場合も解決できる
    cid = child.get("discord_user_id")
    if cid:
        msg2 = _FakeMessage(_FakeChannel(name="general", members=[_FakeMember(int(cid))]))
        got2 = B._find_channel_child_user_conf(msg2)
        _check("channel_member_resolves_child",
               got2 is not None and str(got2.get("name")) == name,
               f"got={(got2 or {}).get('name')}")

    # 手掛かりが無ければ解決しない（誤って別の子を対象にしない）
    msg3 = _FakeMessage(_FakeChannel(name="general"))
    _check("channel_without_hint_returns_none",
           B._find_channel_child_user_conf(msg3) is None,
           B._find_channel_child_user_conf(msg3))


def _test_parent_child_same_id_lookup_order():
    """親子で同じ ID の場合、親優先で返るという前提が変わっていないこと。

    この前提が成り立つからこそチャンネル文脈での解決が必要になる。
    前提自体が変わったらこのテストが落ち、設計を見直す合図になる。
    """
    from app.config import (find_child_user_by_discord_id, find_user_by_discord_id,
                            load_all_parents, load_all_users)

    child_ids = {int(u["discord_user_id"]): str(u.get("name", ""))
                 for u in load_all_users() if u.get("discord_user_id")}
    shared = [(pid, cname) for p in load_all_parents()
              if (pid := p.get("discord_user_id")) and (cname := child_ids.get(int(pid)))]
    if not shared:
        # 衝突が無い環境ではこの観点を検証できない。前提が消えたことを記録する
        _check("same_id_precondition_absent", True, "親子同一IDの設定が無い（検証スキップ）")
        return
    pid, cname = shared[0]
    _check("same_id_parent_lookup_wins",
           str((find_user_by_discord_id(int(pid)) or {}).get("name")) != cname,
           (find_user_by_discord_id(int(pid)) or {}).get("name"))
    _check("same_id_child_lookup_finds_child",
           str((find_child_user_by_discord_id(int(pid)) or {}).get("name")) == cname,
           (find_child_user_by_discord_id(int(pid)) or {}).get("name"))


def _test_resolution_chain_not_broken():
    """user_conf を解決する if/elif 連鎖が分断されていないこと（構造の固定）。

    `if proxy_name:` と `elif is_parent(...)` の間に文を挿入すると elif が切り離され、
    親のチャンネル文脈が効かなくなる。実際にその壊し方をしたため、AST で構造を検査する。
    """
    src = (ROOT / "app" / "bot.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    target = None
    for node in ast.walk(tree):
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "_on_message_impl":
            target = node
            break
    if target is None:
        _check("chain_function_found", False, "_on_message_impl が見つからない")
        return

    # proxy_name を判定する if 文を探し、その orelse に is_parent の分岐があることを確かめる
    found = False
    for node in ast.walk(target):
        if not isinstance(node, ast.If):
            continue
        if not (isinstance(node.test, ast.Name) and node.test.id == "proxy_name"):
            continue
        # orelse が elif（If ノード1つ）で、その条件に is_parent が含まれること
        if len(node.orelse) == 1 and isinstance(node.orelse[0], ast.If):
            cond = ast.dump(node.orelse[0].test)
            found = "is_parent" in cond
        break
    _check("proxy_elif_parent_chain_intact", found,
           "if proxy_name → elif is_parent の連鎖が切れている（間に文を挿入していないか確認）")


def main():
    _test_channel_context_resolves_child()
    _test_parent_child_same_id_lookup_order()
    _test_resolution_chain_not_broken()
    passed = sum(1 for x in _results if x["passed"])
    for x in _results:
        print(json.dumps(x, ensure_ascii=False))
    print(json.dumps({"summary": True, "passed": passed, "total": len(_results)}, ensure_ascii=False))
    return passed == len(_results)


if __name__ == "__main__":
    sys.exit(0 if main() else 1)
