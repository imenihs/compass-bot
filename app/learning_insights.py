"""
learning_insights.py - 支出ログから親子会話カード用の分析結果を作る。

このモジュールは既存ログを読むだけで、状態ファイルや JSONL は変更しない。
"""

from __future__ import annotations

import hashlib
import json
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any


JST = timezone(timedelta(hours=9))
DEFAULT_DAYS = 90
LOW_SATISFACTION_MAX = 4
HIGH_SATISFACTION_MIN = 8
MAX_INSIGHT_CARDS = 2

NON_REASON_WORDS = {
    "",
    "-",
    "ー",
    "なし",
    "無し",
    "なし。",
    "未記入",
    "スキップ",
    "手動支出",
}

SUPPLEMENT_ACTIONS = {
    "expense_supplement",
    "spending_supplement",
    "pocket_journal_supplement",
}

INCOME_ACTIONS = {
    "manual_income",
    "allowance_grant",
    "allowance_manual_grant",
    "allowance_monthly_auto_grant",
}

ONLINE_TERMS = ("課金", "ガチャ", "サブスク", "オンライン", "アプリ", "ゲーム内", "広告", "個人情報")
PLANNING_TERMS = ("予定", "計画", "貯金", "必要", "比較", "比べ", "調べ", "ため", "学び", "参考書", "本")
CATEGORY_TERMS = {
    "おやつ": ("お菓子", "おかし", "ジュース", "アイス", "ガム", "チョコ"),
    "文具": ("ノート", "鉛筆", "えんぴつ", "消しゴム", "文具", "ペン"),
    "本・学び": ("本", "参考書", "漫画", "まんが", "図鑑", "ドリル"),
    "ゲーム・オンライン": ("ゲーム", "課金", "ガチャ", "アプリ", "オンライン", "サブスク"),
    "交通": ("電車", "バス", "交通", "切符"),
    "プレゼント": ("プレゼント", "誕生日", "おみやげ", "お土産"),
}

POLICY_TYPE_BOOSTS = {
    "record_habit": {"record_habit": 40},
    "planning": {
        "low_satisfaction_high_amount": 22,
        "positive_planned_purchase": 20,
        "saving_goal_impact": 16,
    },
    "satisfaction_reflection": {
        "low_satisfaction_high_amount": 36,
        "repeated_small_spending": 30,
        "positive_planned_purchase": 28,
    },
    "saving_goal": {"saving_goal_impact": 42},
    "impulse_spending": {
        "repeated_small_spending": 42,
        "online_risk": 34,
        "low_satisfaction_high_amount": 18,
    },
    "income_balance": {"income_balance": 42},
}


def build_learning_insights(
    user_conf: dict,
    system_conf: dict,
    audit_state: dict | None = None,
    days: int = DEFAULT_DAYS,
) -> dict:
    """学習支援ダッシュボード用の会話カードと短い要点を返す。

    既存の pocket_journal JSONL と wallet_ledger JSONL を読み、補足レコードは
    読み取り時だけ購買単位へ暫定マージする。
    """
    user_name = str(user_conf.get("name", "")).strip() if isinstance(user_conf, dict) else ""
    days_int = _normalize_days(days)
    now = datetime.now(JST)
    cutoff = now - timedelta(days=days_int)

    log_dir = _get_log_dir(system_conf)
    source_notes = ["既存ログは書き換えず、読み取り時だけ購買単位へ整えています。"]

    journal_rows: list[dict] = []
    skipped_journal = 0
    if user_name:
        journal_rows, skipped_journal = _load_jsonl(log_dir / f"{user_name}_pocket_journal.jsonl")
    else:
        source_notes.append("ユーザー名が空のため、支出ログは読み込めませんでした。")
    if skipped_journal:
        source_notes.append(f"読み込めない支出ログ行を{skipped_journal}件スキップしました。")

    window_rows = _rows_in_window(journal_rows, cutoff)
    purchases, merge_info = _normalize_purchase_units(window_rows)
    if merge_info["merged_count"]:
        source_notes.append(
            f"補足レコード{merge_info['merged_count']}件を同日の支出へ読み取り時だけ統合しました。"
        )
    if merge_info["unmerged_supplement_count"]:
        source_notes.append(
            f"元支出を特定できない補足レコード{merge_info['unmerged_supplement_count']}件は単独の記録として扱いました。"
        )
    if merge_info["missing_entry_id_count"]:
        source_notes.append(
            f"entry_id のない既存支出{merge_info['missing_entry_id_count']}件は読み取り時IDで扱いました。"
        )

    ledger_rows: list[dict] = []
    skipped_ledger = 0
    if user_name:
        ledger_rows, skipped_ledger = _load_jsonl(log_dir / f"{user_name}_wallet_ledger.jsonl")
    if skipped_ledger:
        source_notes.append(f"読み込めない入出金ログ行を{skipped_ledger}件スキップしました。")
    window_ledger_rows = _rows_in_window(ledger_rows, cutoff)

    wallet_user = _load_wallet_user(system_conf, user_name)
    goals = _extract_savings_goals(user_conf, wallet_user)
    stats = _build_stats(
        user_conf=user_conf,
        user_name=user_name,
        purchases=purchases,
        ledger_rows=window_ledger_rows,
        goals=goals,
        wallet_user=wallet_user,
        audit_state=audit_state,
        now=now,
        cutoff=cutoff,
        days=days_int,
        merge_info=merge_info,
    )

    policy = _normalize_policy(user_conf)
    support_state = _learning_support_state(audit_state)
    candidates = _build_card_candidates(stats=stats, policy=policy)
    insight_cards = _rank_cards(candidates, support_state=support_state, now=now)[:MAX_INSIGHT_CARDS]
    child_challenge = _build_child_challenge(insight_cards[0] if insight_cards else None, stats)
    metrics = _build_metrics(stats)
    prompt_points = _build_prompt_points(stats, insight_cards, child_challenge)
    summary_text = _build_summary_text(stats, insight_cards)

    return {
        "summary_text": summary_text,
        "metrics": metrics,
        "insight_cards": insight_cards,
        "child_challenge": child_challenge,
        "prompt_points": prompt_points,
        "source_notes": source_notes,
    }


