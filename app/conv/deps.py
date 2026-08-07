"""対話層（app/conv/**）が使う外部依存を、呼び出し時に解決して返す薄い層。

このモジュールの目的は「app/conv/** が app.config 等の名前を直接 import しない」ことである。
既存テスト（tests/fake_discord_flow_tests.py:230-253）は app.bot と app.handlers_parent の
モジュール変数（wallet_service / load_system / find_user_by_name / get_parent_ids /
intent_normalizer.normalize_intent など）を実行時に差し替える方式で動く。
app/conv/** が名前を直接 import すると、束縛が import 時に固定され差し替えが効かず、
テストが実設定・実残高・実 Gemini を見に行ってしまう。

そのため本モジュールは:
- import 時にどの依存も束縛しない（app.bot / app.handlers_parent を遅延 import する）
- 呼び出しのたびに app.bot / app.handlers_parent の**現在の**属性を getattr で読む
- 差し替え済みの属性があればそれを、無ければ本番の実装を返す
という形を取る。app.bot と対話層は将来相互に import するため、遅延 import で循環も避ける。

参照元の使い分け（テストの差し替え先に合わせる）:
- 大半の依存は app.bot 側で差し替えられる（bot.wallet_service / bot.load_system 等）。
- update_user_field は app.handlers_parent 側だけが差し替えられる（bot 側は据え置き。
  実装仕様.md 第0段③参照）。get_allow_channel_ids も同様に handlers_parent 側が差し替え対象。
"""

import importlib
from typing import Any


def _bot():
    """app.bot モジュールを遅延取得する。

    import 時に束縛しないため、循環 import を避けつつ差し替え済み属性を読める。

    Returns:
        module: app.bot モジュールオブジェクト。
    """
    # importlib.import_module はロード済みなら sys.modules を即返すため安価
    return importlib.import_module("app.bot")


def _handlers_parent():
    """app.handlers_parent モジュールを遅延取得する。

    update_user_field / get_allow_channel_ids はこちら側が差し替え対象。

    Returns:
        module: app.handlers_parent モジュールオブジェクト。
    """
    return importlib.import_module("app.handlers_parent")


def _user_key_mod():
    """app.user_key モジュールを遅延取得する。

    learning_support_state のファイル名 user_key を全経路で一意に生成する共有関数
    canonical_user_key を提供する。遅延 import で循環を避ける。

    Returns:
        module: app.user_key モジュールオブジェクト。
    """
    return importlib.import_module("app.user_key")


# ------------------------------------------------------------------
# サービスオブジェクト（実行時に差し替えられる可能性がある）
# ------------------------------------------------------------------

def wallet_service() -> Any:
    """現在の WalletService インスタンスを返す。

    テストは bot.wallet_service を一時ディレクトリ版へ差し替える。

    Returns:
        WalletService: 残高・目標貯金・監査状態を扱うサービス。
    """
    return _bot().wallet_service


def client() -> Any:
    """現在の discord.Client（またはテストの FakeClient）を返す。

    Returns:
        discord.Client: Discord クライアント。
    """
    return _bot().client




# ------------------------------------------------------------------
# 設定の読み出し（config 関数の呼び出しをラップする）
# ------------------------------------------------------------------

def load_system() -> dict:
    """システム設定 dict を読み込んで返す。

    テストは bot.load_system をラムダへ差し替える。呼び出し時に現在の関数を引くことで
    差し替えを反映する。

    Returns:
        dict: システム設定。
    """
    return _bot().load_system()


def load_all_users() -> list:
    """登録済み子ユーザーの一覧を返す。

    Returns:
        list[dict]: 子ユーザー設定のリスト。
    """
    return _bot().load_all_users()


def find_user_by_discord_id(user_id: int) -> dict | None:
    """Discord ID から子ユーザー設定を引く。

    Args:
        user_id: 対象の Discord ユーザーID。

    Returns:
        dict | None: 一致する子ユーザー設定。無ければ None。
    """
    return _bot().find_user_by_discord_id(user_id)


def find_user_by_name(name: str) -> dict | None:
    """名前から子ユーザー設定を引く。

    Args:
        name: 子ユーザー名。

    Returns:
        dict | None: 一致する子ユーザー設定。無ければ None。
    """
    return _bot().find_user_by_name(name)


