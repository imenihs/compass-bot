"""思考混入の検知(_detect_thinking_leak)の回帰テスト。

2026/08/09 に方針を転換した。従来は応答冒頭の英語思考ブロックを正規表現で除去していたが、
(1)実機の漏れ方(先頭が日本語)を検出できず素通りさせ、(2)除去が効きすぎると本文が空になり
「もう一度話しかけて」を連発する二次被害を出した。原因は CLI 2.1.179 の既知バグであり
2.1.226 で解消を確認したため、出口での加工をやめて検知のみに切り替えた。

このテストは「実機で実際に漏れた形を検知できること」と「正常な日本語応答を誤検知しないこと」を守る。
誤検知は診断ログを汚すだけで会話は壊れないが、狼少年になると再発時に気づけなくなるため重要。
"""
import json, sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.conv.ai_conversation import _detect_thinking_leak as detect

_results = []
def _check(n, p, d=""): _results.append({"test": n, "passed": bool(p), "detail": d})

def _run():
    # (名前, 入力, 検知されるべきか)
    cases = [
        # --- 実機で実際に漏れた形。必ず検知する ---
        ("real_leak_th_friends",
         'thJust "friends have it" — no effort, no real reason. Don\'t propose allowance.そっか、お友だちが持ってるといいなって思うよね。', True),
        ("real_leak_th_persistent",
         "thPersistent. But there might be a legitimate reason if for study.パソコンほしい気持ち、ずっと続いてるね。", True),
        ("real_leak_th_reflection",
         "thReflection opportunity. Good teaching moment about spending habits.そうなんだ、おかし毎日買っちゃうんだね。", True),
        ("real_leak_th_cost_compare",
         "thChild is doing great cost comparison. Store: 600円 + 400円 bus round trip.つまり、お店だと1回600円で、", True),
        # 先頭が日本語で始まる混入(旧実装が素通りさせた形)
        ("leak_starting_with_japanese",
         "円 1.5L syrup wouldnt fit; they want variety. Restate, affirm, ask.なるほど、いちご・メロン・", True),
        # 英文が連続する思考(th なし)
        ("leak_english_sentences",
         "Reflection opportunity. Ask one question. Kind.そうなんだ、おかし毎日買っちゃうんだね。", True),

        # --- 正常な応答。誤検知してはいけない ---
        ("normal_japanese",
         "パソコンほしいんだね。一緒に考えよう。", False),
        ("normal_with_inline_english",
         "300円のジュース、記録したよ。good なものを選べるかも。", False),
        ("normal_short_english_ok",
         "OK! いいね、それやってみよう。", False),
        ("normal_th_lowercase_word",
         "theater に行きたいんだね、いいね！", False),
        ("normal_english_midway_short",
         "やったね！That is great. すごいね。", False),
        ("normal_balance_report",
         "ぜんぶ記録したよ。シロップ4種セット1000円、みぞれ500円、練乳500円で、合わせて2000円。今の残高は 84700円 だよ。", False),
        ("normal_coaching_long",
         "つまり、お店だと1回600円で、そこにバス代が往復400円かかるから、1回食べるのに1000円くらい。ちゃんと全部くらべられてて、えらいなあ。", False),
    ]
    for name, inp, should_detect in cases:
        got = detect(inp)
        _check(name, got == should_detect, f"detected={got} expected={should_detect}")
    passed = sum(1 for x in _results if x["passed"])
    for x in _results:
        print(json.dumps(x, ensure_ascii=False))
    print(json.dumps({"summary": True, "passed": passed, "total": len(_results)}, ensure_ascii=False))
    # 集計表示だけだと失敗が exit 0 で埋もれるため、明示的に終了コードへ反映する
    return passed == len(_results)

if __name__ == "__main__":
    sys.exit(0 if _run() else 1)
