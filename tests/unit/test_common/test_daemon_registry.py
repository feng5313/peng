"""
测试 daemon_registry 模块

覆盖:
- normalize_nest 归一化
- 注册表 CRUD（register/get/list/remove/task分组）
- 本地daemon启动/脱钩（不跟随task进程结束：被init收养+自成会话首进程）
- 本地kill（killpg整组）
- stop_daemon_ids / stop_all_daemons（工具层stop内置命令逻辑）
- cleanup_dead_daemons
- kill_daemon_by_id_sync / kill_task_daemons_sync / kill_all_daemons_sync
- platform kill_tree 保护已注册daemon（防线2）
"""

import asyncio
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

import peng.common.daemon_registry as dr
from peng.common.platform import kill_process


@pytest.fixture(autouse=True)
def isolated_registry(tmp_path, monkeypatch):
    """隔离daemon.json到临时目录，避免污染真实注册表"""
    reg_file = tmp_path / "daemon.json"
    monkeypatch.setattr(dr, "_get_daemon_file_path", lambda: reg_file)
    yield reg_file


def _alive(pid: int) -> bool:
    """存活检查（排除僵尸进程，与生产逻辑一致）"""
    from peng.common.platform import process_exists

    return process_exists(pid)


# ============================================================
# normalize_nest
# ============================================================

class TestNormalizeNest:
    def test_local_variants_to_none(self):
        assert dr.normalize_nest(None) is None
        assert dr.normalize_nest("local") is None
        assert dr.normalize_nest("LOCAL") is None
        assert dr.normalize_nest("") is None
        assert dr.normalize_nest("  ") is None

    def test_remote_kept(self):
        assert dr.normalize_nest("epyc") == "epyc"
        assert dr.normalize_nest(" win11 ") == "win11"


# ============================================================
# 注册表 CRUD
# ============================================================

class TestRegistryCRUD:
    def test_register_and_get(self):
        entry = dr.register_daemon(
            task_id=123, command="sleep 100", pid=99999, nest="local",
            log_file="/tmp/x.log", workdir="/tmp", platform="linux",
        )
        assert entry["id"].startswith("d")
        assert entry["pid"] == 99999
        assert entry["nest"] is None  # local归一化为None
        got = dr.get_daemon(entry["id"])
        assert got == entry

    def test_get_missing(self):
        assert dr.get_daemon("dnope") is None

    def test_remove(self):
        entry = dr.register_daemon(task_id=1, command="c", pid=1)
        removed = dr.remove_daemon(entry["id"])
        assert removed["id"] == entry["id"]
        assert dr.get_daemon(entry["id"]) is None
        assert dr.remove_daemon(entry["id"]) is None

    def test_list_and_task_grouping(self):
        e1 = dr.register_daemon(task_id=1, command="a", pid=101)
        e2 = dr.register_daemon(task_id=1, command="b", pid=102)
        e3 = dr.register_daemon(task_id=2, command="c", pid=103)
        e4 = dr.register_daemon(task_id=None, command="d", pid=104)

        all_daemons = dr.list_daemons()
        assert set(all_daemons.keys()) == {e1["id"], e2["id"], e3["id"], e4["id"]}

        task1 = dr.get_task_daemons(1)
        assert {e["id"] for e in task1} == {e1["id"], e2["id"]}

        summary = dr.get_tasks_summary()
        assert {e["id"] for e in summary["1"]} == {e1["id"], e2["id"]}
        assert {e["id"] for e in summary["2"]} == {e3["id"]}
        assert {e["id"] for e in summary[""]} == {e4["id"]}

    def test_unique_ids(self):
        ids = {dr.register_daemon(task_id=1, command="c", pid=i)["id"] for i in range(50)}
        assert len(ids) == 50


# ============================================================
# 本地daemon启动（核心：脱钩 + 会话首进程 + 日志）
# ============================================================

