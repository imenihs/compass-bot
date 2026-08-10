"""公的相談窓口の定数と、安全に関する「守れる約束」の回帰テスト。

**2026/08/11 に全面的に書き直した。**
以前は正規表現による危険信号の検知（898行）を検証していたが、その機構を全廃したため。
廃止の理由は docs/設計_安全機能の再設計.md を参照。要点だけ:

  ・拾えるのは直球だけで、遠回しな表現は素通り。一方で日常語に誤爆する
  ・親が子チャンネルに在室しているため、「親には伝えない」系の機構が成立していなかった

いま守るべきものは3つに絞られる。

  ① 電話番号は定数から返す（AI に生成させない）
  ② 守れない約束をプロンプトに書かない
  ③ 危険信号の判定機構が復活していない（語彙リストの再導入を検出する）
"""
import ast
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

_results = []


def _check(name, passed, detail=""):
    """1件の判定を記録する。"""
    _results.append({"test": name, "passed": bool(passed), "detail": str(detail)[:200]})


def _test_hotlines_are_constants():
    """窓口の電話番号が定数として存在し、tool から取れること。

    **AI に番号を生成させない**ための最重要の守り。
    自殺予防やいじめ相談の文脈で存在しない番号を渡すのは、最悪の失敗になる。
    """
    from app import safety

    for kind in ("abuse", "self_harm", "bullying"):
        text = safety.hotlines_for(kind)
        _check(f"hotlines_exist::{kind}", bool(text and text.strip()), text[:40])
        # 実在の番号が含まれること（公式サイトで確認した値）
        _check(f"hotlines_has_number::{kind}", any(n in text for n in
               ("189", "0120-0-78310", "0120-99-7777", "0120-279-338")), text[:60])

    # 未知の種別でも黙らない（無言は最悪なので必ず何か返す）
    _check("hotlines_unknown_kind_not_empty",
           bool(safety.hotlines_for("unknown-kind")), safety.hotlines_for("unknown-kind")[:40])

    # 緊急時の案内
    _check("emergency_line_exists", "119" in safety.EMERGENCY_LINE and "110" in safety.EMERGENCY_LINE,
           safety.EMERGENCY_LINE)


def _test_hotlines_tool_returns_constant():
    """get_hotlines tool が定数をそのまま返すこと。"""
    from app import mcp_wallet as m

    _check("get_hotlines_registered", "get_hotlines" in m._HANDLERS, sorted(m._HANDLERS)[:3])
    out = m._do_get_hotlines({"kind": "self_harm"})
    _check("get_hotlines_returns_number", "0120" in out or "189" in out, out[:60])
    # 種別が壊れていても無言にしない
    fallback = m._do_get_hotlines({"kind": ""})
    _check("get_hotlines_handles_bad_kind", bool(fallback.strip()), fallback[:40])


def _test_no_unkeepable_promises():
    """守れない約束がプロンプトに残っていないこと。

    親が子チャンネルに在室しているため、「おうちの人には伝えない」「ここだけの話」は
    **構造的に守れない**。守れない約束は、破れたときに子が
    「大人は嘘をつく」を学習し、次の告白経路まで潰す。
    """
    prompt = (ROOT / "app/conv/ai_conversation.py").read_text(encoding="utf-8")

    # 「〜しない」と約束する形が無いこと。
    # ただし「言ってはいけない」と禁止する文の中の引用は許す（それは是正の記述のため）
    # 「〜と言ってはいけない」という**禁止文の中の引用**は許す（是正の記述のため）。
    # 危ないのは AI へ「そう言え」と指示している形なので、そちらだけを検出する。
    bad_instructions = [
        "ここだけの話にしておくね」と伝え",
        "そのままおうちの人に伝えることはしない**と伝えて",
        "ここだけの話にしておくよ」と言",
    ]
    for phrase in bad_instructions:
        _check(f"no_unkeepable::{phrase[:16]}", phrase not in prompt, phrase)
    # 禁止として書かれていることは確認する
    _check("forbids_secret_promise", "絶対に言ってはいけない" in prompt, "禁止の明記が無い")

    # 「何も送らない」とは言えない（査定の理由は親チャンネルへ届く）。
    # 送るものがあることを先に伝えているか
    _check("discloses_what_is_forwarded", "その理由だけは伝えるよ" in prompt,
           "査定の理由が親へ届くことを先に言っていない")
    _check("no_absolute_no_forward",
           "きみが話してくれたことを、私から別のチャンネルへ送ることはしないよ" not in prompt,
           "守れない「何も送らない」が残っている")

    # 逆に、正直に伝えるルールが入っていること
    for must in ("おうちの人も見ることがある", "守れない約束をしない", "いつも見ているわけじゃない"):
        _check(f"has_honest_rule::{must[:14]}", must in prompt, must)

    # 低学年向けは肯定形にする（二重否定だと意味を逆に取る恐れがある）
    _check("low_age_uses_positive_form",
           "おうちの人もいっしょに見ているところだよ" in prompt, "低学年向けの文言が無い")

    # 番号は tool から取る
    _check("prompt_points_to_get_hotlines", "get_hotlines" in prompt, "get_hotlines への誘導が無い")


