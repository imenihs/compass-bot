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

# テスト用のダミー Discord ID。**実 ID は書かない**（Git 履歴に残るため）。
# 兼務アカウント（同じ人が親としても子としても登録されている）を再現するため、
# 親と子で同じ値を使う
_PARENT_ID = 111


def _sandbox_users(tmp):
    """設定ディレクトリをテスト用へ差し替え、親1人・子1人を置く。

    `_do_get_dashboard_url` は設定ファイルを走査して Discord ID を引くため、
    本番の settings/ を読ませない。実 ID をテストに書かないためでもある。

    Args:
        tmp: 一時ディレクトリ。

    Returns:
        tuple: 差し替え前の (PARENTS_DIR, CHILDREN_DIR)。復元に使う。
    """
    from app import config

    parents, children = tmp / "parents", tmp / "children"
    parents.mkdir(parents=True, exist_ok=True)
    children.mkdir(parents=True, exist_ok=True)
    # 兼務アカウント: 親「とうちゃん」と子「テスト」が同じ Discord ID を持つ
    (parents / "toucyan.json").write_text(
        json.dumps({"name": "とうちゃん", "discord_user_id": _PARENT_ID}, ensure_ascii=False),
        encoding="utf-8")
    (children / "test.json").write_text(
        json.dumps({"name": "テスト", "discord_user_id": _PARENT_ID}, ensure_ascii=False),
        encoding="utf-8")

    orig = (config.PARENTS_DIR, config.CHILDREN_DIR)
    config.PARENTS_DIR, config.CHILDREN_DIR = parents, children
    return orig


def _restore_users(orig):
    """_sandbox_users で差し替えた設定ディレクトリを戻す。"""
    from app import config
    config.PARENTS_DIR, config.CHILDREN_DIR = orig


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


