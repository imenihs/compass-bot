"""claude CLI（AI主導会話）へ財布操作を公開する MCP サーバ（stdio, JSON-RPC 2.0）。

Phase N-11 の核。AI が会話を主導し、金額を動かす確定処理だけをこのサーバの tool として呼ぶ。
金額計算・残高更新は Python（wallet_service）が行い、AI には計算させない。誤りが実残高へ
直結するためである。

【設計の要】
- **operation_key を全金額 tool で必須にする**。AI の再試行や二重発火で残高が二重更新される事故を
  根絶する。wallet_service.update_balance は operation_key 既適用ならスキップする（冪等）。
- **安全弁は tool 内 Python で検証する**。自己申告入金の上限・金額の上限・対象児童の実在確認は、
  AI の判断に委ねず Python が弾く。AI がプロンプトで騙されても残高を守る。
- 応答はすべて日本語のテキストで返す（AI が会話へ自然に織り込めるよう、成否と金額を明示する）。

このサーバは Discord ボット本体とは別プロセスとして claude CLI から subprocess 起動される。
wallet_state.json など実データは wallet_service を通じてのみ触る（直接ファイルを開かない）。
"""
import json
import os
import sys
import uuid
from datetime import datetime, timedelta, timezone

from app import config, wallet_service as wallet_module
from app.config import MAX_WALLET_INPUT_AMOUNT

# JST。査定の月次・日次集計は台帳の ts（JST の isoformat）と揃える
_JST = timezone(timedelta(hours=9))

# 金額入力の上限。AI 経由でも桁あふれ・異常値を弾く（本文パースの上限と揃える）
MAX_AMOUNT = MAX_WALLET_INPUT_AMOUNT

# 言い直し二重適用を弾く内容キーの有効窓（秒）。tool後失敗の言い直しは数十秒〜1分程度で起きるため、
# この窓内の同一内容(金額+品目)を二重適用とみなす。窓を超えたら「別の支出」として通す。分バケット方式の
# 分境界漏れ(12:00:59 と 12:02:00 が重ならない)を無くすため経過秒で判定する。
DEDUP_WINDOW_SEC = 120

# 単一の WalletService を共有する。サーバは1プロセス1インスタンスで直列化される
_wallet = wallet_module.WalletService()

# 会話層が subprocess 起動時に env で渡す「今この会話をしている子ども」の名前。
# tool の対象児童は AI が渡す name 引数ではなく必ずこの値で決める（AI がプロンプト
# インジェクションで別の子を対象にする越境を、モデルの制御外で塞ぐ）。会話ボット用途では
# 会話の相手は1人に固定されるため、この env が唯一の正当な対象児童になる。
ACTIVE_CHILD = os.environ.get("COMPASS_ACTIVE_CHILD", "").strip()

# 危険な管理操作（親承認を飛ばす直接支給 grant_allowance、残高を絶対値でセットし自己申告上限を迂回する
# set_initial_balance）を許可するか。子会話の spawn では設定しないため、会話 claude が --allowedTools を
# すり抜けても Python 境界で拒否できる。越境防止(ACTIVE_CHILD)と同じく、安全は CLI フラグでなく Python で守る。
ALLOW_ADMIN_OPS = os.environ.get("COMPASS_ALLOW_ADMIN_OPS", "").strip() == "1"

# 親モード。親会話の spawn で 1 を設定する。親モードでは ACTIVE_CHILD 束縛でなく、親が対象児を
# tool 引数で明示する（親は全児童を操作できる保護者のため）。ただし対象児は必ず子ディレクトリに実在する
# 子に限定し（_resolve_parent_target）、AI が金額・対象を推測せず親が明示した値だけを渡す前提で設計する。
# 親モードは COMPASS_ALLOW_ADMIN_OPS=1 と併せて設定され、grant/adjust 等の管理操作を許可する。
PARENT_MODE = os.environ.get("COMPASS_PARENT_MODE", "").strip() == "1"


