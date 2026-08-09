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
    def __init__(self, channel_id: int, members: list[FakeMember], name: str = ""):
        self.id = channel_id
        self.members = members
        self.name = name
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
        user = {"name": "はな", "discord_user_id": 101, "age": 10}
        channel = FakeChannel(1, [FakeMember(101)])
        service = _service(tmp, [user], channel)

        await service.send_proactive_child_nudges(log_dir=tmp / "logs", now=now)

        assert len(channel.outputs) == 1
        text = channel.outputs[0]
        # Phase N-11 で子の入口は AI 自然会話へ一本化。旧コマンド「支出記録」を教えず、
        # 「なにをいくらで買ったか教えて/話しかけて」と自然文で促す（AI主導との一貫性）。
        assert "支出記録" not in text
        assert "いくらで買ったか" in text
        assert "放置" not in text
        assert "ペナルティ" not in text


async def test_channel_name_fallback_sends_when_member_cache_empty() -> None:
    with tempfile.TemporaryDirectory(prefix="compass-proactive-") as d:
        tmp = Path(d)
        now = datetime(2026, 5, 2, 18, 30, tzinfo=JST)
        user = {"name": "はな", "discord_user_id": 101, "age": 10}
        channel = FakeChannel(1, [], name="compass-はな")
        service = _service(tmp, [user], channel)

        sent = await service.send_proactive_child_nudges(log_dir=tmp / "logs", now=now)

        assert sent == 1
        assert len(channel.outputs) == 1
        assert "はな" in channel.outputs[0]


async def test_recent_record_does_not_send() -> None:
    with tempfile.TemporaryDirectory(prefix="compass-proactive-") as d:
        tmp = Path(d)
        now = datetime(2026, 5, 2, 18, 30, tzinfo=JST)
        user = {"name": "はな", "discord_user_id": 101, "age": 10}
        channel = FakeChannel(1, [FakeMember(101)])
        service = _service(tmp, [user], channel)
        _write_jsonl(
            tmp / "logs" / "はな_pocket_journal.jsonl",
            [{"ts": (now - timedelta(days=1)).isoformat(), "item": "ノート", "amount": 120}],
        )

        await service.send_proactive_child_nudges(log_dir=tmp / "logs", now=now)

        assert channel.outputs == []


async def test_wallet_ledger_record_suppresses_no_record_nudge() -> None:
    """AI主導会話の記録(wallet_ledger の spending_record)があれば no_record ナッジを送らない。

    codex 指摘の不整合回帰防止: record_expense は wallet_ledger にだけ書き pocket_journal は空。
    pocket_journal だけ見ると「449円のおかし買った」とAIに記録したのに『記録があいてる』と誤通知される。
    """
    with tempfile.TemporaryDirectory(prefix="compass-proactive-") as d:
        tmp = Path(d)
        now = datetime(2026, 5, 2, 18, 30, tzinfo=JST)
        user = {"name": "はな", "discord_user_id": 101, "age": 10}
        channel = FakeChannel(1, [FakeMember(101)])
        service = _service(tmp, [user], channel)
        # pocket_journal は空。wallet_ledger にだけ支出記録がある(AI主導会話の record_expense)
        _write_jsonl(
            tmp / "logs" / "はな_wallet_ledger.jsonl",
            [{"ts": (now - timedelta(days=1)).isoformat(), "action": "spending_record",
              "delta": -449, "note": "おかし", "balance_after": 551}],
        )
        await service.send_proactive_child_nudges(log_dir=tmp / "logs", now=now)
        assert channel.outputs == [], f"unexpected nudge: {channel.outputs}"