def _test_show_and_reissue_are_separate_tools():
    """「見たいだけ」と「作り直したい」が別の tool に分かれていること。

    以前は「ダッシュボードURL」という語も再発行コマンドに含めており、
    **見たいだけなのに古いURLが無効になった**。他の端末で開いていたURLが
    使えなくなり、子が困る。

    さらに 2026/08/10、文字列一致そのものを全廃した。
    「ダッシュボードを見たい」が `show_words` の完全一致に当たらず AI 経路へ落ち、
    そこでは案内文しか返らないため **DM が永久に届かなかった**ため。
    いまは AI が意図を汲み、見たいだけなら get_dashboard_url、
    作り直しなら reissue_dashboard_url を呼ぶ。
    """
    from app import dashboard_token as dt, mcp_wallet as m

    tmp = Path(tempfile.mkdtemp())
    try:
        dt.TOKENS_PATH = tmp / "tokens.json"
        dt.DM_QUEUE_PATH = tmp / "dm_queue.json"
        orig_dirs = _sandbox_users(tmp)
        key = dt.build_user_key(dt.ROLE_CHILD, "test")

        orig_resolve, orig_mode = m._resolve_child, m.PARENT_MODE
        m._resolve_child = lambda n=None: {"name": "テスト"}
        m.PARENT_MODE = False
        try:
            # 見るだけ: 何回呼んでも同じトークン
            m._do_get_dashboard_url({"name": "テスト"})
            first = dt.find_active_token(key)
            m._do_get_dashboard_url({"name": "テスト"})
            m._do_get_dashboard_url({"name": "テスト"})
            _check("show_keeps_same_token", dt.find_active_token(key) == first,
                   (str(first)[:8], str(dt.find_active_token(key))[:8]))

            # 作り直す: トークンが変わり、前のものは失効する
            msg = m._do_reissue_dashboard_url({"name": "テスト"})
            second = dt.find_active_token(key)
            _check("reissue_changes_token", second != first,
                   (str(first)[:8], str(second)[:8]))
            _check("reissue_kills_old_token", dt.resolve(first) is None, first)
            _check("reissue_explains_old_is_dead", "まえのURLはもう使えない" in msg, msg)
            _check("reissue_returns_no_url", "/compass-bot/d/" not in msg, msg[:60])
        finally:
            m._resolve_child, m.PARENT_MODE = orig_resolve, orig_mode
            _restore_users(orig_dirs)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _test_dm_is_requested_not_returned():
    """URL は tool の戻り値ではなく、**DM 要求キュー**で届くこと。

    tool（mcp_wallet）は別プロセスで Discord を持たないため、自分では送れない。
    そして AI の応答は必ずチャンネルへ出るため、tool に URL を返させると
    親チャンネルの相方にも自分専用URLが見えてしまう。
    そこで tool は「この人へ送って」と積み、bot プロセスが DM を送る。
    """
    import os

    from app import dashboard_token as dt, mcp_wallet as m

    tmp = Path(tempfile.mkdtemp())
    try:
        dt.TOKENS_PATH = tmp / "tokens.json"
        dt.DM_QUEUE_PATH = tmp / "dm_queue.json"
        orig_dirs = _sandbox_users(tmp)

        orig_mode = m.PARENT_MODE
        orig_env = os.environ.get("COMPASS_PARENT_DISCORD_ID")
        orig_resolve = m._resolve_child
        try:
            # --- 親が聞いたとき: 親自身のURLがDM要求として積まれる ---
            m.PARENT_MODE = True
            os.environ["COMPASS_PARENT_DISCORD_ID"] = str(_PARENT_ID)
            parent_msg = m._do_get_dashboard_url({})
            _check("parent_msg_has_no_url", "/compass-bot/d/" not in parent_msg,
                   parent_msg[:60])
            _check("parent_msg_says_dm", "DM" in parent_msg, parent_msg[:60])
            # 親が「誰のを出す？」と聞き返す事故（実機で発生）が再発しないこと
            _check("parent_does_not_ask_which_child", "誰の" not in parent_msg,
                   parent_msg[:60])

            reqs = dt.take_dm_requests()
            _check("parent_dm_queued", len(reqs) == 1, reqs)
            _check("parent_dm_target_is_parent",
                   reqs and reqs[0]["user_key"].startswith("parent:"), reqs[:1])
            parent_key = reqs[0]["user_key"] if reqs else ""

            # --- 同じ Discord ID の子として聞くと、子のURLが積まれる（兼務の解決） ---
            m.PARENT_MODE = False
            m._resolve_child = lambda n=None: {"name": "テスト"}
            child_msg = m._do_get_dashboard_url({"name": "テスト"})
            _check("child_msg_has_no_url", "/compass-bot/d/" not in child_msg,
                   child_msg[:60])

            reqs = dt.take_dm_requests()
            _check("child_dm_queued", len(reqs) == 1, reqs)
            _check("child_dm_target_is_child",
                   reqs and reqs[0]["user_key"].startswith("child:"), reqs[:1])
            _check("dual_role_gets_different_keys",
                   reqs and reqs[0]["user_key"] != parent_key,
                   (parent_key, reqs[0]["user_key"] if reqs else None))

            # 取り出したらキューは空になる（同じDMを二度送らない）
            _check("queue_is_drained", dt.take_dm_requests() == [], "not drained")
        finally:
            m.PARENT_MODE, m._resolve_child = orig_mode, orig_resolve
            if orig_env is None:
                os.environ.pop("COMPASS_PARENT_DISCORD_ID", None)
            else:
                os.environ["COMPASS_PARENT_DISCORD_ID"] = orig_env
            _restore_users(orig_dirs)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _test_dm_queue_keeps_dual_role_requests():
    """兼務アカウントで、子と親の DM 要求が両方とも残ること。

    キーを Discord ID だけにしていたため、同じ人が子としても親としても
    要求すると**先の要求が上書きされて消えていた**。
    実データに兼務アカウントが存在するので、これは仮定の話ではない。
    キーを (Discord ID, user_key) の組にして両方残す。
    同じ組の連打は畳んでよい（1通だけ届く）。
    """
    from app import dashboard_token as dt

    tmp = Path(tempfile.mkdtemp())
    try:
        dt.DM_QUEUE_PATH = tmp / "dm_queue.json"
        dt.request_dm(_PARENT_ID, "child:test", "child")
        dt.request_dm(_PARENT_ID, "parent:toucyan", "parent")
        dt.request_dm(_PARENT_ID, "child:test", "child")  # 連打は畳む

        reqs = dt.take_dm_requests()
        keys = sorted(r["user_key"] for r in reqs)
        _check("dual_role_both_survive", keys == ["child:test", "parent:toucyan"], keys)
        _check("same_key_repeat_collapses", len(reqs) == 2, len(reqs))
        _check("dm_queue_drained", dt.take_dm_requests() == [], "not drained")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _test_bot_actions_are_deduped():
    """同じ種類の依頼が2回積まれても、1回しか実行されないこと。

    一斉送信は全チャンネルへ飛ぶうえ取り消せない。
    親が2回言っても2回配らない（request_dm が連打を畳むのと揃える）。
    低残高アラートは子ごとに内容が違うので、名前まで含めて重複判定する。
    """
    import asyncio

    from app import dashboard_token as dt, handlers_parent as H

    tmp = Path(tempfile.mkdtemp())
    try:
        dt.ACTION_QUEUE_PATH = tmp / "actions.json"
        dt.request_bot_action("broadcast_usage_guide", {})
        dt.request_bot_action("broadcast_usage_guide", {})
        dt.request_bot_action("low_balance_alert", {"name": "たろう", "balance": 100})
        dt.request_bot_action("low_balance_alert", {"name": "はな", "balance": 50})

        executed = []
        orig_bc, orig_safety = H._broadcast_usage_guide, H._run_safety_setup_check

        async def _fake_bc():
            executed.append("broadcast")

        H._broadcast_usage_guide = _fake_bc
        # 低残高は handlers_child 側を差し替える
        from app import handlers_child as C
        orig_low = C.send_low_balance_alert

        async def _fake_low(name, balance, threshold):
            executed.append(f"low:{name}")

        C.send_low_balance_alert = _fake_low
        try:
            asyncio.new_event_loop().run_until_complete(H._drive_bot_actions())
        finally:
            H._broadcast_usage_guide, H._run_safety_setup_check = orig_bc, orig_safety
            C.send_low_balance_alert = orig_low

        _check("broadcast_runs_once", executed.count("broadcast") == 1, executed)
        _check("low_balance_per_child",
               sorted(x for x in executed if x.startswith("low:")) == ["low:たろう", "low:はな"],
               executed)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _test_reissue_queues_dm_before_revoking():
    """作り直しは、**DM要求を積んでから**古いトークンを失効させること。

    逆順だと、積む処理が失敗したときに「前のURLは死んだのに新しいURLは届かない」
    となり、本人がダッシュボードから完全に締め出される。
    ここでは request_dm を失敗させ、古いトークンが生き残る（安全側に倒れる）ことを見る。
    """
    from app import dashboard_token as dt, mcp_wallet as m

    tmp = Path(tempfile.mkdtemp())
    try:
        dt.TOKENS_PATH = tmp / "tokens.json"
        dt.DM_QUEUE_PATH = tmp / "dm_queue.json"
        orig_dirs = _sandbox_users(tmp)
        key = dt.build_user_key(dt.ROLE_CHILD, "test")

        orig_resolve, orig_mode, orig_req = m._resolve_child, m.PARENT_MODE, dt.request_dm
        m._resolve_child = lambda n=None: {"name": "テスト"}
        m.PARENT_MODE = False
        try:
            m._do_get_dashboard_url({"name": "テスト"})
            old_token = dt.find_active_token(key)

            # DM 要求が失敗する状況を作る
            def _boom(*a, **kw):
                raise OSError("queue write failed")

            dt.request_dm = _boom
            try:
                m._do_reissue_dashboard_url({"name": "テスト"})
            except OSError:
                pass
            # 古いトークンがまだ生きている（＝締め出されていない）
            _check("old_token_survives_when_dm_fails",
                   dt.resolve(old_token) is not None, old_token)
        finally:
            m._resolve_child, m.PARENT_MODE = orig_resolve, orig_mode
            dt.request_dm = orig_req
            _restore_users(orig_dirs)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _test_bot_action_queue_roundtrip():
    """bot にしかできない依頼（一斉通知・安全設定チェック）が積んで取り出せること。

    tool は Discord を持たないため、送信そのものは bot プロセスが行う。
    積んだ順に取り出せ、取り出したら空になることを確認する。
    """
    from app import dashboard_token as dt

    tmp = Path(tempfile.mkdtemp())
    try:
        dt.ACTION_QUEUE_PATH = tmp / "actions.json"
        dt.request_bot_action("broadcast_usage_guide", {})
        dt.request_bot_action("safety_setup_check", {})
        actions = dt.take_bot_actions()
        _check("actions_queued_in_order",
               [a["kind"] for a in actions] == ["broadcast_usage_guide", "safety_setup_check"],
               actions)
        _check("actions_drained", dt.take_bot_actions() == [], "not drained")
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