def _send(msg: dict) -> None:
    """JSON-RPC メッセージを1行で標準出力へ書く（stdio トランスポート）。"""
    sys.stdout.write(json.dumps(msg, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def _text_result(req_id, text: str) -> None:
    """tool 呼び出しの成功応答（テキスト1件）を返す。AI はこの文面を会話へ織り込む。"""
    _send({
        "jsonrpc": "2.0", "id": req_id,
        "result": {"content": [{"type": "text", "text": text}]},
    })


def _system_conf() -> dict:
    """現在のシステム設定を返す。log_dir 解決などに使う。"""
    return config.load_system()


def _payout_store():
    """payout_requests.json を所有する SessionStore を返す（同期利用）。

    査定の支給提案は残高を動かさず payout_requests へ pending として積む。mcp_wallet は
    別プロセスの同期サーバのため、SessionStore の同期メソッド（_load_doc/_save_doc）を直接使う。
    書き込みは tmp+replace で原子的。提案は親承認前提の低頻度操作のため、bot 側との read-modify-write
    競合はまず起きないが、原子的書き込みで少なくとも1件ずつは壊れない。
    """
    # 遅延 import で循環を避ける
    from app.conv.session import SessionStore
    return SessionStore()


def _payout_locked():
    """payout_requests.json の read-modify-write をプロセス間で直列化する flock を返す。

    wallet_state.json と同じく、claude 子プロセス(propose)と bot プロセス(approve/reject/take)が
    同じ payout_requests.json を無ロックで read-modify-write するとロストアップデート(後勝ちの save が
    相手の変更を全消し)が起きる。wallet_service の _interprocess_lock を流用し、payout 専用ロックファイルで
    両プロセスを直列化する。write 系関数(propose/approve/reject/take)は必ずこのロック配下で行う。
    """
    from app.wallet_service import _interprocess_lock
    lock_path = _payout_store().payout_requests_path.with_suffix(".json.lock")
    return _interprocess_lock(lock_path)


class _ChildMismatch(Exception):
    """AI が渡した name が、束縛された発話者（ACTIVE_CHILD）と食い違うことを表す。

    越境操作の試みを tool 層で拒否し、AI へ明示エラーを返すために使う。
    """


def _resolve_child(name: str) -> dict | None:
    """操作対象の子ユーザー設定を引く。親・未登録名は None。越境は例外。

    ACTIVE_CHILD（会話層が env で束縛した発話者）が設定されていれば、対象児童は必ず
    ACTIVE_CHILD にする。AI が渡す name はこの値と一致するときだけ許し、異なれば _ChildMismatch を
    上げて越境を拒否する（プロンプトインジェクションで別の子の実残高を操作する穴を、モデルの
    制御外で塞ぐ）。ACTIVE_CHILD 未設定時（会話以外の呼び出し）は従来どおり name で子を引く。

    Args:
        name: AI が tool 引数で渡した対象名。

    Returns:
        dict | None: 対象の子ユーザー設定。子に無ければ None。

    Raises:
        _ChildMismatch: ACTIVE_CHILD と異なる子を対象にしようとした場合。
    """
    supplied = (name or "").strip()
    if ACTIVE_CHILD:
        # 発話者が束縛されている。AI が別の子を指定したら越境として拒否する
        if supplied and supplied != ACTIVE_CHILD:
            raise _ChildMismatch(supplied)
        # 対象は常に束縛された発話者。AI の name は無視して信頼値で引く
        conf = config.find_child_user_by_name(ACTIVE_CHILD)
    else:
        # 会話以外の呼び出し。子ユーザー限定で引く（親は対象にしない）
        conf = config.find_child_user_by_name(supplied)
    if not isinstance(conf, dict):
        return None
    return conf


def _resolve_parent_target(name: str) -> dict | None:
    """親モードで、親が明示した対象児を引く。子ディレクトリに実在する子のみ（親・未登録は None）。

    親は全児童の保護者なので、子経路の ACTIVE_CHILD 束縛は使わない。代わりに親が tool 引数で明示した
    name を子ディレクトリだけで解決する。AI に対象を推測させず、親が「たろうに」と明示した値をそのまま
    渡す前提で、find_child_user_by_name（子ディレクトリのみ）で厳密に引く。親名・未登録名は None を返し、
    親の残高や存在しない子を操作する経路を塞ぐ。PARENT_MODE 前提で呼ぶ。

    Args:
        name: 親が明示した対象児名。

    Returns:
        dict | None: 対象児の設定。子に実在しなければ None。
    """
    conf = config.find_child_user_by_name((name or "").strip())
    return conf if isinstance(conf, dict) else None


def _scoped_op_key(child_name: str, action: str, ai_op_key: str) -> str:
    """AI が渡す operation_key を、子ども・操作種別でサーバ側名前空間化した実効キーにする。

    二重課金根絶の核が「AI が毎回グローバル一意な文字列を渡すこと」だけに依存すると、
    claude セッションは子ごとに完全分離されるため、子 A と子 B が独立に低エントロピーな
    同一キー（例 "expense_1" "income" "1"）を選ぶ確率が構造的に高い。冪等判定は
    applied_operation_keys という単一フラット dict を全体共有しているので、先に子 A が
    そのキーを適用済みにすると、後から子 B の本当に別の支出が「すでに記録済み」に化けて
    黙って消え、実残高が乖離する。同一児童でも弱いキー（日付ベース等）の使い回しで
    2件目の実取引が消える。

    そこで update_balance へ渡す前・is_operation_applied 判定の両方で、
    f"{child}:{action}:{ai_key}" に組み立てる。これで最低限クロス児童・クロス操作種別の
    衝突は構造的に不可能になる（設計が明言する「operation_key で二重課金を根絶」を、
    モデル任せでなく Python 境界で担保する）。

    Args:
        child_name: 対象児童名（_resolve_child で確定した信頼値）。
        action: 操作種別（spending_record / manual_income / initial_setup / allowance_grant）。
        ai_op_key: AI が tool 引数で渡した生の operation_key（strip 済み想定）。

    Returns:
        str: 名前空間化した実効 operation_key。
    """
    return f"{child_name}:{action}:{ai_op_key}"


def _natural_dup_key(child_name: str, action: str, amount: int, item: str) -> str:
    """言い直しによる二重適用を弾く「内容キー」を1つ返す（時刻は含めない）。

    operation_key は AI がターンごとに生成するため、tool が残高を動かした直後にタイムアウト/失敗し、
    子が同じ発話（例「300円つかった」）を言い直すと、新ターンで AI は別の生キーを選び operation_key
    冪等をすり抜けて支出/入金が二重適用される。これを Python 境界で防ぐため、発話内容から決まる
    内容キー `{child}:{action}:dup:{amount}:{item}` を補助キーにする。

    時刻は含めない。旧実装は分バケット（yyyymmddHHMM）だったが、(a)分境界で窓が重ならず（12:00:59 と
    12:02:00 は共通分を持たず）二重適用が漏れる、(b)品目空を除外していたため空品目の言い直しが素通りする、
    という穴があった（codex 指摘）。内容キーは時刻を持たず、経過秒の判定は update_balance 側が
    applied エントリの ts と比較して行う（dedup_window_sec 以内なら言い直し、超なら別支出として通す）。
    品目正規化は NFKC + 空白畳み込みで、AI が「ジュース」「ジュースを買った」等と揺らしても同一視しやすくする。

    Args:
        child_name: 対象児童名。
        action: 操作種別（spending_record / manual_income）。
        amount: 金額（正の整数）。
        item: 品目・メモ（空でも可。空でも内容キーは生成し、金額だけの言い直しも弾く）。

    Returns:
        str: 内容キー（時刻を含まない）。
    """
    import unicodedata
    # NFKC 正規化 + 連続空白を1つに畳む。全角半角・記号の揺れを吸収して言い直しを取りこぼさない
    norm_item = unicodedata.normalize("NFKC", (item or "")).strip()
    norm_item = " ".join(norm_item.split())
    return f"{child_name}:{action}:dup:{amount}:{norm_item}"


def _parse_amount(raw) -> int | None:
    """金額を安全に整数化する。1〜MAX_AMOUNT の範囲外・非数値は None。

    AI が渡す値でも Python 側で必ず検証する。負数・桁あふれ・文字列崩れを弾く。
    """
    try:
        amount = int(raw)
    except (TypeError, ValueError):
        return None
    # 0 以下と上限超過は不正。実残高を壊す異常入力を弾く
    if amount <= 0 or amount > MAX_AMOUNT:
        return None
    return amount


# ------------------------------------------------------------------
# tool 定義（inputSchema は AI が引数を組み立てるための契約）
# ------------------------------------------------------------------

def _tool_defs() -> list[dict]:
    """公開する tool の一覧を返す。金額 tool は operation_key を required に含める。"""
    # operation_key: 同一操作の二重適用を防ぐ冪等キー。AI は1操作につき一意な値を1回だけ渡す
    op_key = {"type": "string", "description": "この操作の冪等キー。同じ操作を二度実行しないための一意な文字列。"}
    base_defs = [
        {
            "name": "get_balance",
            "description": "指定した子どもの現在の所持金（円）を返す。残高を変えない。",
            "inputSchema": {
                "type": "object",
                "properties": {"name": {"type": "string", "description": "子どもの名前"}},
                "required": ["name"],
            },
        },
        {
            "name": "record_expense",
            "description": "子どもの支出を記録し、残高を減らす。金額は必ず正の整数（円）。子どもが「◯円つかった／買った」と言ったら、雑談で流さず必ずこのツールを呼ぶこと。",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "子どもの名前"},
                    "amount": {"type": "integer", "description": "使った金額（円、正の整数）"},
                    "item": {"type": "string", "description": "何に使ったか（任意）"},
                    "operation_key": op_key,
                },
                "required": ["name", "amount", "operation_key"],
            },
        },
        {
            "name": "record_income",
            "description": "子どもの臨時入金を記録し、残高を増やす。自己申告のため1回の上限を超えると拒否する。子どもが「◯円もらった」と言ったら、雑談で流さず必ずこのツールを呼ぶこと。",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "子どもの名前"},
                    "amount": {"type": "integer", "description": "もらった金額（円、正の整数）"},
                    "note": {"type": "string", "description": "メモ（任意）"},
                    "operation_key": op_key,
                },
                "required": ["name", "amount", "operation_key"],
            },
        },
        {
            "name": "set_initial_balance",
            "description": "子どもの所持金を指定額へ設定する（初期設定）。現在残高との差分を記録する。",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "子どもの名前"},
                    "amount": {"type": "integer", "description": "今の所持金（円、0以上）"},
                    "operation_key": op_key,
                },
                "required": ["name", "amount", "operation_key"],
            },
        },
        {
            "name": "get_savings_goals",
            "description": "子どもの貯金目標一覧と残高を返す。残高を変えない。",
            "inputSchema": {
                "type": "object",
                "properties": {"name": {"type": "string", "description": "子どもの名前"}},
                "required": ["name"],
            },
        },
        {
            "name": "set_savings_goal",
            "description": "子どもの貯金目標を追加または更新する（同名は金額更新）。残高は変えない。",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "子どもの名前"},
                    "title": {"type": "string", "description": "目標の名前"},
                    "target_amount": {"type": "integer", "description": "目標金額（円、正の整数）"},
                },
                "required": ["name", "title", "target_amount"],
            },
        },
        {
            "name": "propose_allowance",
            "description": (
                "査定の結果としてお小遣いの支給を『提案』する（残高はまだ動かさない）。おうちの人が承認して"
                "初めて支給される。fixed（固定の増額）と temporary（臨時支給）を指定する。上限は Python 側で"
                "強制され、超える分は自動で減る。何でもかんでも増額・追加支給はできない。理由 reason は必須。"
                "買いたい物がある相談なら purchase_amount（その物の値段）を必ず渡すこと。"
                "所持金だけでその物が買えるときは、Python 側が提案を拒否する（自分のお金で買えるものに追加支給はしない）。"
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "子どもの名前"},
                    "fixed": {"type": "integer", "description": "固定の増額（円、0以上）。省略時0。"},
                    "temporary": {"type": "integer", "description": "臨時支給（円、0以上）。省略時0。"},
                    "reason": {"type": "string", "description": "査定の理由（必須）。子どもが実際に話した内容だけを書く。推測・美化しない。"},
                    "purchase_amount": {
                        "type": "integer",
                        "description": (
                            "買いたい物がある相談のときの、その物の値段（円）。子どもがはっきり言った額だけを入れる。"
                            "所持金でその値段を払えるなら提案は拒否される。買い物と無関係の相談なら省略可。"
                        ),
                    },
                },
                "required": ["name", "reason"],
            },
        },
        {
            "name": "grant_allowance",
            "description": (
                "査定の結果としてお小遣いを支給する（残高を増やす）。親の承認経路からのみ使う。fixed（固定の増額）と "
                "temporary（臨時支給）を指定する。金額の上限は Python 側で強制され、超える分は支給されない。"
                "何でもかんでも増額・追加支給はできない。理由を reason に必ず書くこと。"
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "子どもの名前"},
                    "fixed": {"type": "integer", "description": "固定の増額（円、0以上）。省略時0。"},
                    "temporary": {"type": "integer", "description": "臨時支給（円、0以上）。省略時0。"},
                    "reason": {"type": "string", "description": "査定の理由（必須）"},
                    "operation_key": op_key,
                },
                "required": ["name", "reason", "operation_key"],
            },
        },
    ]
    # 親モードでは親用 tool を追加する。子会話（PARENT_MODE 未設定）では公開しないため、子から親用
    # 操作は tool 一覧にも現れない（--allowedTools と二重に絞る）。AI に金額・対象を推測させない前提で、
    # description に「親が明示した金額・対象のみ。曖昧なら実行せず聞き返す」ことを明記する。
    if PARENT_MODE:
        parent_defs = [
            {
                "name": "parent_grant",
                "description": (
                    "親が指定した子どもへお小遣いを支給する（残高を増やす）。親が『たろうに500円』のように"
                    "対象と金額をはっきり言ったときだけ呼ぶ。金額や対象があいまいなら呼ばず『誰にいくら？』と"
                    "聞き返すこと。金額を推測して決めてはいけない。"
                ),
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "name": {"type": "string", "description": "対象の子どもの名前（親が明示した値）"},
                        "amount": {"type": "integer", "description": "支給額（円。親が明示した値。推測しない）"},
                        "operation_key": op_key,
                    },
                    "required": ["name", "amount", "operation_key"],
                },
            },
            {
                "name": "parent_adjust_balance",
                "description": (
                    "親が指定した子どもの残高を増やす/減らす調整をする。親が『たろうの残高を+500』『りかを-300』"
                    "のように対象と増減額をはっきり言ったときだけ呼ぶ。あいまいなら呼ばず聞き返すこと。"
                ),
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "name": {"type": "string", "description": "対象の子どもの名前（親が明示した値）"},
                        "delta": {"type": "integer", "description": "増減額（円。+で増やす、-で減らす。親が明示した値）"},
                        "operation_key": op_key,
                    },
                    "required": ["name", "delta", "operation_key"],
                },
            },
            {
                "name": "parent_approve_assessment",
                "description": "親が指定した子どもの承認待ちの査定を承認して支給する。対象があいまいなら聞き返す。",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "name": {"type": "string", "description": "対象の子どもの名前"},
                        "operation_key": op_key,
                    },
                    "required": ["name", "operation_key"],
                },
            },
            {
                "name": "parent_reject_assessment",
                "description": "親が指定した子どもの承認待ちの査定を却下する（残高は変わらない）。",
                "inputSchema": {
                    "type": "object",
                    "properties": {"name": {"type": "string", "description": "対象の子どもの名前"}},
                    "required": ["name"],
                },
            },
            {
                "name": "parent_list_balances",
                "description": "全員の子どもの残高一覧を返す（残高は変えない）。親が『みんなの残高』等と聞いたら呼ぶ。",
                "inputSchema": {"type": "object", "properties": {}, "required": []},
            },
            {
                "name": "parent_get_pending",
                "description": "承認待ちの査定提案の一覧を返す（残高は変えない）。親が『承認待ちは？』等と聞いたら呼ぶ。",
                "inputSchema": {"type": "object", "properties": {}, "required": []},
            },
        ]
        return base_defs + parent_defs
    return base_defs


