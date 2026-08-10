"""codex blocker#2回帰防止: 言い直し冪等の穴(品目空・分境界61秒)を経過秒判定で塞いだことを検証。

旧実装(分バケット yyyymmddHHMM)の穴:
- 品目空だと自然キーを生成せず、tool後失敗の言い直しで二重適用
- 12:00:59 は {12:00,11:59}、61秒後の 12:02:00 は {12:02,12:01} で窓が重ならず二重適用
新実装: 内容キー(時刻なし {child}:{action}:dup:{amount}:{item}) + 経過秒判定(DEDUP_WINDOW_SEC=120)。
is_recent_dup_applied を、applied_keys に既知 ts を仕込んで直接検証する。
"""
import json, sys, tempfile
from datetime import datetime, timezone, timedelta
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
JST = timezone(timedelta(hours=9))
_results=[]
def _check(n,p,d=""): _results.append({"test":n,"passed":bool(p),"detail":d})

def _run():
    import app.wallet_service as ws
    from app.mcp_wallet import _natural_dup_key, DEDUP_WINDOW_SEC
    with tempfile.TemporaryDirectory() as d:
        tmp=Path(d); (tmp/"data").mkdir()
        w=ws.WalletService(); w.wallet_state_path=tmp/"data"/"wallet_state.json"
        now=datetime.now(JST)

        # 品目空の内容キーが生成される(旧実装は空文字→キー無しだった)
        k_empty=_natural_dup_key("はな","spending_record",100,"")
        _check("empty_item_key_generated", k_empty=="はな:spending_record:dup:100:", k_empty)

        # NFKC/空白畳み込みで品目揺れを吸収(「ジュース」と「ジュース を かった」は別だが全角半角は同一)
        k1=_natural_dup_key("はな","spending_record",80,"ジュース")
        k2=_natural_dup_key("はな","spending_record",80,"ジュース")  # 半角カナ→NFKCで全角化
        _check("nfkc_normalizes_item", k1==k2, f"{k1} vs {k2}")

        # --- 経過秒判定: 窓内は弾く、窓外は通す ---
        def _put(state_path, key, ts):
            st={"users":{},"applied_operation_keys":{key:{"ts":ts.isoformat(),"aux":True}}}
            state_path.write_text(json.dumps(st,ensure_ascii=False),encoding="utf-8")
        # 30秒前に適用 → 窓内(120秒)なので弾く
        _put(w.wallet_state_path, k_empty, now-timedelta(seconds=30))
        _check("within_window_blocked", w.is_recent_dup_applied(k_empty, DEDUP_WINDOW_SEC) is True)
        # 61秒前(旧分境界の穴ケース) → 窓内なので弾く(旧実装は素通りしていた)
        _put(w.wallet_state_path, k_empty, now-timedelta(seconds=61))
        _check("minute_boundary_61s_blocked", w.is_recent_dup_applied(k_empty, DEDUP_WINDOW_SEC) is True)
        # 121秒前 → 窓外なので別支出として通す(弾かない)
        _put(w.wallet_state_path, k_empty, now-timedelta(seconds=121))
        _check("beyond_window_passes", w.is_recent_dup_applied(k_empty, DEDUP_WINDOW_SEC) is False)
        # 未適用キー → 弾かない
        _check("unknown_key_passes", w.is_recent_dup_applied("はな:spending_record:dup:999:x", DEDUP_WINDOW_SEC) is False)

    passed=sum(1 for x in _results if x["passed"])
    for x in _results: print(json.dumps(x,ensure_ascii=False))
    print(json.dumps({"summary":True,"passed":passed,"total":len(_results)},ensure_ascii=False))
    # 判定を終了コードへ返す（返さないと落ちても PASS 扱いになる・2026/08/10 是正）
    return passed == len(_results)

if __name__=="__main__":
    raise SystemExit(0 if _run() else 1)