@pytest.mark.skipif(sys.platform == "win32", reason="POSIX专用测试")
class TestStartLocalDaemon:
    def test_start_stop_and_detachment(self, tmp_path):
        """启动daemon：立即返回PID；daemon被init收养（不跟随task进程）；
        自成会话首进程；日志写入正常；kill后整组退出"""
        workdir = tmp_path / "wd"
        workdir.mkdir()
        cmd = "echo daemon-started; sleep 300"

        t0 = time.time()
        pid, log_file = dr.start_local_daemon(cmd, workdir, "dtest01")
        elapsed = time.time() - t0
        # 非阻塞：立即返回
        assert elapsed < 5, f"daemon启动阻塞了 {elapsed:.1f}s"

        try:
            assert _alive(pid)
            # daemon自成会话首进程（pgid==pid），可与task进程组隔离
            assert os.getpgid(pid) == pid
            # daemon被init收养（父进程不是当前测试进程）——不跟随task进程结束的核心保证
            with open(f"/proc/{pid}/status") as f:
                status = f.read()
            ppid_line = [ln for ln in status.splitlines() if ln.startswith("PPid:")][0]
            ppid = int(ppid_line.split()[1])
            assert ppid != os.getpid(), "daemon不应是当前进程的子进程（应被init收养）"
            # 日志文件
            assert Path(log_file).exists()
            assert str(workdir) in log_file
            for _ in range(20):
                content = Path(log_file).read_text()
                if "daemon-started" in content:
                    break
                time.sleep(0.1)
            assert "daemon-started" in Path(log_file).read_text()
        finally:
            ok, msg = dr._kill_local_daemon(pid)
            assert ok, f"kill失败: {msg}"
            time.sleep(0.3)
            assert not _alive(pid)

    def test_kill_daemon_kills_process_group(self, tmp_path):
        """killpg整组：daemon的子进程也一起被杀"""
        workdir = tmp_path / "wd"
        workdir.mkdir()
        # daemon bash 启动一个子sleep
        cmd = "sleep 300 & echo CHILD=$!; sleep 300"
        pid, log_file = dr.start_local_daemon(cmd, workdir, "dtest02")
        try:
            for _ in range(30):
                content = Path(log_file).read_text()
                if "CHILD=" in content:
                    break
                time.sleep(0.1)
            child_pid = int([ln for ln in Path(log_file).read_text().splitlines() if ln.startswith("CHILD=")][0].split("=")[1])
            assert _alive(child_pid)
            ok, _ = dr._kill_local_daemon(pid)
            assert ok
            time.sleep(0.5)
            assert not _alive(pid)
            assert not _alive(child_pid), "daemon子进程应随进程组一起被杀"
        finally:
            if _alive(pid):
                dr._kill_local_daemon(pid)

    def test_kill_already_dead(self):
        ok, msg = dr._kill_local_daemon(999999)
        assert ok and "自行退出" in msg


# ============================================================
# stop 内置命令逻辑（工具层）
# ============================================================

class TestStopBuiltinLogic:
    def test_stop_daemon_ids(self, tmp_path):
        workdir = tmp_path / "wd"
        workdir.mkdir()
        pid, log_file = dr.start_local_daemon("sleep 300", workdir, "dtest03")
        entry = dr.register_daemon(
            task_id=7, command="sleep 300", pid=pid,
            log_file=log_file, workdir=str(workdir), platform=sys.platform,
        )
        assert _alive(pid)

        output = asyncio.run(dr.stop_daemon_ids([entry["id"]]))
        assert "已停止" in output
        assert dr.get_daemon(entry["id"]) is None  # 条目已移除
        time.sleep(0.3)
        assert not _alive(pid)

    def test_stop_missing_id(self):
        output = asyncio.run(dr.stop_daemon_ids(["dnope1"]))
        assert "不存在" in output

    def test_stop_all(self, tmp_path):
        workdir = tmp_path / "wd"
        workdir.mkdir()
        pids = []
        entries = []
        try:
            for i in range(2):
                pid, log_file = dr.start_local_daemon("sleep 300", workdir, f"dtest0{i}")
                pids.append(pid)
                entries.append(dr.register_daemon(
                    task_id=8, command="sleep 300", pid=pid, log_file=log_file,
                ))
            output = asyncio.run(dr.stop_all_daemons())
            assert output.count("✅") == 2
            assert dr.list_daemons() == {}
        finally:
            for pid in pids:
                if _alive(pid):
                    dr._kill_local_daemon(pid)

    def test_stop_all_empty(self):
        output = asyncio.run(dr.stop_all_daemons())
        assert "没有" in output