def find_parent_by_discord_id(user_id: int) -> dict | None:
    """Discord ID から親ユーザー設定を引く。

    Args:
        user_id: 対象の Discord ユーザーID。

    Returns:
        dict | None: 一致する親設定。無ければ None。
    """
    return _bot().find_parent_by_discord_id(user_id)


def get_parent_ids() -> set:
    """親（管理者）の Discord ID 集合を返す。

    テストは bot.get_parent_ids を差し替える。import 時に束縛された bot.PARENT_IDS
    ではなく、都度この関数を呼ぶことで差し替えを反映する。

    Returns:
        set[int]: 親の Discord ID 集合。
    """
    return _bot().get_parent_ids()


def get_allow_channel_ids():
    """許可チャンネルIDの集合（または None）を返す。

    handlers_parent 側の関数が差し替え対象のため、そちらから解決する。

    Returns:
        set[int] | None: 許可チャンネルID集合。制限なしなら None。
    """
    return _handlers_parent().get_allow_channel_ids()


def update_user_field(name: str, field: str, value: Any) -> bool:
    """子ユーザー設定の1フィールドを更新する。

    テストは handlers_parent.update_user_field のみを差し替える（bot 側は据え置き。
    実装仕様.md 第0段③参照）。本番の実データへ書かないよう handlers_parent から解決する。

    Args:
        name: 対象の子ユーザー名。
        field: 更新するフィールド名。
        value: 設定する値。

    Returns:
        bool: 更新に成功したかどうか。
    """
    return _handlers_parent().update_user_field(name, field, value)


def get_log_dir(system_conf: dict | None = None) -> Any:
    """ログ・データ出力ディレクトリの Path を返す。

    system_conf を渡せばそれを、渡さなければ現在のシステム設定を使う。

    Args:
        system_conf: システム設定 dict。None なら load_system() で取得する。

    Returns:
        Path: ログ・データディレクトリ。
    """
    bot = _bot()
    conf = system_conf if system_conf is not None else bot.load_system()
    return bot.get_log_dir(conf)


def conversation_log_setting() -> dict:
    """会話ログの保持方針（保持日数・行数上限）を返す。

    この設定はテストハーネスの差し替え対象ではないため、app.config を遅延 import して
    直接読む。app/conv/** が app.config を直接 import しない規約の唯一の解決口が deps であり、
    設定値の取得はここへ集約する。app.bot は本関数を re-export していないため bot 経由では引けない。

    Returns:
        dict: {"retention_days": int, "max_lines": int} 形式。会話ログの切り詰めに使う。
    """
    # app.config はロード済みなら sys.modules を即返すため遅延 import は安価
    config = importlib.import_module("app.config")
    return config.get_conversation_log_setting()


def learning_insights(user_conf: dict, system_conf: dict | None = None, days: int = 90) -> dict:
    """その子の学習支援インサイト（会話カード・子ども向けチャレンジ・要点）を返す。

    app/conv/** は app.learning_insights を直接 import しない規約のため、ここで遅延 import する。
    AI 主導の会話層はこの結果を system prompt へ注入し、「観察→問い→次の小さな行動」という
    学習支援要件（学習支援要件再定義.md）どおりのコーチングを会話に織り込む。build_learning_insights は
    既存ログ（pocket_journal / wallet_ledger）を読むだけで残高は動かさない。

    Args:
        user_conf: 対象児童の設定 dict。
        system_conf: システム設定。None なら現在値を使う。
        days: 集計する日数（既定90日）。

    Returns:
        dict: build_learning_insights の返り値（insight_cards / child_challenge / prompt_points 等）。
    """
    li = importlib.import_module("app.learning_insights")
    # system_conf 未指定時は app.config を直接使う。deps.load_system() は app.bot を引き
    # GeminiService 初期化を誘発するため、ログ読取だけの学習支援には app.config で足りる。
    if system_conf is None:
        config = importlib.import_module("app.config")
        system_conf = config.load_system()
    # learning_support_state（同テーマの連続抑制・親の抑制・子のフィードバック）を読み、audit_state として
    # 渡す。これで会話コーチングも、ダッシュボード・reminder と同じ永続 dedup（要件: 短期間に同じテーマを
    # 繰り返さない）を尊重する。読み取りのみ（書き戻しは server/reminder と競合するため行わない）。
    audit_state = None
    try:
        audit_state = {"learning_support_state": _load_learning_support_state(user_conf)}
    except Exception:
        # state 読み取りの失敗は致命でない。抑制なしで通常のインサイトを返す
        audit_state = None
    return li.build_learning_insights(user_conf, system_conf, audit_state=audit_state, days=days)


