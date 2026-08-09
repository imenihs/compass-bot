"""金額が動くコマンドが、必ず確認を挟んでから実行されることの回帰テスト（N-11.17）。

守るべき不変条件:
  1. **支給・調整・一括支給の3コマンドは、その場で残高を動かさない**。
     以前はコマンド経路だけ即実行で、AI 経路にだけ確認があった。この非対称があると
     「AI 経由なら桁の打ち間違いを捕まえられるが、コマンドなら素通り」になる。
  2. 確認文には対象・金額が出る（親が食い違いに気づける）。
  3. 一括支給は、実行前に **誰にいくら入るのか** を見せる。全員の残高が一度に動くため。
"""
import asyncio
import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

_results = []


def _check(name, passed, detail=""):
    _results.append({"test": name, "passed": bool(passed), "detail": str(detail)[:200]})


class _Ch:
    """送信内容を捕まえるだけのチャンネル代用。"""

    def __init__(self):
        self.sent = []

    async def send(self, msg, **kw):
        self.sent.append(msg)
        return type("M", (), {"id": 1})()


def _msg(ch, parent_id=880001):
    return type("M", (), {"channel": ch,
                          "author": type("A", (), {"id": parent_id})(), "id": 4242})()


def _setup(tmp):
    """本番データに触れないよう、残高もログも一時ディレクトリへ隔離する。"""
    (tmp / "logs").mkdir(exist_ok=True)
    json.dump({"users": {"たろう": {"expected_balance": 1000},
                         "はな": {"expected_balance": 2000}}},
              open(tmp / "wallet_state.json", "w"))
    from app import config, wallet_service, handlers_parent as H
    # get_log_dir は両モジュールが個別に持つため、片方だけ差し替えると本番へ書きに行く
    config.get_log_dir = lambda *a, **k: tmp / "logs"
    wallet_service.get_log_dir = lambda *a, **k: tmp / "logs"
    ws = wallet_service.WalletService()
    ws.wallet_state_path = tmp / "wallet_state.json"
    H._wallet_service = ws
    H._is_parent = lambda uid: True
    H.find_child_user_by_name = lambda n: {"name": n} if n in ("たろう", "はな") else None
    H.load_all_users = lambda: [{"name": "たろう", "fixed_allowance": 500},
                                {"name": "はな", "fixed_allowance": 300}]
    H.load_system = lambda: {}
    H.extract_input_from_mention = lambda t, u: None
    H._client = type("C", (), {"user": type("U", (), {"id": 1, "name": "compass-bot"})()})()
    return H, ws


