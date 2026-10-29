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


class TestRemoteKillSync:
    """_kill_remote_daemon_sync 两级通道（进程内bridge → HTTP API fallback）"""

    def _no_conn_mgr(self, monkeypatch):
        """进程内无nest连接（CLI独立进程场景）"""

        class FakeMgr:
            def get_connection(self, name):
                return None

        import peng.nest.bridge as bridge

        monkeypatch.setattr(bridge.NestBridgeManager, "get_instance", lambda: FakeMgr())

    def test_kill_sync_uses_bridge_connection(self, monkeypatch):
        """进程内有nest连接时优先使用（不发HTTP）"""
        http_called = []
        monkeypatch.setattr(dr, "_http_nest_exec", lambda *a, **k: http_called.append(1))

        class FakeConn:
            async def send_request(self, request, timeout):
                class R:
                    error = None
                    data = {"exit_code": 0, "stdout": "DEAD", "stderr": ""}

                return R()

        class FakeMgr:
            def get_connection(self, name):
                return FakeConn()

        import peng.nest.bridge as bridge

        monkeypatch.setattr(bridge.NestBridgeManager, "get_instance", lambda: FakeMgr())
        ok, msg = dr._kill_remote_daemon_sync("epyc", 100, "linux")
        assert ok and "已停止" in msg
        assert not http_called

    def test_kill_sync_fallback_http(self, monkeypatch):
        """进程内无nest连接（CLI独立进程）→ fallback HTTP API杀远程daemon"""
        self._no_conn_mgr(monkeypatch)
        http_cmds = []

        def fake_http(nest, cmd, timeout=15):
            http_cmds.append((nest, cmd, timeout))
            return {"exit_code": 0, "stdout": "DEAD", "stderr": ""}

        monkeypatch.setattr(dr, "_http_nest_exec", fake_http)
        ok, msg = dr._kill_remote_daemon_sync("epyc", 26180, "windows")
        assert ok and "已停止" in msg
        # kill命令经HTTP转发，且用更长timeout
        assert http_cmds and http_cmds[0][0] == "epyc"
        assert "taskkill /PID 26180" in http_cmds[0][1]
        assert http_cmds[0][2] > 15

    def test_kill_sync_http_still_alive(self, monkeypatch):
        """HTTP fallback执行后进程仍存活 → 失败"""
        self._no_conn_mgr(monkeypatch)
        monkeypatch.setattr(
            dr, "_http_nest_exec",
            lambda nest, cmd, timeout=15: {"exit_code": 1, "stdout": "ALIVE", "stderr": "x"},
        )
        ok, msg = dr._kill_remote_daemon_sync("epyc", 100, "linux")
        assert not ok and "仍存活" in msg

    def test_kill_sync_unreachable(self, monkeypatch):
        """进程内无连接且HTTP失败 → nest连接未找到"""
        self._no_conn_mgr(monkeypatch)
        monkeypatch.setattr(dr, "_http_nest_exec", lambda nest, cmd, timeout=15: None)
        ok, msg = dr._kill_remote_daemon_sync("epyc", 100, "linux")
        assert not ok and "nest连接未找到" in msg


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
# daemon退出码捕获（wrap命令/解析/日志尾读）
# ============================================================