def _normalize_days(days: int) -> int:
    try:
        value = int(days)
    except (TypeError, ValueError):
        return DEFAULT_DAYS
    return value if value > 0 else DEFAULT_DAYS


def _get_repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


def _get_log_dir(system_conf: dict) -> Path:
    raw = "data/logs"
    if isinstance(system_conf, dict):
        raw = str(system_conf.get("log_dir") or raw)
    path = Path(raw)
    if path.is_absolute():
        return path
    return _get_repo_root() / path


def _load_jsonl(path: Path) -> tuple[list[dict], int]:
    if not path.exists():
        return [], 0

    rows: list[dict] = []
    skipped = 0
    try:
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    skipped += 1
                    continue
                if isinstance(obj, dict):
                    rows.append(obj)
                else:
                    skipped += 1
    except OSError:
        return [], 0
    return rows, skipped


def _load_json(path: Path) -> dict:
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _load_wallet_user(system_conf: dict, user_name: str) -> dict:
    if not user_name:
        return {}
    raw_path = None
    if isinstance(system_conf, dict):
        raw_path = system_conf.get("wallet_state_path")
    path = Path(str(raw_path)) if raw_path else _get_repo_root() / "data" / "wallet_state.json"
    if not path.is_absolute():
        path = _get_repo_root() / path
    state = _load_json(path)
    users = state.get("users") if isinstance(state, dict) else {}
    if not isinstance(users, dict):
        return {}
    wallet_user = users.get(user_name, {})
    return wallet_user if isinstance(wallet_user, dict) else {}


def _parse_ts(value: Any) -> datetime | None:
    if value in ("", None):
        return None
    text = str(value).strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=JST)
    return dt.astimezone(JST)


def _rows_in_window(rows: list[dict], cutoff: datetime) -> list[dict]:
    picked: list[dict] = []
    for row in rows:
        dt = _parse_ts(row.get("ts"))
        if dt is not None and dt >= cutoff:
            item = dict(row)
            item["_dt"] = dt
            picked.append(item)
    return sorted(picked, key=lambda r: (r.get("_dt") or datetime.min.replace(tzinfo=JST), str(r.get("ts", ""))))


def _to_int(value: Any) -> int | None:
    if isinstance(value, bool) or value in ("", None):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value) if value.is_integer() else None
    if isinstance(value, str):
        text = value.strip().replace(",", "")
        if not text:
            return None
        sign = -1 if text.startswith("-") else 1
        if text[0] in "+-":
            text = text[1:]
        if text.isdigit():
            return sign * int(text)
    return None


def _amount(row: dict) -> int | None:
    value = _to_int(row.get("amount"))
    if value is None:
        return None
    return max(0, value)


def _satisfaction(row: dict) -> int | None:
    value = _to_int(row.get("satisfaction"))
    if value is None or value < 0 or value > 10:
        return None
    return value


def _item(row: dict) -> str:
    item = str(row.get("item") or "").strip()
    return item if item else "品目未記入"


def _item_key(item: str) -> str:
    return " ".join(str(item or "").strip().lower().split())


def _reason(row: dict) -> str:
    return str(row.get("reason") or "").strip()


def _has_meaningful_reason(row: dict) -> bool:
    return _reason(row) not in NON_REASON_WORDS


def _is_supplement_row(row: dict) -> bool:
    action = str(row.get("action") or row.get("source") or "").strip()
    if action in SUPPLEMENT_ACTIONS:
        return True
    has_followup = _has_meaningful_reason(row) or _satisfaction(row) is not None
    return _amount(row) is None and bool(str(row.get("item") or "").strip()) and has_followup


def _normalize_purchase_units(rows: list[dict]) -> tuple[list[dict], dict]:
    purchases: list[dict] = []
    by_entry_id: dict[str, dict] = {}
    merged_count = 0
    unmerged_supplement_count = 0
    missing_entry_id_count = 0

    for index, row in enumerate(rows):
        row_copy = dict(row)
        row_copy.pop("_dt", None)
        dt = row.get("_dt")
        entry_id = str(row.get("entry_id") or "").strip()
        parent_entry_id = str(row.get("parent_entry_id") or "").strip()
        lookup_entry_id = parent_entry_id or entry_id
        if _is_supplement_row(row):
            if lookup_entry_id and lookup_entry_id in by_entry_id:
                _merge_supplement_into_purchase(by_entry_id[lookup_entry_id], row)
                merged_count += 1
                continue
            target = _find_supplement_target(row, purchases)
            if target is not None:
                _merge_supplement_into_purchase(target, row)
                merged_count += 1
                continue
            unmerged_supplement_count += 1
        elif entry_id and entry_id in by_entry_id:
            _merge_supplement_into_purchase(by_entry_id[entry_id], row)
            merged_count += 1
            continue

        if not str(row_copy.get("entry_id") or "").strip():
            row_copy["entry_id"] = f"legacy-read-{index + 1}"
            missing_entry_id_count += 1
        row_copy["_dt"] = dt
        row_copy["_source_count"] = 1
        purchases.append(row_copy)
        by_entry_id[str(row_copy["entry_id"])] = row_copy

    return purchases, {
        "merged_count": merged_count,
        "unmerged_supplement_count": unmerged_supplement_count,
        "missing_entry_id_count": missing_entry_id_count,
    }


