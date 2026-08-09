"""約束フォロー管理（N-11.18）の回帰テスト。

守るべき不変条件:
  1. **子が勝手に約束を作れない**。承認前（draft）は追跡・履行の対象外である。
  2. **履行カウンタが並行更新で消えない**。カウンタは金銭に直結する
     （貯金目標が flock 無しでロストアップデートを起こした前例がある）。
  3. **減点表示にしない**。「あと9回」でなく「3回できた」を前面に出す。
     返済の可視化が自己効力感を削るという指摘への対処。

テストデータは全て合成データで、本番の data/ には一切触れない。
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.promise_service import (MAX_ACTIVE_PROMISES, STATUS_ACTIVE, STATUS_DONE,
                                 STATUS_DRAFT, PromiseService)

_results = []


def _check(name, passed, detail=""):
    _results.append({"test": name, "passed": bool(passed), "detail": str(detail)[:200]})


def _test_approval_gates_tracking():
    """承認前は追跡対象外で、履行も進められないこと（子が勝手に約束を作れない）。"""
    tmp = Path(tempfile.mkdtemp())
    try:
        ps = PromiseService(data_dir=tmp)
        ok, pid, _msg = ps.create_draft("たろう", "パソコン代の返済", "毎月500円ずつ10回返す",
                                        10, note="1万円を貸した")
        _check("draft_created", ok and pid, _msg)
        _check("draft_status_is_draft",
               (ps.get_promise(pid) or {}).get("status") == STATUS_DRAFT,
               (ps.get_promise(pid) or {}).get("status"))

        # 承認前に履行を進めようとしても通らない
        ok2, msg2 = ps.record_progress(pid)
        _check("progress_blocked_before_approval", ok2 is False, msg2)

        # 承認して初めて active になる
        ok3, _ = ps.approve(pid, approved_by="とうちゃん")
        _check("approve_activates", ok3 and
               (ps.get_promise(pid) or {}).get("status") == STATUS_ACTIVE,
               (ps.get_promise(pid) or {}).get("status"))

        # 二重承認はしない
        ok4, _ = ps.approve(pid)
        _check("double_approve_rejected", ok4 is False)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _test_progress_and_completion():
    """履行が積み上がり、完了で done になること。文面が減点表示でないこと。"""
    tmp = Path(tempfile.mkdtemp())
    try:
        ps = PromiseService(data_dir=tmp)
        _ok, pid, _ = ps.create_draft("はな", "ゲーム時間の約束", "1日1時間まで", 3)
        ps.approve(pid)

        _ok1, m1 = ps.record_progress(pid, note="1日目")
        _check("progress_counts_up", "1回目" in m1 and "1/3" in m1, m1)
        # 「あと2回」のような減点表示にしない（自己効力感を削らない）
        _check("progress_not_deficit_framing", "あと" not in m1, m1)

        ps.record_progress(pid)
        _ok3, m3 = ps.record_progress(pid)
        _check("completion_marks_done",
               (ps.get_promise(pid) or {}).get("status") == STATUS_DONE, m3)
        _check("completion_celebrates", "やりきった" in m3 or "終わった" in m3, m3)

        # 完了後はそれ以上進まない
        ok4, _ = ps.record_progress(pid)
        _check("no_progress_after_done", ok4 is False)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _test_limits_and_validation():
    """同時に持てる件数の上限と、入力の検証。"""
    tmp = Path(tempfile.mkdtemp())
    try:
        ps = PromiseService(data_dir=tmp)
        for i in range(MAX_ACTIVE_PROMISES):
            ok, _pid, _ = ps.create_draft("ゆい", f"約束{i}", "test", 3)
            _check(f"create_within_limit[{i}]", ok is True)
        ok_over, _pid, msg = ps.create_draft("ゆい", "はみ出し", "test", 3)
        _check("create_over_limit_rejected", ok_over is False, msg)

        # 別の子は影響を受けない
        ok_other, _pid, _ = ps.create_draft("はな", "別の子の約束", "test", 3)
        _check("limit_is_per_child", ok_other is True)

        # 入力の検証
        ng1, _p, m1 = ps.create_draft("", "名前なし", "test", 3)
        _check("reject_without_child", ng1 is False, m1)
        ng2, _p, m2 = ps.create_draft("はな", "", "test", 3)
        _check("reject_without_title", ng2 is False, m2)
        ng3, _p, m3 = ps.create_draft("はな", "回数ゼロ", "test", 0)
        _check("reject_zero_times", ng3 is False, m3)
        ng4, _p, m4 = ps.create_draft("はな", "回数過大", "test", 999)
        _check("reject_too_many_times", ng4 is False, m4)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _test_concurrent_progress_no_lost_update():
    """並行して履行を進めてもカウントが消えないこと（flock の検証）。

    履行カウンタは金銭に直結する。貯金目標が flock 無しでロストアップデートを
    起こした前例があるため、同じ作りを真似ていないことを実証で固定する。
    """
    tmp = tempfile.mkdtemp()
    try:
        setup = (
            'import sys; sys.path.insert(0,"%s")\n'
            'from pathlib import Path\n'
            'from app.promise_service import PromiseService\n'
            'ps = PromiseService(data_dir=Path("%s"))\n'
            'ok, pid, _ = ps.create_draft("たろう","返済","毎月500円",20)\n'
            'ps.approve(pid)\n'
            'print(pid)\n' % (ROOT, tmp)
        )
        pid = subprocess.run([sys.executable, "-c", setup],
                             capture_output=True, text=True).stdout.strip()
        script = (
            'import sys, time\n'
            'sys.path.insert(0,"%s")\n'
            'from pathlib import Path\n'
            'from app.promise_service import PromiseService\n'
            'b=Path("%s/go"); Path("%s/r_"+sys.argv[1]).write_text("1")\n'
            'while not b.exists(): time.sleep(0.005)\n'
            'PromiseService(data_dir=Path("%s")).record_progress("%s", note=sys.argv[1])\n'
            % (ROOT, tmp, tmp, tmp, pid)
        )
        Path(f"{tmp}/run.py").write_text(script)
        procs = [subprocess.Popen([sys.executable, f"{tmp}/run.py", str(i)],
                                  stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
                 for i in range(6)]
        for _ in range(600):
            if len([f for f in os.listdir(tmp) if f.startswith("r_")]) >= 6:
                break
            time.sleep(0.05)
        Path(f"{tmp}/go").write_text("1")
        for p in procs:
            p.communicate()
        doc = json.loads(Path(f"{tmp}/promises.json").read_text(encoding="utf-8"))
        pr = [x for x in doc["promises"] if x["id"] == pid][0]
        _check("concurrent_progress_no_lost_update",
               pr["done_times"] == 6 and len(pr["history"]) == 6,
               f"done={pr['done_times']} history={len(pr['history'])}")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _test_tool_layer_permissions():
    """tool 層で「子は提案まで・確定は親だけ」が守られること。

    子が勝手に約束を作れないことは保存層(draft)と tool 層(親モード判定)の
    二重で守る。片方だけでは、AI が親モード相当の呼び出しを試みたときに抜ける。
    """
    tmp = Path(tempfile.mkdtemp())
    try:
        os.environ["COMPASS_ACTIVE_CHILD"] = "テスト"
        from app import mcp_wallet as m
        from app import promise_service as psvc

        orig_cls = psvc.PromiseService

        class _Scoped(orig_cls):
            def __init__(self, data_dir=None):
                super().__init__(data_dir=tmp)

        psvc.PromiseService = _Scoped
        try:
            m.ACTIVE_CHILD = "テスト"
            r = m._do_propose_promise({"name": "テスト", "title": "返済",
                                       "detail": "毎月500円ずつ10回", "total_times": 10})
            _check("tool_child_can_propose", "下書き" in r, r)

            pid = _Scoped().list_promises("テスト")[0]["id"]

            # 子モードでは承認できない
            m.PARENT_MODE, m.ALLOW_ADMIN_OPS = False, False
            r2 = m._do_parent_approve_promise({"promise_id": pid})
            _check("tool_child_cannot_approve", "親のチャンネル" in r2, r2)
            _check("tool_still_draft_after_child_attempt",
                   _Scoped().get_promise(pid)["status"] == STATUS_DRAFT,
                   _Scoped().get_promise(pid)["status"])

            # 子モードでは履行も進められない
            r3 = m._do_parent_record_promise_progress({"promise_id": pid})
            _check("tool_child_cannot_record", "親のチャンネル" in r3, r3)

            # 親モードなら承認できる
            m.PARENT_MODE, m.ALLOW_ADMIN_OPS = True, True
            r4 = m._do_parent_approve_promise({"promise_id": pid})
            _check("tool_parent_can_approve",
                   _Scoped().get_promise(pid)["status"] == STATUS_ACTIVE, r4)

            # 履行の文面が積み上がり表示であること
            r5 = m._do_parent_record_promise_progress({"promise_id": pid, "note": "1回目"})
            _check("tool_progress_is_additive", "1/10" in r5 and "あと" not in r5, r5)
        finally:
            psvc.PromiseService = orig_cls
            m.PARENT_MODE, m.ALLOW_ADMIN_OPS = False, False
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def main():
    _test_approval_gates_tracking()
    _test_progress_and_completion()
    _test_limits_and_validation()
    _test_concurrent_progress_no_lost_update()
    _test_tool_layer_permissions()
    passed = sum(1 for x in _results if x["passed"])
    for x in _results:
        print(json.dumps(x, ensure_ascii=False))
    print(json.dumps({"summary": True, "passed": passed, "total": len(_results)},
                     ensure_ascii=False))
    return passed == len(_results)


if __name__ == "__main__":
    sys.exit(0 if main() else 1)
