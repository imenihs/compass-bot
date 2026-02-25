import os
import json
import re

import discord
from datetime import datetime

from app.config import (
    find_user_by_discord_id,
    find_user_by_name,
    get_allow_channel_ids,
    get_assess_keyword,
    get_allowance_reminder_setting,
    get_chat_setting,
    get_force_assess_test_keyword,
    get_gemini_model,
    get_log_dir,
    get_parent_ids,
    get_wallet_audit_setting,
    load_all_users,
    load_system,
)
from app.gemini_service import GeminiService, count_recent_allowance_requests
from app.message_parser import (
    extract_input_from_mention,
    parse_balance_report,
    parse_usage_report,
    parse_usage_report_flexible,
    parse_proxy_request,
)
from app.prompts import build_prompt
from app.reminder_service import ReminderService
from app.storage import append_jsonl, now_jst_iso, JST
from app.wallet_service import WalletService

DISCORD_BOT_TOKEN = os.environ.get("DISCORD_BOT_TOKEN")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")

PARENT_IDS = get_parent_ids()
ALLOW_CHANNEL_IDS = get_allow_channel_ids()
ALLOWANCE_REMINDER = get_allowance_reminder_setting()
WALLET_AUDIT = get_wallet_audit_setting()
CHAT_SETTING = get_chat_setting()
ASSESS_KEYWORD = get_assess_keyword()
FORCE_ASSESS_TEST_KEYWORD = get_force_assess_test_keyword()

gemini_service = GeminiService(
    api_key=GEMINI_API_KEY,
    model_name=get_gemini_model(),
    assess_keyword=ASSESS_KEYWORD,
)
wallet_service = WalletService()

intents = discord.Intents.default()
intents.message_content = True
client = discord.Client(intents=intents)
reminder_service = ReminderService(
    client=client,
    allowance_reminder_conf=ALLOWANCE_REMINDER,
    wallet_audit_conf=WALLET_AUDIT,
    load_all_users_fn=load_all_users,
    wallet_service=wallet_service,
)


def is_parent(user_id: int) -> bool:
    return user_id in PARENT_IDS

def _usage_guide_text() -> str:
    return (
        "【Compass つかいかた】\n"
        "・お小遣い金額相談: `@compass-bot 相談内容`\n"
        "・支出の記録: `支出記録` と送ると入力案内が出る\n"
        "・現在のお小遣いの金額確認: `残高確認` で確認できる\n\n"
        "【初期設定】\n"
        "1. `初期設定` と送る\n"
        "2. 続けて現在の所持金を `1234円` の形で送る\n"
        "3. ウォレットに現在所持金を反映する"
    )

def _rough_word_count(text: str) -> int:
    tokens = [t for t in re.split(r"[\s、。,.!?！？]+", (text or "").strip()) if t]
    return len(tokens)

def _load_jsonl(path) -> list[dict]:
    if not path.exists():
        return []
    rows: list[dict] = []
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
                if isinstance(obj, dict):
                    rows.append(obj)
            except json.JSONDecodeError:
                continue
    return rows

def _self_compare_message(log_dir, user_name: str, current_score: int) -> str:
    path = log_dir / f"{user_name}_pocket_journal.jsonl"
    rows = _load_jsonl(path)
    if len(rows) <= 1:
        return "初回記録だよ。これから過去の自分との比較を作っていこう。"
    prev = [int(r.get("satisfaction", 0)) for r in rows[:-1] if isinstance(r.get("satisfaction"), int)]
    if not prev:
        return "比較データがまだ少ないので、記録を続けてみよう。"
    avg = sum(prev) / len(prev)
    diff = current_score - avg
    sign = "+" if diff >= 0 else ""
    return f"過去の自分平均は {avg:.1f}/10、今回は {current_score}/10（差分 {sign}{diff:.1f}）。"

def _recent_conversation_history(log_dir, user_name: str, limit: int = 6) -> list[dict]:
    path = log_dir / f"{user_name}_events.jsonl"
    rows = _load_jsonl(path)
    picked: list[dict] = []
    for r in rows[-limit:]:
        picked.append(
            {
                "ts": r.get("ts"),
                "user": r.get("input"),
                "assistant": r.get("reply"),
            }
        )
    return picked

