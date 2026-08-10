#!/usr/bin/env python3
"""シナリオ網羅テスト用: AI の tool 呼び出しを記録する MCP サーバ(stdio)。

本番 mcp_wallet の tool ロジックをそのまま使いつつ、tools/call のたびに (tool名, 引数) を
COMPASS_REC_LOG が指すファイルへ JSONL 追記する。これで「AI が発話に対しどの tool を
どの引数で呼んだか」を実測できる。残高は隔離環境(COMPASS_ACTIVE_CHILD + config差し替え)で応答。

環境変数:
- COMPASS_REC_LOG: tool 呼び出しを記録する JSONL パス
- COMPASS_TEST_ROOT: 隔離 settings/data のルート
- COMPASS_ACTIVE_CHILD: 束縛する発話者(本番同様)
"""
import json
import os
import sys
from pathlib import Path

_ROOT = Path(os.environ["COMPASS_TEST_ROOT"])
sys.path.insert(0, "/web/compass-bot")

from app import config
config.SETTINGS_DIR = _ROOT / "settings"
config.USERS_DIR = config.SETTINGS_DIR / "users"
config.PARENTS_DIR = config.USERS_DIR / "parents"
# 子は children/ 配下が実配置。ここを設定しないと発話者を解決できない
config.CHILDREN_DIR = config.USERS_DIR / "children"
config.SYSTEM_PATH = config.SETTINGS_DIR / "system.json"
config.SETTING_PATH = config.SETTINGS_DIR / "setting.json"

import app.wallet_service as ws
from app import mcp_wallet
from app.conv.session import SessionStore

_w = ws.WalletService()
_w.wallet_state_path = _ROOT / "data" / "wallet_state.json"
_w.wallet_audit_state_path = _ROOT / "data" / "wallet_audit_state.json"
mcp_wallet._wallet = _w
mcp_wallet._payout_store = lambda: SessionStore(data_dir=_ROOT / "data")

_REC_LOG = Path(os.environ["COMPASS_REC_LOG"])


def _record(tool: str, args: dict) -> None:
    """tool 呼び出しを JSONL へ記録する。"""
    with open(_REC_LOG, "a", encoding="utf-8") as f:
        f.write(json.dumps({"tool": tool, "args": args}, ensure_ascii=False) + "\n")


def _send(msg: dict) -> None:
    sys.stdout.write(json.dumps(msg, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def main() -> None:
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
        except json.JSONDecodeError:
            continue
        method = req.get("method")
        rid = req.get("id")
        if method == "initialize":
            _send({"jsonrpc": "2.0", "id": rid, "result": {"protocolVersion": "2024-11-05", "capabilities": {"tools": {}}, "serverInfo": {"name": "wallet", "version": "rec"}}})
        elif method == "notifications/initialized":
            continue
        elif method == "tools/list":
            _send({"jsonrpc": "2.0", "id": rid, "result": {"tools": mcp_wallet._tool_defs()}})
        elif method == "tools/call":
            params = req.get("params", {}) or {}
            tool = params.get("name")
            args = params.get("arguments", {}) or {}
            # AI が何を呼んだかを記録(判定の実測データ)
            _record(tool, args)
            # 本番の tool ロジックで応答(記録だけでなく実際に動かす)
            out = []
            orig = mcp_wallet._send
            mcp_wallet._send = lambda m: out.append(m)
            try:
                mcp_wallet._handle_tool_call(rid, params)
            finally:
                mcp_wallet._send = orig
            if out:
                _send(out[0])
        else:
            if rid is not None:
                _send({"jsonrpc": "2.0", "id": rid, "error": {"code": -32601, "message": "method not found"}})


if __name__ == "__main__":
    main()
