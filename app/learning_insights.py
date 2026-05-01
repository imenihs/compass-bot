"""
learning_insights.py — 支出記録を親子会話カードへ変換する。

既存ログは読み取り専用で扱う。補足レコードの紐づけも読み取り時の暫定マージに留める。
"""

from __future__ import annotations

import hashlib
import json
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any


JST = timezone(timedelta(hours=9))
DEFAULT_DAYS = 90
LOW_SATISFACTION_MAX = 4
HIGH_SATISFACTION_MIN = 8
HIGH_AMOUNT_RATIO = 0.35
SMALL_AMOUNT_MAX = 500
REPEATED_SMALL_COUNT = 3
MAX_CARDS = 4

FOCUS_CARD_WEIGHTS = {
    "record_habit": {"record_habit": 35},
    "planning": {
        "low_satisfaction_high_amount": 22,
        "repeated_small_spending": 18,
        "positive_planned_purchase": 10,
    },
    "satisfaction_reflection": {
        "low_satisfaction_high_amount": 26,
        "repeated_small_spending": 18,
        "positive_planned_purchase": 16,
    },
    "saving_goal": {"saving_goal_impact": 35},
    "impulse_spending": {
        "repeated_small_spending": 28,
        "low_satisfaction_high_amount": 16,
        "online_risk": 14,
    },
    "income_balance": {"income_balance": 35, "saving_goal_impact": 12},
}

UNSAFE_WORDS = ("罰", "ペナルティ", "叱", "浪費家", "だらしない", "兄弟と比べ")


def build_learning_insights(
    user_conf: dict,
    system_conf: dict,
    audit_state: dict | None = None,
    days: int = DEFAULT_DAYS,
) -> dict:
    """支出ログから親向けカードと子ども向けチャレンジを返す。"""
    user_name = str(user_conf.get("name", "")).strip()
    days_int = _normalize_days(days)
    now = datetime.now(JST)
    cutoff = now - timedelta(days=days_int)
    log_dir = _get_log_dir(system_conf)
    rows = _load_jsonl(log_dir / f"{user_name}_pocket_journal.jsonl") if user_name else []
    purchases, merge_notes = _normalize_purchases(rows, user_name)
    window_purchases = [p for p in purchases if _parse_ts(p.get("ts")) and _parse_ts(p.get("ts")) >= cutoff]
    wallet_gap = _wallet_gap_for_user(audit_state or {}, user_name, cutoff)
    fixed_allowance = _to_int(user_conf.get("fixed_allowance")) or 0
    goals = _load_goals(user_name)
    focus_area = _focus_area(user_conf)

    metrics = _build_metrics(window_purchases, fixed_allowance, goals, wallet_gap, days_int)
    cards = _build_cards(
        purchases=window_purchases,
        metrics=metrics,
        user_conf=user_conf,
        focus_area=focus_area,
        wallet_gap=wallet_gap,
    )
    child_challenge = _pick_child_challenge(cards, metrics)
    prompt_points = _build_prompt_points(cards, metrics, child_challenge)
    summary_text = _summary_text(user_name, metrics, cards)

    source_notes = merge_notes
    if wallet_gap:
        source_notes.append("財布チェック差分は記録確認メモとして扱った。")
    if not window_purchases:
        source_notes.append("対象期間内の購買記録が少ないため、記録習慣カードを優先した。")

    return {
        "summary_text": summary_text,
        "metrics": metrics,
        "insight_cards": cards[:MAX_CARDS],
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


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


def _get_log_dir(system_conf: dict) -> Path:
    raw = str(system_conf.get("log_dir") or "data/logs") if isinstance(system_conf, dict) else "data/logs"
    path = Path(raw)
    return path if path.is_absolute() else _repo_root() / path


def _load_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    rows: list[dict] = []
    try:
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                text = line.strip()
                if not text:
                    continue
                try:
                    row = json.loads(text)
                except json.JSONDecodeError:
                    continue
                if isinstance(row, dict):
                    rows.append(row)
    except OSError:
        return []
    return rows


def _parse_ts(value: Any) -> datetime | None:
    if value in ("", None):
        return None
    text = str(value).strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=JST)
    return dt.astimezone(JST)


def _to_int(value: Any) -> int | None:
    if isinstance(value, bool) or value in ("", None):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value) if value.is_integer() else None
    if isinstance(value, str):
        text = value.strip().replace(",", "").replace("円", "")
        if text.startswith("-"):
            sign = -1
            text = text[1:]
        else:
            sign = 1
        if text.isdigit():
            return sign * int(text)
    return None


