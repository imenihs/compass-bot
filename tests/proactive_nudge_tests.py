#!/usr/bin/env python3
"""能動伴走メッセージのテスト。"""

from __future__ import annotations

import asyncio
import json
import sys
import tempfile
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
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


async def _run_all() -> int:
    tests = [
        test_no_recent_record_sends_gentle_nudge,
        test_recent_record_does_not_send,
        test_stale_challenge_takes_priority_and_rate_limits,
        test_growth_plan_review_nudge,
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