def _monthly_increase_stats(log_dir, user_name: str, base_dt: datetime) -> dict:
    path = log_dir / f"{user_name}_allowance_amounts.jsonl"
    rows = _load_jsonl(path)
    monthly_rows: list[dict] = []
    all_rows: list[dict] = []
    last_total: int | None = None
    last_fixed: int | None = None

    for r in rows:
        ts = r.get("ts")
        try:
            dt = datetime.fromisoformat(str(ts))
        except Exception:
            continue
        total = r.get("total")
        fixed = r.get("fixed")
        if not isinstance(total, int) or not isinstance(fixed, int):
            continue
        all_rows.append({"ts": dt, "total": total, "fixed": fixed})
        if dt.year == base_dt.year and dt.month == base_dt.month:
            monthly_rows.append({"ts": dt, "total": total, "fixed": fixed})
        last_total = total
        last_fixed = fixed

    monthly_rows.sort(key=lambda x: x["ts"])
    total_increase_count = 0
    fixed_increase_count = 0
    prev_total: int | None = None
    prev_fixed: int | None = None
    for r in monthly_rows:
        if prev_total is not None and r["total"] > prev_total:
            total_increase_count += 1
        if prev_fixed is not None and r["fixed"] > prev_fixed:
            fixed_increase_count += 1
        prev_total = r["total"]
        prev_fixed = r["fixed"]

    all_rows.sort(key=lambda x: x["ts"])
    fixed_increase_count_this_year = 0
    last_fixed_increase_ts: datetime | None = None
    prev_fixed_all: int | None = None
    for r in all_rows:
        if prev_fixed_all is not None and r["fixed"] > prev_fixed_all:
            if r["ts"].year == base_dt.year:
                fixed_increase_count_this_year += 1
            last_fixed_increase_ts = r["ts"]
        prev_fixed_all = r["fixed"]

    months_since_last_fixed_increase: int | None = None
    if last_fixed_increase_ts is not None:
        months_since_last_fixed_increase = (
            (base_dt.year - last_fixed_increase_ts.year) * 12
            + (base_dt.month - last_fixed_increase_ts.month)
        )

    return {
        "total_increase_count": total_increase_count,
        "last_total": last_total,
        "last_fixed": last_fixed,
        "fixed_increase_count_month": fixed_increase_count,
        "fixed_increase_count_this_year": fixed_increase_count_this_year,
        "months_since_last_fixed_increase": months_since_last_fixed_increase,
    }

def _latest_assessed_amount(log_dir, user_name: str) -> dict:
    path = log_dir / f"{user_name}_allowance_amounts.jsonl"
    rows = _load_jsonl(path)
    for r in reversed(rows):
        fixed = r.get("fixed")
        temporary = r.get("temporary")
        total = r.get("total")
        if isinstance(fixed, int) and isinstance(temporary, int) and isinstance(total, int):
            return {"fixed": fixed, "temporary": temporary, "total": total}
    return {}

def _normalize_assessed_amounts(user_conf: dict, assessed: dict | None, previous_assessed: dict | None) -> dict | None:
    if assessed is None:
        return None

    prev = previous_assessed or {}
    fixed = assessed.get("fixed")
    temporary = assessed.get("temporary")
    total = assessed.get("total")

    if not isinstance(fixed, int):
        prev_fixed = prev.get("fixed")
        if isinstance(prev_fixed, int):
            fixed = prev_fixed
        else:
            fixed = int(user_conf.get("fixed_allowance", 0))

    if not isinstance(temporary, int):
        prev_tmp = prev.get("temporary")
        if isinstance(prev_tmp, int):
            temporary = prev_tmp
        else:
            temporary = 0

    if not isinstance(total, int):
        total = int(fixed) + int(temporary)

    return {"fixed": int(fixed), "temporary": int(temporary), "total": int(total)}