def _test_logout_clears_uuid_cookie():
    """ログアウトで UUID の Cookie も消えること。

    UUID 方式に移したとき session_token しか消しておらず、
    ログアウトしても dash_token が残って**そのまま入り直せる**状態だった。
    Cookie は365日有効なので、共有端末で「閉じる手段が無い」ことになる。
    """
    import ast

    src = (ROOT / "app" / "server.py").read_text(encoding="utf-8")
    tree = ast.parse(src)

    logout_fn = None
    for node in ast.walk(tree):
        if isinstance(node, (ast.AsyncFunctionDef, ast.FunctionDef)) and node.name == "logout":
            logout_fn = node
            break
    _check("logout_exists", logout_fn is not None, logout_fn)
    if logout_fn is None:
        return

    dumped = ast.dump(logout_fn)
    _check("logout_clears_session", "session_token" in dumped, "")
    _check("logout_clears_dash_token", "DASH_COOKIE" in dumped,
           "logout が UUID の Cookie を消していない")


def _test_admin_check_always_uses_token():
    """_is_admin の呼び出しが**全て** dash_token を渡していること。

    1箇所でも渡し忘れると、そこだけ廃止済みの web_users.json の
    is_admin フラグにフォールバックし、画面と権限が食い違う。
    """
    import re

    src = (ROOT / "app" / "server.py").read_text(encoding="utf-8")
    # 定義行を除いた呼び出しを数える
    calls = [m for m in re.findall(r"_is_admin\(([^)]*)\)", src)
             if "username: str" not in m]
    missing = [c for c in calls if "dash_token" not in c]
    _check("all_admin_checks_pass_token", not missing,
           f"dash_token 無しの呼び出し: {missing}")


