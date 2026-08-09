"""codex safety blocker回帰防止: 親の自然文中のコマンド文字列で実残高が動かないことを検証する。

re.search(部分一致)だと「昨日 支給 はな 300円 ってやった?」の文中コマンドにマッチし、親の疑問・引用・
説明の自然文で子の実残高が動く越境が成立していた。re.fullmatch(厳密一致)+子限定に修正した。
このテストは handlers_parent の maybe_handle_* を直接呼び、厳密コマンドは実行・文中コマンドは非実行を固定する。
"""
import asyncio
import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app import config, handlers_parent

_results = []
def _check(name, passed, detail=""):
    _results.append({"test": name, "passed": bool(passed), "detail": detail})


class _FakeAuthor:
    def __init__(self, uid): self.id = uid
class _FakeChannel:
    def __init__(self): self.sent = []
    async def send(self, msg): self.sent.append(msg)
_MSG_ID_SEQ = [0]


class _FakeMessage:
    """Discord メッセージの代用。

    id は**毎回変える**。冪等キーが Discord のメッセージ ID から作られるため、
    固定値にすると2件目以降が「同じ操作の再送」として弾かれ、
    実行されるはずのコマンドが実行されない（実際にこれで検証が空振りした）。
    """

    def __init__(self, content, uid):
        self.content = content
        self.author = _FakeAuthor(uid)
        self.channel = _FakeChannel()
        _MSG_ID_SEQ[0] += 1
        self.id = 123456789 + _MSG_ID_SEQ[0]


def _setup(tmp):
    (tmp / "settings" / "users" / "parents").mkdir(parents=True, exist_ok=True)
    (tmp / "data").mkdir(parents=True, exist_ok=True)
    (tmp / "settings" / "users" / "rika.json").write_text(
        json.dumps({"name": "はな", "age": 7, "discord_user_id": 111}, ensure_ascii=False), encoding="utf-8")
    (tmp / "settings" / "users" / "parents" / "oya.json").write_text(
        json.dumps({"name": "とうちゃん", "discord_user_id": 999}, ensure_ascii=False), encoding="utf-8")
    (tmp / "settings" / "setting.json").write_text(json.dumps({}, ensure_ascii=False), encoding="utf-8")
    (tmp / "settings" / "system.json").write_text(json.dumps({"log_dir": str(tmp / "data")}, ensure_ascii=False), encoding="utf-8")
    (tmp / "data" / "wallet_state.json").write_text(
        json.dumps({"users": {"はな": {"expected_balance": 1000}}, "applied_operation_keys": {}}, ensure_ascii=False), encoding="utf-8")
    config.SETTINGS_DIR = tmp / "settings"
    config.USERS_DIR = config.SETTINGS_DIR / "users"
    config.PARENTS_DIR = config.USERS_DIR / "parents"
    config.SYSTEM_PATH = config.SETTINGS_DIR / "system.json"
    config.SETTING_PATH = config.SETTINGS_DIR / "setting.json"


def _run():
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        _setup(tmp)
        import app.wallet_service as ws
        w = ws.WalletService()
        w.wallet_state_path = tmp / "data" / "wallet_state.json"
        w.wallet_audit_state_path = tmp / "data" / "wallet_audit_state.json"
        handlers_parent._wallet_service = w
        handlers_parent._parent_ids_cache = None
        # 親判定を差し替え(999を親に)
        handlers_parent._is_parent = lambda uid: uid == 999
        # _command_body がメンション除去に _client.user を使うため、先に差し替える
        handlers_parent._client = type("C", (), {
            "user": type("U", (), {"id": 1, "name": "compass-bot", "discriminator": "0"})()})()

        def bal():
            return w.get_balance("はな")

        # --- 文中コマンド(引用・疑問・説明)は実行されない ---
        cases_block = [
            "昨日の話だけど「支給 はな 300円」ってもう反映したんだっけ？",
            "支給 はな 300円 は昨日やったよ",
            "残高調整 はな -500円 ってどういう意味？",
            "この前 設定変更 はな 固定 800円 にしたよね",
        ]
        for c in cases_block:
            before = bal()
            msg = _FakeMessage(c, 999)
            # 3ハンドラを順に試す(bot.pyの順序を模擬)
            asyncio.get_event_loop().run_until_complete(handlers_parent.maybe_handle_manual_grant(msg, c))
            asyncio.get_event_loop().run_until_complete(handlers_parent.maybe_handle_balance_adjustment(msg, c))
            asyncio.get_event_loop().run_until_complete(handlers_parent.maybe_handle_user_setting_change(msg, c))
            _check(f"block::{c[:20]}", bal() == before, f"balance moved {before}->{bal()} on: {c}")

        # --- 厳密コマンドは受理され、その場で実行される ---
        # 確認ステップは置かない（お小遣い管理で毎回2ターンは割に合わない）。
        # 取り違えは台帳に残るので後から追える。
        before = bal()
        c = "支給 はな 200円"
        msg = _FakeMessage(c, 999)
        handled = asyncio.get_event_loop().run_until_complete(
            handlers_parent.maybe_handle_manual_grant(msg, c))
        _check("exact_grant_applied", handled and bal() == before + 200, f"{before}->{bal()}")

        before = bal()
        c = "残高調整 はな -100円"
        msg = _FakeMessage(c, 999)
        handled = asyncio.get_event_loop().run_until_complete(
            handlers_parent.maybe_handle_balance_adjustment(msg, c))
        _check("exact_adjust_applied", handled and bal() == before - 100, f"{before}->{bal()}")
        before = bal()
        c = "支給 はな 200円"
        msg = _FakeMessage(c, 999)
        before = bal()
        handled = asyncio.get_event_loop().run_until_complete(
            handlers_parent.maybe_handle_manual_grant(msg, c))
        _check("exact_grant_applied", handled and bal() == before + 200, f"{before}->{bal()}")

        # --- 厳密コマンド(残高調整)もその場で実行される ---
        before = bal()
        c = "残高調整 はな -100円"
        msg = _FakeMessage(c, 999)
        handled = asyncio.get_event_loop().run_until_complete(
            handlers_parent.maybe_handle_balance_adjustment(msg, c))
        _check("exact_adjust_applied", handled and bal() == before - 100, f"{before}->{bal()}")

    # 固定コマンドが完全一致で判定され、順序依存が無いこと（N-11.17）
    from app import handlers_parent as _H

    class _U:
        id = 1
        name = "compass-bot"
        discriminator = "0"

    _orig_client = _H._client
    _H._client = type("C", (), {"user": _U()})()
    try:
        # 完全一致したときだけ発火する（疑問文・文中では発火しない）
        for _t, _want in [("全体確認", True), ("ぜんたいかくにん", True),
                          ("全体確認ってどうやるの？", False), ("あとで全体確認するね", False)]:
            _got = _H._is_exact_command(_t, "全体確認", "ぜんたいかくにん")
            _check(f"exact_dashboard::{_t[:14]}", _got is _want, _got)
        # 「使い方の説明」と「使い方の説明と初期設定」が否定条件なしで排他になる
        for _t, _ws, _wb in [("使い方の説明", True, False),
                             ("使い方の説明と初期設定", False, True)]:
            _s1 = _H._is_exact_command(_t, "使い方の説明", "つかいかたのせつめい")
            _s2 = _H._is_exact_command(_t, "使い方の説明と初期設定",
                                       "つかいかたのせつめいとしょきせってい")
            _check(f"exact_usage_exclusive::{_t[:12]}",
                   _s1 is _ws and _s2 is _wb, f"single={_s1} broad={_s2}")
    finally:
        _H._client = _orig_client





