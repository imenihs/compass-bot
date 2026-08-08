"""Phase N-11 r6: コーチング出し分け(_should_coach)と能動ナッジ橋渡しの決定的テスト。

claude を起動せず、判定ロジックと橋渡しの保存→消費を直接検証する。目的は2つ:
  1. _should_coach が食べ物・遊びの雑談で誤爆しない（個数・時刻の数字、素の「ほしい」を拾わない）
     こと、かつお金の話題（お金語 or 数字＋円＋購買動詞）では確実に出ること。
  2. 能動ナッジ橋渡し（save_pending_nudge_bridge → take_pending_nudge_bridge）が1回だけ
     消費されること（次ターンの system prompt に孤立を防ぐ文脈として渡り、二度は出ない）。

隔離ディレクトリで実データに触れない。結果は1行1 JSON で出力し集計する。
"""
import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

_results: list[dict] = []


def _check(name: str, passed: bool, detail: str = "") -> None:
    _results.append({"test": name, "passed": bool(passed), "detail": detail})


def _run() -> bool:
    from app.conv.ai_conversation import _should_coach

    # --- 誤爆してはいけない: 食べ物・遊びの雑談（個数・時刻の数字、素の「ほしい」）---
    no_coach = [
        "ポケモン3匹ほしい",        # 個数の数字＋ほしい（金額でない）
        "あと5分でごはんほしい",     # 時刻の数字＋ほしい
        "100円のパンたべたい",       # 金額はあるが購買動詞なし＝雑談
        "きょうは3回あそんだ",       # 個数の数字＋動作（お金語なし）
        "パンほしい",               # 素のほしい
        "たのしかった",             # かった の部分一致誤爆チェック
        "すいかたべた",             # 食べ物雑談
    ]
    for t in no_coach:
        _check(f"no_coach::{t}", _should_coach(t) is False, f"should be False: {t}")

    # --- 純粋な確定記録（もう買った/もらった・相談や振り返り無し）は出さない ---
    # 確定取引は記録が最優先で観察の問いを挟まない（codex #3）。計画・振り返り・相談は下で True に残す。
    pure_txn_no_coach = [
        "300円つかった",            # 完了した支出の報告だけ
        "500円のおかしを買った",     # 完了した支出の報告だけ
        "200円もらった",            # 完了した入金の報告だけ
        "パン買った",               # 金額なし・完了報告だけ
        "おかし買った",             # 完了報告だけ
        "おこづかいもらった",        # 完了報告だけ
        "おこづかいを1000円もらいました",  # 丁寧形
        "お金を300円使いました",      # 丁寧形
        "おこづかいを受け取った",      # 言い換え
        "お小遣いが少なかった",       # 取引でも相談でもない状態のつぶやき(かった誤爆しない)
        "300円払った",               # 完了(払った)だけ
        "500円受け取った",           # 完了(受け取った)だけ
    ]
    for t in pure_txn_no_coach:
        _check(f"pure_txn_no_coach::{t}", _should_coach(t) is False, f"pure confirmed txn should be False: {t}")

    # --- 出すべき: 計画・目標・振り返り・複合相談（記録＋相談）はコーチング対象に残す（codex 実装レビュー #1 の過剰抑制修正）---
    do_coach = [
        "おこづかいをためたい",      # 貯金の計画（確定取引ではない）
        "貯金したいな",             # 貯金の計画
        "査定してほしい",           # 査定相談
        "ゲームを買うために毎週100円ためることにした",  # 計画（確定取引ではない）
        "目標は5000円",            # 目標（計画）
        "500円使ったけど高かったかな",   # 振り返り（後悔・かな）
        "500円のお菓子買っちゃった。どう思う",  # 記録＋相談（どう思う）
        "300円のお菓子買った。またすぐ買いたいけどいい？",  # 記録＋相談
        "500円使ったけど無駄だった",  # 振り返り(けど・無駄)
        "300円使った。次から節約する",  # 計画(次から)
        "おこづかいをもらった。何に使うのがよさそう",  # 複合相談(何に・よさそう)
        "500円使ったら残り100円",    # 仮定(たら)
        "500円使った場合はどうなる",  # 仮定(場合)
        "300円払ったので後悔してる",  # 振り返り(払っ+後悔)
        "500円受け取った。使い道を考えたい",  # 複合(受け取っ+使い道)
        "500円使ったのは失敗だった",  # 振り返り(失敗)
        "500円使ったかな",           # 迷い(かな)
    ]
    for t in do_coach:
        _check(f"do_coach::{t}", _should_coach(t) is True, f"should be True: {t}")

    # --- 残高・履歴の照会だけなら出さない（監視的にしない）---
    _check("suppress_balance_only", _should_coach("ざんだかいくら？") is False, "balance query only")
    _check("suppress_history_only", _should_coach("りれきみせて") is False, "history query only")

    # --- 能動ナッジ橋渡し: 保存→消費は1回だけ ---
    from app.conv import deps
    with tempfile.TemporaryDirectory() as td:
        # deps はリポジトリ直下の data/ を見るため、parents[2] を一時ディレクトリへ向けたいが
        # 本関数群は data/learning_support_state/{key}.json を parents[2] 基準で開く。実データを
        # 汚さないよう、テスト専用の user_key を使い、消費後に必ず消えることだけを検証する。
        user_conf = {"name": "＿coaching_gate_test＿", "user_key": "＿coaching_gate_test＿"}
        from urllib.parse import quote as _q
        import json as _j
        _key = _q(str(user_conf["user_key"]), safe="-_.")[:120]
        _root = Path(__file__).resolve().parents[1]
        _sp = _root / "data" / "learning_support_state" / f"{_key}.json"

        # (A) challenge_stale の橋渡し: 消費=その返事とみなし child_response を challenge_id 付きで記録する
        deps.save_pending_nudge_bridge(
            user_conf, "前に決めた『レシートを1つ記録』どうなった？",
            reason="challenge_stale", challenge_action="レシートを1つ記録",
        )
        first = deps.take_pending_nudge_bridge(user_conf)
        second = deps.take_pending_nudge_bridge(user_conf)
        _check("bridge_first_returns_text", "どうなった" in first, first)
        _check("bridge_second_empty", second == "", f"second should be empty, got: {second!r}")
        _st = _j.loads(_sp.read_text(encoding="utf-8")) if _sp.exists() else {}
        _cr = _st.get("child_response") or {}
        _check(
            "challenge_stale_bridge_records_child_response",
            _cr.get("feedback") == "conversation_reply"
            and _cr.get("challenge_id") == "レシートを1つ記録"
            and bool(_cr.get("responded_at")),
            f"child_response={_cr}",
        )

        # (A2) 橋渡し温存: 返事らしくない発話(record_response=False)では消費せず温存し、注入しない。
        #      次に返事らしい発話(True)が来たとき初めて注入・消費する（無関係発話1回での焼失を防ぐ）。
        deps.save_pending_nudge_bridge(
            user_conf, "前に決めた『レシートを1つ記録』どうなった？",
            reason="challenge_stale", challenge_action="レシートを1つ記録",
        )
        kept = deps.take_pending_nudge_bridge(user_conf, record_response=False)  # 無関係発話
        _check("bridge_preserved_on_non_reply", kept == "", f"non-reply must not consume/inject: {kept!r}")
        # 温存されているので、次の返事らしいターンで注入される
        replied = deps.take_pending_nudge_bridge(user_conf, record_response=True)
        _check("bridge_injected_on_reply", "どうなった" in replied, replied)
        # 消費後は空
        _check("bridge_gone_after_reply", deps.take_pending_nudge_bridge(user_conf, True) == "", "")

        # (B) no_record の橋渡し: 無関係な会話でも消費されるが child_response は書かない
        #     (書くと別チャレンジの challenge_stale を誤って抑制するため)。既存 child_response を消して検証。
        if _sp.exists():
            _tmp = _j.loads(_sp.read_text(encoding="utf-8"))
            _tmp.pop("child_response", None)
            _sp.write_text(_j.dumps(_tmp, ensure_ascii=False), encoding="utf-8")
        deps.save_pending_nudge_bridge(
            user_conf, "さいきん記録あいてるみたい。なにか買った？", reason="no_record",
        )
        _ = deps.take_pending_nudge_bridge(user_conf)
        _st2 = _j.loads(_sp.read_text(encoding="utf-8")) if _sp.exists() else {}
        _check(
            "no_record_bridge_does_not_record_child_response",
            not (_st2.get("child_response") or {}),
            f"child_response should stay empty, got={_st2.get('child_response')}",
        )

        # 後片付け: テスト用 state ファイルを消す（実データ隔離のため parents[2] 側に作られる）
        from urllib.parse import quote
        key = quote(str(user_conf["user_key"]), safe="-_.")[:120]
        root = Path(__file__).resolve().parents[1]
        for suffix in (".json", ".json.lock", ".json.tmp"):
            p = root / "data" / "learning_support_state" / f"{key}{suffix}"
            if p.exists():
                p.unlink()

    # --- 会話コーチングとリマインダ能動伴走の状態分離(major修正の回帰防止) ---
    # save_coaching_nudge は会話専用キー(last_coaching_*)だけ書き、challenge_stale が見る
    # last_nudge_at / last_child_action を触ってはいけない。触ると、よく話す子ほど challenge_stale が
    # 構造的に発火しなくなる/能動ナッジのアクションが会話コーチングの選択に化ける。
    import json as _json
    from pathlib import Path as _Path
    from urllib.parse import quote as _quote
    with tempfile.TemporaryDirectory():
        user_conf2 = {"name": "＿coach_sep_test＿", "user_key": "＿coach_sep_test＿"}
        root = _Path(__file__).resolve().parents[1]
        key2 = _quote(str(user_conf2["user_key"]), safe="-_.")[:120]
        state_path = root / "data" / "learning_support_state" / f"{key2}.json"
        # 事前に challenge_stale が見る本来のチャレンジ状態を置く(Web/能動ナッジ由来を模す)
        state_path.parent.mkdir(parents=True, exist_ok=True)
        state_path.write_text(_json.dumps({
            "last_nudge_at": "2020-01-01T00:00:00+09:00",
            "last_child_action": "本来のチャレンジ",
            "last_card_type": "save_habit",
        }, ensure_ascii=False), encoding="utf-8")
        # 会話コーチングを注入(専用キーへ書く)
        deps.save_coaching_nudge(user_conf2, "record_habit", "会話で選んだ別アクション")
        after = _json.loads(state_path.read_text(encoding="utf-8"))
        _check(
            "coaching_does_not_touch_challenge_clock",
            after.get("last_nudge_at") == "2020-01-01T00:00:00+09:00"
            and after.get("last_child_action") == "本来のチャレンジ",
            f"last_nudge_at={after.get('last_nudge_at')} last_child_action={after.get('last_child_action')}",
        )
        _check(
            "coaching_writes_own_keys",
            after.get("last_coaching_action") == "会話で選んだ別アクション"
            and after.get("last_coaching_card_type") == "record_habit"
            and bool(after.get("last_coaching_at")),
            f"last_coaching_*={after.get('last_coaching_action')},{after.get('last_coaching_card_type')}",
        )
        # recent_coaching_action は直近に注入した action を返す(時間ベース抑制の材料)
        recent = deps.recent_coaching_action(user_conf2, within_hours=20)
        _check("recent_coaching_action_returns_recent", recent == "会話で選んだ別アクション", recent)
        # 後片付け
        for suffix in (".json", ".json.lock", ".json.tmp"):
            p = root / "data" / "learning_support_state" / f"{key2}{suffix}"
            if p.exists():
                p.unlink()

    passed = sum(1 for x in _results if x["passed"])
    for x in _results:
        print(json.dumps(x, ensure_ascii=False))
    print(json.dumps({"summary": True, "passed": passed, "total": len(_results)}, ensure_ascii=False))
    # 失敗が1件でもあれば非0終了する（集計表示だけで exit 0 だと CI/回帰で失敗が見逃される・codex #3）
    return passed == len(_results)


if __name__ == "__main__":
    import sys as _sys
    _sys.exit(0 if _run() else 1)
