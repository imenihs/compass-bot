import json
from pathlib import Path
from datetime import datetime, timezone, timedelta

JST = timezone(timedelta(hours=9))

def append_jsonl(path: Path, record: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")

def now_jst_iso() -> str:
    return datetime.now(JST).isoformat()