def _satisfaction(row: dict) -> int | None:
    value = _to_int(row.get("satisfaction"))
    if value is None or value < 0 or value > 10:
        return None
    return value


def _amount(row: dict) -> int | None:
    value = _to_int(row.get("amount"))
    if value is None or value < 0:
        return None
    return value


def _item(row: dict) -> str:
    return str(row.get("item") or "").strip() or "品目未記入"


def _entry_id(row: dict, user_name: str) -> str:
    raw = str(row.get("entry_id") or "").strip()
    if raw:
        return raw
    seed = "|".join(
        [
            user_name,
            str(row.get("ts") or ""),
            str(row.get("item") or ""),
            str(row.get("amount") or ""),
            str(row.get("reason") or ""),
        ]
    )
    return "legacy-" + hashlib.sha1(seed.encode("utf-8")).hexdigest()[:14]


def _normalize_purchases(rows: list[dict], user_name: str) -> tuple[list[dict], list[str]]:
    """支出と後追い補足を読み取り時だけマージする。"""
    purchases: list[dict] = []
    by_entry_id: dict[str, dict] = {}
    notes: list[str] = []
    sorted_rows = sorted(rows, key=lambda r: str(r.get("ts", "")))

    for row in sorted_rows:
        amount = _amount(row)
        action = str(row.get("action") or "")
        if amount is None and action == "expense_supplement":
            target = _find_supplement_target(purchases, row)
            if target is not None:
                if not str(target.get("reason") or "").strip() and str(row.get("reason") or "").strip():
                    target["reason"] = str(row.get("reason") or "").strip()
                sat = _satisfaction(row)
                if target.get("satisfaction") is None and sat is not None:
                    target["satisfaction"] = sat
                target.setdefault("supplement_rows", []).append(row)
                notes.append(f"補足レコードを「{target.get('item')}」へ暫定マージした。")
            continue

        if amount is None:
            continue

        eid = _entry_id(row, user_name)
        if eid in by_entry_id:
            target = by_entry_id[eid]
        else:
            target = {
                "entry_id": eid,
                "ts": row.get("ts"),
                "item": _item(row),
                "amount": amount,
                "reason": str(row.get("reason") or "").strip(),
                "satisfaction": _satisfaction(row),
                "source": action or "spending_record",
                "raw": row,
            }
            purchases.append(target)
            by_entry_id[eid] = target
        if not target.get("reason") and str(row.get("reason") or "").strip():
            target["reason"] = str(row.get("reason") or "").strip()
        if target.get("satisfaction") is None and _satisfaction(row) is not None:
            target["satisfaction"] = _satisfaction(row)

    return purchases, list(dict.fromkeys(notes))


def _find_supplement_target(purchases: list[dict], supplement: dict) -> dict | None:
    sup_eid = str(supplement.get("entry_id") or supplement.get("parent_entry_id") or "").strip()
    if sup_eid:
        for purchase in reversed(purchases):
            if purchase.get("entry_id") == sup_eid:
                return purchase

    sup_item = _item(supplement)
    sup_dt = _parse_ts(supplement.get("ts"))
    candidates = []
    for purchase in reversed(purchases[-20:]):
        if _item(purchase) != sup_item:
            continue
        pur_dt = _parse_ts(purchase.get("ts"))
        if sup_dt and pur_dt and abs((sup_dt - pur_dt).total_seconds()) > 60 * 60 * 24:
            continue
        candidates.append(purchase)
    return candidates[0] if candidates else (purchases[-1] if purchases else None)


def _load_goals(user_name: str) -> list[dict]:
    path = _repo_root() / "data" / "wallet_state.json"
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError):
        return []
    user_state = data.get("users", {}).get(user_name, {}) if isinstance(data, dict) else {}
    goals = user_state.get("savings_goals", [])
    return goals if isinstance(goals, list) else []


def _wallet_gap_for_user(audit_state: dict, user_name: str, cutoff: datetime) -> dict | None:
    penalties = audit_state.get("wallet_check_penalties", {}) if isinstance(audit_state, dict) else {}
    if not isinstance(penalties, dict):
        return None
    row = penalties.get(user_name)
    if not isinstance(row, dict):
        return None
    dt = _parse_ts(row.get("ts"))
    if dt is not None and dt < cutoff:
        return None
    return row


def _focus_area(user_conf: dict) -> str:
    policy = user_conf.get("ai_follow_policy") if isinstance(user_conf, dict) else {}
    if not isinstance(policy, dict):
        return "record_habit"
    focus = str(policy.get("focus_area") or "").strip()
    if focus:
        return focus
    focuses = policy.get("focus_areas")
    if isinstance(focuses, list):
        for item in focuses:
            item_text = str(item).strip()
            if item_text:
                return item_text
    return "record_habit"


