"""
reflection_context.py — お小遣い記録から振り返り用の短い文脈を作る。

このモジュールは既存のログファイルを読むだけで、状態は変更しない。
"""

from __future__ import annotations

import json
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any


JST = timezone(timedelta(hours=9))
LOW_SATISFACTION_MAX = 4
HIGH_SATISFACTION_MIN = 8
DEFAULT_DAYS = 90
MAX_POINT_ITEMS = 3

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


def build_reflection_context(
    user_conf: dict,
    system_conf: dict,
    audit_state: dict | None = None,
    days: int = DEFAULT_DAYS,
) -> dict:
    """支出記録と財布チェック差分から、学習・振り返り用の短い文脈を返す。

    返却値:
      - summary_text: Bot応答や査定プロンプトに差し込める短い要約
      - dashboard_points: ダッシュボード向けの短い箇条書き
      - prompt_points: 生成AIに渡す観点リスト
      - stats: 集計値と抽出結果
    """
    user_name = str(user_conf.get("name", "")).strip()
    days_int = _normalize_days(days)
    now = datetime.now(JST)
    cutoff = now - timedelta(days=days_int)

    log_dir = _get_log_dir(system_conf)
    journal_rows = _load_jsonl(log_dir / f"{user_name}_pocket_journal.jsonl") if user_name else []
    window_rows = _rows_in_window(journal_rows, cutoff)

    repeated_low = _repeated_low_satisfaction(window_rows)
    high_reasoned = _high_satisfaction_with_reason(window_rows)
    richness = _record_richness(window_rows)
    satisfaction_stats = _satisfaction_stats(window_rows)
    amount_stats = _amount_stats(window_rows)
    last_entry = _last_entry_info(journal_rows, now)

    state = audit_state if isinstance(audit_state, dict) else _load_audit_state()
    wallet_gap = _wallet_gap_for_user(state, user_name, cutoff)
    recent_gap_note = _recent_gap_note(last_entry, wallet_gap)

    stats = {
        "user_name": user_name,
        "days": days_int,
        "window_start": cutoff.date().isoformat(),
        "window_end": now.date().isoformat(),
        "journal_records": len(journal_rows),
        "records_in_window": len(window_rows),
        "records_with_amount": amount_stats["records_with_amount"],
        "total_amount": amount_stats["total_amount"],
        "satisfaction_count": satisfaction_stats["count"],
        "average_satisfaction": satisfaction_stats["average"],
        "low_satisfaction_count": satisfaction_stats["low_count"],
        "high_satisfaction_count": satisfaction_stats["high_count"],
        "repeated_low_satisfaction_items": repeated_low,
        "high_satisfaction_reasoned_items": high_reasoned,
        "reason_coverage": richness["reason_coverage"],
        "satisfaction_coverage": richness["satisfaction_coverage"],
        "amount_coverage": richness["amount_coverage"],
        "richness_score": richness["score"],
        "complete_record_count": richness["complete_count"],
        "last_entry_date": last_entry["date"],
        "days_since_last_entry": last_entry["days_since"],
        "recent_record_gap_note": recent_gap_note,
        "wallet_check_gap": wallet_gap,
    }

    dashboard_points = _build_dashboard_points(
        stats=stats,
        repeated_low=repeated_low,
        high_reasoned=high_reasoned,
        richness=richness,
        recent_gap_note=recent_gap_note,
    )
    prompt_points = _build_prompt_points(
        user_name=user_name,
        stats=stats,
        repeated_low=repeated_low,
        high_reasoned=high_reasoned,
        richness=richness,
        recent_gap_note=recent_gap_note,
    )
    summary_text = _build_summary_text(
        user_name=user_name,
        stats=stats,
        repeated_low=repeated_low,
        high_reasoned=high_reasoned,
        recent_gap_note=recent_gap_note,
    )

    return {
        "summary_text": summary_text,
        "dashboard_points": dashboard_points,
        "prompt_points": prompt_points,
        "stats": stats,
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


def _load_jsonl(path: Path) -> list[dict]:
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
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(obj, dict):
                    rows.append(obj)
    except OSError:
        return []
    return rows


def _load_audit_state() -> dict:
    path = _get_repo_root() / "data" / "wallet_audit_state.json"
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


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
            picked.append(row)
    return picked


def _to_int(value: Any) -> int | None:
    if isinstance(value, bool) or value in ("", None):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value) if value.is_integer() else None
    if isinstance(value, str):
        text = value.strip().replace(",", "")
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
    if value is None:
        return None
    return max(0, value)


