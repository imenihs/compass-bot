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


def gemini_service() -> Any:
    """現在の GeminiService（またはテストダブル）を返す。

    テストは bot.gemini_service を StubGeminiService 系へ差し替える。
    対話層が呼んでよいのは call_silent / call_with_progress / extract_assessed_amounts の3つのみ。

    Returns:
        GeminiService: Gemini 呼び出しサービス。
    """
    return _bot().gemini_service


def client() -> Any:
    """現在の discord.Client（またはテストの FakeClient）を返す。

    Returns:
        discord.Client: Discord クライアント。
    """
    return _bot().client


def intent_normalizer() -> Any:
    """intent 正規化モジュールを返す。

    モジュールごと返すのが要点。テストは bot.intent_normalizer.normalize_intent
    という属性単位で差し替えるため、モジュール参照を返せば差し替えが効く。
    個別関数を束縛して返すと差し替え前の関数を掴んでしまう。

    Returns:
        module: intent_normalizer モジュール（normalize_intent / is_no_reply 等を持つ）。
    """
    return _bot().intent_normalizer


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
