"""
事务#2729测试：daemon工具stop命令无法杀死进程的修复

根因（#2726实证）:
1. 复合命令 "stop d5ef7 & 重启命令" 不被stop内置命令拦截 → stop段作为
   远程shell命令执行失败（'stop'不是shell命令），daemon未被停止而后续
   命令照常执行，产生无人管理的残留进程
2. Windows远程kill命令 "taskkill /PID {pid} /T /F & exit /b 0" 尾部
   "& exit /b 0" 强制退出码0，解析器按exit_code==0判定成功 → 无论
   taskkill成败一律谎报"已停止(远程)"
3. Windows孤儿进程：daemon命令含start /b时宿主cmd链断裂，taskkill /T
   按PPID树杀不到孤儿后代

修复:
- _handle_stop_builtin 支持复合形式 stop <严格id|all> [&|&&|;] <rest>
- _build_remote_kill_command Windows分支带PowerShell存活验证(ALIVE/DEAD)
- Windows启动路径改Job Object + KILL_ON_JOB_CLOSE（ps1启动器，
  宿主退出/被杀→内核自动清杀Job内全部孤儿后代；失败降级NOJOB）
"""

import asyncio
import os
import re
import subprocess
import time
from pathlib import Path

import pytest

import peng.common.daemon_registry as dr
from peng.tools.bash import BashTool


@pytest.fixture(autouse=True)
def isolated_registry(tmp_path, monkeypatch):
    """隔离daemon.json到临时目录，避免污染真实注册表"""
    reg_file = tmp_path / "daemon.json"
    monkeypatch.setattr(dr, "_get_daemon_file_path", lambda: reg_file)
    yield reg_file


# ============================================================
# 修复2: Windows远程kill命令不再掩盖失败
# ============================================================

class TestWindowsRemoteKillCommand:
    def test_no_forced_exit_zero(self):
        """旧bug: '& exit /b 0'强制退出码0掩盖taskkill失败，必须移除"""
        cmd = dr._build_remote_kill_command(1234, "windows")
        assert "exit /b" not in cmd.lower()

    def test_taskkill_tree_force(self):
        cmd = dr._build_remote_kill_command(1234, "windows")
        assert "taskkill /PID 1234 /T /F" in cmd

    def test_alive_dead_verification(self):
        """必须有PowerShell按PID验证存活的ALIVE/DEAD输出（解析协议）"""
        cmd = dr._build_remote_kill_command(1234, "windows")
        assert "ALIVE" in cmd
        assert "DEAD" in cmd
        assert "Get-Process -Id 1234" in cmd

    def test_delay_uses_ping_not_timeout(self):
        """等待用ping（timeout命令在stdin重定向下报错，#2726实测）"""
        cmd = dr._build_remote_kill_command(1234, "windows")
        assert "ping -n 2" in cmd
        assert "timeout" not in cmd.lower()

    def test_linux_command_unchanged(self):
        """Linux分支保持组杀+验证（原有正确行为不回归）"""
        cmd = dr._build_remote_kill_command(1234, "linux")
        assert "kill -TERM -- -1234" in cmd
        assert "ALIVE" in cmd and "DEAD" in cmd

    def test_parse_alive_dead(self):
        ok, msg = dr._parse_remote_kill_result({"stdout": "DEAD"}, "windows")
        assert ok is True
        ok, msg = dr._parse_remote_kill_result({"stdout": "ALIVE"}, "windows")
        assert ok is False

    def test_windows_cmd_quoting_balanced(self):
        """PowerShell -Command体的双引号必须配对（嵌套在cmd命令行中）"""
        cmd = dr._build_remote_kill_command(1234, "windows")
        assert cmd.count('"') % 2 == 0


# ============================================================
# 修复3: Windows Job启动器（ps1静态检查 + 输出解析）
# ============================================================

