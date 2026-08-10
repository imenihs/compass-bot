"""codex blocker#3回帰防止: _run_claude の例外分類。

_spawn_claude は起動フェーズ(create_subprocess_exec)と通信フェーズ(communicate)を扱う。
- 起動失敗(_ProcessNeverStarted): tool未実行確実 → tool_never_ran=True(再入力を促してよい)
- タイムアウト: tool実行後の可能性 → timed_out=True, tool_never_ran=False(再入力を促さない)
- 通信例外: tool実行後の可能性 → tool_never_ran=False
resume再試行側も同じ分類であることを検証する。
"""
import asyncio, json, sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.conv import ai_conversation as ac

_results=[]
def _check(n,p,d=""): _results.append({"test":n,"passed":bool(p),"detail":d})

def _run():
    orig_spawn = ac._spawn_claude
    orig_diag = ac._diag
    ac._diag = lambda *a, **k: None  # 診断ログ抑制
    loop = asyncio.new_event_loop()

    try:
        # ① 起動失敗 → tool_never_ran=True
        async def spawn_never_started(*a, **k):
            raise ac._ProcessNeverStarted("claude not found")
        ac._spawn_claude = spawn_never_started
        ok, res, sid, timed_out, tool_never_ran = loop.run_until_complete(
            ac._run_claude("300円つかった", None, "sys", "はな"))
        _check("process_never_started_sets_tool_never_ran",
               (ok is False and tool_never_ran is True and timed_out is False),
               f"ok={ok} timed_out={timed_out} never_ran={tool_never_ran}")

        # ② タイムアウト → timed_out=True, tool_never_ran=False
        async def spawn_timeout(*a, **k):
            raise asyncio.TimeoutError()
        ac._spawn_claude = spawn_timeout
        ok, res, sid, timed_out, tool_never_ran = loop.run_until_complete(
            ac._run_claude("300円つかった", None, "sys", "はな"))
        _check("timeout_sets_timed_out_not_never_ran",
               (ok is False and timed_out is True and tool_never_ran is False),
               f"ok={ok} timed_out={timed_out} never_ran={tool_never_ran}")

        # ③ 通信例外(起動後) → tool_never_ran=False
        async def spawn_comm_error(*a, **k):
            raise RuntimeError("decode failed after tool ran")
        ac._spawn_claude = spawn_comm_error
        ok, res, sid, timed_out, tool_never_ran = loop.run_until_complete(
            ac._run_claude("300円つかった", None, "sys", "はな"))
        _check("comm_error_not_never_ran",
               (ok is False and tool_never_ran is False and timed_out is False),
               f"ok={ok} timed_out={timed_out} never_ran={tool_never_ran}")

        # ④ resume再試行のタイムアウト: session_id有り+resume失敗シグナルで再試行に入り、そこでタイムアウト
        #    → timed_out=True, tool_never_ran=False (再試行タイムアウトでの二重課金防止)
        call_count = {"n": 0}
        async def spawn_resume_then_timeout(prompt, session_id, system_prompt, child_name, **k):
            call_count["n"] += 1
            if call_count["n"] == 1:
                # 1回目: resume失敗シグナルをstderrに含めて返す(ok=False)
                return (1, "", ac._RESUME_FAILED_SIGNAL)
            # 2回目(新規再試行): タイムアウト
            raise asyncio.TimeoutError()
        ac._spawn_claude = spawn_resume_then_timeout
        ok, res, sid, timed_out, tool_never_ran = loop.run_until_complete(
            ac._run_claude("300円つかった", "dead-session", "sys", "はな"))
        _check("resume_retry_timeout_not_never_ran",
               (ok is False and timed_out is True and tool_never_ran is False),
               f"ok={ok} timed_out={timed_out} never_ran={tool_never_ran} calls={call_count['n']}")

    finally:
        ac._spawn_claude = orig_spawn
        ac._diag = orig_diag
        loop.close()

    passed=sum(1 for x in _results if x["passed"])
    for x in _results: print(json.dumps(x,ensure_ascii=False))
    print(json.dumps({"summary":True,"passed":passed,"total":len(_results)},ensure_ascii=False))
    # 判定を終了コードへ返す（返さないと落ちても PASS 扱いになる・2026/08/10 是正）
    return passed == len(_results)

if __name__=="__main__":
    raise SystemExit(0 if _run() else 1)
