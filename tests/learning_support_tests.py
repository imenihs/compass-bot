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
                "learning_insights": {
                    "source": "learning_insights",
                    "summary_text": "今週は買う前に待つ練習が向いています。",
                    "metrics": [{"label": "記録件数", "value": "2件"}],
                    "insight_cards": [
                        {
                            "card_id": "card-1",
                            "type": "repeated_small_spending",
                            "title": "少額の反復支出",
                            "evidence_lines": ["お菓子 3回"],
                            "skill": "待つ",
                            "parent_question": "次に買う前に一回待ってみる？",
                            "parent_action": "予算を一緒に決める",
                            "child_action": "買う前に残り予算を見る",
                            "avoid": "叱責しない",
                            "policy_match": "方針に合っています",
                            "next_observation": "次の記録で見る",
                        }
                    ],
                },
                "active_growth_plan": None,
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
    assert "/compass-bot/op/learning_card_feedback" in html
    assert "/compass-bot/op/growth_plan" in html
    assert "今週の会話カード" in html
    assert 'option value="income_balance" selected' in html
    assert "買う前に待つ" in html


def test_learning_card_and_growth_plan_endpoints_write_isolated_state() -> None:
    import app.server as server

    old_get_current_user = server._get_current_user
    old_is_admin = server._is_admin
    old_load_all_users = server.load_all_users
    old_state_dir = server.LEARNING_SUPPORT_STATE_DIR
    old_plan_dir = server.GROWTH_PLANS_DIR

    async def fake_current_user(_token: str | None) -> str:
        return "parent"

    with tempfile.TemporaryDirectory(prefix="compass-web-state-") as d:
        root = Path(d)
        try:
            server._get_current_user = fake_current_user
            server._is_admin = lambda username: username == "parent"
            server.load_all_users = lambda: [{"name": "りか"}]
            server.LEARNING_SUPPORT_STATE_DIR = root / "learning_support_state"
            server.GROWTH_PLANS_DIR = root / "growth_plans"

            card_response = asyncio.run(
                server.op_learning_card_feedback(
                    session_token="token",
                    target="りか",
                    card_id="card-1",
                    feedback="use_this_week",
                    card_type="record_habit",
                    parent_question="次の買い物で一言理由を足す？",
                    child_action="理由を一言書く",
                )
            )
            assert card_response.status_code == 303
            state_files = list((root / "learning_support_state").glob("*.json"))
            assert len(state_files) == 1
            state = json.loads(state_files[0].read_text(encoding="utf-8"))
            assert state["last_card_id"] == "card-1"
            assert state["last_parent_question"] == "次の買い物で一言理由を足す？"

            plan_response = asyncio.run(
                server.op_growth_plan(
                    session_token="token",
                    target="りか",
                    plan_id="",
                    action="save",
                    status="active",
                    request_type="allowance_increase",
                    child_reason="本を買いたい",
                    parent_condition="1週間記録する",
                    agreed_action="毎日1つ記録する",
                    review_at="2026-05-10",
                    reward_amount="100",
                    notes="親確認済み",
                )
            )
            assert plan_response.status_code == 303
            plan_files = list((root / "growth_plans").glob("*.json"))
            assert len(plan_files) == 1
            plans = json.loads(plan_files[0].read_text(encoding="utf-8"))
            assert plans["plans"][0]["agreed_action"] == "毎日1つ記録する"
            assert plans["plans"][0]["reward_amount"] == 100
        finally:
            server._get_current_user = old_get_current_user
            server._is_admin = old_is_admin
            server.load_all_users = old_load_all_users
            server.LEARNING_SUPPORT_STATE_DIR = old_state_dir
            server.GROWTH_PLANS_DIR = old_plan_dir