def _load_learning_support_state(user_conf: dict) -> dict:
    """その子の learning_support_state（data/learning_support_state/{key}.json）を読む。

    server.py と同じ user_key・パスで読み、会話コーチングの抑制判定に使う。読めなければ空 dict。
    """
    import json
    from pathlib import Path
    # server.py の _user_key_for_storage と同じ規則で user_key を作る（同じ state ファイルを読むため
    # safe="-_." と 120文字制限を厳密に合わせる）。ずれると別ファイルを読み抑制が効かない
    key = _user_key_mod().canonical_user_key(user_conf)
    key = key[:120] if key else "unknown"
    root = Path(__file__).resolve().parents[2]
    path = root / "data" / "learning_support_state" / f"{key}.json"
    if not path.exists():
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def save_coaching_nudge(user_conf: dict, card_type: str, child_action: str) -> None:
    """会話でコーチングを注入したターンを、会話専用キー
    last_coaching_card_type / last_coaching_at / last_coaching_action へ best-effort で書き戻す。

    これにより (a) 再起動を跨いだ反復抑制（時間ベース）、(b) learning_insights の3日 type dedup が
    会話経路にも効く。**重要**: reminder の能動伴走(challenge_stale)が見る last_nudge_at /
    last_child_action は絶対に触らない。会話コーチングでそれらを更新すると、よく話す子ほど
    last_nudge_at が現在時刻へ進み challenge_stale が構造的に発火しなくなる／last_child_action が
    会話コーチングの選択で上書きされ能動ナッジのアクションが化ける、という副作用が起きる。
    会話コーチングとリマインダ能動伴走で状態を分離するのが本関数の要点。

    server.py / reminder との競合を避けるため payout と同じ flock で直列化し、read→更新→原子的
    tmp+replace で書く。失敗は握って会話を止めない。

    Args:
        user_conf: 対象児童の設定 dict。
        card_type: 注入したカード種別（insight_card の type）。
        child_action: 注入した child_action。
    """
    import json
    import os as _os
    from pathlib import Path
    try:
        key = _user_key_mod().canonical_user_key(user_conf)
        root = Path(__file__).resolve().parents[2]
        path = root / "data" / "learning_support_state" / f"{key}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        # server/reminder と同じ state ファイルを触るため flock で直列化する
        from app.wallet_service import _interprocess_lock
        with _interprocess_lock(path.with_suffix(".json.lock")):
            state = {}
            if path.exists():
                try:
                    with open(path, "r", encoding="utf-8") as f:
                        loaded = json.load(f)
                    state = loaded if isinstance(loaded, dict) else {}
                except Exception:
                    state = {}
            storage_mod = importlib.import_module("app.storage")
            # 会話専用キーだけ書く。challenge_stale が見る last_nudge_at / last_child_action /
            # last_card_type は触らない（能動伴走の時計を会話で汚さない）。
            state["last_coaching_card_type"] = card_type
            state["last_coaching_action"] = child_action
            state["last_coaching_at"] = storage_mod.now_jst_iso()
            tmp = path.with_suffix(".json.tmp")
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(state, f, ensure_ascii=False, indent=2)
                f.write("\n")
            _os.replace(tmp, path)
    except Exception:
        # 書き戻しの失敗はコーチング・会話を止めない
        pass