def _test_detection_machinery_is_gone():
    """危険信号の判定機構が復活していないこと。

    語彙リストを足す方向へ戻ると、また「直球しか拾えず日常語に誤爆する」状態になる。
    実測で収束しないことが分かっているので、再導入を検出する。
    """
    tree = ast.parse((ROOT / "app/safety.py").read_text(encoding="utf-8"))
    names = {n.name for n in tree.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}
    consts = {t.id for n in tree.body if isinstance(n, ast.Assign)
              for t in n.targets if isinstance(t, ast.Name)}

    gone_funcs = {"detect", "merge_judgments", "build_parent_notification",
                  "redact_third_party", "should_send_alert", "classify_consent",
                  "summarize_long_term_signals", "build_ai_judge_prompt"}
    for fn in gone_funcs:
        _check(f"removed_func::{fn}", fn not in names, fn)

    gone_consts = {"_ABUSE", "_BULLYING", "_URGENT_SELF_HARM", "_GROOMING",
                   "_VIOLENCE", "_FAMILY", "_COMPILED"}
    for c in gone_consts:
        _check(f"removed_const::{c}", c not in consts, c)

    # 残すべきものは残っている
    _check("kept_hotlines", "HOTLINES" in consts, sorted(consts))
    _check("kept_hotlines_for", "hotlines_for" in names, sorted(names))

    # ファイルが小さくなっていること（語彙リストが戻ると必ず膨らむ）
    lines = len((ROOT / "app/safety.py").read_text(encoding="utf-8").splitlines())
    _check("file_stays_small", lines < 150, f"{lines}行")


def _test_no_orphaned_references():
    """削除した機能への参照が残っていないこと（起動時・実行時の落ちを防ぐ）。

    会話中 import のため、参照が残っていても**起動時には落ちない**。
    子や親が話しかけた瞬間に ImportError になる遅延爆弾を作らないよう、機械的に検査する。
    """
    import subprocess

    dead = ["judge_safety", "_handle_safety_signal", "_load_safety_history",
            "get_safety_alert_setting", "parent_safety_setup_check",
            "_run_safety_setup_check", "safety_task"]
    for name in dead:
        r = subprocess.run(["grep", "-rn", name, "app/", "--include=*.py"],
                           cwd=str(ROOT), capture_output=True, text=True)
        # コメント行だけなら許容する（経緯の記録として残すため）
        hits = [ln for ln in r.stdout.splitlines()
                if ln.split(":", 2)[-1].strip() and not ln.split(":", 2)[-1].strip().startswith("#")]
        _check(f"no_orphan::{name}", not hits, hits[:2])


def _test_money_safety_tool_exists():
    """金銭にまつわる困りごとを受ける tool が生きていること。

    社長の判断「いじめを受けて支出する可能性は否定できないから、その程度はあってもいい」に対応する。
    辞書ではなく AI が意味で判断して呼ぶ。
    """
    from app import mcp_wallet as m
    from app.conv.ai_conversation import ALLOWED_WALLET_TOOLS

    _check("money_safety_tool_registered",
           "record_money_safety_concern" in m._HANDLERS, "未登録")
    _check("money_safety_tool_allowed",
           "mcp__wallet__record_money_safety_concern" in ALLOWED_WALLET_TOOLS, "許可リストに無い")
    _check("get_hotlines_allowed",
           "mcp__wallet__get_hotlines" in ALLOWED_WALLET_TOOLS, "許可リストに無い")


def _test_concern_writer_matches_reader():
    """相談の記録が、ナッジ抑止（読む側）に**実際に届く**こと。

    writer（tool）と reader（reminder_service）でキー名が食い違うと、
    どちらのテストも単体では PASS するのに**結線だけが死ぬ**。
    実際この形のバグが過去にあった（書き手が `selected_user` を出さず、
    読み手がそれで絞っていたため一度も一致しなかった）。
    ここでは writer で書いて reader で読む、通しで検証する。
    """
    import json as _json
    import shutil
    import tempfile
    from datetime import datetime, timedelta, timezone

    from app import config, mcp_wallet as m
    from app.reminder_service import ReminderService

    tmp = Path(tempfile.mkdtemp())
    (tmp / "logs").mkdir()
    (tmp / "settings").mkdir()
    (tmp / "settings" / "system.json").write_text(
        _json.dumps({"log_dir": str(tmp / "logs")}), encoding="utf-8")
    (tmp / "settings" / "setting.json").write_text("{}", encoding="utf-8")

    orig = (config.SYSTEM_PATH, config.SETTING_PATH, m._resolve_child)
    config.SYSTEM_PATH = tmp / "settings" / "system.json"
    config.SETTING_PATH = tmp / "settings" / "setting.json"
    m._resolve_child = lambda n=None: {"name": "たろう"}
    try:
        service = ReminderService(
            client=None, allowance_reminder_conf={}, wallet_audit_conf={},
            load_all_users_fn=lambda: [], wallet_service=None, allow_channel_ids=set(),
        )
        now = datetime.now(timezone(timedelta(hours=9)))
        logs = tmp / "logs"

        # 何も無ければ通常どおり送る
        _check("nudge_not_blocked_when_empty",
               service._has_recent_safety_signal(logs, "たろう", now) is False, "空で True になった")

        # ① お金の困りごと tool が書いたら、抑止が効く
        m._do_record_money_safety_concern(
            {"name": "たろう", "kind": "money_taken", "operation_key": "k1"})
        _check("nudge_blocked_by_money_concern",
               service._has_recent_safety_signal(logs, "たろう", now) is True,
               "money_taken を書いても抑止が効かない")

        # ② 窓口を渡した事実でも抑止が効く（つらさ・いじめ・家のこと）
        shutil.rmtree(logs); logs.mkdir()
        m._do_get_hotlines({"kind": "self_harm"})
        _check("nudge_blocked_by_hotlines",
               service._has_recent_safety_signal(logs, "たろう", now) is True,
               "get_hotlines を呼んでも抑止が効かない（設計の結線漏れ）")

        # ③ 別の子は巻き込まない
        _check("nudge_not_blocked_for_other_child",
               service._has_recent_safety_signal(logs, "はな", now) is False, "他の子まで止まる")
    finally:
        config.SYSTEM_PATH, config.SETTING_PATH, m._resolve_child = orig
        shutil.rmtree(tmp, ignore_errors=True)