# ============================================================
# BashTool._handle_stop_builtin 拦截规则
# ============================================================

class TestStopBuiltinIntercept:
    @pytest.fixture
    def tool(self):
        from peng.tools.bash import BashTool

        return BashTool()

    def test_intercepts_pure_ids(self, tool):
        result = asyncio.run(tool._handle_stop_builtin("stop d1a2b3"))
        assert result is not None
        assert "d1a2b3" in result.summary

    def test_intercepts_multiple_ids(self, tool):
        result = asyncio.run(tool._handle_stop_builtin("stop d1 d2"))
        assert result is not None
        assert "d1" in result.summary and "d2" in result.summary

    def test_intercepts_all(self, tool):
        result = asyncio.run(tool._handle_stop_builtin("stop all"))
        assert result is not None

    def test_intercepts_usage(self, tool):
        result = asyncio.run(tool._handle_stop_builtin("stop"))
        assert result is not None
        assert "用法" in result.summary

    def test_no_intercept_shell_commands(self, tool):
        # 含shell元字符的命令不拦截（防误杀真实shell命令）
        for cmd in ["stop; rm x", "stop && echo hi", "stopall", "systemctl stop nginx", "echo stop", "stops"]:
            assert asyncio.run(tool._handle_stop_builtin(cmd)) is None, f"不应拦截: {cmd}"


# ============================================================
# cleanup_dead_daemons
# ============================================================

class TestCleanup:
    def test_cleanup_dead_local(self, monkeypatch):
        dead = dr.register_daemon(task_id=1, command="x", pid=999999, nest=None)
        alive_entry = dr.register_daemon(task_id=1, command="x", pid=1, nest="epyc")

        # 远程检查不可用（保守保留），仅本地死记录被清理
        monkeypatch.setattr(dr, "_check_remote_alive_pids_sync", lambda *a: None)
        cleaned = dr.cleanup_dead_daemons()
        assert cleaned == 1
        assert dr.get_daemon(dead["id"]) is None
        assert dr.get_daemon(alive_entry["id"]) is not None

    def test_cleanup_dead_remote(self, monkeypatch):
        """远程daemon已退出 → 记录被清理"""
        dead = dr.register_daemon(task_id=1, command="x", pid=11111, nest="epyc")
        alive = dr.register_daemon(task_id=1, command="x", pid=22222, nest="epyc")

        def fake_check(nest_name, pids, platform):
            return {p for p in pids if p == 22222}

        monkeypatch.setattr(dr, "_check_remote_alive_pids_sync", fake_check)
        cleaned = dr.cleanup_dead_daemons()
        assert cleaned == 1
        assert dr.get_daemon(dead["id"]) is None
        assert dr.get_daemon(alive["id"]) is not None

    def test_cleanup_remote_unreachable_kept(self, monkeypatch):
        """远程nest不可达（查询返回None）→ 保守保留全部记录"""
        e1 = dr.register_daemon(task_id=1, command="x", pid=11111, nest="epyc")
        e2 = dr.register_daemon(task_id=1, command="x", pid=22222, nest="win11")

        monkeypatch.setattr(dr, "_check_remote_alive_pids_sync", lambda *a: None)
        cleaned = dr.cleanup_dead_daemons()
        assert cleaned == 0
        assert dr.get_daemon(e1["id"]) is not None
        assert dr.get_daemon(e2["id"]) is not None

    def test_cleanup_remote_batching(self, monkeypatch):
        """远程记录超过批大小(50)时分批检查，全部死记录清理"""
        entries = []
        for i in range(120):
            entries.append(dr.register_daemon(task_id=1, command="x", pid=100000 + i, nest="epyc"))

        batches: list[list[int]] = []

        def fake_check(nest_name, pids, platform):
            batches.append(list(pids))
            return set()  # 全部已死

        monkeypatch.setattr(dr, "_check_remote_alive_pids_sync", fake_check)
        cleaned = dr.cleanup_dead_daemons()
        assert cleaned == 120
        assert dr.list_daemons() == {}
        assert len(batches) == 3  # 50+50+20
        assert [len(b) for b in batches] == [50, 50, 20]


