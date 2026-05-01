#!/usr/bin/env python3
"""Learning insights engine tests."""

from __future__ import annotations

import json
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


def _write_jsonl(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")


def _metric(result: dict, key: str) -> dict:
    for metric in result["metrics"]:
        if metric.get("key") == key:
            return metric
    raise AssertionError(f"metric not found: {key}")


def _card_types(result: dict) -> list[str]:
    return [card["type"] for card in result["insight_cards"]]


def _build(
    rows: list[dict],
    user_conf: dict | None = None,
    ledger_rows: list[dict] | None = None,
    days: int = 90,
    audit_state: dict | None = None,
) -> dict:
    from app.learning_insights import build_learning_insights

    with tempfile.TemporaryDirectory(prefix="compass-insights-") as d:
        root = Path(d)
        log_dir = root / "logs"
        conf = {
            "name": "りか",
            "fixed_allowance": 1000,
            "ai_follow_policy": {"enabled": True, "focus_area": "satisfaction_reflection"},
        }
        if user_conf:
            conf.update(user_conf)
        _write_jsonl(log_dir / "りか_pocket_journal.jsonl", rows)
        if ledger_rows is not None:
            _write_jsonl(log_dir / "りか_wallet_ledger.jsonl", ledger_rows)
        return build_learning_insights(conf, {"log_dir": str(log_dir)}, audit_state=audit_state, days=days)


def test_supplement_records_are_merged_as_purchase_units() -> None:
    now = datetime.now(timezone.utc)
    rows = [
        {
            "entry_id": "purchase-1",
            "ts": now.isoformat(),
            "item": "お菓子",
            "amount": 180,
            "reason": "",
            "satisfaction": None,
        },
        {
            "ts": (now + timedelta(minutes=5)).isoformat(),
            "action": "expense_supplement",
            "parent_entry_id": "purchase-1",
            "item": "お菓子",
            "amount": None,
            "reason": "すぐ食べたくなった",
            "satisfaction": 3,
        },
        {
            "ts": (now + timedelta(minutes=8)).isoformat(),
            "item": "お菓子",
            "amount": 160,
            "reason": "なんとなく買った",
            "satisfaction": 4,
        },
    ]

    result = _build(rows, {"ai_follow_policy": {"enabled": True, "focus_area": "impulse_spending"}})

    assert _metric(result, "records")["raw_value"] == 2
    assert _metric(result, "total_amount")["raw_value"] == 340
    assert _metric(result, "top_category")["raw_value"]["おやつ"] == 340
    assert "repeated_small_spending" in _card_types(result)
    assert any("補足レコード1件" in note for note in result["source_notes"])


def test_distinguishes_low_high_amount_and_positive_high_satisfaction_cards() -> None:
    now = datetime.now(timezone.utc)
    low_result = _build(
        [
            {
                "ts": now.isoformat(),
                "item": "ゲームソフト",
                "amount": 1800,
                "reason": "急いで選んだ",
                "satisfaction": 2,
            }
        ],
        {"ai_follow_policy": {"enabled": True, "focus_area": "satisfaction_reflection"}},
    )
    positive_result = _build(
        [
            {
                "ts": now.isoformat(),
                "item": "参考書",
                "amount": 1800,
                "reason": "調べものに使う予定だった",
                "satisfaction": 9,
            }
        ],
        {"ai_follow_policy": {"enabled": True, "focus_area": "satisfaction_reflection"}},
    )

    assert low_result["insight_cards"][0]["type"] == "low_satisfaction_high_amount"
    assert low_result["insight_cards"][0]["evidence"]["satisfaction_band"] == "low"
    assert positive_result["insight_cards"][0]["type"] == "positive_planned_purchase"
    assert positive_result["insight_cards"][0]["evidence"]["satisfaction_band"] == "high"
    assert positive_result["insight_cards"][0]["evidence"]["amount_band"] == "high"


def test_focus_area_changes_card_priority() -> None:
    now = datetime.now(timezone.utc)
    rows = [
        {"ts": now.isoformat(), "item": "お菓子", "amount": 220, "reason": "つい買った", "satisfaction": 3},
        {
            "ts": (now + timedelta(minutes=3)).isoformat(),
            "item": "お菓子",
            "amount": 240,
            "reason": "目についた",
            "satisfaction": 4,
        },
        {
            "ts": (now + timedelta(minutes=6)).isoformat(),
            "item": "文具",
            "amount": 350,
            "reason": "学校で使う",
            "satisfaction": 8,
        },
    ]

    impulse = _build(rows, {"ai_follow_policy": {"enabled": True, "focus_area": "impulse_spending"}})
    income = _build(rows, {"ai_follow_policy": {"enabled": True, "focus_area": "income_balance"}})

    assert impulse["insight_cards"][0]["type"] == "repeated_small_spending"
    assert impulse["insight_cards"][0]["policy_match"] is True
    assert income["insight_cards"][0]["type"] == "income_balance"
    assert income["insight_cards"][0]["policy_match"] is True


def test_saving_goal_and_record_habit_cards_are_distinct() -> None:
    now = datetime.now(timezone.utc)
    saving = _build(
        [
            {
                "ts": now.isoformat(),
                "item": "文具",
                "amount": 300,
                "reason": "学校で使う",
                "satisfaction": 7,
            }
        ],
        {
            "ai_follow_policy": {"enabled": True, "focus_area": "saving_goal"},
            "savings_goals": [{"title": "ゲーム機", "target_amount": 30000, "current": 5000}],
        },
    )
    record = _build(
        [
            {
                "ts": now.isoformat(),
                "item": "消しゴム",
                "amount": 120,
                "reason": "",
                "satisfaction": None,
            }
        ],
        {"ai_follow_policy": {"enabled": True, "focus_area": "record_habit"}},
    )

    assert saving["insight_cards"][0]["type"] == "saving_goal_impact"
    assert saving["insight_cards"][0]["evidence"]["goal_title"] == "ゲーム機"
    assert record["insight_cards"][0]["type"] == "record_habit"
    assert record["insight_cards"][0]["evidence"]["completion_rate"] < 1


def test_parent_question_child_action_and_safe_language() -> None:
    now = datetime.now(timezone.utc)
    result = _build(
        [
            {"ts": now.isoformat(), "item": "お菓子", "amount": 220, "reason": "つい買った", "satisfaction": 3},
            {
                "ts": (now + timedelta(minutes=2)).isoformat(),
                "item": "お菓子",
                "amount": 240,
                "reason": "目についた",
                "satisfaction": 4,
            },
        ],
        {
            "ai_follow_policy": {"enabled": True, "focus_area": "impulse_spending"},
            "parent_followup_note": "内部メモは子どもに見せない",
        },
    )

    assert result["insight_cards"]
    for card in result["insight_cards"]:
        question = card["parent_question"]
        assert question.count("？") + question.count("?") == 1

    challenge = result["child_challenge"]
    assert isinstance(challenge["action"], str)
    assert challenge["action"].count("。") == 1

    output_text = json.dumps(result, ensure_ascii=False)
    forbidden = [
        "危険",
        "罰",
        "ペナルティ",
        "叱",
        "禁止",
        "兄弟",
        "人格",
        "ギャンブル",
        "借金",
        "高リスク投機",
        "内部メモ",
    ]
    assert not any(word in output_text for word in forbidden)


def test_learning_support_state_suppresses_recent_card_type() -> None:
    now = datetime.now(timezone.utc)
    rows = [
        {"ts": now.isoformat(), "item": "お菓子", "amount": 220, "reason": "つい買った", "satisfaction": 3},
        {
            "ts": (now + timedelta(minutes=2)).isoformat(),
            "item": "お菓子",
            "amount": 240,
            "reason": "目についた",
            "satisfaction": 4,
        },
        {
            "ts": (now + timedelta(minutes=4)).isoformat(),
            "item": "ノート",
            "amount": 120,
            "reason": "",
            "satisfaction": None,
        },
    ]
    result = _build(
        rows,
        {"ai_follow_policy": {"enabled": True, "focus_area": "impulse_spending"}},
        audit_state={
            "learning_support_state": {
                "suppressed_card_types": [
                    {
                        "card_type": "repeated_small_spending",
                        "ts": now.isoformat(),
                    }
                ]
            }
        },
    )

    assert "repeated_small_spending" not in _card_types(result)
    assert result["insight_cards"][0]["type"] == "record_habit"


def main() -> int:
    tests = [
        test_supplement_records_are_merged_as_purchase_units,
        test_distinguishes_low_high_amount_and_positive_high_satisfaction_cards,
        test_focus_area_changes_card_priority,
        test_saving_goal_and_record_habit_cards_are_distinct,
        test_parent_question_child_action_and_safe_language,
        test_learning_support_state_suppresses_recent_card_type,
    ]
    failures = []
    for test in tests:
        try:
            test()
            print(json.dumps({"test": test.__name__, "passed": True}, ensure_ascii=False), flush=True)
        except Exception as exc:
            failures.append(test.__name__)
            print(
                json.dumps(
                    {"test": test.__name__, "passed": False, "error": f"{type(exc).__name__}: {exc}"},
                    ensure_ascii=False,
                ),
                flush=True,
            )
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