def recent_coaching_action(user_conf: dict, within_hours: int = 20) -> str:
    """直近 within_hours 以内に会話コーチングで注入した child_action を返す（無ければ空文字）。

    会話コーチングの反復抑制を、プロセス内 dict でなく learning_support_state の永続キー
    (last_coaching_action / last_coaching_at) で行うための読み取り口。プロセス内 dict だと
    (a) 再起動で抑制が消える、(b) 稼働が長いほど「最初のお金ターンで1回出たら以後出ない」など
    プロセス寿命に抑制期間が依存する両極端になる。時間ベースに一本化してプロセス非依存にする。
    読めなければ空文字（抑制なし＝通常どおりコーチングを出す）。

    Args:
        user_conf: 対象児童の設定 dict。
        within_hours: この時間以内の注入だけを「直近」とみなす。

    Returns:
        str: 直近に注入した child_action。無ければ空文字。
    """
    import json
    from datetime import datetime, timedelta
    from pathlib import Path
    try:
        key = _user_key_mod().canonical_user_key(user_conf)
        root = Path(__file__).resolve().parents[2]
        path = root / "data" / "learning_support_state" / f"{key}.json"
        if not path.exists():
            return ""
        with open(path, "r", encoding="utf-8") as f:
            state = json.load(f)
        if not isinstance(state, dict):
            return ""
        action = str(state.get("last_coaching_action") or "").strip()
        at_raw = state.get("last_coaching_at")
        if not action or not at_raw:
            return ""
        # last_coaching_at が within_hours 以内なら「直近」とみなす。解釈できなければ抑制しない
        try:
            at = datetime.fromisoformat(str(at_raw))
        except ValueError:
            return ""
        # 現在時刻は storage の now_jst_iso() を基準にする（保存側と同じ時計・TZ）
        now = datetime.fromisoformat(importlib.import_module("app.storage").now_jst_iso())
        if at.tzinfo is None:
            at = at.replace(tzinfo=now.tzinfo)
        if at <= now - timedelta(hours=within_hours):
            return ""
        return action
    except Exception:
        return ""


def save_pending_nudge_bridge(
    user_conf: dict, nudge_text: str, reason: str = "", challenge_action: str = ""
) -> None:
    """能動伴走ナッジ（reminder が channel.send で直接送る問いかけ）を、次の会話ターンへ
    橋渡しするため learning_support_state へ best-effort で記録する。

    能動ナッジは claude セッションにも会話ログにも claude 経由では載らないため、次に子が
    「やった」「あとで」と返すと claude はその問いかけを送った記憶が無い孤立発話として受け取り、
    伴走が会話として成立しない（学習支援要件『未反応の小さなチャレンジに返答しやすい形で
    声をかける』が切れる）。そこで直近ナッジ本文をここへ残し、次ターンの system prompt に
    「前回きみに《…》と聞いたよ」として1回だけ注入して文脈を繋ぐ（take で消費）。

    reason と challenge_action も保存する。take 側は「元ナッジが challenge_stale で、その返事とみなせる」
    ときだけ child_response を書くため、どのチャレンジ由来かを区別する必要がある。no_record や
    growth_plan_review の橋渡しで child_response を書くと、無関係な会話1回で別チャレンジの
    challenge_stale が誤って抑制される（要件が構造的に無効化される）ため、reason を持ち回す。

    Args:
        user_conf: 対象児童の設定 dict。
        nudge_text: 送った能動ナッジの本文。
        reason: ナッジの種別（challenge_stale / no_record / growth_plan_review 等）。
        challenge_action: challenge_stale のとき、その対象アクション（child_response 照合用）。
    """
    import json
    import os as _os
    from pathlib import Path
    try:
        key = _user_key_mod().canonical_user_key(user_conf)
        root = Path(__file__).resolve().parents[2]
        path = root / "data" / "learning_support_state" / f"{key}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        from app.wallet_service import _interprocess_lock
        with _interprocess_lock(path.with_suffix(".json.lock")):
            state = {}
            if path.exists():
                try:
                    with open(path, "r", encoding="utf-8") as f:
                        loaded = json.load(f)
                    state = loaded if isinstance(loaded, dict) else {}
                except Exception:
                    state = {}
            storage_mod = importlib.import_module("app.storage")
            # 橋渡しは会話コーチングの last_nudge_at とは別キーに持つ。challenge_stale 判定が
            # 会話コーチングの副作用で抑制されないよう、状態を混ぜない。
            state["pending_nudge_bridge"] = {
                "text": nudge_text,
                "at": storage_mod.now_jst_iso(),
                "reason": str(reason or ""),
                "challenge_action": str(challenge_action or ""),
            }
            tmp = path.with_suffix(".json.tmp")
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(state, f, ensure_ascii=False, indent=2)
                f.write("\n")
            _os.replace(tmp, path)
    except Exception:
        # 橋渡し記録の失敗はナッジ送信・会話を止めない
        pass


