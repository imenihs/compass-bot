import asyncio
import calendar
import json
import time
import traceback
from collections import Counter
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Awaitable, Callable
from urllib.parse import quote

import discord

from app.storage import JST, append_jsonl, now_jst_iso


DEFAULT_LOOP_INTERVAL_SEC = 600
REMINDER_STEP_TIMEOUT_SEC = 120


def _build_pocket_journal_reminder_message(user_conf: dict) -> str:
    """週次支出記録リマインドのメッセージを年齢に応じて生成する。
    強制感を出さず、やさしく記録を促す文体にする。"""
    name = user_conf.get("name", "")
    raw_age = user_conf.get("age")
    # 年齢を int に正規化する（文字列数字も変換する）
    if isinstance(raw_age, int):
        age = raw_age
    elif isinstance(raw_age, str) and raw_age.strip().isdigit():
        age = int(raw_age.strip())
    else:
        age = None

    if age is not None and age <= 9:
        # 低学年向け — ひらがな多め・語りかけるような文体にする
        return (
            f"{name}さん、こんにちは！\n"
            "今週つかったお金はあったかな？\n"
            "あったら「支出記録」ってなげかけてみてね！"
        )
    if age is not None and age <= 12:
        # 小学高学年向け — 記録のメリットを一言添えて促す
        return (
            f"{name}さん、今週使ったお金があれば記録してみよう！\n"
            "「支出記録」と送ると案内が届くよ。"
        )
    # 中学生以上 — 端的に伝える
    return (
        f"{name}さん、今週お金を使ったなら記録しておこう。\n"
        "「支出記録」で記録できるよ。"
    )


def _build_proactive_child_nudge_message(user_conf: dict, nudge: dict) -> str:
    """子どもへ送る能動的な伴走メッセージを作る。責めずに1行動だけ促す。"""
    name = str(user_conf.get("name", "")).strip()
    reason = str(nudge.get("reason") or "").strip()
    action = str(nudge.get("action") or "").strip()
    age = _age_int(user_conf.get("age"))
    prefix = f"{name}さん、" if name else ""

    if reason == "challenge_stale":
        if age is not None and age <= 9:
            return f"{prefix}このまえのチャレンジ、どうだった？\n「やった」「あとで」「ちがう」だけでも教えてね。"
        return f"{prefix}この前の小さなチャレンジ、どうだった？\n「やった」「あとで」「ちがう」だけでも返してね。"

    if reason == "growth_plan_review":
        plan_action = action or "決めた行動"
        return f"{prefix}{plan_action} の確認日が近いよ。\nできたことを1つだけ教えてね。"

    if age is not None and age <= 9:
        return f"{prefix}さいきんのお金の記録、少しあいてるみたい。\n買ったものがあったら「支出記録」って送ってね。"
    if age is not None and age <= 12:
        return f"{prefix}最近の記録が少し空いてるみたい。\n買ったものがあったら、名前だけでも「支出記録」で残してみよう。"
    return f"{prefix}最近の支出記録が少し空いています。\n使ったものがあれば「支出記録」で残しておこう。"


