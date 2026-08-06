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
import contextlib
import json
import os
import signal
from pathlib import Path

from app import storage
from app.conv import deps

# 子どもごとのターン直列化ロック。同じ子が連投すると2つの handle_conversation が並行し、
# 同じ session_id を同時 resume して claude のセッションファイルを壊す・tool が二重実行される
# （実残高の二重課金）ため、子ども単位で直列化する。子が違えば並行を保つ（ロックは per-child）。
_child_locks: dict[str, asyncio.Lock] = {}


def _lock_for(user_name: str) -> asyncio.Lock:
    """指定した子ども用のロックを返す（無ければ生成）。同じ子のターンを直列化する。"""
    lock = _child_locks.get(user_name)
    if lock is None:
        lock = asyncio.Lock()
        _child_locks[user_name] = lock
    return lock

# 会話セッションの種別。SessionStore に張るセッションの kind を統一する
SESSION_KIND = "ai_chat"

# claude session_id ポインタの寿命（分）。会話の idle 窓（conversation_session の expiry_minutes、既定30分）
# とは分離する。claude セッション本体は claude 側にディスク永続化されるため、30分無発話で session_id を
# 捨てると「学校から帰って続きを話しても文脈が切れる」= 現行のクソ対応が残る。7日を寿命にし、それを超えたら
# 新セッションを張り直す（古すぎる文脈は引きずらない）。
SESSION_ID_TTL_MINUTES = 7 * 24 * 60  # 7日

# claude CLI の実行ファイル名。PATH 解決に任せる（サーバ導入済み /usr/local/bin/claude）
# claude CLI の実行ファイル。systemd 配下は PATH が最小限で /usr/local/bin を含まないことがあるため、
# PATH 解決に頼らず絶対パスを既定にする。環境が違う場合は env COMPASS_CLAUDE_BIN で上書きできる。
CLAUDE_BIN = os.environ.get("COMPASS_CLAUDE_BIN", "/usr/local/bin/claude")

# 応答生成のタイムアウト（秒）。素の会話は3〜5秒、wallet tool 込みで最大20秒程度かかる実測に基づき、
# 余裕を見て30秒で打ち切る。これを超える応答は返さずフォールバックへ倒す
CLI_TIMEOUT_SEC = 30

# 「考え中」表示を出すまでの遅延（秒）。これより早く返る素の雑談では出さず連投ノイズを避ける
THINKING_DELAY_SEC = 3

# MCP wallet サーバの設定ファイル。リポジトリ直下 config/wallet_mcp.json を使う
WALLET_MCP_CONFIG = Path(__file__).resolve().parents[2] / "config" / "wallet_mcp.json"

