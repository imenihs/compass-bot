"""子の心身の安全（N-11.16）の回帰テスト。

最重要の不変条件は「**虐待の疑いが親チャンネルへ流れないこと**」である。
児童虐待の加害者は同居の実親であることが多く、親へ通知すると加害者への情報還流となり、
口封じ・報復・エスカレーションを招く。ここが壊れると子を加害者に売ることになるため、
実装を変更するたびに必ず確認する。

テストデータは全て**合成データ**である（実際の子の発言は使わない）。
"""
import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app import safety

_results = []


def _check(name, passed, detail=""):
    _results.append({"test": name, "passed": bool(passed), "detail": str(detail)[:160]})


def _test_python_floor():
    """Python の床が、明白な語を確実に拾い、技術用語を誤検知しないこと。"""
    # 拾うべきもの（カテゴリと親通知の可否）
    for text, want_cat, want_notify in [
        ("もう死にたい", "self_harm", True),
        ("お父さんに殴られた", "abuse", False),
        ("お母さんにたたかれる", "abuse", False),
        ("パパに蹴られた", "abuse", False),
        ("家に帰りたくない", "abuse", False),
        ("ごはんがもらえないんだ", "abuse", False),
        ("クラスの子に殴られた", "bullying", True),
        ("学校でいじめられてる", "bullying", True),
        ("写真を送ってって言われた", "grooming", True),
        ("簡単に稼げるバイトがあるらしい", "illegal_work", True),
        ("たばこ買いたい", "substance", True),
        ("下剤を買いたい", "eating", True),
        ("死 に た い", "self_harm", True),  # 分かち書きでの回避を防ぐ
    ]:
        r = safety.detect(text)
        ok = r is not None and r["category"] == want_cat and r["notify_parent"] == want_notify
        _check(f"floor_detect[{text[:14]}]", ok, f"got={r}")

    # 誤検知してはいけないもの（技術用語・日常語）
    for text in [
        "死にコードをリファクタリングすべき？",
        "プロセスをkillして殺した",
        "このバグ、殺しても殺しても復活する",
        "デバッグで死ぬほど疲れた",
        "HDDが死んでてSSDに換装したい",
        "サーバーが落ちて死んだ",
        "ゲームのキャラが死んだ",
        "お父さんに買ってもらったパソコン",
        "3000円のゲームがほしい",
        "かき氷のシロップ買った",
        "先生に怒られた",
    ]:
        _check(f"floor_no_false_positive[{text[:16]}]", safety.detect(text) is None,
               f"got={safety.detect(text)}")


def _test_merge_abuse_never_to_parent():
    """【最重要】虐待の疑いは、どの経路でも親へ通知されないこと。"""
    # AI が abuse と言った場合
    r = safety.merge_judgments(None, {"category": "abuse", "urgency": "urgent",
                                      "confidence": 0.9, "perpetrator": "family", "uncertain": False})
    _check("abuse_ai_not_to_parent", r and r["category"] == "abuse" and r["notify_parent"] is False, r)

    # Python 床だけが abuse を拾った場合
    r = safety.merge_judgments(safety.detect("お父さんに殴られた"),
                               {"category": "none", "urgency": "none", "confidence": 0.9,
                                "perpetrator": "unknown", "uncertain": False})
    _check("abuse_floor_not_to_parent", r and r["category"] == "abuse" and r["notify_parent"] is False, r)

    # AI がカテゴリを bullying と誤ラベルしても、加害者が家族なら虐待として扱う
    r = safety.merge_judgments(None, {"category": "bullying", "urgency": "high",
                                      "confidence": 0.8, "perpetrator": "family", "uncertain": False})
    _check("family_perpetrator_becomes_abuse",
           r and r["category"] == "abuse" and r["notify_parent"] is False, r)

    # 加害者が不明なら、家族の可能性を否定できないので親へ送らない
    r = safety.merge_judgments(None, {"category": "bullying", "urgency": "high",
                                      "confidence": 0.5, "perpetrator": "unknown", "uncertain": False})
    _check("unknown_perpetrator_not_to_parent", r and r["notify_parent"] is False, r)


def _test_merge_or_and_suppression():
    """OR 結合と、AI による誤検知の抑制が意図通りに働くこと。"""
    # AI が見逃しても Python 床が拾う
    r = safety.merge_judgments(safety.detect("もう死にたい"), None)
    _check("or_python_catches", r and r["category"] == "self_harm" and r["notify_parent"] is True, r)

    # Python 床がすり抜けても AI が拾う（方言）
    _check("floor_misses_dialect", safety.detect("おとんにどつかれてん") is None)
    r = safety.merge_judgments(None, {"category": "abuse", "urgency": "high",
                                      "confidence": 0.85, "perpetrator": "family", "uncertain": False})
    _check("or_ai_catches_dialect", r and r["category"] == "abuse", r)

    # AI が高い確信で否定 → 抑制（通知しないが記録は残す）
    r = safety.merge_judgments(safety.detect("この機能は消えたい…じゃなくて消したい"),
                               {"category": "none", "urgency": "none", "confidence": 0.95,
                                "perpetrator": "unknown", "uncertain": False})
    _check("suppress_high_confidence_denial",
           r and r["notify_parent"] is False and r.get("suppressed_by_ai") is True, r)

    # AI の否定でも確信が低ければ抑制しない（AI 自身が迷っている）
    r = safety.merge_judgments(safety.detect("消えたい"),
                               {"category": "none", "urgency": "none", "confidence": 0.4,
                                "perpetrator": "unknown", "uncertain": False})
    _check("no_suppress_low_confidence_denial",
           r and r["notify_parent"] is True and not r.get("suppressed_by_ai"), r)

    # 両方が検知なしなら None
    _check("both_none", safety.merge_judgments(None, {"category": "none", "confidence": 0.9}) is None)