def _build_metrics(
    purchases: list[dict],
    fixed_allowance: int,
    goals: list[dict],
    wallet_gap: dict | None,
    days: int,
) -> dict:
    amounts = [p["amount"] for p in purchases if _to_int(p.get("amount")) is not None]
    sats = [p["satisfaction"] for p in purchases if isinstance(p.get("satisfaction"), int)]
    complete_count = sum(1 for p in purchases if p.get("amount") is not None and p.get("reason") and p.get("satisfaction") is not None)
    item_totals: dict[str, int] = defaultdict(int)
    item_counts: Counter[str] = Counter()
    for purchase in purchases:
        item = str(purchase.get("item") or "品目未記入")
        item_totals[item] += int(purchase.get("amount") or 0)
        item_counts[item] += 1
    total_amount = sum(amounts)
    goal_targets = sum(max(0, _to_int(g.get("target_amount")) or 0) for g in goals)
    return {
        "days": days,
        "record_count": len(purchases),
        "total_amount": total_amount,
        "average_satisfaction": round(sum(sats) / len(sats), 1) if sats else None,
        "low_satisfaction_count": sum(1 for s in sats if s <= LOW_SATISFACTION_MAX),
        "high_satisfaction_count": sum(1 for s in sats if s >= HIGH_SATISFACTION_MIN),
        "complete_record_count": complete_count,
        "record_completion_rate": round(complete_count / len(purchases), 2) if purchases else 0.0,
        "fixed_allowance": fixed_allowance,
        "fixed_spending_ratio": round(total_amount / fixed_allowance, 2) if fixed_allowance > 0 else None,
        "item_totals": dict(sorted(item_totals.items(), key=lambda kv: kv[1], reverse=True)[:5]),
        "item_counts": dict(item_counts.most_common(5)),
        "goal_count": len(goals),
        "goal_target_total": goal_targets,
        "wallet_gap": wallet_gap,
    }


def _build_cards(
    purchases: list[dict],
    metrics: dict,
    user_conf: dict,
    focus_area: str,
    wallet_gap: dict | None,
) -> list[dict]:
    fixed = int(metrics.get("fixed_allowance") or 0)
    high_amount_line = max(1000, int(fixed * HIGH_AMOUNT_RATIO)) if fixed > 0 else 1000
    cards: list[dict] = []
    cards.extend(_low_satisfaction_high_amount_cards(purchases, high_amount_line))
    cards.extend(_repeated_small_spending_cards(purchases))
    cards.extend(_positive_purchase_cards(purchases, high_amount_line))
    cards.extend(_saving_goal_cards(purchases, metrics))
    cards.extend(_record_habit_cards(purchases, metrics, wallet_gap))
    cards.extend(_income_balance_cards(metrics))
    cards.extend(_online_risk_cards(purchases))

    weights = FOCUS_CARD_WEIGHTS.get(focus_area, {})
    for card in cards:
        card["priority"] = int(card.get("priority", 0)) + int(weights.get(card.get("type"), 0))
        card["policy_match"] = card.get("type") in weights
        _sanitize_card(card)

    cards.sort(key=lambda c: (-int(c.get("priority", 0)), str(c.get("card_id", ""))))
    deduped: list[dict] = []
    seen_types = set()
    for card in cards:
        ctype = card.get("type")
        if ctype in seen_types:
            continue
        seen_types.add(ctype)
        deduped.append(card)
    if not deduped:
        deduped.append(_default_card())
    return deduped


def _low_satisfaction_high_amount_cards(purchases: list[dict], high_amount_line: int) -> list[dict]:
    rows = [
        p for p in purchases
        if int(p.get("amount") or 0) >= high_amount_line and isinstance(p.get("satisfaction"), int)
        and int(p["satisfaction"]) <= LOW_SATISFACTION_MAX
    ]
    if not rows:
        return []
    row = max(rows, key=lambda p: int(p.get("amount") or 0))
    return [
        _card(
            "low_satisfaction_high_amount",
            90,
            f"高額で満足度が低め: {row['item']} {int(row['amount']):,}円 / 満足度{row['satisfaction']}/10",
            "比較・待つ・期待確認",
            "次に似たものを買う前、値段以外に何を比べるとよさそう？",
            "同じ種類を2つ見つけて、値段以外の違いを1つメモする。",
            "一緒に候補を2つ並べて、買う前の期待を1つ確認する。",
            "買ったこと自体を責めたり、金額だけで否定したりしない。",
        )
    ]