# ------------------------------------------------------------------
# tool 実装（各 tool は日本語テキストを返す）
# ------------------------------------------------------------------

def _do_get_balance(args: dict) -> str:
    """残高照会。実在の子どものみ。残高を変えない。"""
    conf = _resolve_child(str(args.get("name", "")))
    if conf is None:
        return f"「{args.get('name')}」は登録された子どもに見つからなかったよ。"
    name = str(conf.get("name", ""))
    return f"{name}さんの現在の所持金は {_wallet.get_balance(name)}円 だよ。"


def _do_record_expense(args: dict) -> str:
    """支出記録。残高を減らす。operation_key 必須で二重適用を防ぐ。"""
    conf = _resolve_child(str(args.get("name", "")))
    if conf is None:
        return f"「{args.get('name')}」は登録された子どもに見つからなかったよ。"
    amount = _parse_amount(args.get("amount"))
    if amount is None:
        return f"きんがくがうまく読めなかったよ。1円から{MAX_AMOUNT}円までの数字で教えてね。"
    op_key = str(args.get("operation_key") or "").strip()
    if not op_key:
        # 冪等キーが無ければ二重適用を防げないため実行しない
        return "ちょっとうまくできなかったよ。もう一度ゆっくり教えてくれる？"
    name = str(conf.get("name", ""))
    item = str(args.get("item") or "").strip()
    # AI の生キーを子ども・操作種別で名前空間化する（クロス児童・クロス操作の冪等衝突を構造的に防ぐ）
    eff_key = _scoped_op_key(name, "spending_record", op_key)
    # 言い直しによる二重適用を弾く内容キー（金額+品目、時刻なし）。tool後失敗で子が言い直すと AI が
    # 別の生キーを選び eff_key 冪等をすり抜けるため、内容ベースの補助キーで防ぐ。判定は経過秒
    # (DEDUP_WINDOW_SEC)で行い、分境界の漏れ・品目空の素通りを無くす。
    dup_key = _natural_dup_key(name, "spending_record", amount, item)
    # 主キー命中は「同一操作の再送」なので黙って冪等でよい。内容キーが窓内命中なら言い直しの二重適用と
    # みなすが、本当に別の買い物(同額の駄菓子を2つ等)の可能性も残る。黙って落とすと実残高が実支出より
    # 高く残り静かに狂うため、子が別支出だと明示できる逃げ道を文面で必ず示す。窓を超えた別支出は通す。
    if _wallet.is_operation_applied(eff_key):
        return f"この支出はさっき記録したよ（残高は変わっていないよ）。今の残高は {_wallet.get_balance(name)}円。"
    if _wallet.is_recent_dup_applied(dup_key, DEDUP_WINDOW_SEC):
        return (
            f"さっきも同じ「{amount}円{('・' + item) if item else ''}」を記録したよ。"
            "二重にならないよう、今回はまだ記録していないよ（残高は変わっていないよ）。\n"
            "もしこれが【べつの買いもの】なら、すこし時間をおいてから、"
            "『べつのかいもので◯◯を◯円つかった』のように、ちがう言い方でもう一度教えてね。"
        )
    before = _wallet.get_balance(name)
    # delta は負数（支出）。主キー＋内容キー(窓付き)で二重記録を防ぐ
    after, _ = _wallet.update_balance(
        user_conf=conf, system_conf=_system_conf(),
        delta=-amount, action="spending_record", note=item,
        operation_key=eff_key, aux_operation_keys=[dup_key], aux_dedup_window_sec=DEDUP_WINDOW_SEC,
    )
    return (
        f"支出を記録したよ。\n- 金額: {amount}円\n- 何に: {item if item else 'なし'}\n"
        f"残高: {before}円 → {after}円"
    )


