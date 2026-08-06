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

from app import config, wallet_service as wallet_module
from app.config import MAX_WALLET_INPUT_AMOUNT

# 金額入力の上限。AI 経由でも桁あふれ・異常値を弾く（本文パースの上限と揃える）
MAX_AMOUNT = MAX_WALLET_INPUT_AMOUNT

# 単一の WalletService を共有する。サーバは1プロセス1インスタンスで直列化される
_wallet = wallet_module.WalletService()

# 会話層が subprocess 起動時に env で渡す「今この会話をしている子ども」の名前。
# tool の対象児童は AI が渡す name 引数ではなく必ずこの値で決める（AI がプロンプト
# インジェクションで別の子を対象にする越境を、モデルの制御外で塞ぐ）。会話ボット用途では
# 会話の相手は1人に固定されるため、この env が唯一の正当な対象児童になる。
ACTIVE_CHILD = os.environ.get("COMPASS_ACTIVE_CHILD", "").strip()


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
    return [
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
            "description": "子どもの支出を記録し、残高を減らす。金額は必ず正の整数（円）。",
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
            "description": "子どもの臨時入金を記録し、残高を増やす。自己申告のため1回の上限を超えると拒否する。",
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
    ]


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
        return f"金額が正しくないよ（1〜{MAX_AMOUNT}円の正の数で教えてね）。"
    op_key = str(args.get("operation_key") or "").strip()
    if not op_key:
        # 冪等キーが無ければ二重適用を防げないため実行しない
        return "内部エラー: 操作キーが無いため支出を記録できなかったよ。"
    name = str(conf.get("name", ""))
    item = str(args.get("item") or "").strip()
    # 既適用キーなら update_balance はスキップする。事前に検知して誤った二重報告を避ける
    if _wallet.is_operation_applied(op_key):
        return f"この支出はすでに記録済みだよ（残高は変わっていないよ）。今の残高は {_wallet.get_balance(name)}円。"
    before = _wallet.get_balance(name)
    # delta は負数（支出）。operation_key で二重記録を防ぐ
    after, _ = _wallet.update_balance(
        user_conf=conf, system_conf=_system_conf(),
        delta=-amount, action="spending_record", note=item,
        operation_key=op_key,
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
        return f"金額が正しくないよ（1〜{MAX_AMOUNT}円の正の数で教えてね）。"
    # 自己申告入金の上限を Python で検証する。AI がすり抜けさせない安全弁。
    # max_amount が 0 以下（未設定・誤設定）なら「無制限」にはせず、安全側に倒して
    # 既定 5000円を上限にする。安全弁が設定ミスで無効化される事故を防ぐ。
    max_income = int(config.get_child_income_report_setting().get("max_amount", 0))
    if max_income <= 0:
        max_income = 5000
    if amount > max_income:
        return (
            f"1回に自分で入金できるのは {max_income}円までだよ。"
            f"{amount}円は多いから、おうちの人に相談してね。"
        )
    op_key = str(args.get("operation_key") or "").strip()
    if not op_key:
        return "内部エラー: 操作キーが無いため入金を記録できなかったよ。"
    name = str(conf.get("name", ""))
    note = str(args.get("note") or "").strip()
    # 既適用キーなら誤った二重報告を避ける
    if _wallet.is_operation_applied(op_key):
        return f"この入金はすでに記録済みだよ（残高は変わっていないよ）。今の残高は {_wallet.get_balance(name)}円。"
    before = _wallet.get_balance(name)
    after, achieved = _wallet.update_balance(
        user_conf=conf, system_conf=_system_conf(),
        delta=amount, action="manual_income", note=note,
        operation_key=op_key,
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
        return "内部エラー: 操作キーが無いため初期設定できなかったよ。"
    name = str(conf.get("name", ""))
    # 既適用キーなら誤った二重報告を避ける
    if _wallet.is_operation_applied(op_key):
        return f"この初期設定はすでに反映済みだよ（残高は変わっていないよ）。今の残高は {_wallet.get_balance(name)}円。"
    before = _wallet.get_balance(name)
    # 現在残高との差分だけ動かして指定額へ合わせる
    delta = target - before
    after, _ = _wallet.update_balance(
        user_conf=conf, system_conf=_system_conf(),
        delta=delta, action="initial_setup", note="set_current_wallet_balance",
        operation_key=op_key,
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


# tool 名から実装への対応表。dispatch はここを引く
_HANDLERS = {
    "get_balance": _do_get_balance,
    "record_expense": _do_record_expense,
    "record_income": _do_record_income,
    "set_initial_balance": _do_set_initial_balance,
    "get_savings_goals": _do_get_savings_goals,
    "set_savings_goal": _do_set_savings_goal,
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
        # tool 内の想定外例外は AI へエラーとして返し、残高処理の失敗を握りつぶさない
        text = f"内部エラーで処理できなかったよ（{type(e).__name__}）。"
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
