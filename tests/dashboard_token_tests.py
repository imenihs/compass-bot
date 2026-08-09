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


def _test_url_reissue_command():
    """URL再発行コマンドが、打ったチャンネルで役割を正しく分けること。

    実データで子「テスト」と親「とうちゃん」が同一 Discord ID を持つ（兼務アカウント）。
    ID だけでは親か子か決まらないため、**どのチャンネルで打たれたか**で決める。
    親チャンネル → parent、子チャンネル → child。

    また DM が拒否設定のときは、チャンネルへフォールバックする
    （親チャンネルは子から分離済みなので、最悪ここへ出しても子には見えない）。
    """
    import asyncio

    import discord

    from app import config, dashboard_token as dt, handlers_parent as H

    tmp = Path(tempfile.mkdtemp())
    try:
        dt.TOKENS_PATH = tmp / "tokens.json"
        sent, dms = [], []

        class _Ch:
            def __init__(self, cid):
                self.id = cid

            async def send(self, msg, **kw):
                sent.append(msg)
                return type("M", (), {"id": 1})()

        class _Author:
            def __init__(self, uid, dm_ok=True):
                self.id = uid
                self._ok = dm_ok

            async def send(self, msg, **kw):
                if not self._ok:
                    raise discord.Forbidden(type("R", (), {"status": 403})(), "blocked")
                dms.append(msg)

        orig_client = H._client
        H._client = type("C", (), {"user": type("U", (), {
            "id": 1, "name": "compass-bot", "discriminator": "0"})()})()
        orig_extract = H.extract_input_from_mention
        H.extract_input_from_mention = lambda t, u: None
        try:
            parent_ch = config.get_parent_channel_id()
            child_ch = sorted(config.get_allow_channel_ids() or {0})[0]
            # 実データの兼務 ID（子「テスト」と親のどちらにも登録されている）
            dual_id = 111

            def _run(channel_id, dm_ok=True):
                sent.clear()
                dms.clear()
                msg = type("M", (), {"channel": _Ch(channel_id),
                                     "author": _Author(dual_id, dm_ok), "id": 1})()
                return asyncio.new_event_loop().run_until_complete(
                    H.maybe_handle_url_reissue(msg, "URL再発行"))

            def _role_of(text):
                token = text.split("/d/")[1].split(">")[0]
                resolved = dt.resolve(token)
                return resolved["user_key"] if resolved else None

            handled = _run(parent_ch)
            _check("reissue_handled_in_parent_channel", handled is True, handled)
            _check("reissue_sends_dm", bool(dms), sent)
            _check("reissue_parent_role",
                   dms and _role_of(dms[0]).startswith("parent:"), dms[:1])

            handled = _run(child_ch)
            _check("reissue_handled_in_child_channel", handled is True, handled)
            _check("reissue_child_role",
                   dms and _role_of(dms[0]).startswith("child:"), dms[:1])

            # DM 拒否時はチャンネルへ出す（詰まらない構成）
            _run(parent_ch, dm_ok=False)
            _check("reissue_falls_back_to_channel",
                   any("/d/" in m for m in sent), sent[:1])

            # リンクプレビューを抑止するため山括弧で囲む
            _run(parent_ch)
            _check("reissue_suppresses_preview",
                   dms and "<http" in dms[0], dms[:1])
        finally:
            H._client = orig_client
            H.extract_input_from_mention = orig_extract
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _test_discord_id_cannot_be_changed_from_web():
    """Web から discord_user_id を変更できないこと。

    Discord ID は「URL再発行を打ったのが本人か」を判定する唯一の根拠。
    Web の権限を得た者がこれを自分のものへ書き換えると、
    **再発行の主が入れ替わり、認証の防御が丸ごと無効になる**（codex 指摘）。
    """
    import ast

    src = (ROOT / "app" / "server.py").read_text(encoding="utf-8")
    tree = ast.parse(src)

    # 「既存IDと違う値が来たら拒否する」ガードが存在すること
    found_guard = False
    for node in ast.walk(tree):
        if not isinstance(node, ast.If):
            continue
        dumped = ast.dump(node)
        if "existing_id" in dumped and "discord_id" in dumped:
            found_guard = True
            break
    _check("web_forbids_discord_id_change", found_guard,
           "server.py に既存IDとの不一致を拒否するガードが無い")

    # 拒否メッセージが利用者に伝わる形であること
    _check("web_explains_why_forbidden",
           "Discord ID は Web からは変更できません" in src, "")


def main():
    _test_issue_and_resolve()
    _test_invalid_tokens_are_rejected()
    _test_reissue_replaces_old()
    _test_same_discord_id_coexists()
    _test_broken_file_does_not_crash()
    _test_user_key_split()
    _test_url_reissue_command()
    _test_discord_id_cannot_be_changed_from_web()

    passed = sum(1 for x in _results if x["passed"])
    for x in _results:
        print(json.dumps(x, ensure_ascii=False))
    print(json.dumps({"summary": True, "passed": passed, "total": len(_results)},
                     ensure_ascii=False))
    return passed == len(_results)


if __name__ == "__main__":
    sys.exit(0 if main() else 1)