def _do_record_income(args: dict) -> str:
    """臨時入金記録。自己申告上限を超えたら拒否。operation_key 必須。"""
    conf = _resolve_child(str(args.get("name", "")))
    if conf is None:
        return f"「{args.get('name')}」は登録された子どもに見つからなかったよ。"
    amount = _parse_amount(args.get("amount"))
    if amount is None:
        return f"きんがくがうまく読めなかったよ。1円から{MAX_AMOUNT}円までの数字で教えてね。"
    name = str(conf.get("name", ""))
    # 冪等短絡は上限チェックより前に置く。既適用キーの再試行で、その入金が台帳の累計に含まれた状態のまま
    # 上限判定が先に走ると「すでに記録済み」に到達する前に偽の上限拒否を返し、子どもに矛盾した文面が出る。
    # grant/approve と順序を揃える。
    op_key = str(args.get("operation_key") or "").strip()
    if not op_key:
        return "ちょっとうまくできなかったよ。もう一度ゆっくり教えてくれる？"
    # AI の生キーを子ども・操作種別で名前空間化する（クロス児童・クロス操作の冪等衝突を構造的に防ぐ）
    eff_key = _scoped_op_key(name, "manual_income", op_key)
    # 言い直しによる二重適用を弾く内容キー（金額+メモ、時刻なし）。判定は経過秒(DEDUP_WINDOW_SEC)で行う。
    note_for_dup = str(args.get("note") or "").strip()
    dup_key = _natural_dup_key(name, "manual_income", amount, note_for_dup)
    if _wallet.is_operation_applied(eff_key):
        return f"この入金はさっき記録したよ（残高は変わっていないよ）。今の残高は {_wallet.get_balance(name)}円。"
    if _wallet.is_recent_dup_applied(dup_key, DEDUP_WINDOW_SEC):
        # 内容キー窓内命中。黙って落とさず、別の入金なら明示できる逃げ道を示す
        return (
            f"さっきも同じ「{amount}円{('・' + note_for_dup) if note_for_dup else ''}」を記録したよ。"
            "二重にならないよう、今回はまだ記録していないよ（残高は変わっていないよ）。\n"
            "もしこれが【べつのお金】なら、すこし時間をおいてから、ちがう言い方でもう一度教えてね。"
        )
    income_conf = config.get_child_income_report_setting()
    # 安全弁1: 1回あたりの上限。max_amount が 0 以下（誤設定）なら安全側の既定 5000円へ倒す
    max_income = int(income_conf.get("max_amount", 0))
    if max_income <= 0:
        max_income = 5000
    if amount > max_income:
        return (
            f"1回に自分で入金できるのは {max_income}円までだよ。"
            f"{amount}円は多いから、おうちの人に相談してね。"
        )
    # 安全弁2〜4: 自己申告入金の連打で残高を無制限に膨らませられないよう、回数・日次累計・月次累計を強制。
    # 査定支給と同じく Python 側で最終判定する（AI・operation_key ではすり抜けを止められないため）。
    now = datetime.now(_JST)
    day_count, day_total, month_total = _income_day_month_totals(name, now)
    if day_count >= int(income_conf["daily_count_max"]):
        return (
            f"今日はもう自分で入金できる回数（{income_conf['daily_count_max']}回）を使いきったよ。"
            "また明日にしようね。大きいお金はおうちの人に相談してね。"
        )
    if day_total + amount > int(income_conf["daily_total_max"]):
        remaining = max(0, int(income_conf["daily_total_max"]) - day_total)
        return (
            f"今日じぶんで入金できるのは合計 {income_conf['daily_total_max']}円までで、"
            f"あと {remaining}円だよ。それ以上はおうちの人に相談してね。"
        )
    if month_total + amount > int(income_conf["monthly_total_max"]):
        remaining = max(0, int(income_conf["monthly_total_max"]) - month_total)
        return (
            f"今月じぶんで入金できるのは合計 {income_conf['monthly_total_max']}円までで、"
            f"あと {remaining}円だよ。それ以上はおうちの人に相談してね。"
        )
    # op_key の存在・冪等チェックは上限チェックより前で済ませている（矛盾文面を避けるため）
    note = str(args.get("note") or "").strip()
    before = _wallet.get_balance(name)
    after, achieved = _wallet.update_balance(
        user_conf=conf, system_conf=_system_conf(),
        delta=amount, action="manual_income", note=note,
        operation_key=eff_key, aux_operation_keys=[dup_key], aux_dedup_window_sec=DEDUP_WINDOW_SEC,
    )
    msg = (
        f"入金を記録したよ。\n- 金額: {amount}円\n- メモ: {note if note else 'なし'}\n"
        f"残高: {before}円 → {after}円"
    )
    # 入金で目標が達成されたら、その旨も文面に足す（AI が祝福を織り込める）
    for goal in achieved:
        msg += f"\n🎉 目標「{goal.get('title')}」を達成したよ！"
    return msg


def _do_set_initial_balance(args: dict) -> str:
    """初期設定。現在残高との差分を記録して指定額へ合わせる。operation_key 必須。"""
    # set_initial は残高を絶対値でセットし自己申告上限を迂回する管理操作。会話 spawn では
    # ALLOW_ADMIN_OPS を設定しないため、会話 claude から呼ばれても Python 境界で拒否する。
    if not ALLOW_ADMIN_OPS:
        return "残高の初期設定は、おうちの人にお願いしてね。"
    conf = _resolve_child(str(args.get("name", "")))
    if conf is None:
        return f"「{args.get('name')}」は登録された子どもに見つからなかったよ。"
    # 初期設定は 0 を許容するため _parse_amount（1以上）ではなく専用に検証する
    try:
        target = int(args.get("amount"))
    except (TypeError, ValueError):
        return f"金額が正しくないよ（0〜{MAX_AMOUNT}円で教えてね）。"
    if target < 0 or target > MAX_AMOUNT:
        return f"金額が正しくないよ（0〜{MAX_AMOUNT}円で教えてね）。"
    op_key = str(args.get("operation_key") or "").strip()
    if not op_key:
        return "ちょっとうまくできなかったよ。もう一度ゆっくり教えてくれる？"
    name = str(conf.get("name", ""))
    # AI の生キーを子ども・操作種別で名前空間化する（クロス児童・クロス操作の冪等衝突を構造的に防ぐ）
    eff_key = _scoped_op_key(name, "initial_setup", op_key)
    # 既適用キーなら誤った二重報告を避ける
    if _wallet.is_operation_applied(eff_key):
        return f"この初期設定はすでに反映済みだよ（残高は変わっていないよ）。今の残高は {_wallet.get_balance(name)}円。"
    before = _wallet.get_balance(name)
    # 現在残高との差分だけ動かして指定額へ合わせる
    delta = target - before
    after, _ = _wallet.update_balance(
        user_conf=conf, system_conf=_system_conf(),
        delta=delta, action="initial_setup", note="set_current_wallet_balance",
        operation_key=eff_key,
    )
    return f"初期設定を反映したよ。\n対象: {name}\n所持金: {before}円 → {after}円"


def _do_get_savings_goals(args: dict) -> str:
    """貯金目標一覧。残高を変えない。"""
    conf = _resolve_child(str(args.get("name", "")))
    if conf is None:
        return f"「{args.get('name')}」は登録された子どもに見つからなかったよ。"
    name = str(conf.get("name", ""))
    goals = _wallet.get_savings_goals(name)
    current = _wallet.get_balance(name)
    if not goals:
        return f"{name}さんはまだ貯金目標が無いよ（残高: {current}円）。"
    lines = [f"{name}さんの貯金目標（残高: {current}円）:"]
    for g in goals:
        title = str(g.get("title", ""))
        target = int(g.get("target_amount", 0))
        remaining = max(target - current, 0)
        lines.append(f"・{title}: {target}円（あと{remaining}円）")
    return "\n".join(lines)


def _do_set_savings_goal(args: dict) -> str:
    """貯金目標の追加・更新。残高を変えない。"""
    conf = _resolve_child(str(args.get("name", "")))
    if conf is None:
        return f"「{args.get('name')}」は登録された子どもに見つからなかったよ。"
    title = str(args.get("title") or "").strip()
    target = _parse_amount(args.get("target_amount"))
    if not title:
        return "目標の名前を教えてね。"
    if target is None:
        return f"目標金額が正しくないよ（1〜{MAX_AMOUNT}円で教えてね）。"
    name = str(conf.get("name", ""))
    success, result = _wallet.add_savings_goal(name, title, target)
    if not success:
        # 上限超過などのエラーメッセージをそのまま返す
        return result
    action_word = "更新" if result == "updated" else "追加"
    return f"貯金目標を{action_word}したよ。\n・目標: {title} {target}円"


def _read_ledger_by_action(name: str, action: str) -> list[dict]:
    """その子の台帳から指定 action の記録だけを読む。査定・自己申告入金の集計に使う。

    台帳ファイルを直接読む（bot.py 経路に依存しない）。読めなければ空リスト。

    Args:
        name: 子ども名。
        action: 集計対象の action（allowance_grant / manual_income 等）。

    Returns:
        list[dict]: 指定 action の台帳レコード。
    """
    log_dir = config.get_log_dir(_system_conf())
    path = log_dir / f"{name}_wallet_ledger.jsonl"
    if not path.exists():
        return []
    rows: list[dict] = []
    try:
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                # 指定 action の行だけを対象にする
                if isinstance(rec, dict) and rec.get("action") == action:
                    rows.append(rec)
    except Exception:
        # 読めない場合は空を返し、呼び出し側が判断する
        return []
    return rows


def _read_grant_ledger(name: str) -> list[dict]:
    """その子の台帳から action=allowance_grant の記録を読む（後方互換の薄いラッパ）。"""
    return _read_ledger_by_action(name, "allowance_grant")


def _income_day_month_totals(name: str, now: datetime) -> tuple[int, int, int]:
    """今日の自己申告入金の回数・累計と、今月の累計を返す。record_income の上限判定に使う。

    delta が正の manual_income を集計する。ts を JST とみなして年月日で仕分ける。

    Args:
        name: 子ども名。
        now: 現在時刻（JST aware）。

    Returns:
        tuple[int, int, int]: (今日の回数, 今日の累計額, 今月の累計額)。
    """
    day_count = 0
    day_total = 0
    month_total = 0
    for rec in _read_ledger_by_action(name, "manual_income"):
        raw_ts = rec.get("ts")
        if not raw_ts:
            continue
        try:
            ts = datetime.fromisoformat(str(raw_ts))
        except ValueError:
            continue
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=_JST)
        try:
            delta = int(rec.get("delta", 0))
        except (TypeError, ValueError):
            delta = 0
        if delta <= 0:
            continue
        # 今月の累計へ
        if ts.year == now.year and ts.month == now.month:
            month_total += delta
            # 今日の回数・累計へ
            if ts.day == now.day:
                day_count += 1
                day_total += delta
    return day_count, day_total, month_total