def test_child_challenge_feedback_and_template_do_not_expose_parent_policy() -> None:
    from jinja2 import Environment, FileSystemLoader, select_autoescape
    import app.server as server

    old_get_current_user = server._get_current_user
    old_is_admin = server._is_admin
    old_load_all_users = server.load_all_users
    old_state_dir = server.LEARNING_SUPPORT_STATE_DIR
    old_build_learning_insights = server.build_learning_insights

    async def fake_current_user(_token: str | None) -> str:
        return "りか"

    with tempfile.TemporaryDirectory(prefix="compass-child-state-") as d:
        root = Path(d)
        try:
            server._get_current_user = fake_current_user
            server._is_admin = lambda username: False
            server.load_all_users = lambda: [{"name": "りか"}]
            server.LEARNING_SUPPORT_STATE_DIR = root / "learning_support_state"

            def fake_build_learning_insights(**_kwargs) -> dict:
                return {
                    "summary_text": "テスト",
                    "metrics": [],
                    "insight_cards": [],
                    "child_challenge": {
                        "challenge_id": "challenge-1",
                        "title": "親メモからのチャレンジ",
                        "action": "買う前に一度待つ練習を軽く促してほしい",
                    },
                    "source_notes": [],
                }

            server.build_learning_insights = fake_build_learning_insights
            insights = server._normalize_learning_insights(
                "りか",
                {"log_dir": str(root / "logs")},
                {
                    "name": "りか",
                    "ai_follow_policy": {
                        "enabled": True,
                        "parent_note": "買う前に一度待つ練習を軽く促してほしい",
                    },
                },
                [],
                {},
            )
            assert "親メモ" not in insights["child_challenge"]["title"]
            assert "買う前に一度待つ練習" not in insights["child_challenge"]["action"]

            response = asyncio.run(
                server.op_child_challenge_feedback(
                    session_token="token",
                    challenge_id="challenge-1",
                    feedback="done",
                    target="",
                    child_action="理由を一言書く",
                )
            )
            assert response.status_code == 303
            state_files = list((root / "learning_support_state").glob("*.json"))
            assert len(state_files) == 1
            state_text = state_files[0].read_text(encoding="utf-8")
            assert "parent_note" not in state_text
            assert "ai_follow_policy" not in state_text

            env = Environment(
                loader=FileSystemLoader(str(REPO_ROOT / "templates")),
                autoescape=select_autoescape(["html", "xml"]),
            )
            template = env.get_template("dashboard.html")
            html = template.render(
                username="りか",
                is_admin=False,
                my_child_challenge={
                    "challenge_id": "challenge-1",
                    "title": "記録に一言足す",
                    "action": "理由を一言書く",
                    "expected_time": "5分以内",
                },
                my_learning_summary={
                    "signals": ["親メモ: 買う前に一度待つ練習を軽く促してほしい"],
                    "child_hints": ["内部方針"],
                },
                my_balance=1200,
                my_low_balance=False,
                my_goals=[],
                my_month_spending=300,
                my_month_count=2,
                my_recent_items=[],
                flash_msg="",
                flash_error="",
            )
            assert "次の小さなチャレンジ" in html
            assert "買う前に一度待つ練習" not in html
            assert "内部方針" not in html
            assert "/compass-bot/op/followup_policy" not in html
        finally:
            server._get_current_user = old_get_current_user
            server._is_admin = old_is_admin
            server.load_all_users = old_load_all_users
            server.LEARNING_SUPPORT_STATE_DIR = old_state_dir
            server.build_learning_insights = old_build_learning_insights


def test_server_unhandled_exception_handler_logs_and_hides_details() -> None:
    import app.server as server

    with tempfile.TemporaryDirectory(prefix="compass-server-error-") as d:
        root = Path(d)
        log_dir = root / "logs"
        old_load_system = server.load_system
        old_get_log_dir = server.get_log_dir
        try:
            server.load_system = lambda: {"log_dir": str(log_dir)}
            server.get_log_dir = lambda system_conf: Path(system_conf["log_dir"])
            request = types.SimpleNamespace(
                method="GET",
                url=types.SimpleNamespace(path="/compass-bot/dashboard"),
                client=types.SimpleNamespace(host="127.0.0.1"),
                headers={"accept": "application/json"},
            )
            response = asyncio.run(
                server.unhandled_exception_handler(
                    request,
                    RuntimeError("simulated web secret"),
                )
            )
            body = response.body.decode("utf-8")
            assert response.status_code == 500
            assert "管理者に連絡" in body
            assert "待っても直らない" in body
            assert "simulated web secret" not in body
            diagnostics_path = log_dir / "runtime_diagnostics.jsonl"
            rows = [
                json.loads(line)
                for line in diagnostics_path.read_text(encoding="utf-8").splitlines()
                if line.strip()
            ]
            assert any(
                row.get("event") == "server_unhandled_error"
                and row.get("path") == "/compass-bot/dashboard"
                and row.get("error_type") == "RuntimeError"
                and row.get("error_message") == "simulated web secret"
                for row in rows
            )
        finally:
            server.load_system = old_load_system
            server.get_log_dir = old_get_log_dir


