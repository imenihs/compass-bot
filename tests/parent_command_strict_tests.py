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
class _FakeMessage:
    def __init__(self, content, uid):
        self.content = content
        self.author = _FakeAuthor(uid)
        self.channel = _FakeChannel()
        self.id = 123456789


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

        # --- 厳密コマンドは「受理される」が、その場では実行せず確認を出す ---
        # N-11.17 で経路をそろえた。以前はコマンドだけ即実行で、AI 経路にだけ確認があり、
        # 「AI 経由なら桁の打ち間違いを親が捕まえられるが、コマンドなら素通り」だった。
        from app import parent_confirm as pc

        pc.clear_pending(999)
        before = bal()
        c = "支給 はな 200円"
        msg = _FakeMessage(c, 999)
        handled = asyncio.get_event_loop().run_until_complete(handlers_parent.maybe_handle_manual_grant(msg, c))
        _check("exact_grant_accepted", bool(handled), handled)
        _check("exact_grant_defers_to_confirm", bal() == before, f"{before}->{bal()}")
        rec = pc.take_pending(999)
        _check("exact_grant_pending_values",
               rec and rec["action"] == "parent_grant"
               and rec["args"]["name"] == "はな" and rec["args"]["amount"] == 200, rec)

        # --- 厳密コマンド(残高調整)も同じく確認を挟む ---
        pc.clear_pending(999)
        before = bal()
        c = "残高調整 はな -100円"
        msg = _FakeMessage(c, 999)
        handled = asyncio.get_event_loop().run_until_complete(handlers_parent.maybe_handle_balance_adjustment(msg, c))
        _check("exact_adjust_accepted", bool(handled), handled)
        _check("exact_adjust_defers_to_confirm", bal() == before, f"{before}->{bal()}")
        rec = pc.take_pending(999)
        _check("exact_adjust_pending_values",
               rec and rec["action"] == "parent_adjust_balance"
               and rec["args"]["delta"] == -100, rec)
        pc.clear_pending(999)

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


def _test_followup_policy_question_does_not_write():
    """親の疑問文で AI フォロー方針が書き換わらないこと。

    正規表現が DOTALL + `(.+)$` で行末まで飲むため、
    「フォロー方針 <名前> 軽め って設定したっけ？」が
    値「軽め って設定したっけ？」として通り、**実設定が書き換わっていた**。
    さらに疑問文そのものが parent_note として保存され、
    子への AI プロンプトに載るという二重の実害があった。
    質問は設定変更ではなく会話（AI 経路）へ流すのが正しい。
    """
    from app import handlers_parent as H

    for text in ["軽め って設定したっけ？", "軽めかな", "普通ですか", "軽め って言った?",
                 "軽め って設定した", "軽めだっけ", "今どうなってる",
                 "普通にしてましたか", "軽めになってる", "今どんな感じ",
                 "設定は軽めですよね", "軽めだよね", "軽めのままかしら",
                 "方針どうなってるの"]:
        # 注: 「今の方針は」のような助詞止めは、ここ（語尾判定）では捕まえない。
        # 「食事のことだけは」のような正当な指示文まで巻き込むため。
        # 書き換わらないことは _test_followup_policy_write_decision で担保する。
        _check(f"followup_question_blocked[{text[:16]}]",
               H._looks_like_question(text) is True, text)

    # parent_note は自由文なので、長めの指示文も正しく通ること。
    # 当初 marker を部分一致で見ていたため「勉強のこ*とは*あまり」が「とは」に、
    # 「元気*なの*で」が「なの」に当たり、正当な指示文を弾いていた（有識者反証で発見）。
    for text in ["軽め", "普通", "必要なときだけ", "軽め にして",
                 "軽め 勉強のことはあまり言わないで", "普通 元気なので見守って",
                 "軽め 本人のペースを尊重してほしい", "普通 お金の使い方だけ見てあげて",
                 "軽め そっとしておいて", "普通 ゲームの時間だけ気にかけて"]:
        _check(f"followup_value_allowed[{text[:16]}]",
               H._looks_like_question(text) is False, text)