def _find_supplement_target(supplement: dict, purchases: list[dict]) -> dict | None:
    supplement_entry_id = str(supplement.get("parent_entry_id") or supplement.get("entry_id") or "").strip()
    supplement_item_key = _item_key(_item(supplement))
    supplement_amount = _amount(supplement)
    supplement_dt = supplement.get("_dt")
    candidates: list[tuple[float, dict]] = []

    for purchase in purchases:
        if supplement_entry_id and supplement_entry_id == str(purchase.get("entry_id") or "").strip():
            return purchase
        if supplement_item_key != _item_key(_item(purchase)):
            continue
        if supplement_amount is not None and _amount(purchase) is not None and supplement_amount != _amount(purchase):
            continue
        purchase_dt = purchase.get("_dt")
        if not isinstance(supplement_dt, datetime) or not isinstance(purchase_dt, datetime):
            continue
        seconds = abs((supplement_dt - purchase_dt).total_seconds())
        same_day = supplement_dt.date() == purchase_dt.date()
        if not same_day and seconds > 12 * 60 * 60:
            continue
        if seconds > 36 * 60 * 60:
            continue
        missing_score = 0
        if not _has_meaningful_reason(purchase) and _has_meaningful_reason(supplement):
            missing_score -= 200_000
        if _satisfaction(purchase) is None and _satisfaction(supplement) is not None:
            missing_score -= 200_000
        candidates.append((seconds + missing_score, purchase))

    if not candidates:
        return None
    return sorted(candidates, key=lambda item: item[0])[0][1]


def _merge_supplement_into_purchase(purchase: dict, supplement: dict) -> None:
    if not _has_meaningful_reason(purchase) and _has_meaningful_reason(supplement):
        purchase["reason"] = _reason(supplement)
    if _satisfaction(purchase) is None and _satisfaction(supplement) is not None:
        purchase["satisfaction"] = _satisfaction(supplement)
    if _amount(purchase) is None and _amount(supplement) is not None:
        purchase["amount"] = _amount(supplement)
    purchase["_source_count"] = int(purchase.get("_source_count") or 1) + 1


def _extract_savings_goals(user_conf: dict, wallet_user: dict) -> list[dict]:
    for key in ("savings_goals", "goals"):
        value = user_conf.get(key) if isinstance(user_conf, dict) else None
        if isinstance(value, list):
            return [g for g in value if isinstance(g, dict)]
    wallet_goals = wallet_user.get("savings_goals") if isinstance(wallet_user, dict) else None
    return [g for g in wallet_goals if isinstance(g, dict)] if isinstance(wallet_goals, list) else []


def _extract_balance(user_conf: dict, wallet_user: dict) -> int | None:
    for source in (user_conf, wallet_user):
        if not isinstance(source, dict):
            continue
        for key in ("balance", "expected_balance", "current"):
            value = _to_int(source.get(key))
            if value is not None:
                return value
    return None


def _build_stats(
    user_conf: dict,
    user_name: str,
    purchases: list[dict],
    ledger_rows: list[dict],
    goals: list[dict],
    wallet_user: dict,
    audit_state: dict | None,
    now: datetime,
    cutoff: datetime,
    days: int,
    merge_info: dict,
) -> dict:
    fixed_allowance = max(0, _to_int(user_conf.get("fixed_allowance")) or 0)
    amounts = [_amount(row) for row in purchases if _amount(row) is not None]
    total_amount = sum(amounts)
    item_totals: dict[str, int] = defaultdict(int)
    category_totals: dict[str, int] = defaultdict(int)
    for row in purchases:
        amount = _amount(row) or 0
        item_totals[_item_key(_item(row))] += amount
        category_totals[_category_for_purchase(row)] += amount
    sats = [_satisfaction(row) for row in purchases if _satisfaction(row) is not None]
    complete_count = sum(
        1
        for row in purchases
        if _amount(row) is not None and _satisfaction(row) is not None and _has_meaningful_reason(row)
    )
    record_count = len(purchases)
    completion_rate = complete_count / record_count if record_count else 0.0
    fixed_allowance_ratio = (total_amount / fixed_allowance) if fixed_allowance else None
    last_entry_dt = max((row.get("_dt") for row in purchases if isinstance(row.get("_dt"), datetime)), default=None)
    days_since_last = (now.date() - last_entry_dt.date()).days if isinstance(last_entry_dt, datetime) else None

    income_total = 0
    for row in ledger_rows:
        action = str(row.get("action") or "").strip()
        delta = _to_int(row.get("delta"))
        if delta is not None and delta > 0 and (action in INCOME_ACTIONS or action.startswith("allowance_")):
            income_total += delta

    balance = _extract_balance(user_conf, wallet_user)
    goal_summary = _goal_summary(goals, balance, total_amount)
    wallet_gap = _wallet_gap_for_user(audit_state, user_name, cutoff)

    return {
        "user_name": user_name,
        "days": days,
        "window_start": cutoff.date().isoformat(),
        "window_end": now.date().isoformat(),
        "purchases": purchases,
        "record_count": record_count,
        "records_with_amount": len(amounts),
        "records_with_reason": sum(1 for row in purchases if _has_meaningful_reason(row)),
        "records_with_satisfaction": len(sats),
        "complete_count": complete_count,
        "completion_rate": completion_rate,
        "total_amount": total_amount,
        "item_totals": dict(sorted(item_totals.items(), key=lambda x: x[1], reverse=True)),
        "category_totals": dict(sorted(category_totals.items(), key=lambda x: x[1], reverse=True)),
        "average_amount": (total_amount / len(amounts)) if amounts else None,
        "average_satisfaction": (sum(sats) / len(sats)) if sats else None,
        "low_satisfaction_count": sum(1 for value in sats if value <= LOW_SATISFACTION_MAX),
        "high_satisfaction_count": sum(1 for value in sats if value >= HIGH_SATISFACTION_MIN),
        "fixed_allowance": fixed_allowance,
        "fixed_allowance_ratio": fixed_allowance_ratio,
        "income_total": income_total,
        "balance": balance,
        "goals": goals,
        "goal_summary": goal_summary,
        "last_entry_date": last_entry_dt.date().isoformat() if isinstance(last_entry_dt, datetime) else None,
        "days_since_last_entry": days_since_last,
        "wallet_gap": wallet_gap,
        "merge_info": merge_info,
        "small_amount_threshold": _small_amount_threshold(fixed_allowance),
        "high_amount_threshold": _high_amount_threshold(fixed_allowance),
    }