class TestWrapCommand:
    def test_format_and_quoting(self):
        """包装格式：用户命令shlex包裹整体执行 + 退出码标记行 + 相同退出码exit"""
        wrapped = dr.wrap_command_with_exit_code('echo "a b"; rm x y', "/tmp/log file/x.log")
        assert wrapped.startswith("bash -c ")
        assert "__peng_ec=$?" in wrapped
        # printf带前导换行（防末行无换行粘连）；%s接变量（防数值被解释为格式符）
        assert "printf '\\nDAEMON_EXIT_CODE=%s\\n' \"$__peng_ec\" >> " in wrapped
        # 含空格的日志路径被shlex.quote包裹
        assert "'/tmp/log file/x.log'" in wrapped
        assert wrapped.endswith("exit $__peng_ec")

    def test_windows_bat_outer_inner(self):
        """outer/inner双层bat：inner用户命令原样（零转义）；outer含chcp 65001
        （UTF-8解析）、cmd /s /c调inner（exit隔离、退出码可捕获、标记行必执行）、
        ">>"前空格（防"1>>"句柄歧义）、echo.空行防粘连"""
        outer, inner = dr._build_windows_daemon_bats(
            'echo "quoted & piped" | findstr x && exit 5',
            "C:\\wd",
            "C:\\wd\\.peng\\daemon\\d1.log",
            "C:\\wd\\.peng\\daemon\\d1_cmd.bat",
        )
        outer_lines = outer.splitlines()
        # inner：用户命令原样（含引号/管道/&&，无任何转义）
        assert inner.splitlines()[0] == "@echo off"
        assert inner.splitlines()[1] == 'echo "quoted & piped" | findstr x && exit 5'
        # outer：chcp + 隔离调用 + 标记行
        assert outer_lines[0] == "@echo off"
        assert outer_lines[1] == "chcp 65001 >nul"
        assert 'cd /d "C:\\wd"' in outer
        assert 'cmd /s /c "C:\\wd\\.peng\\daemon\\d1_cmd.bat" >> ' in outer
        ec_idx = next(i for i, ln in enumerate(outer_lines) if "DAEMON_EXIT_CODE" in ln)
        assert "echo DAEMON_EXIT_CODE=%ERRORLEVEL% >> " in outer_lines[ec_idx]
        assert outer_lines[ec_idx - 1].startswith("echo.>>")

    def test_parse_exit_code(self):
        assert dr.parse_exit_code("DAEMON_EXIT_CODE=7") == 7
        assert dr.parse_exit_code("out\nDAEMON_EXIT_CODE=0") == 0
        assert dr.parse_exit_code("DAEMON_EXIT_CODE=1\nDAEMON_EXIT_CODE=42") == 42  # 多次取最后
        assert dr.parse_exit_code("doneDAEMON_EXIT_CODE=0") == 0  # 旧日志粘连兼容
        assert dr.parse_exit_code("no marker here") is None
        assert dr.parse_exit_code("") is None
        assert dr.parse_exit_code(None) is None