def _test_followup_policy_redirects_to_web():
    """AI フォロー方針の変更は Web へ誘導し、チャットでは保存しないこと。

    チャットで細かい設定を受け付けるのをやめた（N-11.17）。
    言葉から「指示」と「質問」を見分けるのは実務上できず、
    語彙を調整するたび「軽めだっけ で設定が変わる」と
    「とりあえず軽めで が無視される」の間を往復した（4周）。
    Web には同じ設定の選択式フォームが既にあり、値が曖昧にならない。
    実ログでも親はこのコマンドを使っていない（親の発話48件中0件）。

    現在値の確認だけはチャットで答える（見るだけなら曖昧さが無いため）。
    """
    import asyncio

    from app import handlers_parent as H

    sent = []

    class _Ch:
        async def send(self, msg, **kw):
            sent.append(msg)
            return type("M", (), {"id": 1})()

    def _msg():
        return type("M", (), {"channel": _Ch(),
                              "author": type("A", (), {"id": 999})(), "id": 1})()

    orig_parent, orig_find, orig_client = H._is_parent, H.find_user_by_name, H._client
    H._is_parent = lambda uid: True
    H.find_user_by_name = lambda n: {"name": n, "ai_follow_policy": {}} if n == "たろう" else None
    H._client = type("C", (), {"user": type("U", (), {"id": 1, "name": "compass-bot"})()})()
    try:
        # 設定を変えようとしても、チャットでは保存せず Web へ案内する
        for text in ["フォロー方針 たろう 軽め", "フォロー強さ たろう 普通",
                     "フォロー方針 たろう 軽めだっけ", "フォロー方針 たろう とりあえず軽めで"]:
            sent.clear()
            handled = asyncio.new_event_loop().run_until_complete(
                H.maybe_handle_followup_policy(_msg(), text))
            _check(f"followup_redirects[{text[:20]}]",
                   handled and any("http" in m for m in sent), sent[:1])

        # 現在値の確認はその場で答える
        sent.clear()
        asyncio.new_event_loop().run_until_complete(
            H.maybe_handle_followup_policy(_msg(), "フォロー方針 たろう"))
        _check("followup_shows_current", any("たろう" in m for m in sent), sent[:1])
    finally:
        H._is_parent, H.find_user_by_name, H._client = orig_parent, orig_find, orig_client


def _report():
    """全テストを走らせたあとに結果をまとめて出す。

    _run の中で集計すると、_run より後ろで定義したテストが集計に入らない
    （実際に2件が呼ばれないまま「全件PASS」に見えていた）。
    追加したテストが確実に走ることを担保するため、呼び出しと集計をここへ集める。
    """

    _test_followup_policy_redirects_to_web()

    passed = sum(1 for x in _results if x["passed"])
    for x in _results:
        print(json.dumps(x, ensure_ascii=False))
    print(json.dumps({"summary": True, "passed": passed, "total": len(_results)}, ensure_ascii=False))
    return passed == len(_results)


if __name__ == "__main__":
    _run()
    sys.exit(0 if _report() else 1)