def _item(row: dict) -> str:
    item = str(row.get("item") or "").strip()
    return item if item else "品目未記入"


def _item_key(item: str) -> str:
    return " ".join(item.strip().lower().split())


def _has_meaningful_reason(row: dict) -> bool:
    reason = str(row.get("reason") or "").strip()
    if reason in NON_REASON_WORDS:
        return False
    return bool(reason)


def _short_text(text: Any, limit: int = 28) -> str:
    value = str(text or "").strip()
    if len(value) <= limit:
        return value
    return value[:limit] + "..."


def _format_yen(value: int | None) -> str:
    if value is None:
        return "未記録"
    return f"{value:,}円"


def _format_total_amount(stats: dict) -> str:
    if int(stats.get("records_with_amount") or 0) <= 0:
        return "未記録"
    return _format_yen(_to_int(stats.get("total_amount")) or 0)


def _format_average(value: float | None) -> str:
    return "未記録" if value is None else f"{value:.1f}/10"


def _repeated_low_satisfaction(rows: list[dict]) -> list[dict]:
    grouped: dict[str, list[dict]] = defaultdict(list)
    names: dict[str, str] = {}

    for row in rows:
        sat = _satisfaction(row)
        if sat is None or sat > LOW_SATISFACTION_MAX:
            continue
        item = _item(row)
        key = _item_key(item)
        grouped[key].append(row)
        names.setdefault(key, item)

    results: list[dict] = []
    for key, item_rows in grouped.items():
        if len(item_rows) < 2:
            continue
        scores = [_satisfaction(row) for row in item_rows]
        scores = [score for score in scores if score is not None]
        amounts = [_amount(row) for row in item_rows]
        total_amount = sum(amount for amount in amounts if amount is not None)
        dates = [_parse_ts(row.get("ts")) for row in item_rows]
        dates = [dt for dt in dates if dt is not None]
        latest_dt = max(dates) if dates else None
        results.append(
            {
                "item": names.get(key, "品目未記入"),
                "count": len(item_rows),
                "average_satisfaction": round(sum(scores) / len(scores), 1) if scores else None,
                "total_amount": total_amount,
                "last_date": latest_dt.date().isoformat() if latest_dt else None,
            }
        )

    results.sort(
        key=lambda item: (
            -int(item["count"]),
            float(item["average_satisfaction"] if item["average_satisfaction"] is not None else 99),
            item["item"],
        )
    )
    return results[:MAX_POINT_ITEMS]


def _high_satisfaction_with_reason(rows: list[dict]) -> list[dict]:
    picked: list[dict] = []
    for row in rows:
        sat = _satisfaction(row)
        if sat is None or sat < HIGH_SATISFACTION_MIN or not _has_meaningful_reason(row):
            continue
        dt = _parse_ts(row.get("ts"))
        picked.append(
            {
                "item": _item(row),
                "satisfaction": sat,
                "reason": _short_text(row.get("reason"), 36),
                "amount": _amount(row),
                "date": dt.date().isoformat() if dt else None,
                "_sort_ts": dt.timestamp() if dt else 0,
            }
        )

    picked.sort(
        key=lambda item: (
            -int(item["satisfaction"]),
            -float(item["_sort_ts"]),
            item["item"],
        )
    )
    for item in picked:
        item.pop("_sort_ts", None)
    return picked[:MAX_POINT_ITEMS]