async def test_stale_challenge_takes_priority_and_rate_limits() -> None:
    with tempfile.TemporaryDirectory(prefix="compass-proactive-") as d:
        tmp = Path(d)
        now = datetime(2026, 5, 2, 18, 30, tzinfo=JST)
        user = {"name": "はな", "discord_user_id": 101, "age": 10}
        channel = FakeChannel(1, [FakeMember(101)])
        service = _service(tmp, [user], channel)
        _write_jsonl(
            tmp / "logs" / "はな_pocket_journal.jsonl",
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
        assert "次の買い物で理由を1つ書く" in channel.outputs[0]
        assert "この前の小さなチャレンジ" not in channel.outputs[0]
        assert "このまえのチャレンジ" not in channel.outputs[0]
        # child_challenge_events が無い(親設定由来)ので「前に決めた」と断定せず提案形にする(codex ux)
        assert "前に決めた" not in channel.outputs[0]
        assert "ためしてみる" in channel.outputs[0]


async def test_challenge_stale_asserts_decided_only_when_child_agreed() -> None:
    """child_challenge_events があるとき(子が実際に関わった)だけ「前に決めた」と断定する。

    無いとき(親のWeb操作由来)は「ためしてみる?」の提案形にし、子に『そんなこと決めてない』と
    感じさせない(codex ux)。断定形と提案形の出し分けを固定する。
    """
    with tempfile.TemporaryDirectory(prefix="compass-proactive-") as d:
        tmp = Path(d)
        now = datetime(2026, 5, 2, 18, 30, tzinfo=JST)
        user = {"name": "はな", "discord_user_id": 101, "age": 10}
        channel = FakeChannel(1, [FakeMember(101)])
        service = _service(tmp, [user], channel)
        _write_jsonl(
            tmp / "logs" / "はな_pocket_journal.jsonl",
            [{"ts": (now - timedelta(days=1)).isoformat(), "item": "ノート", "amount": 120}],
        )
        # child_challenge_events あり = 子が実際に関わった → 断定形
        _write_json(
            service.learning_support_state_dir / f"{service._user_key(user)}.json",
            {
                "last_nudge_at": (now - timedelta(days=6)).isoformat(),
                "last_child_action": "次の買い物で理由を1つ書く。",
                "child_challenge_events": [{"ts": (now - timedelta(days=6)).isoformat(),
                                            "feedback": "use_this_week", "child_action": "次の買い物で理由を1つ書く。"}],
            },
        )
        await service.send_proactive_child_nudges(log_dir=tmp / "logs", now=now)
        assert len(channel.outputs) == 1
        assert "前に決めた" in channel.outputs[0], f"agreed case must assert 'decided': {channel.outputs[0]}"


async def test_declined_challenge_suppressed_permanently() -> None:
    """declined(「ちがう/やめたい」と断った)チャレンジは、challenge_days を超えても蒸し返さない(codex ux)。

    通常の child_response は challenge_days 以内だけ抑制するが、declined は終了扱いで期間無関係に抑制する。
    """
    with tempfile.TemporaryDirectory(prefix="compass-proactive-") as d:
        tmp = Path(d)
        now = datetime(2026, 5, 2, 18, 30, tzinfo=JST)
        user = {"name": "はな", "discord_user_id": 101, "age": 10}
        channel = FakeChannel(1, [FakeMember(101)])
        service = _service(tmp, [user], channel)
        _write_jsonl(
            tmp / "logs" / "はな_pocket_journal.jsonl",
            [{"ts": (now - timedelta(days=1)).isoformat(), "item": "ノート", "amount": 120}],
        )
        # declined を、challenge_days(5)より前(10日前)に記録。通常なら期限切れで抑制されないが、
        # declined は終了扱いなので challenge_stale を発火させない。
        _write_json(
            service.learning_support_state_dir / f"{service._user_key(user)}.json",
            {
                "last_nudge_at": (now - timedelta(days=6)).isoformat(),
                "last_child_action": "次の買い物で理由を1つ書く。",
                "child_response": {
                    "challenge_id": "次の買い物で理由を1つ書く。",
                    "feedback": "declined",
                    "responded_at": (now - timedelta(days=10)).isoformat(),
                },
            },
        )
        await service.send_proactive_child_nudges(log_dir=tmp / "logs", now=now)
        # declined したチャレンジは蒸し返さない(challenge_stale 文言が出ない)
        assert all("次の買い物で理由を1つ書く" not in o for o in channel.outputs), \
            f"declined challenge should not resurface: {channel.outputs}"


async def test_challenge_stale_suppressed_only_by_matching_challenge_id() -> None:
    """child_response の challenge_id が現在の last_child_action と一致するときだけ challenge_stale を抑制する。

    別チャレンジへの反応（や会話橋渡しの誤記録）で今放置中のチャレンジまで抑制されると、要件
    『未反応の小さなチャレンジに声をかける』を取りこぼす。challenge_id 照合の回帰防止テスト。
    """
    with tempfile.TemporaryDirectory(prefix="compass-proactive-") as d:
        tmp = Path(d)
        now = datetime(2026, 5, 2, 18, 30, tzinfo=JST)
        user = {"name": "はな", "discord_user_id": 101, "age": 10}
        channel = FakeChannel(1, [FakeMember(101)])
        service = _service(tmp, [user], channel)
        _write_jsonl(
            tmp / "logs" / "はな_pocket_journal.jsonl",
            [{"ts": (now - timedelta(days=1)).isoformat(), "item": "ノート", "amount": 120}],
        )
        # 現在のチャレンジは「理由を書く」。だが child_response は別チャレンジ「レシート記録」への最近の反応。
        _write_json(
            service.learning_support_state_dir / f"{service._user_key(user)}.json",
            {
                "last_nudge_at": (now - timedelta(days=6)).isoformat(),
                "last_child_action": "次の買い物で理由を1つ書く。",
                "child_response": {
                    "challenge_id": "レシートを1つ記録",  # 別チャレンジ
                    "feedback": "conversation_reply",
                    "responded_at": (now - timedelta(days=1)).isoformat(),
                },
            },
        )
        # 別チャレンジの反応では抑制されず、今のチャレンジの challenge_stale が発火する
        sent = await service.send_proactive_child_nudges(log_dir=tmp / "logs", now=now)
        assert sent == 1
        assert "次の買い物で理由を1つ書く" in channel.outputs[0]

        # 一致する challenge_id の反応なら抑制される（発火しない）
        channel.outputs.clear()
        _write_json(
            service.learning_support_state_dir / f"{service._user_key(user)}.json",
            {
                "last_nudge_at": (now - timedelta(days=6)).isoformat(),
                "last_child_action": "次の買い物で理由を1つ書く。",
                "child_response": {
                    "challenge_id": "次の買い物で理由を1つ書く。",  # 一致
                    "feedback": "conversation_reply",
                    "responded_at": (now - timedelta(days=1)).isoformat(),
                },
            },
        )
        sent2 = await service.send_proactive_child_nudges(log_dir=tmp / "logs", now=now + timedelta(minutes=2))
        # challenge_stale は抑制される。ただし記録空白(no_record)等の別ナッジが出る場合はあるので、
        # 「理由を書く」challenge_stale 文言が出ないことで抑制を確認する。
        assert all("次の買い物で理由を1つ書く" not in o for o in channel.outputs)


async def test_growth_plan_review_nudge() -> None:
    with tempfile.TemporaryDirectory(prefix="compass-proactive-") as d:
        tmp = Path(d)
        now = datetime(2026, 5, 2, 18, 30, tzinfo=JST)
        user = {"name": "はな", "discord_user_id": 101, "age": 10}
        channel = FakeChannel(1, [FakeMember(101)])
        service = _service(tmp, [user], channel)
        _write_jsonl(
            tmp / "logs" / "はな_pocket_journal.jsonl",
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
        user = {"name": "はな", "discord_user_id": 101, "age": 10}
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
        user = {"name": "はな", "discord_user_id": 101, "age": 10}
        channel = FakeChannel(1, [FakeMember(101)])
        service = _service(tmp, [user], channel)
        journal_path = log_dir / "はな_pocket_journal.jsonl"
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
        user = {"name": "はな", "discord_user_id": 101, "age": 10}
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
        user = {"name": "はな", "discord_user_id": 101, "age": 10}
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
        user = {"name": "はな", "discord_user_id": 101, "age": 10, "fixed_allowance": 800}
        channel = FakeChannel(1, [FakeMember(101)])
        service = _service(tmp, [user], channel)
        service.wallet_service.wallet_state_path = tmp / "data" / "wallet_state.json"
        service.wallet_service.wallet_audit_state_path = tmp / "data" / "wallet_audit_state.json"
        service.wallet_service.set_balance("はな", 0)

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
        assert service.wallet_service.get_balance("はな") == 800
        ledger_rows = _read_jsonl(log_dir / "はな_wallet_ledger.jsonl")
        assert len(ledger_rows) == 1
        assert ledger_rows[0]["operation_key"] == "allowance_monthly_auto_grant:はな:2026-05-01"


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
            {"name": "はな", "discord_user_id": 102, "age": 10},
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
        assert "はな" in channel.outputs[0]
        state = service._load_reminder_state()
        sent_by_user = state.get("proactive_child_nudge_last_sent_by_user", {})
        assert "あい" not in sent_by_user
        assert "はな" in sent_by_user
        diagnostics = _read_jsonl(log_dir / "runtime_diagnostics.jsonl")
        assert any(
            row.get("event") == "reminder_delivery_error"
            and row.get("step") == "proactive_child_nudge"
            and row.get("details", {}).get("user_name") == "あい"
            for row in diagnostics
        )


async def test_wallet_audit_uses_channel_name_when_member_cache_empty() -> None:
    with tempfile.TemporaryDirectory(prefix="compass-audit-fallback-") as d:
        tmp = Path(d)
        user = {"name": "はな", "discord_user_id": 101, "age": 10}
        channel = FakeChannel(1, [], name="compass-はな")
        service = _service(tmp, [user], channel)
        service.wallet_audit_conf = {"check_time": "20:00"}
        service.wallet_service.wallet_state_path = tmp / "data" / "wallet_state.json"
        service.wallet_service.wallet_audit_state_path = tmp / "data" / "wallet_audit_state.json"
        service.wallet_service.set_balance("はな", 500)

        await service.send_wallet_audit()

        assert len(channel.outputs) == 1
        assert "残高チェック" in channel.outputs[0]
        assert "初期設定" not in channel.outputs[0]



async def test_safety_signal_blocks_proactive_nudge() -> None:
    """直近に危険信号を出した子へは、こちらから催促しないこと（N-11.16 連携）。

    安全は【処理の優先順位】1) に属しナッジより上位である。
    つらさを訴えた子へ翌朝スケジューラが「チャレンジどう？」と送るのは害になる。
    ナッジ経路には安全判定が繋がっていなかったため結線した。
    """
    import shutil as _shutil
    import tempfile as _tempfile
    from datetime import timedelta as _td

    from app.reminder_service import ReminderService

    tmp = Path(_tempfile.mkdtemp())
    now = datetime.now(JST)
    recs = [
        # 2日前に危険信号（猶予3日以内 → 送らない）
        {"ts": (now - _td(days=2)).isoformat(), "event": "safety_signal_detected",
         "selected_user": "たろう", "details": {"category": "self_harm"}},
        # 10日前（猶予外 → 通常どおり送る）
        {"ts": (now - _td(days=10)).isoformat(), "event": "safety_signal_detected",
         "selected_user": "はな", "details": {"category": "bullying"}},
    ]
    with open(tmp / "runtime_diagnostics.jsonl", "w", encoding="utf-8") as f:
        for r in recs:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")

    service = ReminderService(
        client=None, allowance_reminder_conf={}, wallet_audit_conf={},
        load_all_users_fn=lambda: [], wallet_service=None, allow_channel_ids=set(),
    )
    assert service._has_recent_safety_signal(tmp, "たろう", now) is True, "直近の危険信号がある子には送らない"
    assert service._has_recent_safety_signal(tmp, "はな", now) is False, "猶予を過ぎたら通常どおり送る"
    assert service._has_recent_safety_signal(tmp, "みらい", now) is False, "検知の無い子は通常どおり送る"

    empty = Path(_tempfile.mkdtemp())
    assert service._has_recent_safety_signal(empty, "たろう", now) is False, "ログが無ければ通常どおり送る"

    _shutil.rmtree(tmp, ignore_errors=True)
    _shutil.rmtree(empty, ignore_errors=True)


async def _run_all() -> int:
    tests = [
        test_no_recent_record_sends_gentle_nudge,
        test_channel_name_fallback_sends_when_member_cache_empty,
        test_recent_record_does_not_send,
        test_wallet_ledger_record_suppresses_no_record_nudge,
        test_stale_challenge_takes_priority_and_rate_limits,
        test_challenge_stale_asserts_decided_only_when_child_agreed,
        test_declined_challenge_suppressed_permanently,
        test_challenge_stale_suppressed_only_by_matching_challenge_id,
        test_growth_plan_review_nudge,
        test_maybe_send_runs_after_scheduled_time_once_per_day,
        test_maybe_send_marks_run_even_when_no_nudge_sent,
        test_notification_step_error_does_not_block_following_step,
        test_notification_step_timeout_does_not_block_following_step,
        test_auto_grant_operation_key_prevents_duplicate,
        test_proactive_send_failure_continues_to_next_user,
        test_wallet_audit_uses_channel_name_when_member_cache_empty,
        test_safety_signal_blocks_proactive_nudge,
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
