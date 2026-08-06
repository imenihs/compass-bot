"""claude CLI を主導役にした会話層。AI が会話を持ち、金額処理は MCP wallet tool へ委ねる。

Phase N-11 の中核。子どもの発話ごとに claude CLI を subprocess 起動し、その子の session を
`--resume` で継続することで会話文脈を保つ（現行ボットで壊れていた会話永続性の根本解決）。

【会話継続の仕組み】
- 子どもごとに claude の session_id を1つ持つ。第1段の SessionStore の会話セッション（data）へ
  session_id を保存し、次ターンで `--resume <session_id>` に渡す。session は claude 側の
  `~/.claude/projects/` にディスク永続化され、ボット再起動を跨いでも継続する。
- session は子ども単位で分離される（別の子の文脈が混ざらない）。
- resume に失敗した（session が失効・破損した）場合は、その id を捨てて新規セッションで1回やり直す。
  死んだ id を無限に resume し続けて会話が詰まるのを防ぐ。

【子どもの本人性（実残高の越境防止）】
- 発話者の子ども名を env COMPASS_ACTIVE_CHILD として subprocess へ渡す。mcp_wallet はこの値だけを
  操作対象にし、AI が tool 引数で別の子を指定しても拒否する。本人性の保証を system prompt の散文でなく
  Python の境界（モデルの制御外）で行う。

【金額処理】
- record_expense / record_income などの確定処理は MCP wallet サーバ（app/mcp_wallet.py）が tool として
  公開し、AI が tool call で呼ぶ。金額計算・残高更新は Python が行い、AI には計算させない。

【依存】
- 外部依存は deps 経由で解決する（app.config/app.bot を直接 import しない、第1段の規約）。
- 応答の送信・会話ログ記録は reply.send_reply（唯一の出口）を通す。
"""
import asyncio
import json
import os
import signal
from pathlib import Path

from app import storage
from app.conv import deps

# 会話セッションの種別。SessionStore に張るセッションの kind を統一する
SESSION_KIND = "ai_chat"

# claude CLI の実行ファイル名。PATH 解決に任せる（サーバ導入済み /usr/local/bin/claude）
CLAUDE_BIN = "claude"

# 応答生成のタイムアウト（秒）。素の会話は3〜5秒、wallet tool 込みで最大20秒程度かかる実測に基づき、
# 余裕を見て30秒で打ち切る。これを超える応答は返さずフォールバックへ倒す
CLI_TIMEOUT_SEC = 30

# 「考え中」表示を出すまでの遅延（秒）。これより早く返る素の雑談では出さず連投ノイズを避ける
THINKING_DELAY_SEC = 3

# MCP wallet サーバの設定ファイル。リポジトリ直下 config/wallet_mcp.json を使う
WALLET_MCP_CONFIG = Path(__file__).resolve().parents[2] / "config" / "wallet_mcp.json"

# AI へ許可する wallet tool。ここに無い tool は AI が呼べない（最小権限）
ALLOWED_WALLET_TOOLS = [
    "mcp__wallet__get_balance",
    "mcp__wallet__record_expense",
    "mcp__wallet__record_income",
    "mcp__wallet__set_initial_balance",
    "mcp__wallet__get_savings_goals",
    "mcp__wallet__set_savings_goal",
]


# resume に使った session_id が死んでいるとき claude が stderr に出す目印。
# これを検知したときだけ「tool 未実行の resume 失敗」と判断して安全に再試行する
_RESUME_FAILED_SIGNAL = "No conversation found with session ID"


def _diag(event: str, details: dict) -> None:
    """会話層の異常を診断ログへ残す。記録失敗は握って本処理を止めない。

    get_log_dir は既に data/logs を指すため "logs" を重ねない。bot.py・wallet_service と
    同じ data/logs/runtime_diagnostics.jsonl へ集約し、会話が壊れた証跡を運用者が1箇所で追える。
    """
    try:
        storage.append_jsonl(
            deps.get_log_dir() / "runtime_diagnostics.jsonl",
            {"ts": storage.now_jst_iso(), "event": event, "details": details},
        )
    except Exception:
        # 診断ログの失敗で会話経路を巻き込まない
        pass