def test_web_adjust_rejects_invalid_direction_without_balance_change() -> None:
    import app.server as server
    from app.wallet_service import WalletService

    with tempfile.TemporaryDirectory(prefix="compass-adjust-") as d:
        root = Path(d)
        wallet = WalletService()
        wallet.wallet_state_path = root / "wallet_state.json"
        wallet.wallet_audit_state_path = root / "wallet_audit_state.json"
        wallet.set_balance("りか", 1000)

        old_get_current_user = server._get_current_user
        old_is_admin = server._is_admin
        old_wallet_service = server._wallet_service
        old_find_user_by_name = server.find_user_by_name
        old_load_system = server.load_system
        try:
            async def fake_current_user(_token: str | None) -> str:
                return "parent"

            server._get_current_user = fake_current_user
            server._is_admin = lambda username: username == "parent"
            server._wallet_service = wallet
            server.find_user_by_name = lambda name: {"name": name} if name == "りか" else None
            server.load_system = lambda: {"log_dir": str(root / "logs")}

            response = asyncio.run(
                server.op_adjust(
                    session_token="token",
                    target="りか",
                    amount="500円",
                    direction="bad",
                )
            )
            assert response.status_code == 303
            assert "増減の指定が正しくありません" in unquote(response.headers["location"])
            assert wallet.get_balance("りか") == 1000
        finally:
            server._get_current_user = old_get_current_user
            server._is_admin = old_is_admin
            server._wallet_service = old_wallet_service
            server.find_user_by_name = old_find_user_by_name
            server.load_system = old_load_system


def test_wallet_state_corruption_fails_closed() -> None:
    from app.wallet_service import WalletService

    with tempfile.TemporaryDirectory(prefix="compass-wallet-corrupt-") as d:
        root = Path(d)
        wallet = WalletService()
        wallet.wallet_state_path = root / "wallet_state.json"
        wallet.wallet_audit_state_path = root / "wallet_audit_state.json"
        wallet.wallet_state_path.write_text("{broken", encoding="utf-8")

        try:
            wallet.update_balance(
                user_conf={"name": "りか"},
                system_conf={"log_dir": str(root / "logs")},
                delta=500,
                action="test",
            )
        except RuntimeError:
            pass
        else:
            raise AssertionError("corrupt wallet_state.json should fail closed")

        assert wallet.wallet_state_path.read_text(encoding="utf-8") == "{broken"
        assert not (root / "logs" / "りか_wallet_ledger.jsonl").exists()


def test_intent_normalizer_rejects_bad_schema() -> None:
    from app.intent_normalizer import normalize_intent

    class BadSchemaGemini:
        async def call_silent(self, _prompt: str) -> str:
            return '{"intent":"balance_check","entities":["bad"],"confidence":"maybe"}'

    result = asyncio.run(normalize_intent("残高おしえて", BadSchemaGemini()))
    assert result["intent"] == "balance_check"
    assert isinstance(result["entities"], dict)
    assert result["confidence"] == "high"


def main() -> int:
    tests = [
        test_reflection_context,
        test_prompt_receives_policy_and_learning_context,
        test_followup_policy_endpoint_writes_valid_policy_and_rejects_harmful_note,
        test_dashboard_template_renders_policy_form,
        test_learning_card_and_growth_plan_endpoints_write_isolated_state,
        test_child_challenge_feedback_and_template_do_not_expose_parent_policy,
        test_server_unhandled_exception_handler_logs_and_hides_details,
        test_web_adjust_rejects_invalid_direction_without_balance_change,
        test_wallet_state_corruption_fails_closed,
        test_intent_normalizer_rejects_bad_schema,
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