def _repeated_small_spending_cards(purchases: list[dict]) -> list[dict]:
    by_item: dict[str, list[dict]] = defaultdict(list)
    for p in purchases:
        if int(p.get("amount") or 0) <= SMALL_AMOUNT_MAX:
            by_item[str(p.get("item") or "品目未記入")].append(p)
    candidates = [(item, rows) for item, rows in by_item.items() if len(rows) >= REPEATED_SMALL_COUNT]
    if not candidates:
        return []
    item, rows = max(candidates, key=lambda pair: (len(pair[1]), sum(int(r.get("amount") or 0) for r in pair[1])))
    total = sum(int(r.get("amount") or 0) for r in rows)
    avg_sat_values = [int(r["satisfaction"]) for r in rows if isinstance(r.get("satisfaction"), int)]
    avg_text = f" / 平均満足度{sum(avg_sat_values) / len(avg_sat_values):.1f}" if avg_sat_values else ""
    return [
        _card(
            "repeated_small_spending",
            80,
            f"少額の反復: {item} {len(rows)}回 / 合計{total:,}円{avg_text}",
            "少額反復に気づく・予算を決める",
            "次の1週間、この種類に使う上限をいくらにしてみる？",
            "次に同じものを買う前、今週の残り予算を1回だけ見る。",
            "来週分の小さな予算を本人と100円単位で決める。",
            "禁止一辺倒にせず、回数や予算を本人と決める。",
        )
    ]


def _positive_purchase_cards(purchases: list[dict], high_amount_line: int) -> list[dict]:
    rows = [
        p for p in purchases
        if isinstance(p.get("satisfaction"), int) and int(p["satisfaction"]) >= HIGH_SATISFACTION_MIN
        and (str(p.get("reason") or "").strip() or int(p.get("amount") or 0) >= high_amount_line)
    ]
    if not rows:
        return []
    row = max(rows, key=lambda p: (int(p.get("satisfaction") or 0), int(p.get("amount") or 0)))
    return [
        _card(
            "positive_planned_purchase",
            70,
            f"納得感の高い支出: {row['item']} {int(row['amount']):,}円 / 満足度{row['satisfaction']}/10",
            "よい判断の言語化",
            "今回、買ってよかった理由を1つだけ言うなら何？",
            "よかった点を1つ、次も使えそうな選び方を1つ書く。",
            "本人の言葉で、よかった判断を1つだけ確認する。",
            "ほめる時も他の子と比べず、今回の判断だけを見る。",
        )
    ]


def _saving_goal_cards(purchases: list[dict], metrics: dict) -> list[dict]:
    if not metrics.get("goal_count"):
        return []
    total = int(metrics.get("total_amount") or 0)
    fixed = int(metrics.get("fixed_allowance") or 0)
    if fixed <= 0 or total < fixed * 0.25:
        return []
    return [
        _card(
            "saving_goal_impact",
            68,
            f"目標貯金への影響: 対象期間の支出合計{total:,}円 / 月額比{metrics.get('fixed_spending_ratio')}",
            "貯金と使う金額のバランス",
            "目標のために、今月いくら残せたら納得できそう？",
            "目標用に残す金額を1つ決めて、買う前に見返す。",
            "目標までの残りと、今月残したい金額を一緒に確認する。",
            "目標のために楽しみを全部禁止する言い方はしない。",
        )
    ]


def _record_habit_cards(purchases: list[dict], metrics: dict, wallet_gap: dict | None) -> list[dict]:
    completion_rate = float(metrics.get("record_completion_rate") or 0)
    if purchases and completion_rate >= 0.7 and not wallet_gap:
        return []
    evidence = (
        f"記録完全率{int(completion_rate * 100)}% / 記録件数{metrics.get('record_count')}件"
        if purchases else "対象期間の支出記録がまだ少ない"
    )
    if wallet_gap:
        evidence += f" / 記録確認メモ{abs(int(wallet_gap.get('diff', 0))):,}円"
    return [
        _card(
            "record_habit",
            76 if wallet_gap else 60,
            evidence,
            "記録を続ける・理由と満足度を残す",
            "次に買ったあと、金額と一緒に一言だけ残すなら何が書きやすい？",
            "次の記録で、理由か満足度のどちらか1つを足す。",
            "買い物後すぐに、金額・理由・満足度のうち抜けやすい1つを一緒に確認する。",
            "記録漏れを責めず、次に残しやすい形を決める。",
        )
    ]