def _grant_month_total_and_day_count(name: str, now: datetime) -> tuple[int, int]:
    """今月の査定支給の累計額と、今日の査定支給回数を返す。ガードレール判定に使う。

    Args:
        name: 子ども名。
        now: 現在時刻（JST aware）。

    Returns:
        tuple[int, int]: (今月の支給累計額, 今日の支給回数)。
    """
    month_total = 0
    day_count = 0
    for rec in _read_grant_ledger(name):
        raw_ts = rec.get("ts")
        if not raw_ts:
            continue
        try:
            ts = datetime.fromisoformat(str(raw_ts))
        except ValueError:
            continue
        # naive な ts は JST とみなす
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=_JST)
        # 同じ年月なら今月の累計へ加える（delta は支給額）
        if ts.year == now.year and ts.month == now.month:
            try:
                month_total += int(rec.get("delta", 0))
            except (TypeError, ValueError):
                pass
        # 同じ年月日なら今日の回数へ数える
        if ts.year == now.year and ts.month == now.month and ts.day == now.day:
            day_count += 1
    return month_total, day_count


def _do_grant_allowance(args: dict) -> str:
    """査定支給。fixed/temporary を支給する。4層のガードレールを Python で強制する。

    「何でもかんでも増額・追加支給しない」ため、AI が決めた額でも次の上限を必ず適用する:
      1. fixed（固定増額）は、その子の設定 fixed_increase_cap（1回あたり）以内。
      2. temporary（臨時支給）は、設定 assessment_guardrail.temporary_max 以内。
      3. 今月の査定支給の累計が monthly_total_max を超える分は支給しない。
      4. 今日の査定支給回数が daily_count_max 以上なら、その日はもう支給しない。
    いずれも AI の判断でなく Python が最終決定する。理由（reason）は必須。
    """
    # grant は親承認を飛ばす直接支給。会話 spawn では ALLOW_ADMIN_OPS を設定しないため、
    # 会話 claude から呼ばれても Python 境界で拒否する（--allowedTools 頼みにしない）。
    if not ALLOW_ADMIN_OPS:
        return "その操作はできないよ。お小遣いをあげるかどうかは、おうちの人が決めるんだ。"
    conf = _resolve_child(str(args.get("name", "")))
    if conf is None:
        return f"「{args.get('name')}」は登録された子どもに見つからなかったよ。"
    reason = str(args.get("reason") or "").strip()
    if not reason:
        return "査定の理由が必要だよ。何をがんばったか教えてね。"
    op_key = str(args.get("operation_key") or "").strip()
    if not op_key:
        return "ちょっとうまくできなかったよ。もう一度ゆっくり教えてくれる？"
    name = str(conf.get("name", ""))
    # AI の生キーを子ども・操作種別で名前空間化する（クロス児童・クロス操作の冪等衝突を構造的に防ぐ）
    eff_key = _scoped_op_key(name, "allowance_grant", op_key)

    # 既適用キーなら二重支給を避ける
    if _wallet.is_operation_applied(eff_key):
        return f"この査定はすでに反映済みだよ（残高は変わっていないよ）。今の残高は {_wallet.get_balance(name)}円。"

    # AI が渡した額を安全に整数化する（負数は 0 に丸め、支給をマイナスにしない）
    def _nonneg(v) -> int:
        try:
            return max(0, int(v))
        except (TypeError, ValueError):
            return 0
    fixed_req = _nonneg(args.get("fixed"))
    temp_req = _nonneg(args.get("temporary"))

    # 4層ガードレールを共通ロジックで適用する（propose_allowance と同一の判定）
    now = datetime.now(_JST)
    fixed, temporary, notes, rejected = _apply_guardrails(conf, fixed_req, temp_req, now)
    if rejected:
        return rejected
    grant = fixed + temporary
    if grant <= 0:
        return "今回は増額・臨時支給なしの査定だよ。"

    before = _wallet.get_balance(name)
    after, achieved = _wallet.update_balance(
        user_conf=conf, system_conf=_system_conf(),
        delta=grant, action="allowance_grant", note=reason,
        operation_key=eff_key,
    )
    msg = (
        f"査定でお小遣いを {grant}円 あげたよ。\n- 理由: {reason}\n"
        f"残高: {before}円 → {after}円"
    )
    for note in notes:
        # 頭打ちした場合はその旨を伝える（子どもに上限を分かってもらう）
        msg += f"\n（{note}）"
    for goal in achieved:
        msg += f"\n🎉 目標「{goal.get('title')}」を達成したよ！"
    return msg


def _apply_guardrails(conf: dict, fixed_req: int, temp_req: int, now: datetime) -> tuple[int, int, list[str], str | None]:
    """査定額に4層ガードレールを適用し、(許可fixed, 許可temporary, 注記, 拒否理由) を返す。

    grant と propose の両方から使う共通ロジック。拒否理由が返れば支給不可（日次・月次上限）。

    Returns:
        tuple[int, int, list[str], str | None]: (fixed, temporary, 注記, 拒否理由 or None)。
    """
    guard = config.get_assessment_guardrail_setting()
    notes: list[str] = []
    name = str(conf.get("name", ""))

    # ガード1: 固定は fixed_increase_cap 以内
    fixed_cap = max(0, int(conf.get("fixed_increase_cap") or 0))
    fixed = min(fixed_req, fixed_cap)
    if fixed < fixed_req:
        notes.append(f"固定の増額は1回 {fixed_cap}円まで")

    # ガード2: 臨時は temporary_max 以内
    temp_cap = int(guard["temporary_max"])
    temporary = min(temp_req, temp_cap)
    if temporary < temp_req:
        notes.append(f"臨時支給は1回 {temp_cap}円まで")

    month_total, day_count = _grant_month_total_and_day_count(name, now)

    # ガード4: 今日の回数上限
    if day_count >= int(guard["daily_count_max"]):
        return 0, 0, notes, f"今日はもう査定でお小遣いをあげられる回数（{guard['daily_count_max']}回）を使いきったよ。"

    grant = fixed + temporary
    # ガード3: 月次累計の残り枠まで
    monthly_cap = int(guard["monthly_total_max"])
    remaining = monthly_cap - month_total
    if grant > 0 and remaining <= 0:
        return 0, 0, notes, f"今月はもう査定で増やせる上限（{monthly_cap}円）に届いているよ。"
    if grant > remaining:
        # 固定を優先して残り枠に収め、あふれた分を臨時から削る
        notes.append(f"今月の合計が上限 {monthly_cap}円を超えないよう残り {remaining}円まで")
        if fixed > remaining:
            fixed = remaining
            temporary = 0
        else:
            temporary = remaining - fixed
    return fixed, temporary, notes, None