def _test_hotlines_always_include_emergency():
    """緊急通報先（119/110）が**必ず**案内に含まれること。

    以前は `urgent=True` のときだけ添える作りだったが、
    危険度の判定機構を廃止した今「誰が urgent を決めるか」が宙に浮く。
    実際 tool は urgent を渡しておらず、**119/110 が子に一度も届かなかった**。
    """
    from app import mcp_wallet as m, safety

    for kind in ("abuse", "self_harm", "bullying", "unknown"):
        out = safety.hotlines_for(kind)
        _check(f"emergency_in_hotlines::{kind}", "119" in out and "110" in out, out[-40:])

    tool_out = m._do_get_hotlines({"kind": "self_harm"})
    _check("emergency_in_tool_output", "119" in tool_out and "110" in tool_out, tool_out[-40:])


def _test_money_concern_moves_no_balance():
    """お金の困りごとの記録が、残高を1円も動かさないこと。

    「取られた」を支出として記録すると、被害者が金銭的にも二重に損をする。
    """
    import json as _json
    import shutil
    import tempfile

    from app import config, mcp_wallet as m
    import app.wallet_service as ws

    tmp = Path(tempfile.mkdtemp())
    (tmp / "logs").mkdir()
    (tmp / "settings").mkdir()
    (tmp / "settings" / "system.json").write_text(
        _json.dumps({"log_dir": str(tmp / "logs")}), encoding="utf-8")
    (tmp / "settings" / "setting.json").write_text("{}", encoding="utf-8")
    (tmp / "w.json").write_text(_json.dumps(
        {"users": {"たろう": {"expected_balance": 3000}}, "applied_operation_keys": {}},
        ensure_ascii=False), encoding="utf-8")

    orig = (config.SYSTEM_PATH, config.SETTING_PATH, m._wallet, m._resolve_child)
    config.SYSTEM_PATH = tmp / "settings" / "system.json"
    config.SETTING_PATH = tmp / "settings" / "setting.json"
    w = ws.WalletService()
    w.wallet_state_path = tmp / "w.json"
    w.wallet_audit_state_path = tmp / "a.json"
    m._wallet = w
    m._resolve_child = lambda n=None: {"name": "たろう"}
    try:
        for kind in ("money_taken", "suspicious_offer", "illegal_work"):
            m._do_record_money_safety_concern(
                {"name": "たろう", "kind": kind, "operation_key": f"k-{kind}"})
        _check("money_concern_keeps_balance", w.get_balance("たろう") == 3000,
               w.get_balance("たろう"))
        # 同じ相談は二度書かない
        again = m._do_record_money_safety_concern(
            {"name": "たろう", "kind": "money_taken", "operation_key": "k-money_taken"})
        _check("money_concern_is_idempotent", "さっき聞いた" in again, again[:30])
    finally:
        config.SYSTEM_PATH, config.SETTING_PATH, m._wallet, m._resolve_child = orig
        shutil.rmtree(tmp, ignore_errors=True)


def main():
    """全テストを走らせて結果を出す。"""
    _test_hotlines_are_constants()
    _test_hotlines_tool_returns_constant()
    _test_no_unkeepable_promises()
    _test_detection_machinery_is_gone()
    _test_no_orphaned_references()
    _test_money_safety_tool_exists()
    _test_concern_writer_matches_reader()
    _test_hotlines_always_include_emergency()
    _test_money_concern_moves_no_balance()

    passed = sum(1 for x in _results if x["passed"])
    for x in _results:
        print(json.dumps(x, ensure_ascii=False))
    print(json.dumps({"summary": True, "passed": passed, "total": len(_results)},
                     ensure_ascii=False))
    return passed == len(_results)


if __name__ == "__main__":
    sys.exit(0 if main() else 1)