def _test_money_ops_notify_discord():
    """Web からの金額操作が Discord へ事後通知されること。

    設計（docs/設計_UUID認証方式.md 条件2）で、承認（実行前ブロック）を撤回した
    代わりに通知（実行後報知）を残すと決めた。
    これが無いと、URL が漏れたときに気づく手段がゼロになる。
    """
    src = (ROOT / "app" / "server.py").read_text(encoding="utf-8")
    # 定義1 + 呼び出し（支給・調整）
    _check("notify_is_called", src.count("_notify_discord") >= 3,
           f"_notify_discord の出現数: {src.count('_notify_discord')}")
    _check("notify_prefers_parent_channel",
           "channel_id = get_parent_channel_id()" in src,
           "通知先が親チャンネル優先になっていない（子チャンネルへ流れる）")






def _test_dashboard_url_tool_does_not_reissue():
    """AI 経由（tool）でURLを聞いても、**再発行されない**こと。

    tool は URL を返さない仕様に変えた（会話へURLを出さないため）。
    ここでは「呼んでもトークンが作り直されない」ことだけを見る。
    """
    from app import dashboard_token as dt, mcp_wallet as m

    tmp = Path(tempfile.mkdtemp())
    try:
        dt.TOKENS_PATH = tmp / "tokens.json"
        key = dt.build_user_key(dt.ROLE_CHILD, "test")
        issued = dt.issue(key, dt.ROLE_CHILD)

        orig_resolve, orig_mode = m._resolve_child, m.PARENT_MODE
        m._resolve_child = lambda n=None: {"name": "テスト"}
        m.PARENT_MODE = False
        try:
            m._do_get_dashboard_url({"name": "テスト"})
            m._do_get_dashboard_url({"name": "テスト"})
        finally:
            m._resolve_child, m.PARENT_MODE = orig_resolve, orig_mode

        _check("tool_does_not_reissue", dt.find_active_token(key) == issued,
               (issued[:8], dt.find_active_token(key)))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