async def _run():
    from app import parent_confirm as pc
    tmp = Path(tempfile.mkdtemp())
    H, ws = _setup(tmp)
    pid = 880001

    # 1. 支給コマンドは残高を動かさず、確認を出す
    pc.clear_pending(pid)
    ch = _Ch()
    before = ws.get_balance("たろう")
    await H.maybe_handle_manual_grant(_msg(ch, pid), "支給 たろう 700円")
    _check("grant_does_not_move_money_yet", ws.get_balance("たろう") == before,
           ws.get_balance("たろう"))
    _check("grant_asks_confirmation", ch.sent and "実行していい" in ch.sent[0],
           ch.sent[:1])
    _check("grant_confirm_shows_target_and_amount",
           ch.sent and "たろう" in ch.sent[0] and "700" in ch.sent[0], ch.sent[:1])
    rec = pc.take_pending(pid)
    _check("grant_pending_is_parent_grant",
           rec and rec["action"] == "parent_grant" and rec["args"]["amount"] == 700, rec)

    # 2. 調整コマンドも同じく確認を挟む（減算でも同様）
    pc.clear_pending(pid)
    ch = _Ch()
    before = ws.get_balance("はな")
    await H.maybe_handle_balance_adjustment(_msg(ch, pid), "残高調整 はな -300円")
    _check("adjust_does_not_move_money_yet", ws.get_balance("はな") == before,
           ws.get_balance("はな"))
    _check("adjust_asks_confirmation", ch.sent and "実行していい" in ch.sent[0], ch.sent[:1])
    rec = pc.take_pending(pid)
    _check("adjust_pending_is_adjust_balance",
           rec and rec["action"] == "parent_adjust_balance", rec)

    # 3. 一括支給は誰にいくら入るかを見せてから確認する
    pc.clear_pending(pid)
    ch = _Ch()
    b1, b2 = ws.get_balance("たろう"), ws.get_balance("はな")
    await H.maybe_handle_bulk_grant(_msg(ch, pid), "一括支給")
    _check("bulk_does_not_move_money_yet",
           ws.get_balance("たろう") == b1 and ws.get_balance("はな") == b2,
           (ws.get_balance("たろう"), ws.get_balance("はな")))
    body = ch.sent[0] if ch.sent else ""
    _check("bulk_asks_confirmation", "実行していい" in body, body[:80])
    _check("bulk_shows_each_child", "たろう" in body and "はな" in body, body[:200])
    _check("bulk_shows_total", "800" in body, body[:200])

    # 4. 「はい」を受けてはじめて実際に動く。
    #    実行は **確認時点のスナップショット**（items）だけを使う。
    rec = pc.take_pending(pid)
    _check("bulk_pending_has_snapshot",
           rec and isinstance(rec["args"].get("items"), list)
           and len(rec["args"]["items"]) == 2, rec)
    ch2 = _Ch()
    await H.execute_bulk_grant(_msg(ch2, pid), items=rec["args"]["items"],
                               op_key_base=f"confirm-{rec['token']}")
    _check("bulk_executes_after_yes",
           ws.get_balance("たろう") == b1 + 500 and ws.get_balance("はな") == b2 + 300,
           (ws.get_balance("たろう"), ws.get_balance("はな")))

    # 5. 同じ確認 ID で再実行しても二重支給しない（冪等キーが確認 ID 由来であること）
    again1, again2 = ws.get_balance("たろう"), ws.get_balance("はな")
    await H.execute_bulk_grant(_msg(_Ch(), pid), items=rec["args"]["items"],
                               op_key_base=f"confirm-{rec['token']}")
    _check("bulk_idempotent_on_same_confirm",
           ws.get_balance("たろう") == again1 and ws.get_balance("はな") == again2,
           (ws.get_balance("たろう"), ws.get_balance("はな")))

    # 6. 確認の後に固定額が変わっても、**親が見た金額どおり**に動く。
    #    実行時に load_all_users を読み直すと確認文と実額がズレる（有識者反証）。
    pc.clear_pending(pid)
    ch3 = _Ch()
    await H.maybe_handle_bulk_grant(_msg(ch3, pid), "一括支給")
    rec2 = pc.take_pending(pid)
    H.load_all_users = lambda: [{"name": "たろう", "fixed_allowance": 99999},
                                {"name": "はな", "fixed_allowance": 300}]
    base = ws.get_balance("たろう")
    await H.execute_bulk_grant(_msg(_Ch(), pid), items=rec2["args"]["items"],
                               op_key_base=f"confirm-{rec2['token']}")
    _check("bulk_uses_snapshot_not_current_setting",
           ws.get_balance("たろう") == base + 500, (base, ws.get_balance("たろう")))

    # 7. 古い確認を上書きしたら、親へ「取り消した」と伝える材料が返る
    pc.clear_pending(pid)
    pc.put_pending(pid, "parent_grant", {"name": "たろう", "amount": 100})
    _tok, superseded = pc.put_pending(pid, "bulk_grant", {"items": [], "total": 0})
    _check("supersede_is_reported",
           superseded is not None and superseded["action"] == "parent_grant", superseded)
    _check("supersede_message_mentions_previous",
           "取り消した" in pc.describe_superseded(superseded),
           pc.describe_superseded(superseded))
    pc.clear_pending(pid)

    import shutil
    shutil.rmtree(tmp, ignore_errors=True)


def main():
    asyncio.run(_run())
    passed = sum(1 for x in _results if x["passed"])
    for x in _results:
        print(json.dumps(x, ensure_ascii=False))
    print(json.dumps({"summary": True, "passed": passed, "total": len(_results)},
                     ensure_ascii=False))
    return passed == len(_results)


if __name__ == "__main__":
    sys.exit(0 if main() else 1)