def take_pending_nudge_bridge(user_conf: dict, record_response: bool = True) -> str:
    """未消費の能動ナッジ橋渡し本文を返し、同時にクリアする（1回だけ注入するため）。

    会話ターンの system prompt 構築時に呼ぶ。存在すれば本文を返しクリアし、無ければ空文字。
    読み取り・クリアの失敗では空文字を返し会話を止めない。

    Args:
        user_conf: 対象児童の設定 dict。
        record_response: 今回の発話が返事とみなせるか。False なら challenge_stale の child_response を
            書かない（無関係な雑談での誤抑制を防ぐ）。橋渡し本文の返却・消費は record_response に関わらず行う。

    Returns:
        str: 橋渡しナッジ本文。無ければ空文字。
    """
    import json
    import os as _os
    from pathlib import Path
    try:
        key = _user_key_mod().canonical_user_key(user_conf)
        root = Path(__file__).resolve().parents[2]
        path = root / "data" / "learning_support_state" / f"{key}.json"
        if not path.exists():
            return ""
        from app.wallet_service import _interprocess_lock
        with _interprocess_lock(path.with_suffix(".json.lock")):
            try:
                with open(path, "r", encoding="utf-8") as f:
                    state = json.load(f)
            except Exception:
                return ""
            if not isinstance(state, dict):
                return ""
            bridge = state.get("pending_nudge_bridge")
            if not isinstance(bridge, dict):
                return ""
            text = str(bridge.get("text") or "").strip()
            bridge_reason = str(bridge.get("reason") or "")
            bridge_action = str(bridge.get("challenge_action") or "")
            # 返事らしくない発話(record_response=False)では bridge を温存し、注入も child_response 記録も
            # しない。無関係な発話1回(例「おはよう」)で bridge が焼失すると、次に子が本当に「やった/あとで」と
            # 返したとき文脈が残らず伴走が孤立するため。ただし無限温存を防ぐため、保存から48時間を超えた
            # bridge は返事が来なくても破棄する(古い問いかけを蒸し返さない)。
            if not record_response:
                import datetime as _dt
                at_raw = str(bridge.get("at") or "")
                expired = False
                if at_raw:
                    try:
                        at = _dt.datetime.fromisoformat(at_raw)
                        now = _dt.datetime.fromisoformat(
                            importlib.import_module("app.storage").now_jst_iso()
                        )
                        if at.tzinfo is None:
                            at = at.replace(tzinfo=now.tzinfo)
                        expired = at <= now - _dt.timedelta(hours=48)
                    except ValueError:
                        expired = False
                if expired:
                    # 期限切れは破棄する（温存しない）。ファイルを書き換えて pop を確定させる
                    state.pop("pending_nudge_bridge", None)
                    tmp = path.with_suffix(".json.tmp")
                    with open(tmp, "w", encoding="utf-8") as f:
                        json.dump(state, f, ensure_ascii=False, indent=2)
                        f.write("\n")
                    _os.replace(tmp, path)
                # 温存(または破棄)。どちらも今回は注入しない
                return ""
            # 返事らしいターン。読んだら必ず消す（同じ問いかけを毎ターン注入しない）
            state.pop("pending_nudge_bridge", None)
            # child_response は「元ナッジが challenge_stale で、かつ今回の発話が返事とみなせる
            # (record_response=True)」ときだけ書く。no_record / growth_plan_review の橋渡しや、無関係な
            # 雑談(record_response=False)で書くと、その1発話で別チャレンジの challenge_stale が誤って抑制され、
            # 要件『未反応の小さなチャレンジに声をかける』が構造的に無効化される。かつ challenge_id は当時の
            # ナッジ対象アクションに固定し、_has_recent_child_response 側で last_child_action と照合できるように
            # する（別チャレンジの放置を会話返答で免罪しない）。橋渡し本文の会話注入(text)は毎回行う。
            if bridge_reason == "challenge_stale" and record_response:
                # 照合キーは当時のナッジ対象を優先し、無ければ現在の last_child_action にフォールバック
                challenge_id = bridge_action or str(state.get("last_child_action") or "")
                storage_mod = importlib.import_module("app.storage")
                state["child_response"] = {
                    "challenge_id": challenge_id,
                    "feedback": "conversation_reply",
                    "responded_at": storage_mod.now_jst_iso(),
                }
            tmp = path.with_suffix(".json.tmp")
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(state, f, ensure_ascii=False, indent=2)
                f.write("\n")
            _os.replace(tmp, path)
            return text
    except Exception:
        return ""