def _record_richness(rows: list[dict]) -> dict:
    count = len(rows)
    if count == 0:
        return {
            "score": 0,
            "reason_coverage": 0.0,
            "satisfaction_coverage": 0.0,
            "amount_coverage": 0.0,
            "item_coverage": 0.0,
            "complete_count": 0,
            "record_count": 0,
        }

    reason_count = sum(1 for row in rows if _has_meaningful_reason(row))
    satisfaction_count = sum(1 for row in rows if _satisfaction(row) is not None)
    amount_count = sum(1 for row in rows if _amount(row) is not None)
    item_count = sum(1 for row in rows if bool(str(row.get("item") or "").strip()))
    complete_count = sum(
        1
        for row in rows
        if _has_meaningful_reason(row)
        and _satisfaction(row) is not None
        and _amount(row) is not None
        and bool(str(row.get("item") or "").strip())
    )

    reason_coverage = reason_count / count
    satisfaction_coverage = satisfaction_count / count
    amount_coverage = amount_count / count
    item_coverage = item_count / count
    score = round(
        (
            reason_coverage * 0.35
            + satisfaction_coverage * 0.35
            + amount_coverage * 0.20
            + item_coverage * 0.10
        )
        * 100
    )

    return {
        "score": score,
        "reason_coverage": round(reason_coverage, 3),
        "satisfaction_coverage": round(satisfaction_coverage, 3),
        "amount_coverage": round(amount_coverage, 3),
        "item_coverage": round(item_coverage, 3),
        "complete_count": complete_count,
        "record_count": count,
    }


def _satisfaction_stats(rows: list[dict]) -> dict:
    scores = [_satisfaction(row) for row in rows]
    scores = [score for score in scores if score is not None]
    if not scores:
        average = None
    else:
        average = round(sum(scores) / len(scores), 1)
    return {
        "count": len(scores),
        "average": average,
        "low_count": sum(1 for score in scores if score <= LOW_SATISFACTION_MAX),
        "high_count": sum(1 for score in scores if score >= HIGH_SATISFACTION_MIN),
    }


def _amount_stats(rows: list[dict]) -> dict:
    amounts = [_amount(row) for row in rows]
    amounts = [amount for amount in amounts if amount is not None]
    return {
        "records_with_amount": len(amounts),
        "total_amount": sum(amounts),
    }


def _last_entry_info(rows: list[dict], now: datetime) -> dict:
    dated = [(_parse_ts(row.get("ts")), row) for row in rows]
    dated = [(dt, row) for dt, row in dated if dt is not None]
    if not dated:
        return {"date": None, "days_since": None}
    latest_dt = max(dt for dt, _ in dated)
    return {
        "date": latest_dt.date().isoformat(),
        "days_since": max(0, (now.date() - latest_dt.date()).days),
    }


def _wallet_gap_for_user(audit_state: dict, user_name: str, cutoff: datetime) -> dict | None:
    if not user_name:
        return None
    penalties = audit_state.get("wallet_check_penalties", {})
    if not isinstance(penalties, dict):
        return None
    note = penalties.get(user_name)
    if not isinstance(note, dict):
        return None

    dt = _parse_ts(note.get("ts"))
    if dt is not None and dt < cutoff:
        return None

    diff = _to_int(note.get("diff"))
    if diff is None or diff == 0:
        return None

    reported = _to_int(note.get("reported"))
    expected = _to_int(note.get("expected"))
    if diff < 0:
        kind = "spending_record_gap"
        memo = (
            f"財布チェックで支出の記録漏れがありそうでした（差額 {abs(diff):,}円）。"
            "次は買った直後に品目だけでも残すと、あとで思い出しやすくなります。"
        )
    else:
        kind = "income_record_gap"
        memo = (
            f"財布チェックで入金の記録漏れがありそうでした（差額 {abs(diff):,}円）。"
            "もらったお金も短く残すと、帳簿と財布を合わせやすくなります。"
        )

    return {
        "kind": kind,
        "diff": diff,
        "abs_diff": abs(diff),
        "reported": reported,
        "expected": expected,
        "date": dt.date().isoformat() if dt else None,
        "memo": memo,
    }


def _recent_gap_note(last_entry: dict, wallet_gap: dict | None) -> str | None:
    if wallet_gap:
        return str(wallet_gap["memo"])

    days_since = last_entry.get("days_since")
    if isinstance(days_since, int) and days_since >= 14:
        return (
            f"最後の支出記録から{days_since}日空いています。"
            "最近買ったものを1つだけ思い出して書くところから再開できます。"
        )
    return None