# AI へ許可する wallet tool。ここに無い tool は AI が呼べない（最小権限）。
# set_initial_balance（残高を任意額へ直接セットする管理操作）は意図的に含めない。
# 自己申告入金の上限を迂回して子どもが自分の残高を書き換えられてしまうため、初期設定は
# 親コマンド経路に限定する。会話では残高を「増減」だけできる（record_expense/record_income）。
ALLOWED_WALLET_TOOLS = [
    "mcp__wallet__get_balance",
    "mcp__wallet__record_expense",
    "mcp__wallet__record_income",
    "mcp__wallet__get_savings_goals",
    "mcp__wallet__set_savings_goal",
    # 査定は「提案」まで。残高は動かさず親の承認を待つ。実支給(grant_allowance)は親経路のみ
    "mcp__wallet__propose_allowance",
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
    # bot_personality は英語 enum なので日本語の口調説明へ変換する。そのまま「口調は teacher」と
    # 渡すと AI が英単語を口調ラベルとして扱えず不自然になるため、意味を日本語で明示する。
    personality_key = str(user_conf.get("bot_personality", "sibling"))
    personality_label = {
        "teacher": "やさしい先生のように、ていねいに教えてくれる口調",
        "sibling": "年上のきょうだいのように、親しみやすくフランクな口調",
        "friend": "友だちのように、気さくで楽しい口調",
        "parent": "おうちの人のように、あたたかく見守る口調",
    }.get(personality_key, "年上のきょうだいのように親しみやすい口調")
    base = (
        f"あなたは子ども「{name}」（{age_text}）のお小遣い管理を手伝う、やさしい会話ボットです。"
        f"{personality_label}で、{age_text}の子が読める、やさしい日本語で短く話します。\n"
        "【最重要・お金の記録は必ずツールを使う】\n"
        "- 子どもが「◯円つかった／買った」と言ったら、雑談で流さず 必ず record_expense を呼ぶこと。\n"
        "- 「◯円もらった／お小遣いもらった」と言ったら、必ず record_income を呼ぶこと。\n"
        "- 「◯円ためたい／目標」と言ったら、必ず set_savings_goal を呼ぶこと。\n"
        "- 残高を聞かれたら、必ず get_balance を呼ぶこと（記憶や推測で答えない）。\n"
        "- 上のようなお金の話は「ただの報告だ」と思っても、例外なくツールを呼ぶこと。ツールを呼ばずに"
        "「記録したよ」等と言うのは禁止。金額が文に出てきたら、まずツールを呼ぶ。\n"
        "【その他の約束】\n"
        "- 自分で金額を計算したり、残高を勝手に宣言したりしないこと。結果はツールが返った値だけを信じる。\n"
        "- 残高を最初から決め直す「初期設定」はできない。頼まれてもおうちの人に相談するよう伝えること。\n"
        "- 同じ操作を二度実行しないよう、ツールの operation_key には毎回ちがう一意な文字列を渡すこと。\n"
        "- お金がまったく出てこない雑談（好きな食べ物・学校の話など）は、ツールを使わず自然に会話すること。\n"
        f"- 相手は {name} さん本人です。ほかの子の財布は操作できません。\n"
        "- 「わたしは体が無い」「AIだから」「ツールを呼ぶ」「システム」「エラー」等、自分の仕組みや内部事情を"
        "子どもに語らないこと。うまくいかないときも、内部の理由でなく、やさしい言葉で言い直してもらうよう促すこと。"
    )
    # コーチングは呼び出し側（handle_conversation）で input_text を見て付けるか決め、
    # coaching_block を渡してくる。ここでは base に連結するだけ（出し分け・抑制は非同期側で行う）。
    return base


# お金・貯金そのものを指す語。これ単独でもお金の話題とみなす（誤爆の少ない明確な語だけ）。
_COACHING_MONEY_WORDS = (
    "円", "お金", "おかね", "おこづかい", "お小遣い", "貯金", "ちょきん", "査定",
)
# 目標・購買・入金の動詞。ただし単独では純雑談を拾いやすいので、金額語との共起を必須にする。
# （「ほしい」「かった」等の部分一致で「パンほしい」「たのしかった」を誤って拾うのを防ぐ）
_COACHING_ACTION_WORDS = (
    "買っ", "かいもの", "つかった", "使っ", "もらった", "ためた", "貯め", "ためる",
    "目標", "もくひょう", "ほしい", "欲しい",
)
# 単なる残高・履歴の照会。これだけの発話ではコーチングを出さない（お金の話でも問いかけは邪魔）。
_COACHING_SUPPRESS_ONLY = ("残高", "ざんだか", "いくらある", "履歴", "りれき", "台帳")

# 直近にその子へ注入したコーチング行動（プロセス内メモ）。同じ行動を連続で促すくどさを防ぐ。
_last_coached_action: dict[str, str] = {}


def _should_coach(input_text: str) -> bool:
    """この発話でコーチングを付けるべきか判定する。お金・貯金の話題のときだけ True。

    誤爆を避けるため、(1)お金語（円・お金・貯金等）を含むか、(2)金額の数字＋購買/貯金動詞が
    共起するとき、をお金の話題とみなす。「ほしい」「かった」単独では発火させない（純雑談を拾わない）。
    単なる残高・履歴照会だけのときも出さない（監視的にしない）。
    """
    text = input_text or ""
    has_money = any(w in text for w in _COACHING_MONEY_WORDS)
    # 数字（半角/全角）と購買・貯金動詞が共起していれば、お金語が無くてもお金の話題とみなす
    import re as _re
    has_amount_digit = bool(_re.search(r"[0-9０-９]", text))
    has_action = any(w in text for w in _COACHING_ACTION_WORDS)
    is_money_topic = has_money or (has_amount_digit and has_action)
    if not is_money_topic:
        return False
    # 残高・履歴の照会「だけ」ならコーチングを出さない
    stripped = text
    for w in _COACHING_SUPPRESS_ONLY:
        stripped = stripped.replace(w, "")
    # 照会語を除いた残りに、まだお金の話題性が残っているか
    still_money = any(w in stripped for w in _COACHING_MONEY_WORDS) or (
        bool(_re.search(r"[0-9０-９]", stripped)) and any(w in stripped for w in _COACHING_ACTION_WORDS)
    )
    return still_money


async def _build_coaching_block_async(user_conf: dict, input_text: str) -> str:
    """お金・学習の話題のターンだけ、その子の learning_insights をコーチング指示へ整形する。

    出し分け（_should_coach）と反復抑制（直近同一 child_action は出さない）を行い、build_learning_insights
    の同期 I/O は asyncio.to_thread でオフロードしてイベントループをブロックしない。子どもには分析や親メモを
    そのまま見せず、child_action を1つさりげなく促す。叱責しない。取得失敗時は空文字。

    Args:
        user_conf: 対象児童の設定 dict。
        input_text: 今回の子どもの発話（出し分け判定に使う）。

    Returns:
        str: system prompt へ足すコーチング指示。出さない場合は空文字。
    """
    # 出し分け: お金・学習の話題でなければコーチングなし
    if not _should_coach(input_text):
        return ""
    user_name = str(user_conf.get("name", ""))
    try:
        # 同期 I/O（90日ログ読取）をスレッドへ逃がしイベントループを止めない
        insights = await asyncio.to_thread(deps.learning_insights, user_conf)
    except Exception:
        # インサイト計算の失敗で会話を止めない。コーチングなしで通常会話にフォールバック
        return ""
    challenge = insights.get("child_challenge") or {}
    challenge_action = str(challenge.get("action") or "").strip()
    cards = insights.get("insight_cards") or []
    top = cards[0] if cards else {}
    skill = str(top.get("skill") or "").strip()
    child_action = str(top.get("child_action") or challenge_action).strip()
    if not child_action:
        return ""
    # 反復抑制: 直近このターンと同じ child_action を促していたら、今回は促さない（くどさ・監視感を避ける）
    if _last_coached_action.get(user_name) == child_action:
        return ""
    _last_coached_action[user_name] = child_action
    # learning_support_state へ書き戻し、再起動を跨いだ反復抑制・能動伴走(challenge_stale)・3日dedup を
    # 会話経路にも効かせる（best-effort、失敗は握る）。card_type は insight_card の type
    card_type = str(top.get("type") or "").strip()
    try:
        deps.save_coaching_nudge(user_conf, card_type, child_action)
    except Exception:
        pass
    return (
        "\n【学習支援コーチング（この発話はお金・学習の話。意識する）】\n"
        f"- この子に今そっと促したい小さな行動は「{child_action}」"
        + (f"（伸ばしたい力: {skill}）" if skill else "")
        + "。\n"
        "- 「がんばろうね」で終わらせず、①今どうなっているか一言そえて、②5分でできる小さな行動を1つだけ、"
        "押しつけずに提案すること。\n"
        "- 上の分析や親向けメモはそのまま読み上げない。子どもが自分で選べる形（今買う/待つ/別にする 等）にする。\n"
        "- 叱ったり、できていない点を並べたりしない。一度に出す提案は1つだけにする。"
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
        tuple[bool, str, str | None, bool]: (成功, 応答文, session_id, タイムアウトしたか)。
            session_id は失敗時でも取れたものを返す（呼び出し側が捨てず継続に使える）。
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
    # 正常な JSON 応答(is_error 偽・result 有)なら returncode に関わらず成功として扱う。
    # wallet tool 実行時は MCP 子プロセスの teardown 等で returncode≠0 になりやすく、returncode で
    # 弾くと「tool が残高を動かした後なのに失敗応答を返す」不一致になる（残高は動いたのに黙殺）。
    if ok:
        if returncode != 0:
            _diag("ai_conversation_nonzero_exit_ok", {"child": child_name, "returncode": returncode})
        return True, result, new_sid, timed_out

    # 再試行は「resume 対象が見つからない」失敗に限る。タイムアウト・その他失敗では絶対に再試行しない。
    # このシグナルは会話開始前に出るため、tool は1つも実行されておらず二重課金にならない
    resume_dead = bool(session_id) and (_RESUME_FAILED_SIGNAL in stderr)
    if resume_dead and not timed_out:
        _diag("ai_conversation_resume_failed", {"child": child_name, "dead_session": session_id})
        try:
            returncode2, stdout2, stderr2 = await _spawn_claude(prompt, None, system_prompt, child_name)
        except Exception as e:
            _diag("ai_conversation_spawn_error", {"child": child_name, "error": f"{type(e).__name__}: {e}", "retry": True})
            return False, "", None, False
        ok2, result2, new_sid2 = _parse_output(stdout2)
        # 再試行も returncode でなく ok（正常な JSON 応答）で判定する
        if ok2:
            if returncode2 != 0:
                _diag("ai_conversation_nonzero_exit_ok", {"child": child_name, "returncode": returncode2, "retry": True})
            return True, result2, new_sid2, False
        # 再試行も失敗。取れた session_id は返す（呼び出し側が継続に使える）
        return False, "", new_sid2, False

    # 再試行しない失敗。stderr を診断へ残して原因を追えるようにする
    if not ok:
        _diag("ai_conversation_failed", {
            "child": child_name, "returncode": returncode,
            "timed_out": timed_out, "stderr": stderr[:500],
        })
    # 失敗でも取れた session_id は返す。tool が動いたターンの文脈を次へ繋ぐため
    return False, "", new_sid, timed_out


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
    user_name = str(user_conf.get("name", ""))
    # 同じ子のターンを直列化する。連投による session 破損・tool 二重実行を防ぐ。子が違えば並行
    async with _lock_for(user_name):
        return await _handle_conversation_locked(channel, user_conf, input_text, user_name)


async def _handle_conversation_locked(channel, user_conf: dict, input_text: str, user_name: str) -> str:
    """handle_conversation の本体。子どもごとのロック配下で1ターンを処理する。"""
    from app.conv import reply  # 遅延 import で循環を避ける

    store = deps.session_store()

    # 1. 継続する session_id を引く。会話セッションの data に保持する。
    #    セッション I/O の失敗で応答経路を止めない（新規扱いにして会話は必ず続ける）
    session_id = None
    try:
        session = await store.get_session(user_name)
        if isinstance(session, dict) and session.get("kind") == SESSION_KIND:
            data = session.get("data") or {}
            session_id = data.get("claude_session_id")
    except Exception as e:
        _diag("ai_conversation_session_read_error", {"child": user_name, "error": f"{type(e).__name__}: {e}"})

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
    # 基本プロンプトに、お金・学習の話題のターンだけコーチング指示を足す（出し分け・反復抑制・to_thread は
    # _build_coaching_block_async 内で行い、イベントループをブロックしない）。
    system_prompt = _build_system_prompt(user_conf)
    system_prompt += await _build_coaching_block_async(user_conf, input_text)
    try:
        ok, result, new_session_id, timed_out = await _run_claude(input_text, session_id, system_prompt, user_name)
    finally:
        # 応答が返ったら考え中タスクを止め、決着を待つ（考え中送信が本応答より後に来る競合を防ぐ）
        thinking_task.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await thinking_task

    # 5. session_id の保存: 成功・失敗を問わず、取れた新しい id があれば保存する。
    #    tool が残高を動かしたターンでも失敗フラグが立つことがあり、その id を捨てると会話文脈が欠落する。
    #    セッション保存の失敗で応答を止めない（保存は best-effort、応答は必ず送る）
    if new_session_id:
        try:
            # session_id ポインタは長寿命 TTL。会話 idle 窓(expiry_minutes)で切らず、7日は継続する
            ttl = SESSION_ID_TTL_MINUTES
            await store.open_session(
                user_name, SESSION_KIND,
                data={"claude_session_id": new_session_id},
                ttl_minutes=ttl,
            )
        except Exception as e:
            _diag("ai_conversation_session_write_error", {"child": user_name, "error": f"{type(e).__name__}: {e}"})

    # 応答が取れなければフォールバック。タイムアウトは tool が既に残高を動かした後かもしれないため、
    # 「何も起きていない」と断定せず、残高を確かめるよう促す文言にする（実残高との不一致を避ける）
    if not ok or not result:
        if timed_out:
            result = "ちょっと時間がかかっちゃった。残高が変わってないか、あとで「ざんだか」って聞いて確かめてね。"
        else:
            result = "ごめんね、いまうまくお返事できなかったよ。もう一度言ってくれる？"

    # 6. 唯一の出口から送信する（会話ログ記録＋分割を集約）
    return await reply.send_reply(channel, result, user_name=user_name, kind=SESSION_KIND)