def _age_int(raw_age) -> int | None:
    """年齢設定を int に正規化する"""
    if isinstance(raw_age, int):
        return raw_age
    if isinstance(raw_age, str) and raw_age.strip().isdigit():
        return int(raw_age.strip())
    return None


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
        pocket_journal_reminder_conf: dict | None = None,
        proactive_child_nudge_conf: dict | None = None,
    ):
        self.client = client
        self.allowance_reminder_conf = allowance_reminder_conf
        self.wallet_audit_conf = wallet_audit_conf
        self.load_all_users = load_all_users_fn
        self.wallet_service = wallet_service
        self.allow_channel_ids = allow_channel_ids or set()

        self.monthly_summary_conf = monthly_summary_conf or {}
        # 週次支出記録リマインドの設定（未指定は無効扱いとする）
        self.pocket_journal_reminder_conf = pocket_journal_reminder_conf or {}
        self.proactive_child_nudge_conf = proactive_child_nudge_conf or {}

        root = Path(__file__).resolve().parents[1]
        self.reminder_state_path = root / "data" / "reminder_state.json"
        self.learning_support_state_dir = root / "data" / "learning_support_state"
        self.growth_plans_dir = root / "data" / "growth_plans"
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

    def _write_runtime_diagnostic(self, record: dict) -> None:
        """通知ループの異常を運用ログへ残す。ログ失敗は標準出力に逃がす。"""
        try:
            from app.config import get_log_dir, load_system

            log_dir = get_log_dir(load_system())
            append_jsonl(log_dir / "runtime_diagnostics.jsonl", record)
        except Exception as log_error:
            print(f"[reminder_diagnostics] log error: {type(log_error).__name__}: {log_error}")

    def _log_reminder_step_error(self, step_name: str, error: Exception, elapsed_ms: int) -> None:
        """通知ごとの失敗を後で追える形で記録する。"""
        self._write_runtime_diagnostic({
            "ts": now_jst_iso(),
            "event": "reminder_step_error",
            "step": step_name,
            "elapsed_ms": elapsed_ms,
            "error_type": type(error).__name__,
            "error_message": str(error),
            "traceback": "".join(
                traceback.format_exception(type(error), error, error.__traceback__, limit=8)
            )[:4000],
        })

    def _log_reminder_delivery_error(self, step_name: str, error: Exception, details: dict | None = None) -> None:
        """通知送信単位の失敗を記録し、他の宛先処理は継続する。"""
        self._write_runtime_diagnostic({
            "ts": now_jst_iso(),
            "event": "reminder_delivery_error",
            "step": step_name,
            "error_type": type(error).__name__,
            "error_message": str(error),
            "details": details or {},
        })

    async def _run_notification_step(
        self,
        step_name: str,
        handler: Callable[[], Awaitable[None]],
        timeout_sec: float = REMINDER_STEP_TIMEOUT_SEC,
    ) -> bool:
        """1通知の失敗やハングで、後続通知を止めないための実行ラッパー。"""
        started = time.monotonic()
        try:
            await asyncio.wait_for(handler(), timeout=timeout_sec)
            return True
        except Exception as e:
            elapsed_ms = int((time.monotonic() - started) * 1000)
            self._log_reminder_step_error(step_name, e, elapsed_ms)
            print(f"Reminder step error [{step_name}]: {type(e).__name__}: {e}")
            return False

    @staticmethod
    def _safe_month_day(year: int, month: int, day: int) -> date:
        max_day = calendar.monthrange(year, month)[1]
        return date(year, month, min(day, max_day))

    @staticmethod
    def _scheduled_datetime(base: datetime, time_text: str) -> datetime | None:
        """指定日の HH:MM を datetime にする。形式が不正なら None を返す。"""
        try:
            hh_text, mm_text = str(time_text).split(":", 1)
            hh = int(hh_text)
            mm = int(mm_text)
        except (TypeError, ValueError):
            return None
        if not (0 <= hh <= 23 and 0 <= mm <= 59):
            return None
        return base.replace(hour=hh, minute=mm, second=0, microsecond=0)

    def _scheduled_time_reached(self, now: datetime, time_text: str) -> bool:
        """現在時刻が当日の予定時刻以降かを判定する。"""
        scheduled_at = self._scheduled_datetime(now, time_text)
        return scheduled_at is not None and now >= scheduled_at

    def _should_run_daily_schedule(self, state: dict, state_key: str, now: datetime, time_text: str) -> bool:
        """予定時刻を過ぎていて、前回実行が今回予定時刻より前なら True。"""
        scheduled_at = self._scheduled_datetime(now, time_text)
        if scheduled_at is None or now < scheduled_at:
            return False
        last_run_at = self._parse_datetime(state.get(state_key))
        return last_run_at is None or last_run_at < scheduled_at

    def _mark_schedule_run(self, state: dict, state_key: str, now: datetime) -> None:
        """定期処理の最終実行時刻を保存する。"""
        state[state_key] = now.isoformat()

    @staticmethod
    def _safe_int(value, default: int = 0) -> int:
        """集計用に整数化する。壊れた値は default に倒す。"""
        try:
            return int(value)
        except (TypeError, ValueError):
            return default

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

    async def _grant_fixed_allowance_all(self, payday: date | None = None) -> str:
        """全ユーザーの固定お小遣いを残高に自動加算して結果サマリーを返す。
        支給日当日の自動加算（B-1）で呼び出す。"""
        from app.config import load_system
        system_conf = load_system()
        users = sorted(self.load_all_users(), key=lambda x: str(x.get("name", "")))
        lines = []
        for u in users:
            name = str(u.get("name", ""))
            amount = self._safe_int(u.get("fixed_allowance"), 0)
            # 固定額が未設定のユーザーはスキップする
            if amount <= 0:
                continue
            operation_key = ""
            if payday is not None:
                operation_key = f"allowance_monthly_auto_grant:{name}:{payday.isoformat()}"
            new_balance, _ = self.wallet_service.update_balance(
                user_conf=u,
                system_conf=system_conf,
                delta=amount,
                action="allowance_monthly_auto_grant",
                note="auto_grant_on_payday",
                operation_key=operation_key,
            )
            lines.append(f"・{name}: +{amount}円 → {new_balance}円")
        return "\n".join(lines) if lines else "対象ユーザーなし"

    async def maybe_send_allowance_reminder(self) -> None:
        cfg = self.allowance_reminder_conf
        if not cfg.get("enabled"):
            return

        channel_id = cfg.get("channel_id")
        if not channel_id:
            return

        now = datetime.now(JST)
        if not self._scheduled_time_reached(now, cfg["notify_time"]):
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
            # before_days=0（支給日当日）かつ auto_grant_on_payday=true の場合に自動加算する
            if before_days == 0 and cfg.get("auto_grant_on_payday"):
                grant_summary = await self._grant_fixed_allowance_all(payday=payday)
                channel = self.client.get_channel(int(channel_id))
                if channel is None:
                    channel = await self.client.fetch_channel(int(channel_id))
                await channel.send(f"【固定お小遣い自動支給】\n{grant_summary}")
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
        cfg = self.wallet_audit_conf
        state["last_request_key"] = f"{month_key}_{cfg['check_time']}"
        self.wallet_service.save_audit_state(state)

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
                    try:
                        await channel.send(msg)
                    except Exception as e:
                        self._log_reminder_delivery_error(
                            "wallet_audit",
                            e,
                            {"channel_id": channel_id, "user_name": name},
                        )
            else:
                try:
                    await channel.send(make_audit_message())
                except Exception as e:
                    self._log_reminder_delivery_error(
                        "wallet_audit",
                        e,
                        {"channel_id": channel_id, "user_name": ""},
                    )

    async def maybe_request_wallet_audit(self) -> None:
        cfg = self.wallet_audit_conf
        if not cfg.get("enabled"):
            return

        now = datetime.now(JST)
        if now.day != int(cfg["check_day"]) or not self._scheduled_time_reached(now, cfg["check_time"]):
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

            # 支出記録を集計する
            journal_path = log_dir / f"{name}_pocket_journal.jsonl"
            journal_rows = self._load_jsonl(journal_path)
            month_journals = [
                r for r in journal_rows
                if self._is_in_month(r.get("ts"), year, month)
            ]
            spend_count = len(month_journals)
            satisfaction_values = [
                self._safe_int(r.get("satisfaction"), 0)
                for r in month_journals
            ]
            avg_satisfaction = (
                sum(satisfaction_values) / len(satisfaction_values)
                if satisfaction_values else 0
            )
            item_counter: Counter = Counter(
                str(r.get("item", "")).strip()
                for r in month_journals
                if r.get("item")
            )
            top5 = [item for item, _ in item_counter.most_common(5)]

            # お小遣い支給合計を集計する（action=allowance_grant）
            ledger_path = log_dir / f"{name}_wallet_ledger.jsonl"
            ledger_rows = self._load_jsonl(ledger_path)
            grant_total = sum(
                self._safe_int(r.get("delta"), 0)
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

        if not self._scheduled_time_reached(now, cfg["send_time"]):
            return

        # 前月を計算する
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

    def _has_recent_journal_entry(self, user_name: str, log_dir: Path, days: int = 7) -> bool:
        """過去N日間に支出記録（pocket_journal）があるかチェックする。
        記録が1件でもあれば True を返し、リマインドは送信しない。"""
        path = log_dir / f"{user_name}_pocket_journal.jsonl"
        rows = self._load_jsonl(path)
        # 基準日時（now - days）より新しいエントリがあれば記録済みとみなす
        cutoff = datetime.now(JST) - timedelta(days=days)
        for r in rows:
            ts_str = r.get("ts")
            if not ts_str:
                continue
            try:
                dt = datetime.fromisoformat(str(ts_str))
                if dt >= cutoff:
                    return True
            except Exception:
                continue
        return False

    def _latest_journal_entry_at(self, user_name: str, log_dir: Path) -> datetime | None:
        """最後の支出記録日時を返す。記録がなければ None を返す。"""
        path = log_dir / f"{user_name}_pocket_journal.jsonl"
        rows = self._load_jsonl(path)
        latest: datetime | None = None
        for row in rows:
            dt = self._parse_datetime(row.get("ts"))
            if dt is not None and (latest is None or dt > latest):
                latest = dt
        return latest

    @staticmethod
    def _parse_datetime(value) -> datetime | None:
        """ISO文字列をJST基準のaware datetimeにそろえる"""
        if not value:
            return None
        try:
            dt = datetime.fromisoformat(str(value))
        except Exception:
            return None
        if dt.tzinfo is None:
            return dt.replace(tzinfo=JST)
        return dt.astimezone(JST)

    def _user_key(self, user_conf: dict) -> str:
        """状態ファイル用のユーザーキーを返す"""
        name = str(user_conf.get("name") or "").strip()
        discord_id = str(user_conf.get("discord_user_id") or "").strip()
        return quote(name or discord_id or "unknown", safe="")

    def _load_json_file(self, path: Path) -> dict:
        """JSONファイルをdictとして読み込む"""
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, json.JSONDecodeError):
            return {}
        return data if isinstance(data, dict) else {}

    def _load_learning_support_state(self, user_conf: dict) -> dict:
        """子ども別の学習支援状態を読み込む"""
        return self._load_json_file(self.learning_support_state_dir / f"{self._user_key(user_conf)}.json")

    def _load_growth_plans(self, user_conf: dict) -> dict:
        """子ども別の成長行動プランを読み込む"""
        return self._load_json_file(self.growth_plans_dir / f"{self._user_key(user_conf)}.json")

    def _active_growth_plan_due(self, user_conf: dict, now: datetime, days_before: int) -> dict | None:
        """確認日が近い有効な成長行動プランを返す"""
        data = self._load_growth_plans(user_conf)
        plans = data.get("plans")
        if not isinstance(plans, list):
            return None
        today = now.date()
        limit = today + timedelta(days=days_before)
        for plan in plans:
            if not isinstance(plan, dict) or plan.get("status") != "active":
                continue
            review_at = str(plan.get("review_at") or "").strip()
            try:
                review_date = date.fromisoformat(review_at)
            except ValueError:
                continue
            if today <= review_date <= limit:
                return plan
        return None

    def _has_recent_child_response(self, state: dict, now: datetime, days: int) -> bool:
        """子どもが最近チャレンジへ反応したかを判定する"""
        response = state.get("child_response")
        if not isinstance(response, dict):
            return False
        responded_at = self._parse_datetime(response.get("responded_at"))
        if responded_at is None:
            return False
        return responded_at >= now - timedelta(days=days)

    def _select_proactive_nudge(self, user_conf: dict, log_dir: Path, now: datetime) -> dict | None:
        """記録空白・未反応・確認日から、送るべき伴走メッセージを1つ選ぶ"""
        policy = user_conf.get("ai_follow_policy")
        if isinstance(policy, dict) and policy.get("enabled") is False:
            return None

        cfg = self.proactive_child_nudge_conf
        state = self._load_learning_support_state(user_conf)
        challenge_days = int(cfg.get("challenge_stale_days", 5))
        last_nudge_at = self._parse_datetime(state.get("last_nudge_at"))
        if last_nudge_at and last_nudge_at <= now - timedelta(days=challenge_days):
            if state.get("last_child_action") and not self._has_recent_child_response(state, now, challenge_days):
                return {
                    "reason": "challenge_stale",
                    "action": str(state.get("last_child_action") or ""),
                }

        plan = self._active_growth_plan_due(
            user_conf,
            now,
            int(cfg.get("growth_plan_review_days_before", 2)),
        )
        if plan:
            return {
                "reason": "growth_plan_review",
                "action": str(plan.get("agreed_action") or "決めた行動"),
                "plan_id": str(plan.get("plan_id") or ""),
            }

        no_record_days = int(cfg.get("no_record_days", 10))
        latest = self._latest_journal_entry_at(str(user_conf.get("name", "")), log_dir)
        if latest is None or latest <= now - timedelta(days=no_record_days):
            return {
                "reason": "no_record",
                "last_record_at": latest.isoformat() if latest else "",
            }

        return None

    def _is_recent_proactive_sent(self, state: dict, user_name: str, now: datetime) -> bool:
        """同じ子へ短期間に能動メッセージを送りすぎないようにする"""
        sent_by_user = state.get("proactive_child_nudge_last_sent_by_user")
        if not isinstance(sent_by_user, dict):
            return False
        item = sent_by_user.get(user_name)
        if not isinstance(item, dict):
            return False
        sent_at = self._parse_datetime(item.get("ts"))
        if sent_at is None:
            return False
        min_days = int(self.proactive_child_nudge_conf.get("min_days_between_nudges", 3))
        return sent_at > now - timedelta(days=min_days)

    def _mark_proactive_sent(self, state: dict, user_name: str, nudge: dict, now: datetime) -> None:
        """能動メッセージ送信履歴を保存する"""
        sent_by_user = state.get("proactive_child_nudge_last_sent_by_user")
        if not isinstance(sent_by_user, dict):
            sent_by_user = {}
        sent_by_user[user_name] = {
            "ts": now.isoformat(),
            "reason": str(nudge.get("reason") or ""),
        }
        state["proactive_child_nudge_last_sent_by_user"] = sent_by_user

    async def _child_channels(self) -> dict[str, discord.abc.Messageable]:
        """allow_channel_ids から子どもごとの送信先チャンネルを1つ選ぶ"""
        users = self.load_all_users()
        user_by_discord_id = {
            int(u["discord_user_id"]): u
            for u in users
            if u.get("discord_user_id")
        }
        channels_by_name = {}
        for channel_id in self.allow_channel_ids:
            channel = self.client.get_channel(channel_id)
            if channel is None:
                channel = await self.client.fetch_channel(channel_id)
            member_ids = {m.id for m in getattr(channel, "members", [])}
            for member_id in member_ids:
                user_conf = user_by_discord_id.get(member_id)
                if not user_conf:
                    continue
                name = str(user_conf.get("name", "")).strip()
                if name and name not in channels_by_name:
                    channels_by_name[name] = channel
        return channels_by_name

    async def send_proactive_child_nudges(self, log_dir: Path, now: datetime | None = None) -> int:
        """対象の子どもへ能動的な伴走メッセージを送る。テストからも直接呼ぶ。"""
        now_dt = now or datetime.now(JST)
        state = self._load_reminder_state()
        channels_by_name = await self._child_channels()
        sent_count = 0
        max_per_run = int(self.proactive_child_nudge_conf.get("max_per_run", 20))

        for user_conf in sorted(self.load_all_users(), key=lambda x: str(x.get("name", ""))):
            if sent_count >= max_per_run:
                break
            user_name = str(user_conf.get("name", "")).strip()
            channel = channels_by_name.get(user_name)
            if not user_name or channel is None:
                continue
            if self._is_recent_proactive_sent(state, user_name, now_dt):
                continue
            nudge = self._select_proactive_nudge(user_conf, log_dir, now_dt)
            if not nudge:
                continue
            try:
                await channel.send(_build_proactive_child_nudge_message(user_conf, nudge))
            except Exception as e:
                self._log_reminder_delivery_error(
                    "proactive_child_nudge",
                    e,
                    {"user_name": user_name},
                )
                continue
            self._mark_proactive_sent(state, user_name, nudge, now_dt)
            self._save_reminder_state(state)
            sent_count += 1

        self._save_reminder_state(state)
        return sent_count

    async def maybe_send_proactive_child_nudges(self, now: datetime | None = None) -> None:
        """予定時刻到達後、未処理なら子どもへの能動伴走メッセージを送信する"""
        cfg = self.proactive_child_nudge_conf
        if not cfg.get("enabled"):
            return

        now_dt = now or datetime.now(JST)
        state = self._load_reminder_state()
        run_state_key = "proactive_child_nudge_last_run_at"
        if not self._should_run_daily_schedule(state, run_state_key, now_dt, cfg["notify_time"]):
            return

        from app.config import get_log_dir, load_system
        log_dir = get_log_dir(load_system())
        await self.send_proactive_child_nudges(log_dir=log_dir, now=now_dt)
        state = self._load_reminder_state()
        self._mark_schedule_run(state, run_state_key, now_dt)
        self._save_reminder_state(state)

    async def maybe_send_pocket_journal_reminder(self) -> None:
        """週次支出記録リマインドを送信する。
        設定された曜日・時刻に、過去7日間の記録がないユーザーにのみ送信する。"""
        cfg = self.pocket_journal_reminder_conf
        if not cfg.get("enabled"):
            return

        now = datetime.now(JST)
        # 設定曜日（Python weekday: 0=月〜6=日）と現在の曜日が一致する場合のみ処理する
        if now.weekday() != int(cfg["day_of_week"]):
            return
        if not self._scheduled_time_reached(now, cfg["notify_time"]):
            return

        # ISO週番号をキーにして同一週の二重送信を防ぐ
        week_key = now.strftime("%Y-W%W")
        state = self._load_reminder_state()
        sent_keys: list = state.get("sent_pocket_journal_reminder_keys") or []

        from app.config import get_log_dir, load_system
        log_dir = get_log_dir(load_system())

        users = self.load_all_users()
        # discord_user_id をキーにして channel メンバーと突合する
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

            for u in channel_users:
                user_name = str(u.get("name", ""))
                # ユーザー×週のキーで重複チェックをする
                send_key = f"pocket_journal_reminder_{user_name}_{week_key}"
                if send_key in sent_keys:
                    continue
                # 今週すでに記録がある場合はリマインドを省略する
                if not self._has_recent_journal_entry(user_name, log_dir, days=7):
                    try:
                        await channel.send(
                            _build_pocket_journal_reminder_message(u)
                        )
                    except Exception as e:
                        self._log_reminder_delivery_error(
                            "pocket_journal_reminder",
                            e,
                            {"channel_id": channel_id, "user_name": user_name},
                        )
                        continue
                # 記録あり・なし問わず今週分は処理済みとしてマークする
                sent_keys.append(send_key)
                state["sent_pocket_journal_reminder_keys"] = sent_keys
                self._save_reminder_state(state)

        state["sent_pocket_journal_reminder_keys"] = sent_keys
        self._save_reminder_state(state)

    async def loop(self) -> None:
        while not self.client.is_closed():
            steps: list[tuple[str, Callable[[], Awaitable[None]]]] = [
                ("allowance_reminder", self.maybe_send_allowance_reminder),
                ("wallet_audit", self.maybe_request_wallet_audit),
                ("monthly_summary", self.maybe_send_monthly_summary),
                ("pocket_journal_reminder", self.maybe_send_pocket_journal_reminder),
                ("proactive_child_nudge", self.maybe_send_proactive_child_nudges),
            ]
            for step_name, handler in steps:
                await self._run_notification_step(step_name, handler)
            await asyncio.sleep(DEFAULT_LOOP_INTERVAL_SEC)

    def start_loop_if_needed(self) -> None:
        if self.loop_task is None or self.loop_task.done():
            self.loop_task = asyncio.create_task(self.loop())