def _extract_keyword_hits(user_conf: dict, input_text: str) -> dict:
    text = (input_text or "").lower()
    kw = user_conf.get("keywords", {}) if isinstance(user_conf, dict) else {}
    if not isinstance(kw, dict):
        kw = {}

    def _hits(bucket: str) -> list[str]:
        items = kw.get(bucket, [])
        if not isinstance(items, list):
            return []
        matched: list[str] = []
        for x in items:
            token = str(x).strip()
            if token and token.lower() in text:
                matched.append(token)
        return matched

    return {
        "investment": _hits("investment"),
        "fun": _hits("fun"),
        "danger": _hits("danger"),
    }

def _normalize_keyword(text: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (text or "").lower())

def _contains_force_assess_keyword(input_text: str, keyword: str) -> bool:
    if not keyword:
        return False
    return _normalize_keyword(keyword) in _normalize_keyword(input_text)

def _parse_fixed_delta_request(input_text: str) -> int | None:
    text = (input_text or "").strip()
    m = re.search(r"固定(?:を|は)?\s*([+-]\d+)\s*円", text)
    if m:
        return int(m.group(1))
    m2 = re.search(r"([+-]\d+)\s*円", text)
    if m2:
        return int(m2.group(1))
    return None

def _contains_any_keyword(text: str, keywords: list[str]) -> bool:
    body = (text or "").strip().lower()
    return any(k.lower() in body for k in keywords if k)

def _normalize_japanese_command(text: str) -> str:
    body = (text or "").strip().lower()
    body = re.sub(r"\s+", "", body)
    body = re.sub(r"[、。,.!！?？:：`'\"「」『』\(\)\[\]【】]", "", body)
    return body

def _parse_yen_amount(text: str) -> int | None:
    body = (text or "").strip()
    m = re.search(r"(\d[\d,]*)\s*円?", body)
    if not m:
        return None
    try:
        return int(m.group(1).replace(",", ""))
    except ValueError:
        return None

def _is_initial_setup_pending(user_name: str) -> bool:
    state = wallet_service.load_audit_state()
    pending = state.get("initial_setup_pending_by_user", {})
    return bool(isinstance(pending, dict) and user_name in pending)

def _set_initial_setup_pending(user_name: str, enabled: bool) -> None:
    state = wallet_service.load_audit_state()
    pending = state.get("initial_setup_pending_by_user")
    if not isinstance(pending, dict):
        pending = {}
    if enabled:
        pending[user_name] = now_jst_iso()
    else:
        pending.pop(user_name, None)
    state["initial_setup_pending_by_user"] = pending
    wallet_service.save_audit_state(state)

async def maybe_handle_help_and_initial_setup(
    message: discord.Message,
    user_conf: dict,
    system_conf: dict,
    input_block: str,
) -> bool:
    user_name = str(user_conf.get("name", ""))
    if not user_name:
        return False

    setup_keywords = ["初期設定", "しょきせってい"]
    usage_keywords = ["使い方", "つかいかた"]
    balance_keywords = ["現在のお小遣い", "おこづかいいくら", "残高確認", "いまの残高", "今の残高"]
    spending_keywords = ["支出記録", "ししゅつきろく", "支出の記録", "ししゅつのきろく","使った記録","つかったきろく"]

    is_setup = _contains_any_keyword(input_block, setup_keywords)
    is_usage = _contains_any_keyword(input_block, usage_keywords)
    is_balance_check = _contains_any_keyword(input_block, balance_keywords)
    is_spending = _contains_any_keyword(input_block, spending_keywords)
    is_pending = _is_initial_setup_pending(user_name)

    if is_setup or is_pending:
        amount = _parse_yen_amount(input_block)
        if amount is None:
            _set_initial_setup_pending(user_name, True)
            await message.channel.send(
                "初期設定をはじめるよ。"
                "\nいまの所持金を `1234円` の形で送ってね。"
            )
            return True

        before = wallet_service.get_balance(user_name)
        delta = int(amount) - int(before)
        wallet_service.update_balance(
            user_conf=user_conf,
            system_conf=system_conf,
            delta=delta,
            action="initial_setup",
            note="set_current_wallet_balance",
            extra={"discord_user_id": int(message.author.id)},
        )
        _set_initial_setup_pending(user_name, False)
        await message.channel.send(
            "初期設定を反映したよ。"
            f"\n対象: {user_name}"
            f"\n所持金: {before}円 → {amount}円"
        )
        return True

    if is_usage:
        await message.channel.send(_usage_guide_text())
        return True

    if is_balance_check:
        now_balance = wallet_service.get_balance(user_name)
        await message.channel.send(f"{user_name}さんの現在の所持金は {now_balance}円 だよ。")
        return True

    if is_spending:
        state = wallet_service.load_audit_state()
        pending = state.get("spending_record_pending_by_user", {})
        if not isinstance(pending, dict):
            pending = {}
        pending[user_name] = now_jst_iso()
        state["spending_record_pending_by_user"] = pending
        wallet_service.save_audit_state(state)
        await message.channel.send(
            "支出の記録をするよ。次のどちらかで送ってね。"
            "\n1. `使った物: ノート` `理由: テスト勉強のため` `満足度: 8`"
            "\n2. `ノート / テスト勉強のため / 8`"
        )
        return True

    return False