def _test_followup_policy_write_decision():
    """「実際に設定が書き換わるか」で判定する（語尾判定だけに頼らない）。

    質問ガードは語尾のゆらぎに弱く、語彙を足すと今度は正当な指示文を弾く
    （「食事のことだけは」が「は」に当たる等）。いたちごっこになるため、
    **設定語は値の先頭にあるときだけ拾う**という構造側の防御を主にした。
    指示なら「軽め …」と先頭に来るのが自然で、質問文では先頭に来ない。
    質問ガードと合わせて二重に防ぐ。ここでは両方を通した最終結果を固定する。
    """
    from app import handlers_parent as H

    def _writes(text):
        if H._looks_like_question(text):
            return False
        updates, _note = H._parse_follow_policy_updates(text)
        return bool(updates)

    for text in ["今の方針は", "設定は", "いまの設定教えて", "普通にしてましたか",
                 "軽めになってる", "今どんな感じ", "設定は軽めですよね", "軽めだよね",
                 "軽めのままかしら", "方針どうなってるの", "軽めだっけ", "どうなってる",
                 "軽め って設定したっけ？", "軽めかな", "普通ですか", "現在の設定は",
                 "軽めになってる？", "今の方針は軽めだっけ", "普通にしてたよね",
                 # 回想・確認の言い回し（指示と同じ語を含むが現状の確認をしている）
                 "軽めにしてたのって合ってる", "軽めって前に言ったやつ",
                 "軽めにしてたと思うんだけど", "普通だと思ってた", "軽めじゃなかった"]:
        _check(f"policy_question_no_write[{text[:16]}]", _writes(text) is False, text)

    # 設定語は文頭に来るとは限らない。一時「先頭にあるときだけ拾う」構造にしたところ、
    # 「とりあえず軽めで」「今日から軽めにして」を大量に取りこぼした（有識者の再々反証）。
    # しかも親には「保存したよ」と出るため誤りに気づけず、指示文が
    # parent_note として子の AI プロンプトに載るという二重の実害があった。
    for text in ["軽め", "普通", "必要なときだけ",
                 "軽め 宿題のことは言わないで", "普通 ゲームのことは本人に任せて",
                 "軽め お金のことはしっかり見て", "普通 早寝のことだけは言ってね",
                 "軽め そっと見守ってほしいな", "軽め 食事のことだけは",
                 "普通 なにかあったら教えて", "軽め 何も言わないで",
                 "普通 元気なので見守って", "軽め 勉強のことはあまり言わないで",
                 # 設定語が文頭に無い自然な指示
                 "強さは軽めで", "方針は軽めにして", "とりあえず軽めで",
                 "今日から軽めにして", "これからは普通で", "次から軽めでお願い",
                 "できれば軽めにしてほしい", "なるべく軽めで", "もう少し軽めにして",
                 "やっぱり普通に戻して", "一旦軽めで", "当面は軽めで"]:
        _check(f"policy_instruction_writes[{text[:16]}]", _writes(text) is True, text)


def _test_followup_note_only_update():
    """申し送り（parent_note）だけの更新が保存できること。

    「変化が無ければ保存しない」歯止めを入れたとき、条件を
    「設定語が取れなければ保存しない」にしてしまい、
    「最近ゲームばかりで心配」のような**申し送りだけの更新を潰していた**
    （有識者の4周目反証で発見）。note に300文字制限と安全語チェックがあることから、
    単独更新はもともと想定されている機能である。
    条件を「何も変わらないなら保存しない」に直して両立させた。
    """
    from app import handlers_parent as H

    current = {"parent_note": "", "nudge_strength": "normal", "frequency": "normal",
               "enabled": True, "focus_area": "balanced"}

    def _saved(text):
        if H._looks_like_question(text):
            return False
        updates, note = H._parse_follow_policy_updates(text)
        note_changed = note.strip() != str(current.get("parent_note", "")).strip()
        return bool(updates) or note_changed

    # 申し送りだけ（設定語を含まない）でも保存される
    for text in ["最近ゲームばかりで心配", "来週から塾が始まるよ",
                 "友達関係で悩んでるみたい", "テスト期間だから見守って",
                 "買い物の練習をさせたい", "困ったことがあったら教えてあげて"]:
        _check(f"note_only_saved[{text[:16]}]", _saved(text) is True, text)

    # 現状を尋ねる言い方は、申し送りとして保存してはいけない
    for text in ["今の方針は", "現在の設定は", "設定は", "強さは",
                 "いまの設定教えて", "今の設定見せて"]:
        _check(f"note_only_question_not_saved[{text[:16]}]", _saved(text) is False, text)


def _report():
    """全テストを走らせたあとに結果をまとめて出す。

    _run の中で集計すると、_run より後ろで定義したテストが集計に入らない
    （実際に2件が呼ばれないまま「全件PASS」に見えていた）。
    追加したテストが確実に走ることを担保するため、呼び出しと集計をここへ集める。
    """
    _test_followup_policy_question_does_not_write()
    _test_followup_policy_write_decision()
    _test_followup_note_only_update()

    passed = sum(1 for x in _results if x["passed"])
    for x in _results:
        print(json.dumps(x, ensure_ascii=False))
    print(json.dumps({"summary": True, "passed": passed, "total": len(_results)}, ensure_ascii=False))
    return passed == len(_results)


if __name__ == "__main__":
    _run()
    sys.exit(0 if _report() else 1)
