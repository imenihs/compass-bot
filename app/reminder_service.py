import asyncio
import calendar
import json
from collections import Counter
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
        allow_channel_ids: set[int] | None = None,
        monthly_summary_conf: dict | None = None,
    ):
        self.client = client
        self.allowance_reminder_conf = allowance_reminder_conf
        self.wallet_audit_conf = wallet_audit_conf
        self.load_all_users = load_all_users_fn
        self.wallet_service = wallet_service
        self.allow_channel_ids = allow_channel_ids or set()

        self.monthly_summary_conf = monthly_summary_conf or {}

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

        state = self._load_reminder_state()
        sent_keys: list = state.get("sent_remind_keys") or []

        for before_days in cfg.get("before_days_list", [7]):
            reminder_date = payday - timedelta(days=before_days)
            if now.date() != reminder_date:
                continue
            remind_key = f"{payday.isoformat()}_{cfg['notify_time']}_{channel_id}_{before_days}"
            if remind_key in sent_keys:
                continue
            await self.send_allowance_reminder(payday=payday, channel_id=int(channel_id), is_test=False)
            sent_keys.append(remind_key)
            state["sent_remind_keys"] = sent_keys
            self._save_reminder_state(state)

    async def send_wallet_audit(self) -> None:
        """時刻・日付チェックを省略して今月の残高チェック案内を即時送信する。"""
        if not self.allow_channel_ids:
            return

        now = datetime.now(JST)
        month_key = now.strftime("%Y-%m")
        state = self.wallet_service.load_audit_state()

        users = sorted(self.load_all_users(), key=lambda x: str(x.get("name", "")))
        for u in users:
            state["pending_by_user"][str(u.get("name"))] = month_key

        def make_audit_message() -> str:
            return "\n".join([
                f"毎月の残高チェックです（{month_key}）。",
                "次の形式で残高を報告してね:",
                "@compass-bot 残高報告 1234円",
                "帳簿記録との差がある場合は、設定に従ってペナルティ減額します。",
            ])

        def make_setup_message() -> str:
            return "\n".join([
                f"毎月の残高チェックです（{month_key}）。",
                "まだウォレットの初期設定が完了していないよ。",
                "「初期設定」と送って始めよう！",
            ])

        user_by_discord_id = {
            int(u["discord_user_id"]): u
            for u in users
            if u.get("discord_user_id")
        }

        for channel_id in self.allow_channel_ids:
            channel = self.client.get_channel(channel_id)
            if channel is None:
                channel = await self.client.fetch_channel(channel_id)

            member_ids = {m.id for m in getattr(channel, "members", [])}
            channel_users = [
                user_by_discord_id[mid]
                for mid in member_ids
                if mid in user_by_discord_id
            ]

            if channel_users:
                for u in channel_users:
                    name = str(u.get("name"))
                    msg = make_audit_message() if self.wallet_service.has_wallet(name) else make_setup_message()
                    await channel.send(msg)
            else:
                await channel.send(make_audit_message())

        cfg = self.wallet_audit_conf
        state["last_request_key"] = f"{month_key}_{cfg['check_time']}"
        self.wallet_service.save_audit_state(state)

    async def maybe_request_wallet_audit(self) -> None:
        cfg = self.wallet_audit_conf
        if not cfg.get("enabled"):
            return

        now = datetime.now(JST)
        hh, mm = [int(x) for x in cfg["check_time"].split(":")]
        if now.day != int(cfg["check_day"]) or now.hour != hh or now.minute != mm:
            return

        month_key = now.strftime("%Y-%m")
        state = self.wallet_service.load_audit_state()
        request_key = f"{month_key}_{cfg['check_time']}"
        if state.get("last_request_key") == request_key:
            return

        await self.send_wallet_audit()

    def _load_jsonl(self, path: Path) -> list[dict]:
        if not path.exists():
            return []
        rows: list[dict] = []
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                    if isinstance(obj, dict):
                        rows.append(obj)
                except json.JSONDecodeError:
                    continue
        return rows

    async def send_monthly_summary(self, year: int, month: int, channel_id: int, log_dir: Path) -> None:
        """前月の支出・残高サマリーを指定チャンネルに送信する"""
        from app.config import load_all_users

        users = sorted(load_all_users(), key=lambda x: str(x.get("name", "")))
        lines = [f"【{year}年{month}月 月次サマリーレポート】"]

        for u in users:
            name = str(u.get("name", ""))

            balance = self.wallet_service.get_balance(name)

            # 支出記録集計
            journal_path = log_dir / f"{name}_pocket_journal.jsonl"
            journal_rows = self._load_jsonl(journal_path)
            month_journals = [
                r for r in journal_rows
                if self._is_in_month(r.get("ts"), year, month)
            ]
            spend_count = len(month_journals)
            avg_satisfaction = (
                sum(int(r.get("satisfaction", 0)) for r in month_journals) / spend_count
                if spend_count > 0 else 0
            )
            item_counter: Counter = Counter(
                str(r.get("item", "")).strip()
                for r in month_journals
                if r.get("item")
            )
            top5 = [item for item, _ in item_counter.most_common(5)]

            # お小遣い支給合計(allowance_grant)
            ledger_path = log_dir / f"{name}_wallet_ledger.jsonl"
            ledger_rows = self._load_jsonl(ledger_path)
            grant_total = sum(
                int(r.get("delta", 0))
                for r in ledger_rows
                if r.get("action") == "allowance_grant" and self._is_in_month(r.get("ts"), year, month)
            )

            top5_str = "・".join(top5) if top5 else "なし"
            lines.append(
                f"\n【{name}】"
                f"\n  残高: {balance}円 / 支給合計: {grant_total}円"
                f"\n  支出: {spend_count}件 / 満足度平均: {avg_satisfaction:.1f}/10"
                f"\n  主な支出: {top5_str}"
            )

        channel = self.client.get_channel(int(channel_id))
        if channel is None:
            channel = await self.client.fetch_channel(int(channel_id))
        await channel.send("\n".join(lines))

    def _is_in_month(self, ts_str, year: int, month: int) -> bool:
        if not ts_str:
            return False
        try:
            dt = datetime.fromisoformat(str(ts_str))
            return dt.year == year and dt.month == month
        except Exception:
            return False

    async def maybe_send_monthly_summary(self) -> None:
        cfg = self.monthly_summary_conf
        if not cfg.get("enabled"):
            return
        channel_id = cfg.get("channel_id")
        if not channel_id:
            return

        now = datetime.now(JST)
        if now.day != 1:
            return

        hh, mm = [int(x) for x in cfg["send_time"].split(":")]
        if now.hour != hh or now.minute != mm:
            return

        # 前月を計算
        if now.month == 1:
            target_year, target_month = now.year - 1, 12
        else:
            target_year, target_month = now.year, now.month - 1

        state = self._load_reminder_state()
        sent_keys: list = state.get("sent_monthly_summary_keys") or []
        summary_key = f"{target_year}-{target_month:02d}_{channel_id}"
        if summary_key in sent_keys:
            return

        from app.config import get_log_dir, load_system
        log_dir = get_log_dir(load_system())
        await self.send_monthly_summary(
            year=target_year,
            month=target_month,
            channel_id=int(channel_id),
            log_dir=log_dir,
        )
        sent_keys.append(summary_key)
        state["sent_monthly_summary_keys"] = sent_keys
        self._save_reminder_state(state)

    async def loop(self) -> None:
        while not self.client.is_closed():
            try:
                await self.maybe_send_allowance_reminder()
                await self.maybe_request_wallet_audit()
                await self.maybe_send_monthly_summary()
            except Exception as e:
                print("Reminder loop error:", e)
            await asyncio.sleep(20)

    def start_loop_if_needed(self) -> None:
        if self.loop_task is None or self.loop_task.done():
            self.loop_task = asyncio.create_task(self.loop())