def _do_propose_allowance(args: dict) -> str:
    """査定支給を提案する（残高は動かさない）。親の承認を待つ pending として payout_requests へ積む。

    子供の会話から AI が呼ぶ。ここで4層ガードレールを適用した「実際に支給されうる額」を算出して
    提案に載せる（親が承認時に見る額と一致させる）。残高は承認時に初めて動く。同じ子の未承認提案が
    あれば上書きして1件に保つ（提案を無限に溜めない）。
    """
    conf = _resolve_child(str(args.get("name", "")))
    if conf is None:
        return f"「{args.get('name')}」は登録された子どもに見つからなかったよ。"
    reason = str(args.get("reason") or "").strip()
    if not reason:
        return "査定の理由が必要だよ。何をがんばったか教えてね。"
    name = str(conf.get("name", ""))

    def _nonneg(v) -> int:
        try:
            return max(0, int(v))
        except (TypeError, ValueError):
            return 0
    fixed_req = _nonneg(args.get("fixed"))
    temp_req = _nonneg(args.get("temporary"))

    # 支給の適否（娯楽か必需品か、所持金で足りるか、本来親が負担すべきか、金額は妥当か）は、金額の機械比較
    # では正しく捌けない文脈判断のため Python では一律拒否しない。AI に所持金を含む材料を渡し、AI が包括的に
    # 判断する（判断基準は system prompt に記載）。Python 境界は「上限」と「本人性」だけを守り、支給の是非は
    # AI 判断＋親承認の二段で確定する。purchase_amount は残っていれば提案文へ添えて親の判断材料にする。
    purchase_amount = _nonneg(args.get("purchase_amount"))

    now = datetime.now(_JST)
    fixed, temporary, notes, rejected = _apply_guardrails(conf, fixed_req, temp_req, now)
    if rejected:
        # 日次・月次上限に達しているときは提案自体を作らない
        return rejected
    grant = fixed + temporary
    if grant <= 0:
        return "今回は増額・臨時支給なしの査定だよ。"

    # payout_requests へ pending として積む。残高はまだ動かさない。
    # プロセス間 flock で read-modify-write を直列化し、bot プロセスの承認処理との競合を防ぐ
    # 提案ごとに一意な proposal_id を採番する。承認支給の冪等キー（二重支給防止）と F/B の dedup に使う。
    # 名前キーで pending を上書きしても、古い通知から古い id で承認されたら「別提案」と分かるようにするため。
    proposal_id = uuid.uuid4().hex
    store = _payout_store()
    with _payout_locked():
        doc = store._load_doc(store.payout_requests_path, "requests")
        requests = doc["requests"]
        # 同じ子の未承認提案は1件に保つ（名前をキーにする）
        requests[name] = {
            "name": name,
            "proposal_id": proposal_id,
            "fixed": fixed,
            "temporary": temporary,
            "total": grant,
            "reason": reason,
            # 買いたい物の値段（分かる場合）。親が「所持金で買えるのに追加支給か？」を判断する材料。
            "purchase_amount": purchase_amount if purchase_amount > 0 else None,
            "created": now.isoformat(),
            "status": "pending",
            # 親へ通知したか。bot 側が未通知の pending を検知して親へ知らせ、通知済みにする。
            # mcp_wallet は claude の子プロセスで Discord を叩けないため、通知は bot 側に委ねる
            "notified": False,
        }
        store._save_doc(store.payout_requests_path, doc, "requests")

    msg = (
        f"査定の結果をおうちの人にお願いしたよ。\n- 提案: {grant}円（固定{fixed}円＋臨時{temporary}円）\n"
        f"- 理由: {reason}\nおうちの人が「査定承認 {name}」で OK したら残高に入るよ。"
    )
    for note in notes:
        msg += f"\n（{note}）"
    return msg


def take_unnotified_proposals(only_name: str | None = None) -> list[dict]:
    """親へまだ通知していない査定提案を取り出し、通知済みにマークして返す（in-process）。

    mcp_wallet は claude の子プロセスで Discord を叩けないため、通知は bot 側が行う。bot は
    子の発話処理後に本関数を呼び、返ってきた提案を親チャンネルへ通知する。通知済みマークは
    ここで原子的に書き戻すため、同じ提案を二度通知しない。

    Returns:
        list[dict]: 通知すべき提案（name / total / fixed / temporary / reason）。無ければ空。
    """
    # マークはここでせず、未通知の pending を返すだけにする。bot 側が送信に成功した分だけ
    # mark_proposals_notified で notified を立てる（送信失敗時に notified が立って永久ロストするのを防ぐ）。
    # only_name を渡すとその子の提案だけ返す。全児童分をまとめて返すと、別の子の査定理由（頑張った内容や
    # 家庭事情）が無関係な子のチャンネルへ漏れる越境表示になるため、通知は必ず対象児1人に絞る。
    target = (only_name or "").strip()
    store = _payout_store()
    with _payout_locked():
        doc = store._load_doc(store.payout_requests_path, "requests")
        pending_to_notify = [
            dict(req) for req in doc["requests"].values()
            if isinstance(req, dict) and req.get("status") == "pending" and not req.get("notified")
            and (not target or str(req.get("name", "")).strip() == target)
        ]
    return pending_to_notify


def mark_proposals_notified(items: list) -> None:
    """通知に成功した提案を通知済みにする。bot が親へ送信できた分だけ呼ぶ。

    take_unnotified_proposals と分離することで、Discord 送信が失敗した提案は notified が立たず、
    次回また未通知として拾われ再通知される（見逃し・送信失敗の救済）。

    (name, proposal_id) のタプルで受ける（codex #5・別提案の取り違え防止）：A を送信中に B へ pending が
    上書きされた後にこれが走っても、「送った提案の proposal_id == 現 pending の proposal_id」のときだけ
    notified を立てる。proposal_id を持たない古いデータとの後方互換のため、str（名前のみ）も受け付ける。

    Args:
        items: 通知に成功した (子ども名, proposal_id) タプル、または名前文字列の一覧。
    """
    if not items:
        return
    # (name -> notified 済みにしてよい proposal_id 集合) に正規化。名前のみ指定は proposal_id 不問（後方互換）。
    by_name: dict[str, set] = {}
    name_only: set = set()
    for it in items:
        if isinstance(it, (tuple, list)) and len(it) == 2:
            nm, pid = str(it[0] or "").strip(), str(it[1] or "").strip()
            if pid:
                by_name.setdefault(nm, set()).add(pid)
            else:
                name_only.add(nm)
        else:
            name_only.add(str(it or "").strip())
    store = _payout_store()
    with _payout_locked():
        doc = store._load_doc(store.payout_requests_path, "requests")
        changed = False
        for name, req in doc["requests"].items():
            if not (isinstance(req, dict) and req.get("status") == "pending" and not req.get("notified")):
                continue
            cur_pid = str(req.get("proposal_id", "")).strip()
            # proposal_id 指定があれば一致時のみ。名前のみ指定は無条件（後方互換）
            if name in name_only or (name in by_name and cur_pid in by_name[name]):
                req["notified"] = True
                changed = True
        if changed:
            store._save_doc(store.payout_requests_path, doc, "requests")


def read_all_pending_proposals() -> list[dict]:
    """未承認の査定提案を全児童分まとめて返す（親チャンネルへの定期再通知が使う。読み取りのみ）。

    親向けの通知に使うため全児童分を返してよい（親は全員の保護者）。notified はマークしない
    （マークは子チャンネルへの発話ターン通知側の責務）。

    Returns:
        list[dict]: status==pending の提案一覧。無ければ空。
    """
    store = _payout_store()
    with _payout_locked():
        doc = store._load_doc(store.payout_requests_path, "requests")
        return [
            dict(req) for req in doc["requests"].values()
            if isinstance(req, dict) and req.get("status") == "pending"
        ]


def read_pending_proposal(name: str) -> dict | None:
    """その子の未承認の査定提案を返す（親承認ハンドラが使う。in-process 呼び出し）。

    Args:
        name: 子ども名。

    Returns:
        dict | None: pending 提案。無ければ None。
    """
    store = _payout_store()
    doc = store._load_doc(store.payout_requests_path, "requests")
    req = doc["requests"].get((name or "").strip())
    if isinstance(req, dict) and req.get("status") == "pending":
        return req
    return None