async def maybe_handle_parent_broadcast_guide(message: discord.Message, content: str) -> bool:
    if not is_parent(message.author.id):
        return False

    mention_body = extract_input_from_mention((content or "").strip(), client.user)
    body = mention_body if mention_body is not None else (content or "")
    normalized = _normalize_japanese_command(body)
    is_cmd = (
        "使い方の説明と初期設定" in normalized
        or "つかいかたのせつめいとしょきせってい" in normalized
    )
    if not is_cmd:
        return False

    channel_ids = get_allow_channel_ids()
    if not channel_ids:
        await message.channel.send(
            "`settings/setting.json` の `allow_channel_ids` が未設定なので一斉通知できないよ。"
        )
        return True

    sent = 0
    failed: list[str] = []
    text = _usage_guide_text()
    for cid in sorted(channel_ids):
        try:
            channel = client.get_channel(int(cid))
            if channel is None:
                channel = await client.fetch_channel(int(cid))
            await channel.send(text)
            sent += 1
        except Exception as e:
            failed.append(f"{cid}({type(e).__name__})")

    msg = f"使い方と初期設定のアナウンスを {sent}/{len(channel_ids)} チャネルに送信したよ。"
    if failed:
        msg += f"\n送信失敗: {', '.join(str(x) for x in failed)}"
    await message.channel.send(msg)
    return True

async def maybe_handle_spending_record_flow(
    message: discord.Message,
    user_conf: dict,
    system_conf: dict,
    input_block: str,
) -> bool:
    user_name = str(user_conf.get("name", ""))
    if not user_name:
        return False

    state = wallet_service.load_audit_state()
    pending = state.get("spending_record_pending_by_user", {})
    if not isinstance(pending, dict):
        pending = {}

    is_pending = user_name in pending
    parsed = parse_usage_report(input_block) or parse_usage_report_flexible(input_block)

    if parsed is None and not is_pending:
        return False

    if parsed is None and is_pending:
        await message.channel.send(
            "支出の記録を続けるよ。"
            "\n`使った物 / 理由 / 満足度(0-10)` の形で送ってね。"
        )
        return True

    reason_words = _rough_word_count(parsed["reason"])
    if reason_words < 3:
        await message.channel.send(
            "理由は3語以上で書いてね。例: `理由: 英語 の テスト 対策`"
        )
        return True

    log_dir = get_log_dir(system_conf)
    journal_path = log_dir / f"{user_conf['name']}_pocket_journal.jsonl"
    record = {
        "ts": now_jst_iso(),
        "discord_user_id": message.author.id,
        "name": user_conf.get("name"),
        "item": parsed["item"],
        "reason": parsed["reason"],
        "reason_word_count": reason_words,
        "satisfaction": parsed["satisfaction"],
    }
    append_jsonl(journal_path, record)
    compare_msg = _self_compare_message(log_dir, str(user_conf.get("name", "")), int(parsed["satisfaction"]))
    pending.pop(user_name, None)
    state["spending_record_pending_by_user"] = pending
    wallet_service.save_audit_state(state)
    await message.channel.send(
        "システムにお小遣い帳を記録したよ。"
        f"\n- 使った物: {parsed['item']}"
        f"\n- 理由: {parsed['reason']}"
        f"\n- 満足度: {parsed['satisfaction']}/10"
        f"\n- 比較: {compare_msg}"
    )
    return True