class TestWindowsJobLauncher:
    def test_ps1_kill_on_job_close(self):
        assert "0x2000" in dr._WINDOWS_JOB_START_PS1  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE

    def test_ps1_create_suspended(self):
        """挂起创建消除竞态：宿主未执行任何代码即绑Job，后代必然继承"""
        assert "0x800004" in dr._WINDOWS_JOB_START_PS1  # CREATE_NO_WINDOW|CREATE_SUSPENDED

    def test_ps1_duplicate_before_assign(self):
        """先DuplicateHandle再Assign：dup失败时Assign会因powershell退出时
        kill-on-close误杀daemon（最后一个句柄关闭触发）"""
        ps1 = dr._WINDOWS_JOB_START_PS1
        dup_pos = ps1.find("DuplicateHandle([PengJobStart]::GetCurrentProcess()")
        assign_pos = ps1.find("AssignProcessToJobObject($job, $pi.hProcess)")
        assert 0 < dup_pos < assign_pos

    def test_ps1_ascii_only(self):
        """ps1只含ASCII（UTF-8无BOM写入时PowerShell 5.1按ANSI读也安全）"""
        dr._WINDOWS_JOB_START_PS1.encode("ascii")

    def test_ps1_fallback_exists(self):
        """降级路径：Job初始化失败→Start-Process（NOJOB，行为同旧版）"""
        assert "Start-Fallback" in dr._WINDOWS_JOB_START_PS1
        assert "MODE=NOJOB" in dr._WINDOWS_JOB_START_PS1 or "'NOJOB'" in dr._WINDOWS_JOB_START_PS1

    def test_parse_output_job(self):
        pid, job = dr._parse_windows_daemon_start_output("PID=123\nMODE=JOB\n")
        assert pid == 123 and job is True

    def test_parse_output_nojob(self):
        pid, job = dr._parse_windows_daemon_start_output("PID=456\nMODE=NOJOB\n")
        assert pid == 456 and job is False

    def test_parse_output_mode_missing_defaults_nojob(self):
        pid, job = dr._parse_windows_daemon_start_output("PID=789\n")
        assert pid == 789 and job is False

    def test_parse_output_garbage(self):
        pid, job = dr._parse_windows_daemon_start_output("错误: 拒绝访问。")
        assert pid is None and job is False

    def test_ps1_file_path(self):
        assert dr._windows_daemon_ps1_file(r"D:\w\.peng\daemon") == \
            r"D:\w\.peng\daemon\daemon_start.ps1"

    def test_register_daemon_job_field(self):
        entry = dr.register_daemon(task_id=1, command="x", pid=2, job=True)
        assert entry["job"] is True
        entry2 = dr.register_daemon(task_id=1, command="x", pid=3)
        assert entry2["job"] is False


# ============================================================
# 修复1: stop内置命令支持复合命令
# ============================================================

class TestStopBuiltinCompound:
    @pytest.fixture
    def tool(self, monkeypatch):
        """BashTool实例 + mock stop实现（不触碰真实进程/注册表杀逻辑）"""
        async def fake_stop_ids(ids):
            return "; ".join(f"stopped:{i}" for i in ids)

        async def fake_stop_all():
            return "stopped:all"

        monkeypatch.setattr(dr, "stop_daemon_ids", fake_stop_ids)
        monkeypatch.setattr(dr, "stop_all_daemons", fake_stop_all)
        return BashTool()

    def _run(self, tool, command):
        return asyncio.get_event_loop_policy().new_event_loop().run_until_complete(
            tool._handle_stop_builtin(command)
        )

    def test_pure_stop_id(self, tool):
        result = self._run(tool, "stop d5ef7")
        output, rest = result
        assert rest is None
        assert "stopped:d5ef7" in output.summary

    def test_pure_stop_usage(self, tool):
        output, rest = self._run(tool, "stop")
        assert rest is None
        assert "用法" in output.summary

    def test_pure_stop_all(self, tool):
        output, rest = self._run(tool, "stop all")
        assert rest is None
        assert "stopped:all" in output.summary

    def test_compound_amp(self, tool):
        """#2726实际失败场景：stop d5ef7 & 重启命令"""
        output, rest = self._run(tool, "stop d5ef7 & timeout /t 2 >nul & start /b cmd /c x.exe")
        assert "stopped:d5ef7" in output.summary
        assert rest == "timeout /t 2 >nul & start /b cmd /c x.exe"

    def test_compound_no_spaces(self, tool):
        output, rest = self._run(tool, "stop d5ef7&&echo hi")
        assert "stopped:d5ef7" in output.summary
        assert rest == "echo hi"

    def test_compound_semicolon(self, tool):
        output, rest = self._run(tool, "stop d5ef7 ; echo hi")
        assert "stopped:d5ef7" in output.summary
        assert rest == "echo hi"

    def test_compound_all(self, tool):
        output, rest = self._run(tool, "stop all & echo hi")
        assert "stopped:all" in output.summary
        assert rest == "echo hi"

    def test_compound_multiple_ids(self, tool):
        output, rest = self._run(tool, "stop d5ef7 d1234 & echo hi")
        assert "stopped:d5ef7" in output.summary
        assert "stopped:d1234" in output.summary
        assert rest == "echo hi"

    def test_compound_chained_stop(self, tool):
        """链式stop：tail再以stop开头，递归execute时继续拦截"""
        output, rest = self._run(tool, "stop d5ef7 & stop d1234 & echo done")
        assert "stopped:d5ef7" in output.summary
        assert rest == "stop d1234 & echo done"

    def test_not_intercepted_non_daemon_id(self, tool):
        """非daemon id格式的复合命令不拦截（防误拦真实shell命令）"""
        assert self._run(tool, "stop nginx & echo hi") is None
        assert self._run(tool, "stop service-name & x") is None

    def test_not_intercepted_no_space_stop(self, tool):
        assert self._run(tool, "stopall & echo hi") is None

    def test_not_intercepted_bare_stop_compound(self, tool):
        """裸stop & x：id为空不拦截（按shell命令执行报错，语义合理）"""
        assert self._run(tool, "stop & echo hi") is None

    def test_not_intercepted_pipe(self, tool):
        """管道分隔不拦截（stop输出无管道语义）"""
        assert self._run(tool, "stop d5ef7 | grep y") is None