def _goal_summary(goals: list[dict], balance: int | None, total_amount: int) -> dict:
    if not goals:
        return {"has_goal": False}

    normalized: list[dict] = []
    for goal in goals:
        title = str(goal.get("title") or goal.get("name") or "貯金目標").strip()
        target = _to_int(goal.get("target_amount") if "target_amount" in goal else goal.get("target"))
        current = _to_int(goal.get("current"))
        if current is None:
            current = balance
        remaining = max((target or 0) - (current or 0), 0) if target else None
        normalized.append(
            {
                "title": title,
                "target_amount": target,
                "current": current,
                "remaining": remaining,
            }
        )

    primary = sorted(
        normalized,
        key=lambda g: g["remaining"] if g.get("remaining") is not None else 10**12,
    )[0]
    remaining = primary.get("remaining")
    impact_ratio = (total_amount / remaining) if isinstance(remaining, int) and remaining > 0 else None
    return {
        "has_goal": True,
        "primary": primary,
        "count": len(normalized),
        "impact_ratio": impact_ratio,
    }


def _category_for_purchase(row: dict) -> str:
    explicit = str(row.get("category") or "").strip()
    if explicit:
        return explicit
    text = f"{_item(row)} {_reason(row)}"
    for category, terms in CATEGORY_TERMS.items():
        if any(term in text for term in terms):
            return category
    return "その他"


def _wallet_gap_for_user(audit_state: dict | None, user_name: str, cutoff: datetime) -> dict | None:
    if not isinstance(audit_state, dict) or not user_name:
        return None
    penalties = audit_state.get("wallet_check_penalties", {})
    if not isinstance(penalties, dict):
        return None
    gap = penalties.get(user_name)
    if not isinstance(gap, dict):
        return None
    dt = _parse_ts(gap.get("ts"))
    if dt is not None and dt < cutoff:
        return None
    diff = _to_int(gap.get("diff"))
    return {
        "ts": gap.get("ts"),
        "type": gap.get("type"),
        "diff": diff,
        "reported": _to_int(gap.get("reported")),
        "expected": _to_int(gap.get("expected")),
    }


def _small_amount_threshold(fixed_allowance: int) -> int:
    if fixed_allowance > 0:
        return min(800, max(300, int(fixed_allowance * 0.2)))
    return 500


def _high_amount_threshold(fixed_allowance: int) -> int:
    if fixed_allowance > 0:
        return max(800, int(fixed_allowance * 0.4))
    return 1000


def _normalize_policy(user_conf: dict) -> dict:
    raw_policy = user_conf.get("ai_follow_policy") if isinstance(user_conf, dict) else {}
    policy = raw_policy if isinstance(raw_policy, dict) else {}
    focus_area = str(policy.get("focus_area") or "").strip()
    if not focus_area and isinstance(policy.get("focus_areas"), list):
        focus_area = str(next((x for x in policy["focus_areas"] if str(x).strip()), "")).strip()
    if not focus_area:
        focus_area = "record_habit"
    return {
        "enabled": bool(policy.get("enabled", True)),
        "focus_area": focus_area,
    }


def _build_card_candidates(stats: dict, policy: dict) -> list[dict]:
    purchases = stats["purchases"]
    cards: list[dict] = []

    low_high = _candidate_low_satisfaction_high_amount(stats, policy)
    if low_high:
        cards.append(low_high)

    repeated = _candidate_repeated_small_spending(stats, policy)
    if repeated:
        cards.append(repeated)

    online = _candidate_online_safety(stats, policy)
    if online:
        cards.append(online)

    saving = _candidate_saving_goal_impact(stats, policy)
    if saving:
        cards.append(saving)

    record = _candidate_record_habit(stats, policy)
    if record:
        cards.append(record)

    income = _candidate_income_balance(stats, policy)
    if income:
        cards.append(income)

    positive = _candidate_positive_planned_purchase(stats, policy)
    if positive:
        cards.append(positive)

    if not cards and not purchases:
        cards.append(_empty_record_card(stats, policy))
    return cards


