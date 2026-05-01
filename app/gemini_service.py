import asyncio
import json
import os
import re
import time
from datetime import datetime
from pathlib import Path

import discord
from google import genai

from app.storage import JST


class GeminiService:
    def __init__(self, api_key: str, model_name: str, assess_keyword: str):
        self.model_name = model_name
        self.assess_keyword = assess_keyword
        try:
            timeout_ms = int(os.environ.get("GEMINI_TIMEOUT_MS", "15000"))
        except ValueError:
            timeout_ms = 15000
        try:
            self.retry_attempts = max(1, int(os.environ.get("GEMINI_RETRY_ATTEMPTS", "2")))
        except ValueError:
            self.retry_attempts = 2
        try:
            self.silent_timeout_sec = max(1.0, float(os.environ.get("GEMINI_SILENT_TIMEOUT_SEC", "8")))
        except ValueError:
            self.silent_timeout_sec = 8.0
        try:
            self.progress_interval_sec = max(1.0, float(os.environ.get("GEMINI_PROGRESS_INTERVAL_SEC", "8")))
        except ValueError:
            self.progress_interval_sec = 8.0
        try:
            self.max_wait_sec = max(5.0, float(os.environ.get("GEMINI_MAX_WAIT_SEC", "40")))
        except ValueError:
            self.max_wait_sec = 40.0
        self.client = genai.Client(api_key=api_key, http_options={"timeout": timeout_ms})

    def call(self, prompt: str) -> str:
        last_error = None
        for attempt in range(self.retry_attempts):
            try:
                resp = self.client.models.generate_content(
                    model=self.model_name,
                    contents=prompt,
                )
                return resp.text or "ごめん、空の応答だったよ。"
            except Exception as e:
                last_error = e
                text = str(e)
                transient = any(
                    marker in text
                    for marker in [
                        "408", "429", "500", "502", "503", "504",
                        "UNAVAILABLE", "RESOURCE_EXHAUSTED", "Timeout", "timed out",
                    ]
                )
                if not transient or attempt >= self.retry_attempts - 1:
                    raise
                time.sleep(1.5 * (attempt + 1))
        raise last_error

    async def call_silent(self, prompt: str) -> str:
        """「考え中...」を表示せずに非同期でGeminiを呼び出す（コマンド判定等の軽量用途）"""
        return await asyncio.wait_for(
            asyncio.to_thread(self.call, prompt),
            timeout=self.silent_timeout_sec,
        )

    async def call_with_progress(
        self,
        channel: discord.abc.Messageable,
        prompt: str,
        timeout_reply: str | None = None,
    ) -> str:
        task = asyncio.create_task(asyncio.to_thread(self.call, prompt))
        started = time.monotonic()
        wait_notice_sent = False
        while True:
            elapsed = time.monotonic() - started
            remaining = self.max_wait_sec - elapsed
            if remaining <= 0:
                task.cancel()
                if timeout_reply is not None:
                    return timeout_reply
                raise TimeoutError(f"Gemini response exceeded {self.max_wait_sec:.0f} seconds")
            try:
                return await asyncio.wait_for(
                    asyncio.shield(task),
                    timeout=min(self.progress_interval_sec, remaining),
                )
            except asyncio.TimeoutError:
                if not wait_notice_sent:
                    wait_notice_sent = True
                    try:
                        await channel.send("時間がかかってるから、もう少しだけ待ってね。")
                    except Exception:
                        pass

    def extract_assessed_amounts(self, reply: str) -> dict | None:
        text = reply or ""
        if self.assess_keyword not in text:
            return None

        def _pick(pattern: str) -> int | None:
            m = re.search(pattern, text, flags=re.MULTILINE)
            if not m:
                return None
            return int(m.group(1))

        fixed = _pick(r"固定\s*[：:]?\s*\+?(\d+)\s*円")
        temporary = _pick(r"臨時\s*[：:]?\s*\+?(\d+)\s*円")
        total = _pick(r"合計\s*[：:]\s*(\d+)\s*円")

        # 3フィールドすべて取得できなかった場合は査定なしと判断する
        if fixed is None and temporary is None and total is None:
            return None

        parsed = {
            "fixed": fixed,
            "temporary": temporary,
            "total": total,
        }
        if parsed["total"] is None:
            if temporary is not None and fixed is not None:
                parsed["total"] = int(fixed) + int(temporary)
        return parsed


def _read_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    rows: list[dict] = []
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
                if isinstance(rec, dict):
                    rows.append(rec)
            except json.JSONDecodeError:
                continue
    return rows


def count_recent_allowance_requests(log_dir: Path, user_name: str, days: int = 30) -> int:
    path = log_dir / f"{user_name}_events.jsonl"
    rows = _read_jsonl(path)
    now = datetime.now(JST)
    count = 0
    for r in rows:
        ts = r.get("ts")
        assessed = r.get("assessed")
        if not ts or not assessed:
            continue
        try:
            dt = datetime.fromisoformat(str(ts))
        except ValueError:
            continue
        if (now - dt).days <= days:
            count += 1
    return count