def _income_balance_cards(metrics: dict) -> list[dict]:
    fixed = int(metrics.get("fixed_allowance") or 0)
    total = int(metrics.get("total_amount") or 0)
    ratio = metrics.get("fixed_spending_ratio")
    if fixed <= 0 or total < fixed * 0.5:
        return []
    return [
        _card(
            "income_balance",
            66,
            f"収入と支出のバランス: 月額{fixed:,}円に対して支出{total:,}円 / 比率{ratio}",
            "収入・支出・残す金額を考える",
            "今月、使う分と残す分を分けるなら、どんな割合がよさそう？",
            "次のお小遣いをもらったら、使う分と残す分を先に2つに分ける。",
            "月額、今使った金額、残したい金額を一緒に見る。",
            "お金を増やす話を、借金やギャンブルではなく役割と継続行動に結びつける。",
        )
    ]


def _online_risk_cards(purchases: list[dict]) -> list[dict]:
    risk_terms = ("課金", "ガチャ", "サブスク", "広告", "オンライン", "ゲーム内")
    rows = [p for p in purchases if any(term in f"{p.get('item', '')} {p.get('reason', '')}" for term in risk_terms)]
    if not rows:
        return []
    row = rows[-1]
    return [
        _card(
            "online_risk",
            72,
            f"オンライン支出の注意: {row['item']} {int(row.get('amount') or 0):,}円",
            "安全確認・衝動支出の予防",
            "買う前に、1回だけ家の人に確認した方がよいポイントはどれ？",
            "オンラインで買う前に、金額と次も払う必要があるかを確認する。",
            "課金やサブスクかどうかを一緒に確認する。",
            "怖がらせず、金額・継続支払い・個人情報だけを確認する。",
        )
    ]


def _card(
    ctype: str,
    priority: int,
    evidence: str,
    skill: str,
    parent_question: str,
    child_action: str,
    parent_action: str,
    avoid: str,
) -> dict:
    card_id = ctype + "-" + hashlib.sha1(evidence.encode("utf-8")).hexdigest()[:8]
    return {
        "card_id": card_id,
        "type": ctype,
        "priority": priority,
        "evidence": evidence,
        "skill": skill,
        "parent_question": parent_question,
        "parent_action": parent_action,
        "child_action": child_action,
        "avoid": avoid,
        "policy_match": False,
        "next_observation": "次回の記録で、同じテーマが続くか、本人の行動が変わったかを見る。",
    }


def _default_card() -> dict:
    return _card(
        "record_habit",
        10,
        "支出記録がまだ少ない",
        "記録を始める",
        "次に買ったものを1つだけ一緒に記録してみる？",
        "買ったものがあったら、名前と金額を1つ記録する。",
        "次の買い物後に、まず名前と金額だけを一緒に残す。",
        "記録できていないことを責めない。",
    )


def _sanitize_card(card: dict) -> None:
    for key, value in list(card.items()):
        if not isinstance(value, str):
            continue
        text = value
        for word in UNSAFE_WORDS:
            text = text.replace(word, "記録確認")
        card[key] = text


def _pick_child_challenge(cards: list[dict], metrics: dict) -> dict:
    card = cards[0] if cards else _default_card()
    return {
        "card_id": card.get("card_id"),
        "type": card.get("type"),
        "text": card.get("child_action"),
        "action_label": "やってみること",
        "reason": "今の記録から、次に一番小さく試せる行動です。",
    }


def _build_prompt_points(cards: list[dict], metrics: dict, child_challenge: dict) -> list[str]:
    points = [
        f"記録件数{metrics.get('record_count')}件、支出合計{int(metrics.get('total_amount') or 0):,}円。",
    ]
    if metrics.get("average_satisfaction") is not None:
        points.append(f"平均満足度は{metrics['average_satisfaction']}/10。")
    for card in cards[:2]:
        points.append(
            f"{card.get('type')}: {card.get('evidence')} / 親の1問: {card.get('parent_question')} / 子どもの次アクション: {card.get('child_action')}"
        )
    if child_challenge:
        points.append(f"今回の子ども向けチャレンジ: {child_challenge.get('text')}")
    return points[:6]


def _summary_text(user_name: str, metrics: dict, cards: list[dict]) -> str:
    if not cards:
        return f"{user_name}さんは、まず支出記録を集める段階です。"
    top = cards[0]
    return (
        f"{user_name}さんの対象期間の記録は{metrics.get('record_count')}件、"
        f"支出合計は{int(metrics.get('total_amount') or 0):,}円です。"
        f"今は「{top.get('skill')}」を扱う会話が向いています。"
    )

