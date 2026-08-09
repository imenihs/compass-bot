"""ダッシュボードの UUID トークン認証の回帰テスト（docs/設計_UUID認証方式.md）。

守るべき不変条件:
  1. **未登録・失効したトークンは必ず弾く**（社長指示「未承認のUUIDははじく」）。
  2. **再発行すると古いトークンは即無効**。盗まれた URL が生き続けない。
  3. **同一 Discord ID でも子と親が別トークンとして共存する**。
     実データで子「テスト」と親「とうちゃん」が同一 ID を持つ（既知の兼務アカウント）。
     ID をキーにすると上書きし合い、最悪「閲覧専用の子が管理者権限を持つ」。
     このため user_key（設定ファイル名）をキーにしている。
  4. **user_key は名前変更で壊れない**。ファイル名を使うため。
  5. 壊れたトークンファイルでも落とさない（落とすと誰も入れなくなる）。
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


def _isolate(tmp):
    """本番の dashboard_tokens.json に触れないよう隔離する。"""
    from app import dashboard_token as dt
    dt.TOKENS_PATH = tmp / "dashboard_tokens.json"
    return dt


def _test_issue_and_resolve():
    """発行したトークンで利用者と役割が解決できること。"""
    tmp = Path(tempfile.mkdtemp())
    try:
        dt = _isolate(tmp)
        child_key = dt.build_user_key(dt.ROLE_CHILD, "test")
        token = dt.issue(child_key, dt.ROLE_CHILD)

        resolved = dt.resolve(token)
        _check("issue_returns_token", bool(token) and len(token) >= 16, token)
        _check("resolve_returns_user_key",
               resolved and resolved["user_key"] == child_key, resolved)
        _check("resolve_returns_role",
               resolved and resolved["role"] == dt.ROLE_CHILD, resolved)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _test_invalid_tokens_are_rejected():
    """未登録・空・失効したトークンを必ず弾くこと。"""
    tmp = Path(tempfile.mkdtemp())
    try:
        dt = _isolate(tmp)
        key = dt.build_user_key(dt.ROLE_CHILD, "test")
        token = dt.issue(key, dt.ROLE_CHILD)

        _check("reject_unknown_token", dt.resolve("deadbeef" * 4) is None,
               dt.resolve("deadbeef" * 4))
        _check("reject_empty_token", dt.resolve("") is None, dt.resolve(""))
        _check("reject_none_token", dt.resolve(None) is None, dt.resolve(None))

        # 再発行すると古いものは即無効
        dt.issue(key, dt.ROLE_CHILD, issued_by="111")
        _check("reject_revoked_token", dt.resolve(token) is None, dt.resolve(token))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _test_reissue_replaces_old():
    """再発行で新しいトークンが有効になり、有効なものは常に1つであること。"""
    tmp = Path(tempfile.mkdtemp())
    try:
        dt = _isolate(tmp)
        key = dt.build_user_key(dt.ROLE_PARENT, "akira")
        first = dt.issue(key, dt.ROLE_PARENT)
        second = dt.issue(key, dt.ROLE_PARENT, issued_by="222")

        _check("reissue_creates_new", first != second, (first[:8], second[:8]))
        _check("reissue_old_is_dead", dt.resolve(first) is None, dt.resolve(first))
        _check("reissue_new_is_alive", dt.resolve(second) is not None, dt.resolve(second))
        _check("active_token_is_latest", dt.find_active_token(key) == second,
               dt.find_active_token(key))

        # 失効の記録が残る（誰が再発行したか後から分かるように）
        doc = json.load(open(tmp / "dashboard_tokens.json", encoding="utf-8"))
        _check("revoke_records_who", doc[first].get("revoked_by") == "222", doc[first])
        _check("revoke_records_when", bool(doc[first].get("revoked_at")), doc[first])
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _test_same_discord_id_coexists():
    """同一 Discord ID の子と親が、別トークンとして共存すること。

    実データで子「テスト」と親「とうちゃん」が同一 ID を持つ（既知の兼務アカウント）。
    discord_user_id をキーにするとトークンが上書きし合い、
    最悪「閲覧専用の子が管理者権限を持つ」。user_key で分けることでこれを防ぐ。
    """
    tmp = Path(tempfile.mkdtemp())
    try:
        dt = _isolate(tmp)
        child_key = dt.build_user_key(dt.ROLE_CHILD, "test")
        parent_key = dt.build_user_key(dt.ROLE_PARENT, "akira")

        child_token = dt.issue(child_key, dt.ROLE_CHILD)
        parent_token = dt.issue(parent_key, dt.ROLE_PARENT)

        # 親の発行で子のトークンが消えていないこと
        _check("child_token_survives", dt.resolve(child_token) is not None,
               dt.resolve(child_token))
        _check("parent_token_alive", dt.resolve(parent_token) is not None,
               dt.resolve(parent_token))
        _check("roles_are_separate",
               dt.resolve(child_token)["role"] == dt.ROLE_CHILD
               and dt.resolve(parent_token)["role"] == dt.ROLE_PARENT,
               [dt.resolve(child_token), dt.resolve(parent_token)])
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _test_broken_file_does_not_crash():
    """トークンファイルが壊れていても落ちないこと。

    ここで例外を出すと**ダッシュボードに誰も入れなくなる**ため、
    空として扱い、再発行で復旧できる状態にする。
    """
    tmp = Path(tempfile.mkdtemp())
    try:
        dt = _isolate(tmp)
        dt.TOKENS_PATH.parent.mkdir(parents=True, exist_ok=True)
        dt.TOKENS_PATH.write_text("これはJSONではない", encoding="utf-8")

        try:
            result = dt.resolve("anything")
            crashed = False
        except Exception as exc:  # noqa: BLE001 - 落ちないことの確認が目的
            crashed = True
            result = f"{type(exc).__name__}: {exc}"
        _check("broken_file_no_crash", not crashed, result)
        _check("broken_file_returns_none", (not crashed) and result is None, result)

        # 壊れていても新規発行で復旧できる
        key = dt.build_user_key(dt.ROLE_CHILD, "test")
        token = dt.issue(key, dt.ROLE_CHILD)
        _check("broken_file_recovers_by_issue", dt.resolve(token) is not None,
               dt.resolve(token))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _test_user_key_split():
    """user_key の分解が正しいこと（壊れた入力でも落ちない）。"""
    tmp = Path(tempfile.mkdtemp())
    try:
        dt = _isolate(tmp)
        _check("split_child", dt.split_user_key("child:test") == ("child", "test"),
               dt.split_user_key("child:test"))
        _check("split_parent", dt.split_user_key("parent:akira") == ("parent", "akira"),
               dt.split_user_key("parent:akira"))
        _check("split_broken", dt.split_user_key("こわれた") == ("", ""),
               dt.split_user_key("こわれた"))
        _check("split_empty", dt.split_user_key("") == ("", ""), dt.split_user_key(""))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def main():
    _test_issue_and_resolve()
    _test_invalid_tokens_are_rejected()
    _test_reissue_replaces_old()
    _test_same_discord_id_coexists()
    _test_broken_file_does_not_crash()
    _test_user_key_split()

    passed = sum(1 for x in _results if x["passed"])
    for x in _results:
        print(json.dumps(x, ensure_ascii=False))
    print(json.dumps({"summary": True, "passed": passed, "total": len(_results)},
                     ensure_ascii=False))
    return passed == len(_results)


if __name__ == "__main__":
    sys.exit(0 if main() else 1)