def _candidate_low_satisfaction_high_amount(stats: dict, policy: dict) -> dict | None:
    threshold = stats["high_amount_threshold"]
    rows = [
        row
        for row in stats["purchases"]
        if (_amount(row) or 0) >= threshold and (_satisfaction(row) is not None and _satisfaction(row) <= LOW_SATISFACTION_MAX)
    ]
    if not rows:
        return None
    row = sorted(rows, key=lambda r: (_amount(r) or 0, _satisfaction(r) or 0), reverse=True)[0]
    evidence = _purchase_evidence(row, stats)
    evidence["amount_band"] = "high"
    evidence["satisfaction_band"] = "low"
    return _make_card(
        card_type="low_satisfaction_high_amount",
        title="大きめ支出の納得感チェック",
        base_priority=76,
        evidence=evidence,
        skill="比較・待つ",
        parent_question="買う前に比べられる候補が1つあるとしたら、どれがよさそう？",
        parent_action="次に似た買い物が出た時の候補を一緒に1つ書く。",
        child_action="次に大きめの買い物をする前に、候補を2つ見つけてから選ぶ。",
        avoid="金額だけで決めつけず、本人が期待していたことを先に聞く。",
        next_observation="次の大きめ支出で、比べた候補が1つ残っているかを見る。",
        policy=policy,
    )


def _candidate_repeated_small_spending(stats: dict, policy: dict) -> dict | None:
    groups: dict[str, list[dict]] = defaultdict(list)
    for row in stats["purchases"]:
        amount = _amount(row)
        if amount is None or amount > stats["small_amount_threshold"]:
            continue
        groups[_item_key(_item(row))].append(row)

    candidates = []
    for rows in groups.values():
        if len(rows) < 2:
            continue
        total = sum(_amount(row) or 0 for row in rows)
        sats = [_satisfaction(row) for row in rows if _satisfaction(row) is not None]
        low_count = sum(1 for value in sats if value <= LOW_SATISFACTION_MAX)
        avg_sat = sum(sats) / len(sats) if sats else None
        if sats and low_count == 0 and (avg_sat or 0) > 6:
            continue
        candidates.append((low_count, len(rows), total, rows, avg_sat))

    if not candidates:
        return None

    low_count, count, total, rows, avg_sat = sorted(candidates, key=lambda x: (x[0], x[1], x[2]), reverse=True)[0]
    item = _item(rows[-1])
    evidence = {
        "item": item,
        "amount": total,
        "count": count,
        "average_amount": round(total / count),
        "average_satisfaction": _round_one(avg_sat),
        "period": f"過去{stats['days']}日",
        "amount_band": "small",
        "satisfaction_band": "low" if low_count else "mixed",
    }
    return _make_card(
        card_type="repeated_small_spending",
        title="少額の反復支出",
        base_priority=72,
        evidence=evidence,
        skill="待つ・計画",
        parent_question="次に同じものを買う前に、1回だけ待つならどのタイミングがよさそう？",
        parent_action="今週分の小さな予算を本人と100円単位で決める。",
        child_action="次に同じものがほしくなったら、今買うか少し待つかを1つ選ぶ。",
        avoid="回数だけを責めず、本人が選べる小さなルールにする。",
        next_observation="同じ品目の回数と、買う前に待てた記録が増えたかを見る。",
        policy=policy,
    )


def _candidate_online_safety(stats: dict, policy: dict) -> dict | None:
    rows = [row for row in stats["purchases"] if _contains_any(f"{_item(row)} {_reason(row)}", ONLINE_TERMS)]
    if not rows:
        return None
    row = sorted(rows, key=lambda r: _amount(r) or 0, reverse=True)[0]
    evidence = _purchase_evidence(row, stats)
    return _make_card(
        card_type="online_risk",
        title="オンライン支出の安全確認",
        base_priority=70,
        evidence=evidence,
        skill="安全確認・待つ",
        parent_question="次にオンラインで買う前に、確認することを1つ選ぶなら何にする？",
        parent_action="アプリ内の購入画面や通知設定を一緒に1つ確認する。",
        child_action="オンラインで買う前に、金額と回数を声に出して確認する。",
        avoid="怖がらせず、確認する順番を短く決める。",
        next_observation="次のオンライン支出で、買う前確認が1つ残っているかを見る。",
        policy=policy,
    )


def _candidate_saving_goal_impact(stats: dict, policy: dict) -> dict | None:
    goal_summary = stats["goal_summary"]
    if not goal_summary.get("has_goal") or stats["total_amount"] <= 0:
        return None
    primary = goal_summary["primary"]
    evidence = {
        "goal_title": primary.get("title"),
        "target_amount": primary.get("target_amount"),
        "current_amount": primary.get("current"),
        "remaining_amount": primary.get("remaining"),
        "amount": stats["total_amount"],
        "period": f"過去{stats['days']}日",
        "goal_impact_ratio": _round_one(goal_summary.get("impact_ratio")),
    }
    return _make_card(
        card_type="saving_goal_impact",
        title="目標貯金へのつながり",
        base_priority=58,
        evidence=evidence,
        skill="貯金・計画",
        parent_question="目標に近づくために、今週残したい金額はいくらにする？",
        parent_action="目標までの残り金額を見て、今週残す額を一緒に決める。",
        child_action="目標のために今週残したい金額を1つ書く。",
        avoid="目標を理由に決めつけず、本人が残す額を選べる形にする。",
        next_observation="次回、決めた金額が残せたかを確認する。",
        policy=policy,
    )