def approve_proposal(name: str, operation_key: str = "", expected_proposal_id: str = "",
                     approve_parent_intent: str = "", approve_note: str = "") -> str:
    """未承認の査定提案を承認して実支給する（親承認ハンドラが呼ぶ。in-process）。

    提案時のガードレール済みの額をそのまま支給し、payout_requests から提案を消す。支給は
    proposal_id 由来の operation_key で冪等。expected_proposal_id が渡された場合は、現在の pending の
    proposal_id と一致するときだけ支給する（古い親通知から古い提案を承認して二重支給する事故を防ぐ）。

    Args:
        name: 子ども名。
        operation_key: 後方互換用の冪等キー（省略可）。proposal_id があればそちらから固定生成する。
        expected_proposal_id: 親通知に載っていた提案の proposal_id。現在の pending と食い違えば支給しない。

    Returns:
        str: 親向けの結果メッセージ。
    """
    target = (name or "").strip()
    conf = config.find_child_user_by_name(target)
    if conf is None:
        return f"「{target}」は登録された子どもに見つからなかったよ。"
    store = _payout_store()
    # payout の read-modify-write をプロセス間 flock で直列化（propose との競合を防ぐ）
    with _payout_locked():
        doc = store._load_doc(store.payout_requests_path, "requests")
        req = doc["requests"].get(target)
        if not isinstance(req, dict) or req.get("status") != "pending":
            return f"「{target}」の承認待ちの査定は無いよ。"

        # 二重支給防止: 承認しようとした提案 id と、現在の pending の id が食い違うなら、
        # その提案は新しい提案に置き換わっている。古い額を支給しない（親には置き換わった旨を伝える）。
        cur_pid = str(req.get("proposal_id", "")).strip()
        exp_pid = str(expected_proposal_id or "").strip()
        if exp_pid and cur_pid and exp_pid != cur_pid:
            return f"{target} の査定はそのあと新しい内容に変わったよ。最新の内容を確認してから承認してね。"

        # 支給の冪等キーは proposal_id から固定生成する（AI の自由キーに依存しない）。propose 前の
        # 古いデータで proposal_id が無い場合だけ、後方互換で渡された operation_key を使う。
        base_key = cur_pid or str(operation_key or "").strip()
        # 他 tool と冪等空間を共有するため、承認支給も子ども・操作種別で名前空間化する
        eff_key = _scoped_op_key(target, "allowance_grant", base_key) if base_key else ""
        if eff_key and _wallet.is_operation_applied(eff_key):
            # 既適用なら二重支給しない。提案だけ消しておく
            doc["requests"].pop(target, None)
            store._save_doc(store.payout_requests_path, doc, "requests")
            return f"この査定はすでに承認済みだよ（残高は変わっていないよ）。今の残高は {_wallet.get_balance(target)}円。"

        reason = str(req.get("reason", ""))
        # 承認時点で4層ガードレールを再適用する。提案作成から承認までに時間が空き、その間に同月の
        # 査定支給が増えた／月境界をまたいだ場合、提案時の判定と実態がずれるため、支給直前に最終担保する。
        now = datetime.now(_JST)
        r_fixed, r_temp, notes, rejected = _apply_guardrails(
            conf, int(req.get("fixed", 0)), int(req.get("temporary", 0)), now
        )
        if rejected:
            # 承認時に日次・月次上限へ達していたら支給しない。提案は残す（親が翌日以降に再承認できる）
            return f"いま {target} は上限に達しているため支給できないよ。（{rejected}）"
        grant = r_fixed + r_temp
        if grant <= 0:
            doc["requests"].pop(target, None)
            store._save_doc(store.payout_requests_path, doc, "requests")
            return f"いま支給できる枠が無いため {target} の査定は見送ったよ（残高は変わっていないよ）。"
        before = _wallet.get_balance(target)
        after, achieved = _wallet.update_balance(
            user_conf=conf, system_conf=_system_conf(),
            delta=grant, action="allowance_grant", note=reason,
            operation_key=eff_key,
        )
        # 子への F/B（opener）生成入力を積む。承認は前向きに「なぜ OK か」の入口を子へ届ける材料。
        _enqueue_feedback(store, doc, {
            "name": target, "proposal_id": cur_pid, "kind": "approve",
            "grant": grant, "before": before, "after": after,
            "achieved": [str(g.get("title", "")) for g in achieved],
            "parent_intent": str(approve_parent_intent or "").strip(),
            "raw_note": str(approve_note or "").strip(),
        })
        # 承認済みの提案は消す
        doc["requests"].pop(target, None)
        store._save_doc(store.payout_requests_path, doc, "requests")
    msg = f"✅ {target} の査定を承認して {grant}円 支給したよ。\n- 理由: {reason}\n残高: {before}円 → {after}円"
    for goal in achieved:
        msg += f"\n🎉 目標「{goal.get('title')}」を達成！"
    return msg


def _enqueue_feedback(store, doc: dict, entry: dict) -> None:
    """子への F/B（opener）生成入力を payout store の feedback_pending キューへ積む。

    承認/却下の core が `_payout_locked()` 内で呼ぶ。bot プロセスが take_pending_feedback で取り出し
    opener を子 session 上で生成する。同じ子に複数積まれても、取り出し側が最新 proposal_id の1件だけ送る。
    entry には name / proposal_id / kind / 金額系 / parent_intent / raw_note を入れる。

    Args:
        store: payout store。
        doc: _load_doc で読んだ現ドキュメント（requests を含む・呼び出し側が同一 lock 内で save する）。
        entry: 追記する F/B 生成入力。
    """
    # requests と同じドキュメントに feedback_pending リストを同居させる（同一 flock で原子的に保存される）
    fb = doc.get("feedback_pending")
    if not isinstance(fb, list):
        fb = []
    entry = dict(entry)
    entry["created"] = datetime.now(_JST).isoformat()
    fb.append(entry)
    doc["feedback_pending"] = fb


def take_pending_feedback() -> list[dict]:
    """未処理の子 F/B 生成入力を取り出してキューを空にする（bot プロセスが呼ぶ・in-process）。

    同じ子に複数滞留していたら（再提案→承認/却下が連続した等）、その子は**最新 proposal_id の1件だけ**を返す
    （古い結果の opener を出さない）。取り出したものは全てキューから消す（送信失敗は log-and-drop・再enqueueしない）。

    Returns:
        list[dict]: 子ごとに最新1件の F/B 生成入力。無ければ空。
    """
    store = _payout_store()
    with _payout_locked():
        doc = store._load_doc(store.payout_requests_path, "requests")
        fb = doc.get("feedback_pending")
        if not isinstance(fb, list) or not fb:
            return []
        # 子ごとに最後（最新）のエントリだけ残す。created 昇順で来る前提で、後勝ちで畳む
        latest_by_child: dict[str, dict] = {}
        for e in fb:
            if isinstance(e, dict) and e.get("name"):
                latest_by_child[str(e["name"])] = e
        # キューは全消し（取りこぼしの再送はしない＝重複 opener 防止）
        doc["feedback_pending"] = []
        store._save_doc(store.payout_requests_path, doc, "requests")
        return list(latest_by_child.values())


def reject_proposal(name: str, note: str = "", parent_intent: str = "", expected_proposal_id: str = "") -> str:
    """未承認の査定提案を却下する（残高は動かさない）。親承認ハンドラが呼ぶ。

    却下時、子への opener 生成に必要な入力を feedback_pending へ積む。note（親の生一言）は
    子出力へそのまま出さないため、翻訳済み parent_intent があればそれを、無ければ note を後段（bot 側）で
    翻訳する材料として渡す。expected_proposal_id が現 pending と食い違えば「新しい提案に変わった」と返す。

    Args:
        name: 子ども名。
        note: 親が却下時に添えた一言（生・text 経路）。子出力へは出さない。
        parent_intent: 親会話 claude が翻訳済みの意図（AI 経路）。あれば feedback へこちらを使う。
        expected_proposal_id: 親通知に載っていた提案 id。現 pending と食い違えば却下しない。

    Returns:
        str: 親向けの結果メッセージ。
    """
    target = (name or "").strip()
    store = _payout_store()
    # payout の read-modify-write をプロセス間 flock で直列化
    with _payout_locked():
        doc = store._load_doc(store.payout_requests_path, "requests")
        req = doc["requests"].get(target)
        if not isinstance(req, dict) or req.get("status") != "pending":
            return f"「{target}」の承認待ちの査定は無いよ。"
        cur_pid = str(req.get("proposal_id", "")).strip()
        exp_pid = str(expected_proposal_id or "").strip()
        if exp_pid and cur_pid and exp_pid != cur_pid:
            return f"{target} の査定はそのあと新しい内容に変わったよ。最新の内容を確認してから見送りを決めてね。"
        total = int(req.get("total", 0))
        doc["requests"].pop(target, None)
        # 子への F/B（opener）生成入力を積む。生 note は raw_note として持たせ、opener 生成入力には
        # parent_intent だけを使う（bot 側で翻訳・出力レベル invariant で守る）。
        _enqueue_feedback(store, doc, {
            "name": target, "proposal_id": cur_pid, "kind": "reject",
            "total": total, "parent_intent": str(parent_intent or "").strip(),
            "raw_note": str(note or "").strip(),
        })
        store._save_doc(store.payout_requests_path, doc, "requests")
    return f"{target} の査定を見送ったよ。残高は変わっていないよ。"


# ------------------------------------------------------------------
# 親モード専用 tool（親会話の AI 主導化。PARENT_MODE + ALLOW_ADMIN_OPS 前提）
# ------------------------------------------------------------------
# 設計原則: AI に金額・対象を推測させない。親が明示した対象児名・金額だけを引数で受け、tool 内 Python で
# 厳密に検証する。曖昧な指示（対象や額が不明）では AI は tool を呼ばず聞き返す想定。対象児は必ず
# _resolve_parent_target（子ディレクトリ実在のみ）で引き、親名・未登録名は弾く。