async def maybe_handle_reminder_test(message: discord.Message, content: str) -> bool:
    mention_body = extract_input_from_mention((content or "").strip(), client.user)
    is_test_cmd = bool(
        mention_body
        and mention_body.strip().lower()
        in {"reminder test", "reminder-test", "リマインダーテスト", "リマインダー テスト"}
    )
    if not is_test_cmd:
        return False

    if not is_parent(message.author.id):
        await message.channel.send("リマインダーテストは親のみ実行できるよ。")
        return True

    channel_id = ALLOWANCE_REMINDER.get("channel_id")
    if not channel_id:
        await message.channel.send("`settings/setting.json` の `allowance_reminder.channel_id` を設定してね。")
        return True

    payday = reminder_service.next_payday(today=datetime.now(JST).date(), payday_day=int(ALLOWANCE_REMINDER["payday_day"]))
    try:
        await reminder_service.send_allowance_reminder(payday=payday, channel_id=int(channel_id), is_test=True)
        await message.channel.send("リマインダーをテスト送信したよ。")
    except Exception as e:
        await message.channel.send(f"テスト送信に失敗したよ。原因: {type(e).__name__}: {e}")
    return True

async def send_assessment_change_notice(
    source_message: discord.Message,
    user_conf: dict,
    input_text: str,
    assessed: dict,
    previous_assessed: dict | None = None,
) -> tuple[bool, str]:
    """
    査定金額の変更があったとき、allowance_reminder.channel_id へ通知する。
    """
    channel_id = ALLOWANCE_REMINDER.get("channel_id")
    if not channel_id:
        return False, "allowance_reminder.channel_id が未設定"

    fixed_now = assessed.get("fixed")
    temporary_now = assessed.get("temporary")
    if not isinstance(fixed_now, int) or not isinstance(temporary_now, int):
        return False, "査定の固定/臨時が取得できない"

    prev = previous_assessed or {}
    fixed_prev = prev.get("fixed")
    temporary_prev = prev.get("temporary")
    if not isinstance(fixed_prev, int):
        fixed_prev = int(user_conf.get("fixed_allowance", 0))
    if not isinstance(temporary_prev, int):
        temporary_prev = 0

    lines = ["【お小遣い査定変更通知】", f"対象: {user_conf.get('name')}さん"]
    changed = False
    if temporary_now != temporary_prev:
        lines.append(f"臨時: {user_conf.get('name')}さん +{temporary_now}円（前回 +{temporary_prev}円）")
        changed = True
    if fixed_now != fixed_prev:
        lines.append(f"固定: {user_conf.get('name')}さん {fixed_prev}円 → {fixed_now}円")
        changed = True
    if not changed:
        return False, "変更なし"

    channel = client.get_channel(int(channel_id))
    if channel is None:
        channel = await client.fetch_channel(int(channel_id))

    lines.extend(
        [
            f"依頼者ID: {source_message.author.id}",
            f"入力内容: {input_text}",
        ]
    )
    await channel.send("\n".join(lines))
    return True, ""

@client.event
async def on_ready():
    print(f"Compass logged in as {client.user}")
    reminder_service.start_loop_if_needed()
    print("Allowance reminder loop started")