def _build_system_prompt(user_conf: dict) -> str:
    """その子ども向けの system prompt を組む。人格・年齢・約束事を渡す。

    金額を動かす操作は必ず wallet tool を使い、AI が金額を自分で計算・宣言しないよう明示する。
    実残高は tool の戻り値のみを信頼する。なお対象児童の強制は env（mcp_wallet 側）で行うため、
    ここでの本人性の記述は補助であり、これに安全を依存させない。

    Args:
        user_conf: 対象児童の設定 dict（name / age / bot_personality 等）。

    Returns:
        str: claude CLI へ --append-system-prompt で渡す指示文。
    """
    name = str(user_conf.get("name", "きみ"))
    age = user_conf.get("age")
    age_text = f"{age}歳" if isinstance(age, int) else "年齢不明"
    personality = str(user_conf.get("bot_personality", "sibling"))
    # 人格の口調は既存プロンプトの語彙に合わせ、年齢に応じた易しさを求める
    return (
        f"あなたは子ども「{name}」（{age_text}）のお小遣い管理を手伝う、やさしい会話ボットです。"
        f"口調は「{personality}」。{age_text}の子が読める、やさしい日本語で短く話します。\n"
        "【重要な約束】\n"
        "- お金（残高・支出・入金・初期設定・貯金目標）を動かすときは、必ず wallet ツールを使うこと。"
        "自分で金額を計算したり、残高を勝手に宣言したりしないこと。\n"
        "- 残高や結果は、ツールが返した値だけを信じて伝えること。\n"
        "- 同じ操作を二度実行しないよう、ツールの operation_key には毎回ちがう一意な文字列を渡すこと。\n"
        "- お金以外の雑談は、ツールを使わず自然に会話すること。\n"
        f"- 相手は {name} さん本人です。ほかの子の財布は操作できません。"
    )


def _parse_output(stdout_text: str) -> tuple[bool, str, str | None]:
    """claude CLI の JSON 出力を解釈し、(成功したか, 応答文, session_id) を返す。

    --output-format json は最終結果を1つの JSON オブジェクトで返す。is_error が真、または
    JSON として解釈できない出力は失敗とみなす。呼び出し側は成功時のみ result を応答として使う。

    Args:
        stdout_text: claude CLI の標準出力全体。

    Returns:
        tuple[bool, str, str | None]: (成功, 応答文, session_id)。失敗時は (False, "", session_id or None)。
    """
    text = stdout_text.strip()
    if not text:
        return False, "", None
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        # JSON でなければ失敗扱い。エラー文を子どもへ流さない
        return False, "", None
    if not isinstance(data, dict):
        return False, "", None
    session_id = data.get("session_id")
    session_id = str(session_id) if session_id else None
    # is_error が真なら CLI はエラーを返している。result はエラー文なので応答に使わない
    if data.get("is_error"):
        return False, "", session_id
    result = str(data.get("result") or "")
    if not result:
        return False, "", session_id
    return True, result, session_id