def _do_parent_grant(args: dict) -> str:
    """親が対象児へお小遣いを支給する（親モード専用・管理操作）。金額は親が明示した値のみ。"""
    if not (PARENT_MODE and ALLOW_ADMIN_OPS):
        return "この操作は親のチャンネルからのみできるよ。"
    conf = _resolve_parent_target(str(args.get("name", "")))
    if conf is None:
        return f"「{args.get('name')}」という子どもは見つからなかったよ。名前を確認してね。"
    amount = _parse_amount(args.get("amount"))
    if amount is None:
        return f"金額が正しくないよ（1〜{MAX_AMOUNT}円で、はっきりした金額を教えてね）。"
    op_key = str(args.get("operation_key") or "").strip()
    if not op_key:
        return "ちょっとうまくできなかったよ。もう一度ゆっくり教えてくれる？"
    name = str(conf.get("name", ""))
    eff_key = _scoped_op_key(name, "allowance_manual_grant", op_key)
    if _wallet.is_operation_applied(eff_key):
        return f"その支給はすでに反映済みだよ。今の残高は {_wallet.get_balance(name)}円。"
    before = _wallet.get_balance(name)
    after, _ = _wallet.update_balance(
        user_conf=conf, system_conf=_system_conf(),
        delta=amount, action="allowance_manual_grant", note="parent_grant_via_ai",
        operation_key=eff_key,
    )
    return f"{name} に {amount}円 支給したよ。\n残高: {before}円 → {after}円"


def _do_parent_adjust_balance(args: dict) -> str:
    """親が対象児の残高を増減調整する（親モード専用・管理操作）。金額は親が明示した符号付き値のみ。"""
    if not (PARENT_MODE and ALLOW_ADMIN_OPS):
        return "この操作は親のチャンネルからのみできるよ。"
    conf = _resolve_parent_target(str(args.get("name", "")))
    if conf is None:
        return f"「{args.get('name')}」という子どもは見つからなかったよ。名前を確認してね。"
    # delta は符号付き。AI に推測させず、親が明示した増減額のみを整数化する
    try:
        delta = int(args.get("delta"))
    except (TypeError, ValueError):
        return "増やす/減らす金額が正しくないよ（例: +500 や -300 のように教えてね）。"
    if delta == 0:
        return "0円だと残高は変わらないよ。"
    if abs(delta) > MAX_AMOUNT:
        return f"一度に調整できるのは {MAX_AMOUNT}円までだよ。"
    op_key = str(args.get("operation_key") or "").strip()
    if not op_key:
        return "ちょっとうまくできなかったよ。もう一度ゆっくり教えてくれる？"
    name = str(conf.get("name", ""))
    eff_key = _scoped_op_key(name, "balance_adjustment", op_key)
    if _wallet.is_operation_applied(eff_key):
        return f"その調整はすでに反映済みだよ。今の残高は {_wallet.get_balance(name)}円。"
    before = _wallet.get_balance(name)
    after, _ = _wallet.update_balance(
        user_conf=conf, system_conf=_system_conf(),
        delta=delta, action="balance_adjustment", note="parent_adjust_via_ai",
        operation_key=eff_key,
    )
    direction = "増やした" if delta > 0 else "減らした"
    return f"{name} の残高を {abs(delta)}円 {direction}よ。\n残高: {before}円 → {after}円"


def _do_parent_approve_assessment(args: dict) -> str:
    """親が対象児の承認待ち査定を承認して支給する（親モード専用）。"""
    if not (PARENT_MODE and ALLOW_ADMIN_OPS):
        return "この操作は親のチャンネルからのみできるよ。"
    conf = _resolve_parent_target(str(args.get("name", "")))
    if conf is None:
        return f"「{args.get('name')}」という子どもは見つからなかったよ。"
    op_key = str(args.get("operation_key") or "").strip()
    if not op_key:
        return "ちょっとうまくできなかったよ。もう一度ゆっくり教えてくれる？"
    # 既存の親承認ロジック（4層ガード再適用・flock・冪等）をそのまま使う
    return approve_proposal(str(conf.get("name", "")), op_key)


def _do_parent_reject_assessment(args: dict) -> str:
    """親が対象児の承認待ち査定を却下する（親モード専用・残高は動かさない）。"""
    if not PARENT_MODE:
        return "この操作は親のチャンネルからのみできるよ。"
    conf = _resolve_parent_target(str(args.get("name", "")))
    if conf is None:
        return f"「{args.get('name')}」という子どもは見つからなかったよ。"
    return reject_proposal(str(conf.get("name", "")))


def _do_parent_list_balances(args: dict) -> str:
    """親が全児童の残高一覧を見る（親モード専用・残高は動かさない）。"""
    if not PARENT_MODE:
        return "この操作は親のチャンネルからのみできるよ。"
    users = config.load_all_users()
    if not users:
        return "登録された子どもがいないみたい。"
    lines = ["みんなの残高だよ。"]
    for u in sorted(users, key=lambda x: str(x.get("name", ""))):
        n = str(u.get("name", ""))
        lines.append(f"- {n}: {_wallet.get_balance(n)}円")
    return "\n".join(lines)


def _do_parent_get_pending(args: dict) -> str:
    """親が承認待ちの査定提案一覧を見る（親モード専用・残高は動かさない）。"""
    if not PARENT_MODE:
        return "この操作は親のチャンネルからのみできるよ。"
    pending = read_all_pending_proposals()
    if not pending:
        return "いま承認待ちの査定はないよ。"
    lines = ["承認待ちの査定だよ。"]
    for p in pending:
        lines.append(f"- {p.get('name','')}: {int(p.get('total',0))}円（理由: {p.get('reason','')}）")
    return "\n".join(lines)


# tool 名から実装への対応表。dispatch はここを引く
_HANDLERS = {
    "get_balance": _do_get_balance,
    "record_expense": _do_record_expense,
    "record_income": _do_record_income,
    "set_initial_balance": _do_set_initial_balance,
    "get_savings_goals": _do_get_savings_goals,
    "set_savings_goal": _do_set_savings_goal,
    "propose_allowance": _do_propose_allowance,
    "grant_allowance": _do_grant_allowance,
    # 親モード専用
    "parent_grant": _do_parent_grant,
    "parent_adjust_balance": _do_parent_adjust_balance,
    "parent_approve_assessment": _do_parent_approve_assessment,
    "parent_reject_assessment": _do_parent_reject_assessment,
    "parent_list_balances": _do_parent_list_balances,
    "parent_get_pending": _do_parent_get_pending,
}


def _handle_tool_call(req_id, params: dict) -> None:
    """tools/call を受けて対応する tool を実行し、テキスト結果を返す。"""
    tool_name = params.get("name")
    args = params.get("arguments", {}) or {}
    handler = _HANDLERS.get(tool_name)
    if handler is None:
        _send({"jsonrpc": "2.0", "id": req_id, "error": {"code": -32601, "message": f"unknown tool {tool_name}"}})
        return
    try:
        text = handler(args)
    except _ChildMismatch as mismatch:
        # 別の子を対象にしようとした越境。実残高は動いていない。AI へ拒否を明示する
        text = (
            f"「{mismatch}」の財布は操作できないよ。この会話でさわれるのは "
            f"{ACTIVE_CHILD}さん自身の分だけだよ。"
        )
    except Exception as e:
        # tool 内の想定外例外。子ども向け文言には例外クラス名等の技術用語・内部事情を出さない
        # （system prompt の「内部事情を子に語らない」と整合させ、AI が戻り値を会話へ織り込んでも
        # メタ用語が漏れないようにする）。例外の詳細は stderr の診断にのみ残す。
        import traceback as _tb
        print(
            f"[mcp_wallet_tool_error] tool={tool_name} {type(e).__name__}: {e}\n{_tb.format_exc()}",
            file=sys.stderr, flush=True,
        )
        text = "ちょっとうまくできなかったよ。もう一度ゆっくり教えてくれる？"
    _text_result(req_id, text)


def main() -> None:
    """stdio で JSON-RPC を1行ずつ受けて MCP プロトコルに応答するループ。"""
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
        except json.JSONDecodeError:
            continue
        method = req.get("method")
        req_id = req.get("id")

        if method == "initialize":
            _send({
                "jsonrpc": "2.0", "id": req_id,
                "result": {
                    "protocolVersion": "2024-11-05",
                    "capabilities": {"tools": {}},
                    "serverInfo": {"name": "wallet", "version": "1.0"},
                },
            })
        elif method == "notifications/initialized":
            # 初期化完了通知には応答不要
            continue
        elif method == "tools/list":
            _send({"jsonrpc": "2.0", "id": req_id, "result": {"tools": _tool_defs()}})
        elif method == "tools/call":
            _handle_tool_call(req_id, req.get("params", {}) or {})
        else:
            # 未対応メソッドは method not found を返す（通知には応答しない）
            if req_id is not None:
                _send({"jsonrpc": "2.0", "id": req_id, "error": {"code": -32601, "message": "method not found"}})


if __name__ == "__main__":
    main()