class TestDecodeLogTail:
    """日志尾部字节解码：多编码探测/碎行丢弃/ANSI与控制字符清理（事务#1909乱码修复）"""

    def test_utf8_chinese(self):
        data = "碎行...\n[渲染统计] dt=1ms\n[粒子系统] ok\nDAEMON_EXIT_CODE=0\n".encode("utf-8")
        text = dr._decode_log_tail(data, drop_first_line=True)
        assert "[渲染统计] dt=1ms" in text
        assert "[粒子系统] ok" in text
        assert "碎行" not in text  # 碎首行被丢弃

    def test_first_line_kept_when_not_from_middle(self):
        """文件小于读取窗口（offset=0）：首行是真实内容，不丢弃"""
        data = "[渲染统计] dt=1ms\nDAEMON_EXIT_CODE=0\n".encode("utf-8")
        text = dr._decode_log_tail(data, drop_first_line=False)
        assert text.startswith("[渲染统计]")

    def test_gbk_chinese(self):
        """GBK编码日志（中文Windows程序常见）正确解码"""
        data = "碎行...\n[渲染统计] dt=1ms\nDAEMON_EXIT_CODE=0\n".encode("gbk")
        text = dr._decode_log_tail(data, drop_first_line=True)
        assert "[渲染统计] dt=1ms" in text

    def test_broken_lead_line_discarded(self):
        """尾部切割导致首行多字节字符被切碎：丢弃首行，后续行完好"""
        raw = "渲染统计行内容较长\n第二行完整\nDAEMON_EXIT_CODE=3\n".encode("utf-8")
        # 模拟从中间切割（切碎首字符的多字节序列）
        data = raw[2:]
        text = dr._decode_log_tail(data, drop_first_line=True)
        assert "第二行完整" in text
        assert "DAEMON_EXIT_CODE=3" in text

    def test_ansi_stripped(self):
        data = b"lead\n\x1b[31mRED\x1b[0m\n\x1b]8;;id:x\x07link\x1b]8;;\x07end\nDAEMON_EXIT_CODE=0\n"
        text = dr._decode_log_tail(data)
        assert "\x1b" not in text
        assert "RED" in text and "linkend" in text

    def test_control_chars_stripped(self):
        data = b"lead\nabc\x00\x01\x0bdef\nok\ttab\nDAEMON_EXIT_CODE=0\n"
        text = dr._decode_log_tail(data)
        assert "abcdef" in text
        assert "ok\ttab" in text  # \n\t保留

    def test_carriage_return_progress(self):
        """进度条\r回刷→按\n分段"""
        data = b"first line\nprog 10%\rprog 50%\rprog 100%\nDAEMON_EXIT_CODE=0\n"
        text = dr._decode_log_tail(data)
        assert "prog 100%" in text
        assert "\r" not in text

    def test_empty_and_no_newline(self):
        assert dr._decode_log_tail(b"") == ""
        # 完整合法文本块（无换行、非切割）：原样保留
        assert dr._decode_log_tail("渪碎".encode("utf-8")) == "渪碎"
        # 碎字符开头的字节（非法UTF-8前导续字节）：utf-8 replace兜底不抛异常
        assert dr._decode_log_tail(b"\xb8\xaa\xe7\xa2")  # 非空即可

    def test_invalid_bytes_fallback_replace(self):
        """既非UTF-8也非GBK的随机字节：utf-8 replace兜底不抛异常"""
        data = b"lead\n\xff\xfe\xfd garbage \xff\nline2\nDAEMON_EXIT_CODE=1\n"
        text = dr._decode_log_tail(data)
        assert "garbage" in text
        assert "DAEMON_EXIT_CODE=1" in text

    def test_read_local_log_tail_gbk_file(self, tmp_path):
        """本地GBK日志文件端到端：中文正确解码+退出码解析"""
        f = tmp_path / "daemon_gbk.log"
        f.write_bytes(
            "头部大量内容\n".encode("gbk") * 100
            + "中文渲染完成\nDAEMON_EXIT_CODE=5\n".encode("gbk")
        )
        tail = dr.read_local_log_tail(str(f))
        assert "中文渲染完成" in tail
        assert dr.parse_exit_code(tail) == 5

    def test_read_local_log_tail_small_file_keeps_first_line(self, tmp_path):
        """小文件（<读取窗口）：首行真实内容保留"""
        f = tmp_path / "small.log"
        f.write_bytes("第一行内容\nDAEMON_EXIT_CODE=0\n".encode("utf-8"))
        tail = dr.read_local_log_tail(str(f))
        assert tail.startswith("第一行内容")
        assert dr.parse_exit_code(tail) == 0

    def test_read_local_log_tail_missing(self):
        assert dr.read_local_log_tail("/nonexistent/x.log") == ""