@client.event
async def on_message(message: discord.Message):
    if message.author.bot:
        return

    content = (message.content or "").strip()
    if await maybe_handle_parent_broadcast_guide(message, content):
        return

    if ALLOW_CHANNEL_IDS is not None and message.channel.id not in ALLOW_CHANNEL_IDS:
        return

    if content.startswith("[#SH-"):
        await message.channel.send("`[#SH-xxx]`形式は非対応です。`@compass-bot 内容` で送ってね。")
        return

    if await maybe_handle_reminder_test(message, content):
        return

    mention_input = extract_input_from_mention(content, client.user)
    if mention_input is None:
        if CHAT_SETTING.get("natural_chat_enabled") and not CHAT_SETTING.get("require_mention"):
            mention_input = content
        else:
            return

    system_conf = load_system()
    user_conf = find_user_by_discord_id(message.author.id)
    proxy_name, input_block = parse_proxy_request(mention_input)

    if not input_block:
        await message.channel.send("相談内容を本文に書いて送ってね。")
        return

    if proxy_name:
        if not is_parent(message.author.id):
            await message.channel.send("`代理登録` は親のみ使用できるよ。")
            return
        user_conf = find_user_by_name(proxy_name)
        if user_conf is None:
            await message.channel.send(
                f"`{proxy_name}` はユーザー設定に見つからなかったよ。`settings/users/*.json` の `name` を確認してね。"
            )
            return

    if user_conf is None:
        await message.channel.send("設定にあなたのDiscord IDが登録されてないみたい。親に `settings/users/*.json` を追加してもらってね。")
        return

    if await maybe_handle_help_and_initial_setup(
        message=message,
        user_conf=user_conf,
        system_conf=system_conf,
        input_block=input_block,
    ):
        return

    if await maybe_handle_spending_record_flow(
        message=message,
        user_conf=user_conf,
        system_conf=system_conf,
        input_block=input_block,
    ):
        return

    reported_balance = parse_balance_report(input_block)
    if reported_balance is not None:
        expected = wallet_service.get_balance(str(user_conf.get("name", "")))
        diff = int(reported_balance) - int(expected)
        state = wallet_service.load_audit_state()
        state["pending_by_user"].pop(str(user_conf.get("name", "")), None)
        wallet_service.save_audit_state(state)

        if diff == 0:
            await message.channel.send(
                f"{user_conf.get('name')}の残高報告を記録したよ。"
                f"\n報告残高: {reported_balance}円 / 帳簿残高: {expected}円（差分0円）"
            )
            return

        penalty = wallet_service.apply_penalty(
            user_conf=user_conf,
            system_conf=system_conf,
            diff=diff,
            wallet_audit_conf=WALLET_AUDIT,
        )
        new_balance = wallet_service.get_balance(str(user_conf.get("name", "")))
        await message.channel.send(
            f"{user_conf.get('name')}の残高差分を検知したよ。"
            f"\n報告残高: {reported_balance}円 / 帳簿残高: {expected}円 / 差分: {diff}円"
            f"\nペナルティ減額: {penalty}円"
            f"\n減額後の帳簿残高: {new_balance}円"
        )
        return

    force_assess_mode = _contains_force_assess_keyword(input_block, FORCE_ASSESS_TEST_KEYWORD)
    await message.channel.send("考え中だよ...")

    log_dir = get_log_dir(system_conf)
    recent_count = count_recent_allowance_requests(
        log_dir=log_dir,
        user_name=str(user_conf.get("name", "")),
        days=30,
    )
    increase_stats = _monthly_increase_stats(
        log_dir=log_dir,
        user_name=str(user_conf.get("name", "")),
        base_dt=datetime.now(JST),
    )
    now_dt = datetime.now(JST)
    current_month_text = f"{now_dt.month}月"
    next_month_num = 1 if now_dt.month == 12 else now_dt.month + 1
    next_month_text = f"{next_month_num}月"
    prompt = build_prompt(
        user_conf=user_conf,
        system_conf=system_conf,
        input_text=input_block,
        recent_request_count=recent_count,
        recent_window_days=30,
        assess_keyword=ASSESS_KEYWORD,
        conversation_history=_recent_conversation_history(log_dir, str(user_conf.get("name", "")), limit=6),
        monthly_total_increase_count=increase_stats["total_increase_count"],
        monthly_total_increase_limit=2,
        last_total=increase_stats["last_total"],
        last_fixed=increase_stats["last_fixed"],
        keyword_hits=_extract_keyword_hits(user_conf, input_block),
        force_assess_test_keyword=FORCE_ASSESS_TEST_KEYWORD,
        is_force_assess_test=force_assess_mode,
        force_requested_fixed_delta=_parse_fixed_delta_request(input_block),
        runtime_now_text=now_dt.strftime("%Y-%m-%d %H:%M:%S %Z"),
        runtime_current_month_text=current_month_text,
        runtime_next_month_text=next_month_text,
        fixed_increase_cap=int(user_conf.get("fixed_increase_cap", 100)),
        months_since_last_fixed_increase=increase_stats["months_since_last_fixed_increase"],
        fixed_increase_count_this_year=increase_stats["fixed_increase_count_this_year"],
    )
    try:
        reply = await gemini_service.call_with_progress(message.channel, prompt)
    except Exception as e:
        print("Gemini error:", e)
        await message.channel.send(f"ごめん、査定でエラーが出たよ。原因: {type(e).__name__}: {e}")
        return

    if force_assess_mode and ASSESS_KEYWORD not in (reply or ""):
        reply = f"{ASSESS_KEYWORD}\n{reply}"
    assessed = gemini_service.extract_assessed_amounts(reply)
    if force_assess_mode and assessed is None:
        force_delta = _parse_fixed_delta_request(input_block)
        delta_rule = ""
        if force_delta is not None:
            delta_rule = f"`固定差分` は必ず {force_delta:+d}円 にすること。"
        repair_prompt = (
            "以下の回答を、査定フォーマットに厳密変換してください。"
            f"先頭行は必ず `{ASSESS_KEYWORD}`。"
            "次に【査定結果】で `固定：N円（翌月 N円 / 今月 N円 / 差分）` と `臨時：+N円（理由）` を整数で出すこと。"
            "`合計`は出力しない。"
            f"{delta_rule}"
            "説明文は短くてよい。\n\n"
            f"元の回答:\n{reply}"
        )
        try:
            repaired = await gemini_service.call_with_progress(message.channel, repair_prompt)
            if ASSESS_KEYWORD not in (repaired or ""):
                repaired = f"{ASSESS_KEYWORD}\n{repaired}"
            repaired_assessed = gemini_service.extract_assessed_amounts(repaired)
            if repaired_assessed is not None:
                reply = repaired
                assessed = repaired_assessed
        except Exception:
            pass

    previous_assessed = _latest_assessed_amount(log_dir, str(user_conf.get("name", "")))
    assessed = _normalize_assessed_amounts(user_conf=user_conf, assessed=assessed, previous_assessed=previous_assessed)

    log_path = log_dir / f"{user_conf['name']}_events.jsonl"
    append_jsonl(
        log_path,
        {
            "ts": now_jst_iso(),
            "discord_user_id": message.author.id,
            "name": user_conf.get("name"),
            "age": user_conf.get("age"),
            "gender": user_conf.get("gender"),
            "input": input_block,
            "reply": reply,
            "assessed": assessed,
        },
    )

    if assessed is not None:
        amount_log_path = log_dir / f"{user_conf['name']}_allowance_amounts.jsonl"
        append_jsonl(
            amount_log_path,
            {
                "ts": now_jst_iso(),
                "discord_user_id": message.author.id,
                "name": user_conf.get("name"),
                "input": input_block,
                "fixed": assessed.get("fixed"),
                "temporary": assessed.get("temporary"),
                "total": assessed.get("total"),
            },
        )
        try:
            sent, reason = await send_assessment_change_notice(
                source_message=message,
                user_conf=user_conf,
                input_text=input_block,
                assessed=assessed,
                previous_assessed=previous_assessed,
            )
            if sent:
                await message.channel.send("査定変更を指定チャンネルに通知したよ。")
            elif reason not in {"変更なし"}:
                await message.channel.send(f"査定変更通知をスキップしたよ（{reason}）。")
        except Exception as e:
            print("assessment change notify error:", e)
            await message.channel.send(f"査定変更通知の送信に失敗したよ。原因: {type(e).__name__}: {e}")
        if assessed.get("total") is not None:
            wallet_service.update_balance(
                user_conf=user_conf,
                system_conf=system_conf,
                delta=int(assessed["total"]),
                action="allowance_grant",
                note="gemini_assessed_total",
                extra={"discord_user_id": int(message.author.id)},
            )

    if len(reply) > 1900:
        for i in range(0, len(reply), 1900):
            await message.channel.send(reply[i : i + 1900])
    else:
        await message.channel.send(reply)


if __name__ == "__main__":
    if not DISCORD_BOT_TOKEN or not GEMINI_API_KEY:
        raise RuntimeError("DISCORD_BOT_TOKEN / GEMINI_API_KEY が未設定")
    client.run(DISCORD_BOT_TOKEN)