def _candidate_record_habit(stats: dict, policy: dict) -> dict | None:
    record_count = stats["record_count"]
    completion_rate = stats["completion_rate"]
    days_since_last = stats["days_since_last_entry"]
    merge_count = int(stats["merge_info"].get("merged_count") or 0)
    wallet_gap = stats.get("wallet_gap")
    focus_record = policy.get("enabled") and policy.get("focus_area") == "record_habit"

    should_create = (
        record_count == 0
        or completion_rate < 0.75
        or merge_count > 0
        or wallet_gap is not None
        or (days_since_last is not None and days_since_last >= 14)
        or focus_record
    )
    if not should_create:
        return None

    evidence = {
        "count": record_count,
        "complete_count": stats["complete_count"],
        "completion_rate": round(completion_rate, 3),
        "records_with_reason": stats["records_with_reason"],
        "records_with_satisfaction": stats["records_with_satisfaction"],
        "last_entry_date": stats["last_entry_date"],
        "merged_supplements": merge_count,
        "wallet_check_note": _wallet_gap_label(wallet_gap),
        "period": f"過去{stats['days']}日",
    }
    return _make_card(
        card_type="record_habit",
        title="記録を続けるきっかけ",
        base_priority=54 if record_count else 78,
        evidence=evidence,
        skill="記録",
        parent_question="次の記録で、金額・理由・満足度のうちどれを足すと続けやすい？",
        parent_action="次の買い物で使う記録テンプレートを一緒に1つ決める。",
        child_action="次に買ったら、金額と満足度を1つずつ記録する。",
        avoid="記録できていない点だけを見ず、次に足す1項目へ絞る。",
        next_observation="次の記録で理由か満足度が1つ増えたかを見る。",
        policy=policy,
    )


def _candidate_income_balance(stats: dict, policy: dict) -> dict | None:
    fixed = stats["fixed_allowance"]
    if fixed <= 0:
        return None
    ratio = stats["fixed_allowance_ratio"] or 0.0
    focus_income = policy.get("enabled") and policy.get("focus_area") == "income_balance"
    if ratio < 0.35 and stats["income_total"] <= 0 and not focus_income:
        return None
    evidence = {
        "fixed_allowance": fixed,
        "amount": stats["total_amount"],
        "fixed_allowance_ratio": round(ratio, 3),
        "income_total": stats["income_total"],
        "balance": stats["balance"],
        "period": f"過去{stats['days']}日",
    }
    return _make_card(
        card_type="income_balance",
        title="収入と支出のバランス",
        base_priority=50,
        evidence=evidence,
        skill="収入理解・計画",
        parent_question="今月のお小遣いから、先に残しておきたい金額はいくらにする？",
        parent_action="固定お小遣い、入金、使った額を一緒に1行で見直す。",
        child_action="次にもらったお金から、先に残す金額を1つ決める。",
        avoid="足りない点を強調せず、残す金額を本人が選べる形にする。",
        next_observation="次の入金後に、先に残す金額を決められたかを見る。",
        policy=policy,
    )


def _candidate_positive_planned_purchase(stats: dict, policy: dict) -> dict | None:
    rows = [row for row in stats["purchases"] if _satisfaction(row) is not None and _satisfaction(row) >= HIGH_SATISFACTION_MIN]
    if not rows:
        return None

    def score(row: dict) -> tuple[int, int]:
        planned = 1 if _has_meaningful_reason(row) and _contains_any(f"{_item(row)} {_reason(row)}", PLANNING_TERMS) else 0
        return planned, _amount(row) or 0

    row = sorted(rows, key=score, reverse=True)[0]
    amount = _amount(row) or 0
    amount_band = "high" if amount >= stats["high_amount_threshold"] else "small" if amount <= stats["small_amount_threshold"] else "standard"
    title = "納得できた大きめの支出" if amount_band == "high" else "満足につながった支出"
    evidence = _purchase_evidence(row, stats)
    evidence["amount_band"] = amount_band
    evidence["satisfaction_band"] = "high"
    evidence["planned_hint"] = _contains_any(f"{_item(row)} {_reason(row)}", PLANNING_TERMS)
    return _make_card(
        card_type="positive_planned_purchase",
        title=title,
        base_priority=46,
        evidence=evidence,
        skill="説明・計画",
        parent_question="この買い物でよかった選び方を、次も使うならどれにする？",
        parent_action="よかった理由を本人の言葉で1つメモする。",
        child_action="買ってよかった理由を1つ書いて、次も使う選び方にする。",
        avoid="結果だけをほめず、選び方や考えたことに目を向ける。",
        next_observation="次の買い物で同じ選び方を使えたかを見る。",
        policy=policy,
    )


def _empty_record_card(stats: dict, policy: dict) -> dict:
    evidence = {"count": 0, "period": f"過去{stats['days']}日"}
    return _make_card(
        card_type="record_habit",
        title="最初の記録づくり",
        base_priority=80,
        evidence=evidence,
        skill="記録",
        parent_question="次に買ったものを、名前だけでも一緒に残してみる？",
        parent_action="買い物のあとに使う短い記録文を1つ用意する。",
        child_action="次に買ったものの名前を1つ記録する。",
        avoid="記録がないことを責めず、最初の1件に絞る。",
        next_observation="次の買い物で品目が1つ残ったかを見る。",
        policy=policy,
    )


