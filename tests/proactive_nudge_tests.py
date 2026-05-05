#!/usr/bin/env python3
"""能動伴走メッセージのテスト。"""

from __future__ import annotations

import asyncio
import json
import sys
import tempfile
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


JST = timezone(timedelta(hours=9))


@dataclass
class FakeMember:
    id: int


class FakeChannel:
    def __init__(self, channel_id: int, members: list[FakeMember]):
        self.id = channel_id
        self.members = members
        self.outputs: list[str] = []

    async def send(self, text: str) -> None:
        self.outputs.append(text)


class FakeClient:
    def __init__(self, channel: FakeChannel):
        self.channel = channel

    def get_channel(self, channel_id: int):
        return self.channel if int(channel_id) == int(self.channel.id) else None

    async def fetch_channel(self, channel_id: int):
        return self.get_channel(channel_id)


def _write_jsonl(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")


def _write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")


def _read_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    rows = []
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                rows.append(json.loads(line))
    return rows


def _service(tmp: Path, users: list[dict], channel: FakeChannel):
    from app.reminder_service import ReminderService
    from app.wallet_service import WalletService

    service = ReminderService(
        client=FakeClient(channel),
        allowance_reminder_conf={},
        wallet_audit_conf={},
        load_all_users_fn=lambda: users,
        wallet_service=WalletService(),
        allow_channel_ids={channel.id},
        proactive_child_nudge_conf={
            "enabled": True,
            "notify_time": "18:30",
            "no_record_days": 10,
            "challenge_stale_days": 5,
            "growth_plan_review_days_before": 2,
            "min_days_between_nudges": 3,
            "max_per_run": 20,
        },
    )
    service.reminder_state_path = tmp / "data" / "reminder_state.json"
    service.learning_support_state_dir = tmp / "data" / "learning_support_state"
    service.growth_plans_dir = tmp / "data" / "growth_plans"
    return service


async def test_no_recent_record_sends_gentle_nudge() -> None:
    with tempfile.TemporaryDirectory(prefix="compass-proactive-") as d:
        tmp = Path(d)
        now = datetime(2026, 5, 2, 18, 30, tzinfo=JST)
        user = {"name": "りか", "discord_user_id": 101, "age": 10}
        channel = FakeChannel(1, [FakeMember(101)])
        service = _service(tmp, [user], channel)

        await service.send_proactive_child_nudges(log_dir=tmp / "logs", now=now)

        assert len(channel.outputs) == 1
        text = channel.outputs[0]
        assert "支出記録" in text
        assert "放置" not in text
        assert "ペナルティ" not in text


async def test_recent_record_does_not_send() -> None:
    with tempfile.TemporaryDirectory(prefix="compass-proactive-") as d:
        tmp = Path(d)
        now = datetime(2026, 5, 2, 18, 30, tzinfo=JST)
        user = {"name": "りか", "discord_user_id": 101, "age": 10}
        channel = FakeChannel(1, [FakeMember(101)])
        service = _service(tmp, [user], channel)
        _write_jsonl(
            tmp / "logs" / "りか_pocket_journal.jsonl",
            [{"ts": (now - timedelta(days=1)).isoformat(), "item": "ノート", "amount": 120}],
        )

        await service.send_proactive_child_nudges(log_dir=tmp / "logs", now=now)

        assert channel.outputs == []


async def test_stale_challenge_takes_priority_and_rate_limits() -> None:
    with tempfile.TemporaryDirectory(prefix="compass-proactive-") as d:
        tmp = Path(d)
        now = datetime(2026, 5, 2, 18, 30, tzinfo=JST)
        user = {"name": "りか", "discord_user_id": 101, "age": 10}
        channel = FakeChannel(1, [FakeMember(101)])
        service = _service(tmp, [user], channel)
        _write_jsonl(
            tmp / "logs" / "りか_pocket_journal.jsonl",
            [{"ts": (now - timedelta(days=1)).isoformat(), "item": "ノート", "amount": 120}],
        )
        _write_json(
            service.learning_support_state_dir / f"{service._user_key(user)}.json",
            {
                "last_nudge_at": (now - timedelta(days=6)).isoformat(),
                "last_child_action": "次の買い物で理由を1つ書く。",
            },
        )

        first = await service.send_proactive_child_nudges(log_dir=tmp / "logs", now=now)
        second = await service.send_proactive_child_nudges(log_dir=tmp / "logs", now=now + timedelta(minutes=1))

        assert first == 1
        assert second == 0
        assert len(channel.outputs) == 1
        assert "チャレンジ" in channel.outputs[0]


async def test_growth_plan_review_nudge() -> None:
    with tempfile.TemporaryDirectory(prefix="compass-proactive-") as d:
        tmp = Path(d)
        now = datetime(2026, 5, 2, 18, 30, tzinfo=JST)
        user = {"name": "りか", "discord_user_id": 101, "age": 10}
        channel = FakeChannel(1, [FakeMember(101)])
        service = _service(tmp, [user], channel)
        _write_jsonl(
            tmp / "logs" / "りか_pocket_journal.jsonl",
            [{"ts": (now - timedelta(days=1)).isoformat(), "item": "ノート", "amount": 120}],
        )
        _write_json(
            service.growth_plans_dir / f"{service._user_key(user)}.json",
            {
                "plans": [
                    {
                        "plan_id": "p1",
                        "status": "active",
                        "agreed_action": "毎週の記録",
                        "review_at": "2026-05-03",
                    }
                ]
            },
        )

        await service.send_proactive_child_nudges(log_dir=tmp / "logs", now=now)

        assert len(channel.outputs) == 1
        assert "毎週の記録" in channel.outputs[0]
        assert "確認日" in channel.outputs[0]


async def test_maybe_send_runs_after_scheduled_time_once_per_day() -> None:
    with tempfile.TemporaryDirectory(prefix="compass-proactive-") as d:
        tmp = Path(d)
        log_dir = tmp / "logs"
        user = {"name": "りか", "discord_user_id": 101, "age": 10}
        channel = FakeChannel(1, [FakeMember(101)])
        service = _service(tmp, [user], channel)

        import app.config as app_config

        original_load_system = app_config.load_system
        original_get_log_dir = app_config.get_log_dir
        app_config.load_system = lambda: {"log_dir": str(log_dir)}
        app_config.get_log_dir = lambda system_conf: Path(system_conf["log_dir"])
        try:
            await service.maybe_send_proactive_child_nudges(
                now=datetime(2026, 5, 2, 18, 20, tzinfo=JST)
            )
            assert channel.outputs == []
            assert "proactive_child_nudge_last_run_at" not in service._load_reminder_state()

            await service.maybe_send_proactive_child_nudges(
                now=datetime(2026, 5, 2, 18, 35, tzinfo=JST)
            )
            assert len(channel.outputs) == 1
            state = service._load_reminder_state()
            assert state["proactive_child_nudge_last_run_at"] == "2026-05-02T18:35:00+09:00"

            await service.maybe_send_proactive_child_nudges(
                now=datetime(2026, 5, 2, 18, 36, tzinfo=JST)
            )
            assert len(channel.outputs) == 1
        finally:
            app_config.load_system = original_load_system
            app_config.get_log_dir = original_get_log_dir


async def test_maybe_send_marks_run_even_when_no_nudge_sent() -> None:
    with tempfile.TemporaryDirectory(prefix="compass-proactive-") as d:
        tmp = Path(d)
        log_dir = tmp / "logs"
        now = datetime(2026, 5, 2, 18, 35, tzinfo=JST)
        user = {"name": "りか", "discord_user_id": 101, "age": 10}
        channel = FakeChannel(1, [FakeMember(101)])
        service = _service(tmp, [user], channel)
        journal_path = log_dir / "りか_pocket_journal.jsonl"
        _write_jsonl(
            journal_path,
            [{"ts": (now - timedelta(days=1)).isoformat(), "item": "ノート", "amount": 120}],
        )

        import app.config as app_config

        original_load_system = app_config.load_system
        original_get_log_dir = app_config.get_log_dir
        app_config.load_system = lambda: {"log_dir": str(log_dir)}
        app_config.get_log_dir = lambda system_conf: Path(system_conf["log_dir"])
        try:
            await service.maybe_send_proactive_child_nudges(now=now)
            assert channel.outputs == []
            state = service._load_reminder_state()
            assert state["proactive_child_nudge_last_run_at"] == "2026-05-02T18:35:00+09:00"

            journal_path.unlink()
            await service.maybe_send_proactive_child_nudges(now=now + timedelta(hours=1))
            assert channel.outputs == []
        finally:
            app_config.load_system = original_load_system
            app_config.get_log_dir = original_get_log_dir


async def test_notification_step_error_does_not_block_following_step() -> None:
    with tempfile.TemporaryDirectory(prefix="compass-proactive-") as d:
        tmp = Path(d)
        log_dir = tmp / "logs"
        user = {"name": "りか", "discord_user_id": 101, "age": 10}
        channel = FakeChannel(1, [FakeMember(101)])
        service = _service(tmp, [user], channel)
        calls: list[str] = []

        async def failing_step() -> None:
            calls.append("failing")
            raise RuntimeError("simulated reminder failure")

        async def following_step() -> None:
            calls.append("following")

        import app.config as app_config

        original_load_system = app_config.load_system
        original_get_log_dir = app_config.get_log_dir
        app_config.load_system = lambda: {"log_dir": str(log_dir)}
        app_config.get_log_dir = lambda system_conf: Path(system_conf["log_dir"])
        try:
            first = await service._run_notification_step("failing_step", failing_step, timeout_sec=1)
            second = await service._run_notification_step("following_step", following_step, timeout_sec=1)
        finally:
            app_config.load_system = original_load_system
            app_config.get_log_dir = original_get_log_dir

        assert first is False
        assert second is True
        assert calls == ["failing", "following"]
        diagnostics = _read_jsonl(log_dir / "runtime_diagnostics.jsonl")
        assert any(
            row.get("event") == "reminder_step_error"
            and row.get("step") == "failing_step"
            and row.get("error_type") == "RuntimeError"
            for row in diagnostics
        )


async def test_notification_step_timeout_does_not_block_following_step() -> None:
    with tempfile.TemporaryDirectory(prefix="compass-proactive-") as d:
        tmp = Path(d)
        log_dir = tmp / "logs"
        user = {"name": "りか", "discord_user_id": 101, "age": 10}
        channel = FakeChannel(1, [FakeMember(101)])
        service = _service(tmp, [user], channel)
        calls: list[str] = []

        async def slow_step() -> None:
            calls.append("slow")
            await asyncio.sleep(1)

        async def following_step() -> None:
            calls.append("following")

        import app.config as app_config

        original_load_system = app_config.load_system
        original_get_log_dir = app_config.get_log_dir
        app_config.load_system = lambda: {"log_dir": str(log_dir)}
        app_config.get_log_dir = lambda system_conf: Path(system_conf["log_dir"])
        try:
            first = await service._run_notification_step("slow_step", slow_step, timeout_sec=0.01)
            second = await service._run_notification_step("following_step", following_step, timeout_sec=1)
        finally:
            app_config.load_system = original_load_system
            app_config.get_log_dir = original_get_log_dir

        assert first is False
        assert second is True
        assert calls == ["slow", "following"]
        diagnostics = _read_jsonl(log_dir / "runtime_diagnostics.jsonl")
        assert any(
            row.get("event") == "reminder_step_error"
            and row.get("step") == "slow_step"
            and row.get("error_type") == "TimeoutError"
            for row in diagnostics
        )


async def test_auto_grant_operation_key_prevents_duplicate() -> None:
    with tempfile.TemporaryDirectory(prefix="compass-auto-grant-") as d:
        tmp = Path(d)
        log_dir = tmp / "logs"
        user = {"name": "りか", "discord_user_id": 101, "age": 10, "fixed_allowance": 800}
        channel = FakeChannel(1, [FakeMember(101)])
        service = _service(tmp, [user], channel)
        service.wallet_service.wallet_state_path = tmp / "data" / "wallet_state.json"
        service.wallet_service.wallet_audit_state_path = tmp / "data" / "wallet_audit_state.json"
        service.wallet_service.set_balance("りか", 0)

        import app.config as app_config

        original_load_system = app_config.load_system
        app_config.load_system = lambda: {"log_dir": str(log_dir)}
        try:
            first = await service._grant_fixed_allowance_all(payday=date(2026, 5, 1))
            second = await service._grant_fixed_allowance_all(payday=date(2026, 5, 1))
        finally:
            app_config.load_system = original_load_system

        assert "+800円 → 800円" in first
        assert "+800円 → 800円" in second
        assert service.wallet_service.get_balance("りか") == 800
        ledger_rows = _read_jsonl(log_dir / "りか_wallet_ledger.jsonl")
        assert len(ledger_rows) == 1
        assert ledger_rows[0]["operation_key"] == "allowance_monthly_auto_grant:りか:2026-05-01"


async def test_proactive_send_failure_continues_to_next_user() -> None:
    class FailFirstChannel(FakeChannel):
        def __init__(self, channel_id: int, members: list[FakeMember]):
            super().__init__(channel_id, members)
            self.attempts = 0

        async def send(self, text: str) -> None:
            self.attempts += 1
            if self.attempts == 1:
                raise RuntimeError("simulated discord send failure")
            await super().send(text)

    with tempfile.TemporaryDirectory(prefix="compass-proactive-fail-") as d:
        tmp = Path(d)
        log_dir = tmp / "logs"
        now = datetime(2026, 5, 2, 18, 30, tzinfo=JST)
        users = [
            {"name": "あい", "discord_user_id": 101, "age": 10},
            {"name": "りか", "discord_user_id": 102, "age": 10},
        ]
        channel = FailFirstChannel(1, [FakeMember(101), FakeMember(102)])
        service = _service(tmp, users, channel)

        import app.config as app_config

        original_load_system = app_config.load_system
        original_get_log_dir = app_config.get_log_dir
        app_config.load_system = lambda: {"log_dir": str(log_dir)}
        app_config.get_log_dir = lambda system_conf: Path(system_conf["log_dir"])
        try:
            sent = await service.send_proactive_child_nudges(log_dir=log_dir, now=now)
        finally:
            app_config.load_system = original_load_system
            app_config.get_log_dir = original_get_log_dir

        assert sent == 1
        assert len(channel.outputs) == 1
        assert "りか" in channel.outputs[0]
        state = service._load_reminder_state()
        sent_by_user = state.get("proactive_child_nudge_last_sent_by_user", {})
        assert "あい" not in sent_by_user
        assert "りか" in sent_by_user
        diagnostics = _read_jsonl(log_dir / "runtime_diagnostics.jsonl")
        assert any(
            row.get("event") == "reminder_delivery_error"
            and row.get("step") == "proactive_child_nudge"
            and row.get("details", {}).get("user_name") == "あい"
            for row in diagnostics
        )


async def _run_all() -> int:
    tests = [
        test_no_recent_record_sends_gentle_nudge,
        test_recent_record_does_not_send,
        test_stale_challenge_takes_priority_and_rate_limits,
        test_growth_plan_review_nudge,
        test_maybe_send_runs_after_scheduled_time_once_per_day,
        test_maybe_send_marks_run_even_when_no_nudge_sent,
        test_notification_step_error_does_not_block_following_step,
        test_notification_step_timeout_does_not_block_following_step,
        test_auto_grant_operation_key_prevents_duplicate,
        test_proactive_send_failure_continues_to_next_user,
    ]
    failures = []
    for test in tests:
        try:
            await test()
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
    raise SystemExit(asyncio.run(_run_all()))