# ============================================================
# Linux本地daemon真实冒烟：整组杀（回归保障 + 三元组返回）
# ============================================================

@pytest.mark.skipif(sys_platform_not_posix := (os.name != "posix"), reason="POSIX only")
class TestLocalDaemonGroupKill:
    def _group_pids(self, pgid: int) -> set[int]:
        """收集指定进程组内的存活进程PID"""
        pids = set()
        for pid in os.listdir("/proc"):
            if not pid.isdigit():
                continue
            try:
                if os.getpgid(int(pid)) == pgid:
                    pids.add(int(pid))
            except (ProcessLookupError, PermissionError, FileNotFoundError):
                continue
        return pids

    def test_start_returns_triple_and_group_kill(self, tmp_path):
        """启动返回三元组；命令后台派生多个子进程时kill整组全灭"""
        cmd = "sleep 300 & sleep 300 & wait"
        pid, log_file, job_mode = dr.start_local_daemon(cmd, str(tmp_path), "dffff")
        assert pid > 0
        assert Path(log_file).exists()
        assert job_mode is False  # Linux无Job概念

        try:
            # 等待两个后台sleep派生完成
            deadline = time.time() + 5
            group = set()
            while time.time() < deadline:
                group = self._group_pids(pid)
                if len(group) >= 3:  # 宿主bash + 2×sleep
                    break
                time.sleep(0.1)
            assert len(group) >= 3, f"后台子进程未派生: {group}"

            # 杀整组
            ok, msg = dr._kill_local_daemon(pid)
            assert ok, msg

            # 全组进程消亡（等清理完成）
            deadline = time.time() + 5
            while time.time() < deadline:
                if not self._group_pids(pid):
                    break
                time.sleep(0.1)
            assert not self._group_pids(pid), "进程组内仍有存活进程"
        finally:
            dr._kill_local_daemon(pid)  # 兜底清理

    def test_stop_daemon_ids_kills_registered(self, tmp_path):
        """stop_daemon_ids端到端：注册→stop→进程死+条目移除"""
        cmd = "sleep 299 & wait"
        pid, log_file, _ = dr.start_local_daemon(cmd, str(tmp_path), "dffee")
        entry = dr.register_daemon(
            task_id=2729, command=cmd, pid=pid, nest=None,
            log_file=log_file, platform="linux", daemon_id="dffee",
        )
        assert dr.get_daemon("dffee") == entry

        deadline = time.time() + 5
        while time.time() < deadline and len(self._group_pids(pid)) < 2:
            time.sleep(0.1)

        result = asyncio.get_event_loop_policy().new_event_loop().run_until_complete(
            dr.stop_daemon_ids(["dffee"])
        )
        assert "已停止" in result

        deadline = time.time() + 5
        while time.time() < deadline and self._group_pids(pid):
            time.sleep(0.1)
        assert not self._group_pids(pid)
        assert dr.get_daemon("dfee" + "e") is None  # 条目已移除