def _purchase_evidence(row: dict, stats: dict) -> dict:
    return {
        "item": _item(row),
        "amount": _amount(row),
        "count": 1,
        "satisfaction": _satisfaction(row),
        "reason": _short_text(_reason(row), 42),
        "period": f"過去{stats['days']}日",
    }


def _make_card(
    card_type: str,
    title: str,
    base_priority: int,
    evidence: dict,
    skill: str,
    parent_question: str,
    parent_action: str,
    child_action: str,
    avoid: str,
    next_observation: str,
    policy: dict,
) -> dict:
    boost = _policy_boost(card_type, policy)
    priority = base_priority + boost
    policy_match = boost > 0
    card_body = {
        "type": card_type,
        "title": title,
        "evidence": evidence,
        "skill": skill,
        "parent_question": _one_question(parent_question),
        "parent_action": parent_action,
        "child_action": child_action,
        "avoid": avoid,
        "policy_match": policy_match,
        "next_observation": next_observation,
    }
    return {
        "card_id": _card_id(card_body),
        **card_body,
        "priority": priority,
    }


def _policy_boost(card_type: str, policy: dict) -> int:
    if not policy.get("enabled", True):
        return 0
    focus_area = str(policy.get("focus_area") or "").strip()
    return int(POLICY_TYPE_BOOSTS.get(focus_area, {}).get(card_type, 0))


def _learning_support_state(audit_state: dict | None) -> dict:
    if not isinstance(audit_state, dict):
        return {}
    state = audit_state.get("learning_support_state")
    return state if isinstance(state, dict) else {}


def _rank_cards(cards: list[dict], support_state: dict | None = None, now: datetime | None = None) -> list[dict]:
    adjusted = _apply_support_state(cards, support_state or {}, now or datetime.now(JST))
    return sorted(adjusted, key=lambda c: (int(c.get("priority") or 0), str(c.get("card_id") or "")), reverse=True)


def _apply_support_state(cards: list[dict], support_state: dict, now: datetime) -> list[dict]:
    if not cards or not support_state:
        return cards

    suppressed_ids, suppressed_types = _active_suppressed_cards(support_state, now)
    filtered = [
        card for card in cards
        if str(card.get("card_id") or "") not in suppressed_ids
        and str(card.get("type") or "") not in suppressed_types
    ]
    if filtered:
        cards = filtered

    latest_feedback = _latest_feedback_event(support_state)
    selected_id = ""
    selected_type = ""
    if latest_feedback.get("feedback") == "use_this_week" and _is_recent_ts(latest_feedback.get("ts"), now, days=7):
        selected_id = str(latest_feedback.get("card_id") or "")
        selected_type = str(latest_feedback.get("card_type") or "")

    last_type = str(support_state.get("last_card_type") or "")
    last_nudge_at = support_state.get("last_nudge_at")
    adjusted: list[dict] = []
    for card in cards:
        copied = dict(card)
        card_id = str(copied.get("card_id") or "")
        card_type = str(copied.get("type") or "")
        priority = int(copied.get("priority") or 0)
        if selected_id and (card_id == selected_id or card_type == selected_type):
            priority += 60
        elif last_type and card_type == last_type and _is_recent_ts(last_nudge_at, now, days=3):
            priority -= 45
            copied["repeat_limited"] = True
        copied["priority"] = priority
        adjusted.append(copied)
    return adjusted


def _active_suppressed_cards(support_state: dict, now: datetime) -> tuple[set[str], set[str]]:
    ids: set[str] = set()
    types: set[str] = set()
    suppressed = support_state.get("suppressed_card_types")
    if not isinstance(suppressed, list):
        return ids, types
    for item in suppressed:
        if not isinstance(item, dict) or not _is_recent_ts(item.get("ts"), now, days=7):
            continue
        card_id = str(item.get("card_id") or "").strip()
        card_type = str(item.get("card_type") or "").strip()
        if card_id:
            ids.add(card_id)
        if card_type:
            types.add(card_type)
    return ids, types


def _latest_feedback_event(support_state: dict) -> dict:
    events = support_state.get("feedback_events")
    if not isinstance(events, list):
        return {}
    for event in reversed(events):
        if isinstance(event, dict):
            return event
    return {}


def _is_recent_ts(value: Any, now: datetime, days: int) -> bool:
    dt = _parse_ts(value)
    if dt is None:
        return False
    return now - timedelta(days=days) <= dt <= now + timedelta(minutes=5)


def _card_id(card_body: dict) -> str:
    raw = json.dumps(card_body, ensure_ascii=False, sort_keys=True, default=str)
    digest = hashlib.sha1(raw.encode("utf-8")).hexdigest()[:10]
    return f"{card_body['type']}:{digest}"


def _one_question(text: str) -> str:
    value = str(text or "").strip()
    value = value.replace("?", "？")
    if "？" not in value:
        value = value.rstrip("。") + "？"
    head = value.split("？", 1)[0].strip()
    return head + "？"