def _test_url_never_posted_to_channel():
    """ダッシュボードURLが**会話へ出ない**こと（Python 側の最後の砦）。

    実機で、親が「ダッシュボードを見たい」と言ったとき
    AI が親チャンネルへURLをそのまま投稿してしまった。
    親チャンネルは夫婦2人が見ているため、相手にも自分専用URLが見え、
    1人1UUID にした意味（片方だけ失効できる）が失われる。

    URL は **DM でしか配らない**。プロンプトと tool の説明でも禁じているが、
    **AI は指示を読み飛ばしうる**ので、応答の唯一の出口で機械的に落とす。
    """
    from app.conv.reply import _strip_dashboard_url

    leaked = ("親用のまとめページ、これ:\n"
              "https://example.com/compass-bot/d/8e00177896144db4bdf23bc573e657cb\n"
              "全員の残高が見られる。")
    stripped = _strip_dashboard_url(leaked)
    _check("strips_plain_url", "/compass-bot/d/" not in stripped, stripped[:80])
    _check("keeps_surrounding_text", "全員の残高が見られる" in stripped, stripped[:80])

    # 山括弧つき（プレビュー抑止の形）も落とす
    bracketed = "<https://example.com/compass-bot/d/abc123def456789>"
    _check("strips_bracketed_url",
           "/compass-bot/d/" not in _strip_dashboard_url(bracketed),
           _strip_dashboard_url(bracketed))

    # URL が無い応答は変えない
    normal = "残高は37,700円だよ。"
    _check("keeps_normal_text", _strip_dashboard_url(normal) == normal, normal)

    # tool 自体も URL を返さない（返すと出口で落とされ、案内が壊れるため）
    import os

    from app import dashboard_token as dt, mcp_wallet as m

    tmp = Path(tempfile.mkdtemp())
    orig_mode = m.PARENT_MODE
    orig_env = os.environ.get("COMPASS_PARENT_DISCORD_ID")
    orig_tokens, orig_queue = dt.TOKENS_PATH, dt.DM_QUEUE_PATH
    try:
        dt.TOKENS_PATH = tmp / "tokens.json"
        dt.DM_QUEUE_PATH = tmp / "dm_queue.json"
        orig_dirs = _sandbox_users(tmp)
        try:
            m.PARENT_MODE = True
            os.environ["COMPASS_PARENT_DISCORD_ID"] = str(_PARENT_ID)
            parent_msg = m._do_get_dashboard_url({})
            _check("tool_returns_no_url", "/compass-bot/d/" not in parent_msg,
                   parent_msg[:60])
            _check("tool_explains_dm", "DM" in parent_msg, parent_msg[:60])

            m.PARENT_MODE = False
            orig_resolve = m._resolve_child
            m._resolve_child = lambda n=None: {"name": "テスト"}
            try:
                child_msg = m._do_get_dashboard_url({"name": "テスト"})
                _check("tool_child_returns_no_url", "/compass-bot/d/" not in child_msg,
                       child_msg[:60])
            finally:
                m._resolve_child = orig_resolve
        finally:
            _restore_users(orig_dirs)
    finally:
        m.PARENT_MODE = orig_mode
        dt.TOKENS_PATH, dt.DM_QUEUE_PATH = orig_tokens, orig_queue
        if orig_env is None:
            os.environ.pop("COMPASS_PARENT_DISCORD_ID", None)
        else:
            os.environ["COMPASS_PARENT_DISCORD_ID"] = orig_env
        shutil.rmtree(tmp, ignore_errors=True)


def main():
    _test_issue_and_resolve()
    _test_invalid_tokens_are_rejected()
    _test_reissue_replaces_old()
    _test_same_discord_id_coexists()
    _test_broken_file_does_not_crash()
    _test_user_key_split()
    _test_show_and_reissue_are_separate_tools()
    _test_discord_id_cannot_be_changed_from_web()
    _test_logout_clears_uuid_cookie()
    _test_admin_check_always_uses_token()
    _test_money_ops_notify_discord()
    _test_dm_is_requested_not_returned()
    _test_bot_action_queue_roundtrip()
    _test_dm_queue_keeps_dual_role_requests()
    _test_bot_actions_are_deduped()
    _test_reissue_queues_dm_before_revoking()
    _test_dashboard_url_tool_does_not_reissue()
    _test_url_never_posted_to_channel()

    passed = sum(1 for x in _results if x["passed"])
    for x in _results:
        print(json.dumps(x, ensure_ascii=False))
    print(json.dumps({"summary": True, "passed": passed, "total": len(_results)},
                     ensure_ascii=False))
    return passed == len(_results)


if __name__ == "__main__":
    sys.exit(0 if main() else 1)