# ============================================================
# 远程批量存活检查辅助函数
# ============================================================

class TestRemoteCheckHelpers:
    def test_build_check_command_windows(self):
        cmd = dr._build_remote_check_command([100, 200], "windows")
        assert cmd.startswith("powershell")
        assert "Get-Process -Id 100,200" in cmd
        assert "Select-Object -ExpandProperty Id" in cmd

    def test_build_check_command_windows_case(self):
        cmd = dr._build_remote_check_command([100], "Windows")
        assert cmd.startswith("powershell")

    def test_build_check_command_unix(self):
        cmd = dr._build_remote_check_command([100, 200], "linux")
        assert cmd == "ps -o pid= -p 100,200 2>/dev/null"
        cmd = dr._build_remote_check_command([100], "darwin")
        assert cmd == "ps -o pid= -p 100 2>/dev/null"

    def test_parse_alive_pids(self):
        assert dr._parse_alive_pids("  100\n200\n") == {100, 200}
        assert dr._parse_alive_pids("") == set()
        assert dr._parse_alive_pids(None) == set()
        assert dr._parse_alive_pids("100 200 300") == {100}

    def test_check_sync_uses_bridge_connection(self, monkeypatch):
        """进程内有nest连接时优先使用（不发HTTP）"""
        class FakeConn:
            async def send_request(self, request, timeout):
                class R:
                    error = None
                    data = {"exit_code": 0, "stdout": "100\n", "stderr": ""}

                return R()

        class FakeMgr:
            def get_connection(self, name):
                return FakeConn()

        import peng.nest.bridge as bridge

        monkeypatch.setattr(bridge.NestBridgeManager, "get_instance", lambda: FakeMgr())
        result = dr._check_remote_alive_pids_sync("epyc", [100, 200], "linux")
        assert result == {100}

    def test_check_sync_fallback_http(self, monkeypatch):
        """进程内无nest连接 → fallback HTTP API"""
        monkeypatch.setattr(
            dr, "_http_nest_exec",
            lambda nest, cmd: {"exit_code": 0, "stdout": "200\n", "stderr": ""},
        )
        # NestBridge无连接（导入成功但get_connection返回None）
        class FakeMgr:
            def get_connection(self, name):
                return None

        import peng.nest.bridge as bridge

        monkeypatch.setattr(bridge.NestBridgeManager, "get_instance", lambda: FakeMgr())
        result = dr._check_remote_alive_pids_sync("epyc", [100, 200], "linux")
        assert result == {200}

    def test_check_sync_unreachable(self, monkeypatch):
        """进程内无连接且HTTP失败 → None（保守保留）"""
        monkeypatch.setattr(dr, "_http_nest_exec", lambda nest, cmd: None)

        class FakeMgr:
            def get_connection(self, name):
                return None

        import peng.nest.bridge as bridge

        monkeypatch.setattr(bridge.NestBridgeManager, "get_instance", lambda: FakeMgr())
        assert dr._check_remote_alive_pids_sync("epyc", [100], "linux") is None

    def test_check_sync_empty_pids(self):
        assert dr._check_remote_alive_pids_sync("epyc", [], "linux") == set()


