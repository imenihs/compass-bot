import re
import discord


_DISCORD_MENTION_RE = re.compile(r"<@!?\d+>|<@&\d+>|@(?:everyone|here)\b", re.IGNORECASE)
_PLAIN_AT_MENTION_RE = re.compile(r"(^|[\s　])@[^\s　@]+")


def parse_proxy_request(text: str) -> tuple[str | None, str]:
    """
    `nameの代理 送信内容` を解析して (name, 本文) を返す。
    """
    body = (text or "").strip()
    if not body:
        return None, ""
    m = re.match(r"^(.+?)の代理[\s:：,，]+(.+)$", body, flags=re.DOTALL)
    if m:
        return m.group(1).strip(), m.group(2).strip()
    return None, body


def extract_input_from_mention(text: str, bot_user: discord.ClientUser | None) -> str | None:
    """
    先頭メンション形式から本文を取り出す。
    対応:
    - <@123...> / <@!123...>
    - @compass-bot#8762
    - @compass-bot
    - @{bot_user.name}
    """
    content = (text or "").strip()
    if not content:
        return None

    prefixes: list[str] = ["@compass-bot#8762", "@compass-bot"]
    if bot_user and bot_user.name:
        prefixes.append(f"@{bot_user.name}")

    if bot_user:
        m = re.match(rf"^<@!?{bot_user.id}>", content)
        if m:
            return content[m.end() :].lstrip(" \t\r\n:：,，")

    for p in prefixes:
        if content.lower().startswith(p.lower()):
            return content[len(p) :].lstrip(" \t\r\n:：,，")

    return None


def contains_any_mention(text: str) -> bool:
    """Discordメンションまたは通常の @名前 表記が含まれるか判定する。"""
    content = (text or "").strip()
    if not content:
        return False
    return bool(_DISCORD_MENTION_RE.search(content) or _PLAIN_AT_MENTION_RE.search(content))


def parse_usage_report(text: str) -> dict | None:
    """
    お小遣い帳の3〜4項目入力を解析する。
    必須:
    - 使った物
    - 理由
    - 満足度(0-10)
    任意:
    - 金額（「金額: 300円」形式）
    """
    body = (text or "").strip()
    if not body:
        return None

    item_m = re.search(r"(?:使った物|使ったもの|項目)\s*[：:]\s*(.+)", body)
    reason_m = re.search(r"理由\s*[：:]\s*(.+)", body)
    sat_m = re.search(r"満足度\s*[：:]\s*(\d{1,2})", body)

    if not (item_m and reason_m and sat_m):
        return None

    satisfaction = int(sat_m.group(1))
    if satisfaction < 0 or satisfaction > 10:
        return None

    item = item_m.group(1).strip()
    reason = reason_m.group(1).strip()
    if not item or not reason:
        return None

    # 金額フィールドの解析（省略可能、後方互換性あり）
    amount_m = re.search(r"金額\s*[：:]\s*(\d[\d,]*)\s*円?", body)
    amount = int(amount_m.group(1).replace(",", "")) if amount_m else None

    return {
        "item": item,
        "reason": reason,
        "satisfaction": satisfaction,
        "amount": amount,
    }


def parse_usage_report_flexible(text: str) -> dict | None:
    """
    ラベルなしの簡易入力も解析する。
    例:
    - ノート, 勉強のために必要だった, 8
    - ノート / テスト対策で使った / 8
    - ノート / テスト対策で使った / 8 / 300円（金額付き）
    """
    body = (text or "").strip()
    if not body:
        return None

    # まず既存の厳密フォーマットを優先
    strict = parse_usage_report(body)
    if strict is not None:
        return strict

    if "\n" in body:
        parts = [p.strip() for p in body.splitlines() if p.strip()]
    else:
        parts = [p.strip() for p in re.split(r"[／/\|,，]", body) if p.strip()]

    if len(parts) < 3:
        return None

    # 4要素以上: item / reason / satisfaction / amount（金額は任意）
    # 3要素: item / reason / satisfaction
    sat_raw = parts[2] if len(parts) >= 4 else parts[-1]
    m = re.search(r"(\d{1,2})", sat_raw)
    if not m:
        return None
    satisfaction = int(m.group(1))
    if satisfaction < 0 or satisfaction > 10:
        return None

    # 4要素目が金額として存在する場合に解析する
    amount = None
    if len(parts) >= 4:
        am = re.search(r"(\d[\d,]*)", parts[3])
        if am:
            amount = int(am.group(1).replace(",", ""))

    item = parts[0]
    reason = parts[1]
    if not item or not reason:
        return None

    return {
        "item": item,
        "reason": reason,
        "satisfaction": satisfaction,
        "amount": amount,
    }


def parse_balance_report(text: str) -> int | None:
    body = (text or "").strip()
    m = re.match(r"^残高報告\s*[：:\s]\s*(\d[\d,]*)\s*万\s*(?:円|えん)\s*$", body)
    multiplier = 10_000
    if not m:
        m = re.match(r"^残高報告\s*[：:\s]\s*(\d[\d,]*)\s*(?:円|えん)\s*$", body)
        multiplier = 1
    if not m:
        return None
    amount = int(m.group(1).replace(",", "")) * multiplier
    if amount > 1_000_000:
        return None
    return amount