class TestRemoteLogTailBytes:
    """远程日志尾部字节读取：nest read_bytes协议两步读取（total_size探测+尾块）"""

    @staticmethod
    def _make_resp(data=None, error=None):
        class R:
            def __init__(self):
                self.data = data
                self.error = error

        return R()

    def test_two_step_read(self, monkeypatch):
        import base64

        raw = "中文日志内容\nDAEMON_EXIT_CODE=9\n".encode("utf-8")
        calls = []

        async def fake_read_bytes(path, offset=0, chunk_size=1048576, nest_name=None):
            calls.append((offset, chunk_size))
            if len(calls) == 1:
                return self._make_resp({"total_size": 5000, "content_b64": ""})
            return self._make_resp(
                {"content_b64": base64.b64encode(raw).decode("ascii"), "total_size": 5000}
            )

        import peng.nest.client as nest_client

        monkeypatch.setattr(nest_client, "read_bytes", fake_read_bytes)

        data, from_middle = asyncio.run(dr._read_remote_log_tail("epyc", "/remote/x.log"))
        # 步骤1: offset=0探测total_size；步骤2: 尾部4096偏移=5000-4096
        assert calls[0] == (0, 1)
        assert calls[1] == (904, dr._LOG_TAIL_BYTES)
        assert from_middle is True
        assert data == raw
        text = dr._decode_log_tail(data, drop_first_line=from_middle)
        assert dr.parse_exit_code(text) == 9

    def test_small_file_from_start(self, monkeypatch):
        """小文件（total_size<窗口）：offset=0整读，from_middle=False"""
        import base64

        raw = "训练完成 loss=0.01\nDAEMON_EXIT_CODE=0\n".encode("utf-8")

        async def fake_read_bytes(path, offset=0, chunk_size=1048576, nest_name=None):
            if offset == 0 and chunk_size == 1:
                return self._make_resp({"total_size": len(raw), "content_b64": ""})
            return self._make_resp(
                {"content_b64": base64.b64encode(raw).decode("ascii"), "total_size": len(raw)}
            )

        import peng.nest.client as nest_client

        monkeypatch.setattr(nest_client, "read_bytes", fake_read_bytes)
        data, from_middle = asyncio.run(dr._read_remote_log_tail("epyc", "/remote/x.log"))
        assert from_middle is False
        text = dr._decode_log_tail(data, drop_first_line=from_middle)
        assert "训练完成 loss=0.01" in text  # 首行保留
        assert dr.parse_exit_code(text) == 0

    def test_probe_error_returns_empty(self, monkeypatch):
        async def fake_read_bytes(path, offset=0, chunk_size=1048576, nest_name=None):
            return self._make_resp(error="连接失败")

        import peng.nest.client as nest_client

        monkeypatch.setattr(nest_client, "read_bytes", fake_read_bytes)
        assert asyncio.run(dr._read_remote_log_tail("epyc", "/remote/x.log")) == (b"", False)

    def test_remote_exit_code_via_read_daemon(self, monkeypatch):
        """read_daemon_exit_code远程分支：字节协议+解码+解析"""
        raw = "训练完成 loss=0.01\nDAEMON_EXIT_CODE=0\n".encode("utf-8")

        async def fake_tail(nest_name, log_file):
            return raw, False

        monkeypatch.setattr(dr, "_read_remote_log_tail", fake_tail)
        entry = {"id": "d9", "pid": 1, "nest": "epyc", "log_file": "/remote/x.log", "platform": "windows"}
        ec, tail = asyncio.run(dr.read_daemon_exit_code(entry))
        assert ec == 0
        assert "训练完成 loss=0.01" in tail


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX专用测试")
class TestExitCodeCapture:
    """本地daemon端到端：退出码捕获写入日志尾部 + 解析"""

    def _wait_exit(self, pid: int, timeout: float = 10.0) -> None:
        deadline = time.time() + timeout
        while time.time() < deadline and _alive(pid):
            time.sleep(0.2)

    def test_local_daemon_exit_code(self, tmp_path):
        pid, log_file = dr.start_local_daemon("echo capture-ok; exit 7", tmp_path, "dcap01")
        self._wait_exit(pid)
        assert not _alive(pid)
        entry = {"id": "dcap01", "pid": pid, "nest": None, "log_file": log_file}
        ec, tail = asyncio.run(dr.read_daemon_exit_code(entry))
        assert ec == 7
        assert "capture-ok" in tail
        assert "DAEMON_EXIT_CODE=7" in tail

    def test_exit_code_zero_and_one_boundaries(self, tmp_path):
        """退出码0/1边界：echo参数双引号包裹规避 '0>>'/'1>>' fd重定向歧义"""
        for expected, cmd in [(0, "echo ok"), (1, "echo bad >&2; exit 1")]:
            pid, log_file = dr.start_local_daemon(cmd, tmp_path, f"dcap{expected}")
            self._wait_exit(pid)
            entry = {"id": f"dcap{expected}", "pid": pid, "nest": None, "log_file": log_file}
            ec, _ = asyncio.run(dr.read_daemon_exit_code(entry))
            assert ec == expected, f"cmd={cmd!r}: expected {expected}, got {ec}"

    def test_no_log_file_returns_none(self):
        ec, tail = asyncio.run(dr.read_daemon_exit_code({"id": "dx", "nest": None, "log_file": None}))
        assert ec is None and tail == ""


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX专用测试")
class TestCheckDaemonAlive:
    def test_local_alive_and_dead(self):
        proc = subprocess.Popen(["sleep", "30"])
        try:
            entry = {"id": "dca1", "pid": proc.pid, "nest": None}
            assert asyncio.run(dr.check_daemon_alive(entry)) is True
        finally:
            proc.kill()
            proc.wait()
        # 已reaped的真实死进程 + 不存在PID
        assert asyncio.run(dr.check_daemon_alive({"id": "dca1", "pid": proc.pid, "nest": None})) is False
        assert asyncio.run(dr.check_daemon_alive({"id": "dca1", "pid": 999999, "nest": None})) is False

    def test_invalid_pid(self):
        assert asyncio.run(dr.check_daemon_alive({"id": "d", "pid": 0, "nest": None})) is False