def _build_child_challenge(card: dict | None, stats: dict) -> dict:
    if card:
        action = str(card.get("child_action") or "").strip()
        card_type = str(card.get("type") or "record_habit")
        source_card_id = card.get("card_id")
    else:
        action = "次に買ったものの名前を1つ記録する。"
        card_type = "record_habit"
        source_card_id = None
    return {
        "challenge_id": _card_id({"type": "child_challenge", "action": action, "user": stats.get("user_name")}),
        "type": card_type,
        "action": _one_action(action),
        "minutes": 5,
        "feedback_options": ["やった", "あとで", "ちがう"],
        "source_card_id": source_card_id,
    }


def _one_action(text: str) -> str:
    value = str(text or "").strip()
    if not value:
        return "次に買ったものの名前を1つ記録する。"
    for sep in ("。", "\n"):
        if sep in value:
            head = value.split(sep, 1)[0].strip()
            return head + "。"
    return value + ("。" if not value.endswith("。") else "")


def _build_metrics(stats: dict) -> list[dict]:
    avg_sat = stats["average_satisfaction"]
    fixed_ratio = stats["fixed_allowance_ratio"]
    goal_summary = stats["goal_summary"]
    if goal_summary.get("has_goal"):
        primary = goal_summary["primary"]
        remaining = primary.get("remaining")
        goal_value = (
            f"{primary.get('title')}まであと{remaining:,}円 / 支出合計{stats['total_amount']:,}円"
            if isinstance(remaining, int)
            else f"{primary.get('title')} / 支出合計{stats['total_amount']:,}円"
        )
    else:
        goal_value = "目標なし"

    return [
        {"key": "records", "label": "記録件数", "value": f"{stats['record_count']}件", "raw_value": stats["record_count"]},
        {
            "key": "total_amount",
            "label": "支出合計",
            "value": f"{stats['total_amount']:,}円" if stats["total_amount"] else "記録なし",
            "raw_value": stats["total_amount"],
        },
        {
            "key": "average_satisfaction",
            "label": "満足度平均",
            "value": f"{avg_sat:.1f}/10" if avg_sat is not None else "記録なし",
            "raw_value": _round_one(avg_sat),
        },
        {
            "key": "top_item",
            "label": "品目上位",
            "value": _top_total_label(stats.get("item_totals")),
            "raw_value": stats.get("item_totals"),
        },
        {
            "key": "top_category",
            "label": "カテゴリ上位",
            "value": _top_total_label(stats.get("category_totals")),
            "raw_value": stats.get("category_totals"),
        },
        {
            "key": "completion_rate",
            "label": "記録完全率",
            "value": f"{round(stats['completion_rate'] * 100):.0f}%",
            "raw_value": round(stats["completion_rate"], 3),
        },
        {
            "key": "fixed_allowance_ratio",
            "label": "固定お小遣い比",
            "value": f"{round(fixed_ratio * 100):.0f}%" if fixed_ratio is not None else "未設定",
            "raw_value": round(fixed_ratio, 3) if fixed_ratio is not None else None,
        },
        {
            "key": "saving_goal_impact",
            "label": "目標影響",
            "value": goal_value,
            "raw_value": goal_summary,
        },
    ]


def _top_total_label(totals: Any) -> str:
    if not isinstance(totals, dict) or not totals:
        return "記録なし"
    key, value = next(iter(totals.items()))
    try:
        amount = int(value)
    except (TypeError, ValueError):
        return str(key)
    return f"{key}: {amount:,}円"


def _build_prompt_points(stats: dict, cards: list[dict], child_challenge: dict) -> list[str]:
    points = [
        f"過去{stats['days']}日の購買単位は{stats['record_count']}件、支出合計は{stats['total_amount']:,}円。",
        f"記録完全率は{round(stats['completion_rate'] * 100):.0f}%、満足度平均は{_format_satisfaction(stats['average_satisfaction'])}。",
    ]
    for card in cards:
        points.append(
            f"会話カード: {card['title']}。親の問いは1つに絞り、今日の伴走は「{card['parent_action']}」。"
        )
    points.append(f"子ども向け次アクション: {child_challenge['action']}")
    return points[:5]


def _build_summary_text(stats: dict, cards: list[dict]) -> str:
    if stats["record_count"] == 0:
        return f"過去{stats['days']}日の支出記録はまだありません。最初の記録づくりを優先します。"
    card_titles = "、".join(card["title"] for card in cards) if cards else "記録の確認"
    avg_sat = _format_satisfaction(stats["average_satisfaction"])
    return (
        f"過去{stats['days']}日で{stats['record_count']}件、支出合計{stats['total_amount']:,}円、"
        f"満足度平均{avg_sat}。今週は「{card_titles}」を優先します。"
    )


def _format_satisfaction(value: float | None) -> str:
    return f"{value:.1f}/10" if value is not None else "記録なし"


def _wallet_gap_label(wallet_gap: dict | None) -> str | None:
    if not wallet_gap:
        return None
    diff = wallet_gap.get("diff")
    if diff is None:
        return "財布チェックで帳簿との差がありました"
    direction = "多い" if diff > 0 else "少ない"
    return f"財布チェックで{abs(int(diff)):,}円{direction}差がありました"


def _contains_any(text: str, terms: tuple[str, ...]) -> bool:
    return any(term in text for term in terms)


def _round_one(value: float | None) -> float | None:
    return round(value, 1) if value is not None else None


def _short_text(text: Any, limit: int = 40) -> str:
    value = str(text or "").strip()
    if len(value) <= limit:
        return value
    return value[:limit] + "..."