def _build_dashboard_points(
    stats: dict,
    repeated_low: list[dict],
    high_reasoned: list[dict],
    richness: dict,
    recent_gap_note: str | None,
) -> list[str]:
    points = [
        (
            f"過去{stats['days']}日: {stats['records_in_window']}件 / "
            f"合計 {_format_total_amount(stats)} / "
            f"平均満足度 {_format_average(stats['average_satisfaction'])}"
        )
    ]

    if repeated_low:
        item = repeated_low[0]
        points.append(
            f"低満足が続いた支出: {item['item']} {item['count']}回"
            f"（平均 {_format_average(item['average_satisfaction'])}）"
        )
    else:
        points.append("低満足が繰り返し出ている品目はまだ目立ちません。")

    if high_reasoned:
        item = high_reasoned[0]
        points.append(
            f"満足度が高く理由もある支出: {item['item']}"
            f"（{item['satisfaction']}/10、{item['reason']}）"
        )
    else:
        points.append("満足度が高い理由つき記録は、これから増やせます。")

    points.append(
        f"記録の充実度: {richness['score']}%"
        f"（理由 {stats['reason_coverage']:.0%} / 満足度 {stats['satisfaction_coverage']:.0%} / "
        f"金額 {stats['amount_coverage']:.0%}）"
    )

    if recent_gap_note:
        points.append(recent_gap_note)

    return points


def _build_prompt_points(
    user_name: str,
    stats: dict,
    repeated_low: list[dict],
    high_reasoned: list[dict],
    richness: dict,
    recent_gap_note: str | None,
) -> list[str]:
    subject = f"{user_name}さん" if user_name else "この子"
    points = [
        (
            f"{subject}の過去{stats['days']}日の記録は{stats['records_in_window']}件。"
            "記録の量が少ない場合は、まず1件を一緒に思い出す声かけにする。"
        )
    ]

    if repeated_low:
        items = "、".join(item["item"] for item in repeated_low)
        points.append(
            f"低満足が続いた「{items}」は責めずに扱い、次に買う前の確認ポイントを一緒に考える。"
        )

    if high_reasoned:
        items = "、".join(item["item"] for item in high_reasoned)
        points.append(
            f"満足度が高く理由も書けた「{items}」は、本人に合うお金の使い方のヒントとして扱う。"
        )

    if richness["score"] < 70:
        points.append(
            "記録は品目だけでも価値があると伝えつつ、理由か満足度を1つ足す提案にする。"
        )
    else:
        points.append("理由・満足度・金額がそろっている記録を、次の判断材料として肯定的に使う。")

    if recent_gap_note:
        points.append(
            "財布チェックや記録の空白は、次の記録方法を決めるための振り返り材料として扱う。"
        )

    return points


def _build_summary_text(
    user_name: str,
    stats: dict,
    repeated_low: list[dict],
    high_reasoned: list[dict],
    recent_gap_note: str | None,
) -> str:
    subject = f"{user_name}さん" if user_name else "この子"
    count = int(stats["records_in_window"])
    if count == 0:
        base = (
            f"{subject}の過去{stats['days']}日の支出記録はまだありません。"
            "まずは買ったものを1つ残すだけでも、次のお金の使い方を考える材料になります。"
        )
        return base + (f" {recent_gap_note}" if recent_gap_note else "")

    total_amount_text = _format_total_amount(stats)
    if total_amount_text == "未記録":
        sentences = [f"{subject}は過去{stats['days']}日で{count}件の支出記録があります。"]
    else:
        sentences = [
            (
                f"{subject}は過去{stats['days']}日で{count}件、"
                f"合計{total_amount_text}を記録しています。"
            )
        ]

    if stats["average_satisfaction"] is not None:
        sentences.append(f"平均満足度は{stats['average_satisfaction']:.1f}/10です。")

    if repeated_low:
        item = repeated_low[0]
        sentences.append(
            f"「{item['item']}」は低めの満足度が{item['count']}回あり、次の買い方を考える材料になります。"
        )

    if high_reasoned:
        item = high_reasoned[0]
        sentences.append(
            f"一方で「{item['item']}」は満足度{item['satisfaction']}/10で理由も残っており、"
            "本人に合う使い方のヒントです。"
        )

    sentences.append(f"記録の充実度は{stats['richness_score']}%です。")

    if recent_gap_note:
        sentences.append(recent_gap_note)

    return "".join(sentences)
