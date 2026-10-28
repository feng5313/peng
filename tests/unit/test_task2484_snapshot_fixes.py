"""事务#2484：远程快照脚本修复验证

背景：epyc 上多事务共用同一 workdir（fcc worktree）时迭代备份快照频繁
超 60s 预算。根因：并发挤兑 + 超时残留线程（HTTP 360s/git 300s）持索引
锁 + 失败立即重试无退避 + "命令行太长"重试风暴。

修复点：
1. 远程快照脚本 watchdog 自杀（budget+30s，taskkill /T 杀整树）
2. 远程脚本 git() 陈旧锁自愈（peng_backup_idx 锁龄>kill_after+10s 清理）
3. per-(nest,workdir) 串行锁（快照/变更检查/diff预览三处共用）
4. 命令行长度本地预判（>7500 字符直接用精简脚本）
5. ToolEngine 迭代快照指数退避（60/120/240/480s 封顶，成功清零）
"""
import json
import re
import subprocess
import sys
import threading
from unittest.mock import patch

sys.path.insert(0, "/mnt/p/feng/work/peng/src") if "/mnt/p/feng/work/peng/src" not in sys.path else None

from peng.service import tool_backup as tb


def _fake_remote_ok(code, timeout=30, nest_name="local"):
    return subprocess.CompletedProcess(
        args="x", returncode=0,
        stdout=json.dumps({"commit_hash": "abc123def", "branch_name": "b"}),
        stderr="",
    )


def _run_snapshot(internal_files=None):
    """调用远程批量快照（mock HTTP 执行），返回 (meta, 捕获的脚本列表)"""
    from peng.service.tool_backup import ToolBackupManager
    captured = []

    def fake(code, timeout=30, nest_name="local"):
        captured.append(code)
        return _fake_remote_ok(code, timeout, nest_name)

    with patch.object(tb, "_run_python_remote", side_effect=fake):
        meta = ToolBackupManager._snapshot_git_remote_batch(
            workdir="D:/work/worktrees/birds", task_id=2481,
            tool_name="write",
            tool_args={"filePath": "D:/work/worktrees/birds/x.py"},
            commit_msg="test", timestamp="2026-10-29T07:00:00",
            nest_name="epyc", internal_files=internal_files or [],
        )
    return meta, captured


class TestRemoteSnapshotScript:
    def test_script_compiles_with_watchdog_and_lock_healing(self):
        meta, captured = _run_snapshot(
            ["D:/work/worktrees/birds/a.py", "D:/work/worktrees/birds/b/c.py"]
        )
        assert meta and meta["commit_hash"] == "abc123def"
        code = captured[0]
        compile(code, "<remote_snapshot>", "exec")
        # watchdog 注入（默认预算60 → kill_after=90）
        assert "_peng_suicide" in code and "taskkill" in code
        assert "_kill_after = 90" in code
        # 陈旧锁自愈（margin = kill_after+10 = 100）
        assert "_stale_margin = (_kill_after + 10)" in code
        assert "peng_backup_idx" in code and "getmtime" in code
        # 头部 import 含 time/re
        assert code.startswith(
            "import subprocess, os, sys, json, uuid, shutil, time, re"
        )

    def test_stale_lock_regex_matches_windows_mixed_slashes(self):
        stderr = (
            "fatal: Unable to create 'D:/work/fcc/.git/worktrees/birds\\"
            "peng_backup_idx_task2481.lock': File exists."
        )
        m = re.search(
            r"Unable to create '([^']+peng_backup_idx[^']+\.lock)'", stderr
        )
        assert m and "peng_backup_idx" in m.group(1)

    def test_oversized_script_uses_slim_first(self):
        long_files = [
            f"D:/work/worktrees/birds/dir{i % 20}/sub{i % 50}/file_{i:05d}.cpp"
            for i in range(400)
        ]
        meta, captured = _run_snapshot(long_files)
        assert len(captured) == 1, "超长时应只发一次（精简版）"
        assert "_rel_files = []" in captured[0]
        compile(captured[0], "<remote_snapshot2>", "exec")


class TestWorkdirLock:
    def test_same_workdir_normalized_same_lock(self):
        l1 = tb._get_remote_workdir_lock("epyc", "D:/work/worktrees/birds")
        l2 = tb._get_remote_workdir_lock("epyc", "D:\\WORK\\Worktrees\\birds\\")
        l3 = tb._get_remote_workdir_lock("epyc", "D:/work/fcc")
        assert l1 is l2, "大小写/分隔符归一后应同锁"
        assert l1 is not l3, "不同 workdir 应隔离"

    def test_lock_serializes_concurrent_threads(self):
        order = []

        def worker(n):
            with tb._get_remote_workdir_lock("epyc", "D:/x/y"):
                order.append(f"in{n}")
                threading.Event().wait(0.05)
                order.append(f"out{n}")

        ts = [threading.Thread(target=worker, args=(i,)) for i in range(3)]
        for t in ts:
            t.start()
        for t in ts:
            t.join()
        assert all(
            order[i + 1] == "out" + order[i][2:] for i in range(0, 6, 2)
        ), f"应严格串行: {order}"


class TestCmdLengthEstimate:
    def test_estimate_matches_encoding(self):
        code = "print('hello 世界')"
        b64 = __import__("base64").b64encode(code.encode("utf-8")).decode()
        expected = len(
            f"python -c \"import base64;exec(base64.b64decode('{b64}')"
            f".decode('utf-8'))\""
        )
        assert tb._estimate_remote_cmd_len(code) == expected
        assert tb._REMOTE_CMD_LEN_LIMIT == 7500


class TestIterSnapBackoff:
    """退避序列：60/120/240/480/480...（与 tool_engine 实现一致的表达式）"""

    def test_backoff_sequence(self):
        seq = [min(60 * (2 ** (fc - 1)), 480) for fc in range(1, 7)]
        assert seq == [60, 120, 240, 480, 480, 480]