async def _spawn_claude(prompt: str, session_id: str | None, system_prompt: str, child_name: str):
    """claude CLI を subprocess 起動して (returncode, stdout, stderr) を返す。失敗時は例外。

    子どもの発話は `--` セパレータの後の位置引数として渡す。`-p <発話>` 方式だと、`--` で
    始まる発話（例「--help」「--model ...」）が claude の CLI フラグとして解釈され、モデルや
    system-prompt を差し替える制御平面バイパスになりうるため（実マネーを扱う系では致命的）。
    `--` 以降は全て位置引数として扱われ、フラグにならない。

    stdin は DEVNULL で閉じる（開けたままだと「no stdin data received in 3s」で毎回3秒無駄）。
    子プロセスは新しいプロセスグループで起動し、タイムアウト時はグループごと kill して claude が
    生む MCP wallet 子プロセスも一緒に落とす（孤児・ゾンビの蓄積を防ぐ）。発話者の本人性は
    env COMPASS_ACTIVE_CHILD で mcp_wallet へ渡す（AI の制御外での越境防止）。stderr も取得して
    resume 失敗シグナルの判別と診断に使う。

    Args:
        prompt: 子どもの発話のみ。system 指示は含めない。
        session_id: 継続する claude セッション。無ければ新規セッション。
        system_prompt: --append-system-prompt で渡す人格・約束事。
        child_name: env COMPASS_ACTIVE_CHILD へ渡す発話者名。

    Returns:
        tuple[int, str, str]: (returncode, stdout テキスト, stderr テキスト)。

    Raises:
        asyncio.TimeoutError: 応答が CLI_TIMEOUT_SEC を超えた場合（プロセスは kill 済み）。
        Exception: subprocess の起動失敗（claude 不在・権限等）。
    """
    # 発話者が制御できない固定フラグを先に置き、子どもの発話は必ず `--` の後の位置引数にする
    args = [
        CLAUDE_BIN, "--print",
        "--output-format", "json",
        "--append-system-prompt", system_prompt,
        "--mcp-config", str(WALLET_MCP_CONFIG),
        "--allowedTools", ",".join(ALLOWED_WALLET_TOOLS),
    ]
    # 継続セッションがあれば resume する。会話文脈を保つ要
    if session_id:
        args += ["--resume", session_id]
    # end-of-options セパレータの後に発話を置く。以降はフラグとして解釈されない
    args += ["--", prompt]
    # env に発話者を束縛して渡す。mcp_wallet はこの子だけを操作対象にする
    child_env = dict(os.environ)
    child_env["COMPASS_ACTIVE_CHILD"] = child_name
    proc = await asyncio.create_subprocess_exec(
        *args,
        stdin=asyncio.subprocess.DEVNULL,   # stdin を閉じて3秒待ちを避ける
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=child_env,
        start_new_session=True,             # 新プロセスグループ。タイムアウト時にグループごと落とす
    )
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=CLI_TIMEOUT_SEC)
    except asyncio.TimeoutError:
        # グループごと kill して claude の MCP 子プロセスも道連れに落とす
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            # 既に終了していれば何もしない
            pass
        await proc.wait()
        raise
    return (
        proc.returncode,
        stdout.decode("utf-8", errors="replace"),
        stderr.decode("utf-8", errors="replace"),
    )


async def _run_claude(prompt: str, session_id: str | None, system_prompt: str, child_name: str) -> tuple[bool, str, str | None]:
    """claude を起動する。resume 対象が死んでいた場合に限り、新規セッションで1回だけやり直す。

    再試行は「resume に使った session_id が失効・破損していて、まだ会話が始まっておらず tool が
    1つも実行されていない」ことが stderr の目印（_RESUME_FAILED_SIGNAL）で確認できた場合だけに
    厳しく限定する。タイムアウトや is_error などその他の失敗では**再試行しない**。それらは wallet
    tool が既に実残高を動かした後かもしれず、同じ発話をもう一度流すと operation_key が変わって
    冪等が効かず、支出・入金が二重適用される（実残高の二重課金）ため。

    Args:
        prompt: 子どもの発話。
        session_id: 継続する claude セッション。
        system_prompt: 人格・約束事。
        child_name: 発話者名（env 束縛用）。

    Returns:
        tuple[bool, str, str | None]: (成功, 応答文, session_id)。
    """
    # 1回目: 継続を試みる。タイムアウトと起動失敗は区別して診断する
    timed_out = False
    stderr = ""
    try:
        returncode, stdout, stderr = await _spawn_claude(prompt, session_id, system_prompt, child_name)
    except asyncio.TimeoutError:
        # タイムアウトは tool 実行後かもしれない。再試行せず終端扱いにする（二重課金防止）
        timed_out = True
        returncode, stdout = 1, ""
        _diag("ai_conversation_timeout", {"child": child_name, "had_session": bool(session_id)})
    except Exception as e:
        returncode, stdout = 1, ""
        _diag("ai_conversation_spawn_error", {"child": child_name, "error": f"{type(e).__name__}: {e}"})

    ok, result, new_sid = _parse_output(stdout)
    if ok and returncode == 0:
        return True, result, new_sid

    # 再試行は「resume 対象が見つからない」失敗に限る。タイムアウト・その他失敗では絶対に再試行しない。
    # このシグナルは会話開始前に出るため、tool は1つも実行されておらず二重課金にならない
    resume_dead = bool(session_id) and (_RESUME_FAILED_SIGNAL in stderr)
    if resume_dead and not timed_out:
        _diag("ai_conversation_resume_failed", {"child": child_name, "dead_session": session_id})
        try:
            returncode2, stdout2, stderr2 = await _spawn_claude(prompt, None, system_prompt, child_name)
        except Exception as e:
            _diag("ai_conversation_spawn_error", {"child": child_name, "error": f"{type(e).__name__}: {e}", "retry": True})
            return False, "", None
        ok2, result2, new_sid2 = _parse_output(stdout2)
        if ok2 and returncode2 == 0:
            return True, result2, new_sid2
        return False, "", None

    # 再試行しない失敗。stderr を診断へ残して原因を追えるようにする
    if not ok:
        _diag("ai_conversation_failed", {
            "child": child_name, "returncode": returncode,
            "timed_out": timed_out, "stderr": stderr[:500],
        })
    return False, "", None


