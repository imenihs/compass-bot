import asyncio
import calendar
import json
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Callable

import discord

from app.storage import JST


class ReminderService:
    def __init__(
        self,
        client: discord.Client,
        allowance_reminder_conf: dict,
        wallet_audit_conf: dict,
        load_all_users_fn: Callable[[], list[dict]],
        wallet_service,
    ):
        self.client = client
        self.allowance_reminder_conf = allowance_reminder_conf
        self.wallet_audit_conf = wallet_audit_conf
        self.load_all_users = load_all_users_fn
        self.wallet_service = wallet_service

        root = Path(__file__).resolve().parents[1]
        self.reminder_state_path = root / "data" / "reminder_state.json"
        self.loop_task: asyncio.Task | None = None

    def _load_reminder_state(self) -> dict:
        if not self.reminder_state_path.exists():
            return {}
        try:
            with open(self.reminder_state_path, "r", encoding="utf-8") as f:
                data = json.load(f)
                if isinstance(data, dict):
                    return data
        except Exception:
            pass
        return {}

    def _save_reminder_state(self, state: dict) -> None:
        self.reminder_state_path.parent.mkdir(parents=True, exist_ok=True)
        with open(self.reminder_state_path, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False, indent=2)

    @staticmethod
    def _safe_month_day(year: int, month: int, day: int) -> date:
        max_day = calendar.monthrange(year, month)[1]
        return date(year, month, min(day, max_day))

    def next_payday(self, today: date, payday_day: int) -> date:
        this_month_payday = self._safe_month_day(today.year, today.month, payday_day)
        if today <= this_month_payday:
            return this_month_payday
        if today.month == 12:
            return self._safe_month_day(today.year + 1, 1, payday_day)
        return self._safe_month_day(today.year, today.month + 1, payday_day)

    async def send_allowance_reminder(self, payday: date, channel_id: int, is_test: bool = False) -> None:
        users = sorted(self.load_all_users(), key=lambda x: str(x.get("name", "")))
        header = f"お小遣いリマインド（支給日: {payday.isoformat()}）"
        if is_test:
            header = f"{header} [テスト送信]"

        lines = [header]
        total = 0
        for u in users:
            amount = int(u.get("fixed_allowance", 0))
            total += amount
            lines.append(f"・{u.get('name', 'unknown')}: {amount}円")
        lines.append(f"合計: {total}円")

        channel = self.client.get_channel(int(channel_id))
        if channel is None:
            channel = await self.client.fetch_channel(int(channel_id))
        await channel.send("\n".join(lines))

    async def maybe_send_allowance_reminder(self) -> None:
        cfg = self.allowance_reminder_conf
        if not cfg.get("enabled"):
            return

        channel_id = cfg.get("channel_id")
        if not channel_id:
            return

        now = datetime.now(JST)
        hh, mm = [int(x) for x in cfg["notify_time"].split(":")]
        if now.hour != hh or now.minute != mm:
            return

        payday = self.next_payday(now.date(), int(cfg["payday_day"]))
        reminder_date = payday - timedelta(days=int(cfg["before_days"]))
        if now.date() != reminder_date:
            return

        state = self._load_reminder_state()
        remind_key = f"{payday.isoformat()}_{cfg['notify_time']}_{channel_id}"
        if state.get("last_remind_key") == remind_key:
            return

        await self.send_allowance_reminder(payday=payday, channel_id=int(channel_id), is_test=False)
        state["last_remind_key"] = remind_key
        self._save_reminder_state(state)

    async def maybe_request_wallet_audit(self) -> None:
        cfg = self.wallet_audit_conf
        if not cfg.get("enabled"):
            return

        channel_id = cfg.get("channel_id") or self.allowance_reminder_conf.get("channel_id")
        if not channel_id:
            return

        now = datetime.now(JST)
        hh, mm = [int(x) for x in cfg["check_time"].split(":")]
        if now.day != int(cfg["check_day"]) or now.hour != hh or now.minute != mm:
            return

        month_key = now.strftime("%Y-%m")
        state = self.wallet_service.load_audit_state()
        request_key = f"{month_key}_{cfg['check_time']}_{channel_id}"
        if state.get("last_request_key") == request_key:
            return

        users = sorted(self.load_all_users(), key=lambda x: str(x.get("name", "")))
        for u in users:
            state["pending_by_user"][str(u.get("name"))] = month_key

        channel = self.client.get_channel(int(channel_id))
        if channel is None:
            channel = await self.client.fetch_channel(int(channel_id))

        lines = [
            f"毎月の残高チェックです（{month_key}）。",
            "各自、次の形式で残高を報告してね:",
            "@compass-bot 残高報告 1234円",
            "帳簿記録との差がある場合は、設定に従ってペナルティ減額します。",
        ]
        await channel.send("\n".join(lines))

        state["last_request_key"] = request_key
        self.wallet_service.save_audit_state(state)

    async def loop(self) -> None:
        while not self.client.is_closed():
            try:
                await self.maybe_send_allowance_reminder()
                await self.maybe_request_wallet_audit()
            except Exception as e:
                print("Reminder loop error:", e)
            await asyncio.sleep(20)

    def start_loop_if_needed(self) -> None:
        if self.loop_task is None or self.loop_task.done():
            self.loop_task = asyncio.create_task(self.loop())
