import asyncio
import json
import re
from datetime import datetime
from pathlib import Path

import discord
from google import genai

from app.storage import JST


class GeminiService:
    def __init__(self, api_key: str, model_name: str, assess_keyword: str):
        self.model_name = model_name
        self.assess_keyword = assess_keyword
        self.client = genai.Client(api_key=api_key)

    def call(self, prompt: str) -> str:
        resp = self.client.models.generate_content(
            model=self.model_name,
            contents=prompt,
        )
        return resp.text or "ごめん、空の応答だったよ。"

    async def call_with_progress(self, channel: discord.abc.Messageable, prompt: str) -> str:
        task = asyncio.create_task(asyncio.to_thread(self.call, prompt))
        while True:
            try:
                return await asyncio.wait_for(asyncio.shield(task), timeout=10)
            except asyncio.TimeoutError:
                try:
                    await channel.send("考え中... ちょっと待ってね。")
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
        legacy_purpose = _pick(r"目的\s*[：:]\s*\+?(\d+)\s*円")
        legacy_discretionary = _pick(r"裁量\s*[：:]\s*\+?(\d+)\s*円")
        total = _pick(r"合計\s*[：:]\s*(\d+)\s*円")

        if fixed is None and temporary is None and legacy_purpose is None and legacy_discretionary is None and total is None:
            return None

        if temporary is None and (legacy_purpose is not None or legacy_discretionary is not None):
            temporary = int(legacy_purpose or 0) + int(legacy_discretionary or 0)

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