# ============================================================
# daemon退出watcher（DaemonTool）：自动结束注入提醒 / stop静默
# ============================================================

@pytest.mark.skipif(sys.platform == "win32", reason="POSIX专用测试")
class TestExitWatcher:
    @pytest.fixture
    def tool(self, monkeypatch):
        from peng.tools.bash import DaemonTool

        monkeypatch.setattr(DaemonTool, "_WATCH_LOCAL_INTERVAL", 0.2)
        return DaemonTool()

    @pytest.fixture
    def injected(self, tool):
        """mock task_manager.db，捕获add_task_info注入的提醒"""
        captured: list[tuple[int, str]] = []

        class FakeDB:
            def add_task_info(self, task_id, info_content, dedup=False):
                captured.append((task_id, info_content))
                return True

        class FakeTM:
            db = FakeDB()

        tool.set_context(task_manager=FakeTM(), task_id=42)
        return captured

    def test_notify_on_auto_exit(self, tmp_path, tool, injected):
        """daemon自动结束 → 注入info提醒（含id/命令/返回值/日志路径）+ 清理注册表条目"""
        wd = tmp_path / "wd"
        wd.mkdir()
        cmd = "echo watcher-test-output; exit 3"
        pid, log_file = dr.start_local_daemon(cmd, wd, "dwt01")
        entry = dr.register_daemon(task_id=42, command=cmd, pid=pid, log_file=log_file)

        asyncio.run(tool._watch_daemon_exit(entry))

        assert len(injected) == 1
        task_id, content = injected[0]
        assert task_id == 42
        assert entry["id"] in content                    # daemon id
        assert cmd in content                            # 命令行
        assert "返回值: 3" in content                     # 返回值
        assert log_file in content                       # log文件路径
        assert "watcher-test-output" in content          # 日志尾部预览
        assert dr.get_daemon(entry["id"]) is None        # 注册表条目已清理
        # 提醒不以感叹号开头（不打断运行中命令，非紧急注入语义）
        assert not content.lstrip().startswith(("!", "！"))

    def test_silent_when_stopped(self, tmp_path, tool, injected):
        """stop移除条目 → watcher静默退出，不注入提醒"""
        wd = tmp_path / "wd"
        wd.mkdir()
        pid, log_file = dr.start_local_daemon("sleep 60", wd, "dwt02")
        entry = dr.register_daemon(task_id=42, command="sleep 60", pid=pid, log_file=log_file)

        async def scenario():
            watcher = asyncio.ensure_future(tool._watch_daemon_exit(entry))
            await asyncio.sleep(0.3)  # watcher进入轮询循环
            await dr.stop_daemon_ids([entry["id"]])  # stop → kill + remove_daemon
            await asyncio.wait_for(watcher, timeout=3)  # watcher应静默结束（非挂死）

        asyncio.run(scenario())
        assert injected == []

    def test_start_exit_watcher_holds_ref(self, tmp_path, tool, injected):
        """_start_exit_watcher创建后台任务并纳入_exit_watchers集合，完成后自动移除"""
        cmd = "exit 0"
        pid, log_file = dr.start_local_daemon(cmd, tmp_path, "dwt03")
        entry = dr.register_daemon(task_id=42, command=cmd, pid=pid, log_file=log_file)

        async def scenario():
            tool._start_exit_watcher(entry)
            assert len(tool._exit_watchers) == 1
            for _ in range(50):  # 等待watcher完成（done_callback移除引用）
                if not tool._exit_watchers:
                    break
                await asyncio.sleep(0.2)
            assert not tool._exit_watchers

        asyncio.run(scenario())
        assert len(injected) == 1  # 自动结束仍注入提醒


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


