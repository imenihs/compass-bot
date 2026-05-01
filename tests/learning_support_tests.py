#!/usr/bin/env python3
"""Learning support feature tests.

Plain-script runner to keep this compatible with the existing test style.
"""

from __future__ import annotations

import asyncio
import json
import sys
import tempfile
import types
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import unquote

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

if "discord" not in sys.modules:
    discord_stub = types.ModuleType("discord")
    discord_stub.Client = object
    sys.modules["discord"] = discord_stub


def _write_jsonl(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")


def test_reflection_context() -> None:
    from app.reflection_context import build_reflection_context

    with tempfile.TemporaryDirectory(prefix="compass-learning-") as d:
        root = Path(d)
        log_dir = root / "logs"
        now = datetime.now(timezone.utc).isoformat()
        _write_jsonl(
            log_dir / "りか_pocket_journal.jsonl",
            [
                {"ts": now, "item": "お菓子", "amount": 180, "satisfaction": 3, "reason": "すぐ食べたくなった"},
                {"ts": now, "item": "お菓子", "amount": 160, "satisfaction": 4, "reason": "なんとなく買った"},
                {"ts": now, "item": "本", "amount": 900, "satisfaction": 9, "reason": "調べものに使えた"},
            ],
        )
        context = build_reflection_context(
            {"name": "りか"},
            {"log_dir": str(log_dir)},
            audit_state={
                "wallet_check_penalties": {
                    "りか": {"ts": now, "diff": -120, "reported": 880, "expected": 1000}
                }
            },
        )

    dashboard_text = "\n".join(context["dashboard_points"])
    prompt_text = "\n".join(context["prompt_points"])
    assert "低満足" in dashboard_text
    assert "満足度が高" in dashboard_text
    assert "記録漏れ" in dashboard_text
    assert "罰" not in dashboard_text + prompt_text
    assert "ペナルティ" not in dashboard_text + prompt_text


def test_prompt_receives_policy_and_learning_context() -> None:
    from app.prompts import build_prompt

    prompt = build_prompt(
        user_conf={
            "name": "りか",
            "age": 10,
            "gender": "female",
            "fixed_allowance": 500,
            "temporary_max": 1000,
            "ai_follow_policy": {
                "enabled": True,
                "focus_area": "income_balance",
                "nudge_strength": "light",
                "frequency": "low",
                "parent_note": "買う前に一度待つ練習を軽く促してほしい",
            },
        },
        system_conf={"currency": "JPY", "log_dir": "data/logs"},
        input_text="お小遣い増やして",
        assess_keyword="【お小遣い査定】",
        reflection_context={"prompt_points": ["低満足の支出は次回の買う前チェックにつなげる"]},
    )

    assert "AIフォロー方針: 有効" in prompt
    assert "focus_area=income_balance" in prompt
    assert "買う前に一度待つ" in prompt
    assert "低満足の支出" in prompt
    assert "そのまま引用しない" in prompt
    assert "ギャンブル" in prompt
    assert "借金" in prompt


def test_followup_policy_endpoint_writes_valid_policy_and_rejects_harmful_note() -> None:
    import app.server as server

    writes: list[tuple[str, str, object]] = []
    old_get_current_user = server._get_current_user
    old_is_admin = server._is_admin
    old_load_all_users = server.load_all_users
    old_update_user_field = server.update_user_field

    async def fake_current_user(_token: str | None) -> str:
        return "parent"

    try:
        server._get_current_user = fake_current_user
        server._is_admin = lambda username: username == "parent"
        server.load_all_users = lambda: [{"name": "りか"}]

        def fake_update_user_field(name: str, field: str, value) -> bool:
            writes.append((name, field, value))
            return True

        server.update_user_field = fake_update_user_field
        response = asyncio.run(
            server.op_followup_policy(
                session_token="token",
                target="りか",
                enabled="on",
                focus_area="income_balance",
                nudge_strength="light",
                frequency="low",
                parent_note="収入と支出のバランスを軽く意識させたい",
            )
        )
        assert response.status_code == 303
        assert writes[-1][0:2] == ("りか", "ai_follow_policy")
        saved = writes[-1][2]
        assert saved["enabled"] is True
        assert saved["focus_area"] == "income_balance"
        assert saved["parent_note"] == "収入と支出のバランスを軽く意識させたい"

        writes.clear()
        bad_response = asyncio.run(
            server.op_followup_policy(
                session_token="token",
                target="りか",
                enabled="on",
                focus_area="record_habit",
                nudge_strength="normal",
                frequency="normal",
                parent_note="兄弟と比べて厳しく叱る",
            )
        )
        assert bad_response.status_code == 303
        assert writes == []
        assert "保存できません" in unquote(bad_response.headers["location"])
    finally:
        server._get_current_user = old_get_current_user
        server._is_admin = old_is_admin
        server.load_all_users = old_load_all_users
        server.update_user_field = old_update_user_field


def test_dashboard_template_renders_policy_form() -> None:
    from jinja2 import Environment, FileSystemLoader, select_autoescape
    import app.server as server

    env = Environment(
        loader=FileSystemLoader(str(REPO_ROOT / "templates")),
        autoescape=select_autoescape(["html", "xml"]),
    )
    template = env.get_template("dashboard.html")
    html = template.render(
        username="parent",
        is_admin=True,
        users=[
            {
                "name": "りか",
                "fixed_allowance": 500,
                "balance": 1200,
                "low_balance": False,
                "month_spending": 300,
                "month_count": 2,
                "last_spent_date": "2026-05-01",
                "audit_reported": True,
                "goals": [],
                "learning_summary": {
                    "source_label": "振り返りシグナル",
                    "metrics": [{"label": "記録件数", "value": "2件"}],
                    "signals": ["低満足の支出は次回の判断材料になります。"],
                    "parent_hints": ["買う前に一度待つ声かけが向いています。"],
                },
                "ai_follow_policy": {
                    "enabled": True,
                    "focus_area": "income_balance",
                    "nudge_strength": "light",
                    "frequency": "low",
                    "parent_note": "買う前に待つ",
                },
            }
        ],
        pending_apps=[],
        follow_focus_choices=server.FOLLOW_FOCUS_CHOICES,
        follow_strength_choices=server.FOLLOW_STRENGTH_CHOICES,
        follow_frequency_choices=server.FOLLOW_FREQUENCY_CHOICES,
        flash_msg="",
        flash_error="",
    )
    assert "/compass-bot/op/followup_policy" in html
    assert 'option value="income_balance" selected' in html
    assert "買う前に待つ" in html


def main() -> int:
    tests = [
        test_reflection_context,
        test_prompt_receives_policy_and_learning_context,
        test_followup_policy_endpoint_writes_valid_policy_and_rejects_harmful_note,
        test_dashboard_template_renders_policy_form,
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