async def handle_conversation(channel, user_conf: dict, input_text: str) -> str:
    """子どもの発話を AI 主導で処理し、応答を返す。会話継続と会話ログ記録を行う。

    処理の流れ:
      1. その子の会話セッションから claude session_id を引く（無ければ新規）。
      2. 入力を会話ログへ記録する（reply.record_incoming）。
      3. 「考え中」を送って無反応に見せない（reply.send_reply は通さない例外送信）。
      4. claude CLI を起動して応答と新しい session_id を得る（resume 失敗は新規で復帰）。
      5. 成功時は新しい session_id を保存、失敗時は既存 id を保持（文脈を捨てない）。
      6. reply.send_reply で応答を送信し会話ログへ記録する（唯一の出口）。

    Args:
        channel: 送信先の Discord チャンネル。
        user_conf: 対象児童の設定 dict。
        input_text: 子どもの発話本文。

    Returns:
        str: 送信した応答の完全な内容。空応答なら空文字。
    """
    from app.conv import reply  # 遅延 import で循環を避ける

    user_name = str(user_conf.get("name", ""))
    store = deps.session_store()

    # 1. 継続する session_id を引く。会話セッションの data に保持する
    session = await store.get_session(user_name)
    session_id = None
    if isinstance(session, dict) and session.get("kind") == SESSION_KIND:
        data = session.get("data") or {}
        session_id = data.get("claude_session_id")

    # 2. 入力を会話ログへ記録する
    reply.record_incoming(user_name, input_text, kind=SESSION_KIND)

    # 3. 「考え中」は遅延送信する。素の雑談は3〜5秒で返るため、その場合は出さずに連投ノイズを避け、
    #    tool 込みで遅いときだけ出す（reply.py のとおり考え中表示は send_reply を通さない例外送信）
    async def _thinking_after_delay():
        try:
            await asyncio.sleep(THINKING_DELAY_SEC)
            await channel.send("考えているよ、ちょっと待ってね。")
        except asyncio.CancelledError:
            # 早く応答できたときはキャンセルされる。何も送らない
            raise
        except Exception:
            # 進捗表示の失敗は本処理を止めない
            pass
    thinking_task = asyncio.create_task(_thinking_after_delay())

    # 4. 発話は素のまま、人格・約束は --append-system-prompt、本人性は env で渡して claude を起動する
    system_prompt = _build_system_prompt(user_conf)
    try:
        ok, result, new_session_id = await _run_claude(input_text, session_id, system_prompt, user_name)
    finally:
        # 応答が返ったら考え中タスクを止める（未送信なら送らずに済む）
        thinking_task.cancel()

    # 5. session_id の更新方針:
    #    - 成功して新しい id が取れたら保存する（次ターンの継続用）
    #    - 失敗したら既存 id を保持する（一時的な失敗で会話文脈を捨てない）
    if ok and new_session_id:
        ttl = int(deps.conversation_session_setting().get("expiry_minutes", 30))
        await store.open_session(
            user_name, SESSION_KIND,
            data={"claude_session_id": new_session_id},
            ttl_minutes=ttl,
        )

    # 応答が取れなければやさしいフォールバックを返す（実残高は動いていない）
    if not ok or not result:
        result = "ごめんね、いまうまくお返事できなかったよ。もう一度言ってくれる？"

    # 6. 唯一の出口から送信する（会話ログ記録＋分割を集約）
    return await reply.send_reply(channel, result, user_name=user_name, kind=SESSION_KIND)