def _test_notification_content():
    """親への通知が、第三者の実名を伏せ、本人の意向を明記すること。"""
    j = {"category": "bullying", "urgency": "high", "confidence": 0.85,
         "ai_reason": "クラスの子からの暴力の訴え。"}
    body = safety.build_parent_notification("たろう", j, "田中くんに殴られた", child_consent="refused")
    _check("redact_third_party_name", "田中" not in body, body[:120])
    _check("keeps_relation_hint", "同じくらいの子" in body, body[:120])
    _check("states_child_refused", "知らせたくない" in body, body[:160])
    _check("warns_not_to_interrogate", "問い詰め" in body, body[:200])

    # 確信度が低いときは正直に添える
    j2 = {"category": "self_harm", "urgency": "high", "confidence": 0.4, "ai_reason": "つらさの表出。"}
    body2 = safety.build_parent_notification("テスト", j2, "もう無理", child_consent="unknown")
    _check("notes_low_confidence", "確信が高くありません" in body2, body2[:200])

    # 性的な話題は原文を載せない（子を辱めない）
    j3 = {"category": "grooming", "urgency": "urgent", "confidence": 0.9, "ai_reason": "見知らぬ人からの接触。"}
    body3 = safety.build_parent_notification("はな", j3, "裸の写真を送ってと言われた", child_consent="unknown")
    _check("grooming_omits_raw_text", "裸の写真" not in body3, body3[:200])


def _test_hotlines_are_constants():
    """緊急連絡先が定数から出ること（LLM に番号を生成させない）。"""
    msg = safety.build_child_hotline_message({"category": "abuse", "urgency": "urgent", "hotline_key": "abuse"})
    _check("hotline_189", "189" in msg, msg[:120])
    _check("hotline_childline", "0120-99-7777" in msg, msg[:160])
    _check("hotline_emergency", "119" in msg, msg[:200])
    # 親へ伝えないと明示して安心を渡す
    _check("promises_not_to_tell_parent", "おうちの人に伝えることはしない" in msg, msg[:160])
    # 「おうちの人に言う」を提案しない（加害者でありうるため）
    _check("does_not_suggest_parent", "おうちの人に相談" not in msg, msg[:200])


def _test_routing():
    """実際の送信経路で、虐待が親チャンネルへ流れないこと。"""
    class Ch:
        def __init__(self): self.sent = []
        async def send(self, m, **k):
            self.sent.append(m)
            return type("M", (), {"id": 1})()

    async def run():
        from app import bot as B
        child_ch, parent_ch = Ch(), Ch()
        B.client.get_channel = lambda cid: parent_ch
        B.SAFETY_ALERT = {"enabled": True, "channel_id": 999}
        B.get_parent_ids = lambda: [111]
        B._log_runtime_event = lambda *a, **k: None
        msg = type("M", (), {"channel": child_ch, "author": type("A", (), {"id": 1})()})()
        conf = {"name": "たろう", "age": 13}

        # 虐待 → 子へのみ
        child_ch.sent.clear(); parent_ch.sent.clear()
        await B._handle_safety_signal({}, msg, conf, "お父さんに殴られた", {
            "category": "abuse", "urgency": "urgent", "notify_parent": False,
            "hotline_key": "abuse", "ai_reason": "家族からの暴力", "confidence": 0.9,
            "perpetrator": "family"})
        _check("route_abuse_to_child_only",
               len(child_ch.sent) == 1 and len(parent_ch.sent) == 0,
               f"child={len(child_ch.sent)} parent={len(parent_ch.sent)}")

        # いじめ → 親へのみ
        child_ch.sent.clear(); parent_ch.sent.clear()
        await B._handle_safety_signal({}, msg, conf, "クラスの子に殴られた", {
            "category": "bullying", "urgency": "high", "notify_parent": True,
            "hotline_key": "bullying", "ai_reason": "クラスの子からの暴力", "confidence": 0.9,
            "perpetrator": "outside"})
        _check("route_bullying_to_parent_only",
               len(child_ch.sent) == 0 and len(parent_ch.sent) == 1,
               f"child={len(child_ch.sent)} parent={len(parent_ch.sent)}")

        # 抑制された誤検知 → どちらにも送らない
        child_ch.sent.clear(); parent_ch.sent.clear()
        await B._handle_safety_signal({}, msg, conf, "この機能は消えたい", {
            "category": "self_harm", "urgency": "urgent", "notify_parent": False,
            "suppressed_by_ai": True, "ai_reason": "言い間違い", "confidence": 0.95})
        _check("route_suppressed_sends_nothing",
               len(child_ch.sent) == 0 and len(parent_ch.sent) == 0,
               f"child={len(child_ch.sent)} parent={len(parent_ch.sent)}")

    asyncio.run(run())


def main():
    _test_python_floor()
    _test_merge_abuse_never_to_parent()
    _test_merge_or_and_suppression()
    _test_notification_content()
    _test_hotlines_are_constants()
    _test_routing()
    passed = sum(1 for x in _results if x["passed"])
    for x in _results:
        print(json.dumps(x, ensure_ascii=False))
    print(json.dumps({"summary": True, "passed": passed, "total": len(_results)}, ensure_ascii=False))
    # 集計表示だけでは失敗が exit 0 に埋もれるため、終了コードへ必ず反映する
    return passed == len(_results)


if __name__ == "__main__":
    sys.exit(0 if main() else 1)