def conversation_session_setting() -> dict:
    """会話セッションの失効設定（expiry_minutes）を返す。

    conversation_log_setting と対をなす解決口。app/conv/** は app.config を直接
    import しない規約のため、セッションを張る側は open_session の ttl_minutes に
    本関数の expiry_minutes を渡してセッション失効を設定値へ連動させる。SessionStore
    自身は依存を持たず、設定の解決は必ずこの deps 経由に集約する。

    Returns:
        dict: {"expiry_minutes": int} 形式。会話セッションの失効判定に使う。
    """
    # log 設定と同じく app.config を遅延 import して直接読む
    config = importlib.import_module("app.config")
    return config.get_conversation_session_setting()


# ------------------------------------------------------------------
# 会話セッションストア（唯一のインスタンスを共有する）
# ------------------------------------------------------------------
# SessionStore の排他は per-instance の asyncio.Lock で行うため、同じ data ファイルを
# 指すインスタンスを2つ作ると各々別ロックになり相互排他が黙って失われる（罠7が破れる）。
# 対話層のハンドラは必ず本アクセサから唯一のインスタンスを取得し、SessionStore() を
# 直接生成してはならない（生成はテストと本アクセサ内部だけ）。
_session_store = None


def session_store():
    """唯一の SessionStore インスタンスを返す（無ければ生成して共有する）。

    第2段以降のハンドラはセッション操作をすべてこの1点から取得する。呼び出しごとに
    SessionStore() を new すると別ロックになり罠7を踏むため、生成口を1つに縛る。
    保存先はリポジトリ直下 data/（SessionStore の既定）で、log_dir とは独立に
    conversation_sessions.json / payout_requests.json を所有する。

    Returns:
        SessionStore: 会話セッションと支給要請を所有する唯一のストア。
    """
    global _session_store
    if _session_store is None:
        # 遅延 import で循環を避けつつ、唯一のインスタンスを1度だけ生成する
        session = importlib.import_module("app.conv.session")
        _session_store = session.SessionStore()
    return _session_store


def set_session_store(store) -> None:
    """テスト用に SessionStore を差し替える。本番経路では呼ばない。

    テストは一時ディレクトリを data_dir に持つ SessionStore を渡し、実データの
    conversation_sessions.json / payout_requests.json を避ける。

    Args:
        store: 差し替える SessionStore（None を渡すと次回 session_store() が再生成）。
    """
    global _session_store
    _session_store = store


# ------------------------------------------------------------------
# 設定値（import 時に bot 側で定数へ束縛される。テストは値ごと差し替える）
# ------------------------------------------------------------------
# 注意: PARENT_IDS / ALLOW_CHANNEL_IDS の値形アクセサ（parent_ids() /
# allow_channel_ids()）は意図的に用意しない。これらは bot.py の import 時に
# get_parent_ids() / get_allow_channel_ids() の結果から一度だけ定数へ束縛されるため、
# 設定リロード後は関数形の返り値と食い違い、古い許可チャンネル集合で判定する罠になる。
# 親 ID・許可チャンネルの解決は必ず get_parent_ids() / get_allow_channel_ids()（関数形）を使う。
# 実データは1つ・解決口も1つに保つ（実装仕様.md:1284 は各依存を1つとして列挙する）。

def chat_setting() -> dict:
    """bot.CHAT_SETTING の現在値を返す。

    Returns:
        dict: 雑談設定（natural_chat_enabled / require_mention 等）。
    """
    return _bot().CHAT_SETTING


def low_balance_alert() -> dict:
    """bot.LOW_BALANCE_ALERT の現在値を返す。

    Returns:
        dict: 低残高アラート設定（enabled / threshold / channel_id）。
    """
    return _bot().LOW_BALANCE_ALERT


def allowance_reminder() -> dict:
    """bot.ALLOWANCE_REMINDER の現在値を返す。

    Returns:
        dict: お小遣いリマインダー設定。
    """
    return _bot().ALLOWANCE_REMINDER


def assess_keyword() -> str:
    """bot.ASSESS_KEYWORD の現在値を返す。

    この定数は import 時に束縛され、テストハーネスも差し替えない（実装仕様.md 第0段②）。
    フィクスチャの設定値がそのまま判定に効くため、値形で参照できるようにしておく。

    Returns:
        str: 査定トリガーのキーワード。
    """
    return _bot().ASSESS_KEYWORD


def force_assess_test_keyword() -> str:
    """bot.FORCE_ASSESS_TEST_KEYWORD の現在値を返す。

    ASSESS_KEYWORD と同じく import 時束縛でハーネスも差し替えない。

    Returns:
        str: 強制査定テスト用のキーワード。
    """
    return _bot().FORCE_ASSESS_TEST_KEYWORD