# ============================================================
# get_alive_task_daemons（report汇报门禁，事务#2507）
# ============================================================

class TestGetAliveTaskDaemons:
    def test_no_daemons(self):
        """无注册daemon → 空列表"""
        assert asyncio.run(dr.get_alive_task_daemons(999999)) == []

    def test_local_alive_and_dead_filtering(self):
        """本地daemon：存活的返回(alive_status=alive)，已退出的被过滤"""
        # 存活进程：当前测试进程自身
        alive_entry = dr.register_daemon(
            task_id=51, command="watch alive", pid=os.getpid(), nest=None, platform="linux"
        )
        # 已退出进程：spawn后立即杀死并reap
        dead = subprocess.Popen(["sleep", "300"])
        dead_pid = dead.pid
        dead.kill()
        dead.wait(timeout=5)
        dead_entry = dr.register_daemon(
            task_id=51, command="watch dead", pid=dead_pid, nest=None, platform="linux"
        )
        result = asyncio.run(dr.get_alive_task_daemons(51))
        ids = [e["id"] for e in result]
        assert alive_entry["id"] in ids
        assert dead_entry["id"] not in ids
        for e in result:
            assert e["alive_status"] == "alive"

    def test_remote_batch_alive(self, monkeypatch):
        """远程daemon：按nest批量RPC，返回存活的；同nest多个条目共用一次RPC"""
        dr.register_daemon(task_id=52, command="http.server", pid=1234, nest="epyc", platform="linux")
        dr.register_daemon(task_id=52, command="old build", pid=5678, nest="epyc", platform="linux")

        calls = []

        class FakeResp:
            error = None
            data = {"exit_code": 0, "stdout": "1234\n", "stderr": ""}

        async def fake_bash(command, timeout, nest_name):
            calls.append((nest_name, command))
            return FakeResp()

        import peng.nest.client as nest_client

        monkeypatch.setattr(nest_client, "bash", fake_bash)
        result = asyncio.run(dr.get_alive_task_daemons(52))

        assert len(calls) == 1, "同nest两个PID应合并为一次批量RPC"
        assert "1234" in calls[0][1] and "5678" in calls[0][1]
        assert [e["pid"] for e in result] == [1234]
        assert result[0]["alive_status"] == "alive"

    def test_remote_query_fail_considered_unknown(self, monkeypatch):
        """远程nest查询失败 → 保守视为存活（alive_status=unknown），防误放行遗留daemon"""
        dr.register_daemon(task_id=53, command="svc", pid=9999, nest="epyc", platform="linux")

        class FakeResp:
            error = "WebSocket已关闭"
            data = None

        async def fake_bash(command, timeout, nest_name):
            return FakeResp()

        import peng.nest.client as nest_client

        monkeypatch.setattr(nest_client, "bash", fake_bash)
        result = asyncio.run(dr.get_alive_task_daemons(53))
        assert len(result) == 1
        assert result[0]["alive_status"] == "unknown"

    def test_mixed_local_and_remote(self, monkeypatch):
        """本地+远程混合：本地直查、远程批量RPC，各自正确归类"""
        dr.register_daemon(task_id=54, command="local svc", pid=os.getpid(), nest="local")
        dr.register_daemon(task_id=54, command="remote svc", pid=4321, nest="epyc", platform="windows")

        class FakeResp:
            error = None
            data = {"exit_code": 0, "stdout": "4321", "stderr": ""}

        async def fake_bash(command, timeout, nest_name):
            assert "Get-Process" in command  # windows平台走PowerShell命令
            return FakeResp()

        import peng.nest.client as nest_client

        monkeypatch.setattr(nest_client, "bash", fake_bash)
        result = asyncio.run(dr.get_alive_task_daemons(54))
        assert len(result) == 2
        assert all(e["alive_status"] == "alive" for e in result)
        nests = {e.get("nest") for e in result}
        assert nests == {None, "epyc"}