# ============================================================
# CLI同步接口
# ============================================================

class TestSyncKillAPI:
    def test_kill_by_id(self, tmp_path):
        workdir = tmp_path / "wd"
        workdir.mkdir()
        pid, log_file = dr.start_local_daemon("sleep 300", workdir, "dtest04")
        entry = dr.register_daemon(task_id=9, command="sleep 300", pid=pid, log_file=log_file)
        try:
            result = dr.kill_daemon_by_id_sync(entry["id"])
            assert len(result["killed"]) == 1
            assert result["killed"][0]["pid"] == pid
            assert result["killed"][0]["id"] == entry["id"]
            assert dr.get_daemon(entry["id"]) is None
        finally:
            if _alive(pid):
                dr._kill_local_daemon(pid)

    def test_kill_by_id_missing(self):
        result = dr.kill_daemon_by_id_sync("dnope")
        assert len(result["errors"]) == 1
        assert "不存在" in result["errors"][0]["error"]

    def test_kill_task_and_all(self, tmp_path):
        workdir = tmp_path / "wd"
        workdir.mkdir()
        p1, l1 = dr.start_local_daemon("sleep 300", workdir, "dtest05")
        p2, l2 = dr.start_local_daemon("sleep 300", workdir, "dtest06")
        e1 = dr.register_daemon(task_id=11, command="a", pid=p1, log_file=l1)
        e2 = dr.register_daemon(task_id=22, command="b", pid=p2, log_file=l2)
        try:
            result = dr.kill_task_daemons_sync(11)
            assert len(result["killed"]) == 1
            assert dr.get_daemon(e1["id"]) is None
            assert dr.get_daemon(e2["id"]) is not None

            result = dr.kill_all_daemons_sync()
            assert len(result["killed"]) == 1
            assert dr.list_daemons() == {}
        finally:
            for p in (p1, p2):
                if _alive(p):
                    dr._kill_local_daemon(p)


# ============================================================
# 防线2：platform kill_tree 保护已注册daemon
# ============================================================

@pytest.mark.skipif(sys.platform == "win32", reason="POSIX专用测试")
class TestKillTreeProtection:
    def test_registered_daemon_survives_kill_tree(self, tmp_path):
        """父进程P持有子进程C；C注册为daemon后，kill_tree(P)不杀C"""
        # P: python进程，spawn子进程C(sleep 300)并等待
        parent_code = (
            "import subprocess, time\n"
            "c = subprocess.Popen(['sleep', '300'])\n"
            "print(c.pid, flush=True)\n"
            "time.sleep(300)\n"
        )
        parent = subprocess.Popen(
            [sys.executable, "-c", parent_code],
            stdout=subprocess.PIPE,
            cwd=str(tmp_path),
        )
        try:
            child_pid = int(parent.stdout.readline().decode().strip())
            assert _alive(parent.pid) and _alive(child_pid)

            # 注册C为daemon → 受保护
            dr.register_daemon(task_id=33, command="protected", pid=child_pid)

            from peng.common.platform import _protected_daemon_pids
            assert child_pid in _protected_daemon_pids()

            # kill_tree(P)：P被杀，C因已注册保护而存活
            kill_process(parent.pid, force=True, kill_tree=True, caller="test", reason="kill_tree保护测试")
            # reap父进程（Popen对象持有，避免僵尸状态干扰存活判断）
            try:
                parent.wait(timeout=5)
            except subprocess.TimeoutExpired:
                pass
            assert not _alive(parent.pid), "父进程应被杀死"
            time.sleep(0.5)
            assert _alive(child_pid), "已注册daemon不应被kill_tree波及（防线2保护）"
        finally:
            if _alive(parent.pid):
                kill_process(parent.pid, force=True, kill_tree=False, caller="test", reason="cleanup")
            if _alive(child_pid):
                os.kill(child_pid, signal.SIGKILL)
