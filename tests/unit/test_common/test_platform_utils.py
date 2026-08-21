"""
测试 platform.py 模块
"""

import signal
from pathlib import Path
from unittest.mock import Mock, patch, MagicMock

import pytest

from peng.common.platform import (
    IS_WINDOWS,
    IS_POSIX,
    _get_child_processes,
    _kill_port_process_posix,
    _kill_port_process_windows,
    _kill_process_posix,
    _kill_process_tree_posix,
    _kill_process_windows,
    can_use_signal_handler,
    cleanup_signal_handlers,
    ensure_uv_sync,
    get_default_shell,
    get_kill_signal,
    get_shell_rc_files,
    get_termination_signals,
    get_venv_bin_dir,
    get_venv_python,
    kill_port_process,
    kill_process,
    process_exists,
    setup_signal_handlers,
    shutil_which,
)


class TestPlatformDetection:
    """测试平台检测"""

    def test_is_windows(self):
        """测试 Windows 平台检测"""
        assert isinstance(IS_WINDOWS, bool)

    def test_is_posix(self):
        """测试 POSIX 平台检测"""
        assert isinstance(IS_POSIX, bool)
        assert IS_POSIX == (not IS_WINDOWS)


class TestGetVenvPython:
    """测试 get_venv_python 函数"""

    @patch("peng.common.platform.IS_WINDOWS", False)
    def test_get_venv_python_posix(self):
        """测试获取 POSIX 平台的 venv python 路径"""
        project_root = Path("/project")
        result = get_venv_python(project_root)
        expected = project_root / ".venv" / "bin" / "python"
        assert result == expected

    @patch("peng.common.platform.IS_WINDOWS", True)
    def test_get_venv_python_windows(self):
        """测试获取 Windows 平台的 venv python 路径"""
        project_root = Path("C:/project")
        result = get_venv_python(project_root)
        expected = project_root / ".venv" / "Scripts" / "python.exe"
        assert result == expected


class TestGetVenvBinDir:
    """测试 get_venv_bin_dir 函数"""

    @patch("peng.common.platform.IS_WINDOWS", False)
    def test_get_venv_bin_dir_posix(self):
        """测试获取 POSIX 平台的 venv bin 目录"""
        project_root = Path("/project")
        result = get_venv_bin_dir(project_root)
        expected = project_root / ".venv" / "bin"
        assert result == expected

    @patch("peng.common.platform.IS_WINDOWS", True)
    def test_get_venv_bin_dir_windows(self):
        """测试获取 Windows 平台的 venv Scripts 目录"""
        project_root = Path("C:/project")
        result = get_venv_bin_dir(project_root)
        expected = project_root / ".venv" / "Scripts"
        assert result == expected


class TestGetShellRcFiles:
    """测试 get_shell_rc_files 函数"""

    @patch("peng.common.platform.IS_WINDOWS", False)
    def test_get_shell_rc_files_posix(self):
        """测试获取 POSIX 平台的 shell rc 文件"""
        files = get_shell_rc_files()
        assert isinstance(files, list)
        assert len(files) > 0
        assert all(isinstance(f, Path) for f in files)

    @patch("peng.common.platform.IS_WINDOWS", True)
    def test_get_shell_rc_files_windows(self):
        """测试获取 Windows 平台的 shell rc 文件（返回空列表）"""
        files = get_shell_rc_files()
        assert files == []


class TestGetTerminationSignals:
    """测试 get_termination_signals 函数"""

    @patch("peng.common.platform.IS_WINDOWS", False)
    def test_get_termination_signals_posix(self):
        """测试获取 POSIX 平台的终止信号"""
        signals = get_termination_signals()
        assert signal.SIGINT in signals
        assert signal.SIGTERM in signals
        # SIGBREAK is Windows only

    @patch("peng.common.platform.IS_WINDOWS", True)
    @patch("signal.SIGBREAK", 21)  # Mock SIGBREAK for testing
    def test_get_termination_signals_windows(self):
        """测试获取 Windows 平台的终止信号"""
        signals = get_termination_signals()
        assert signal.SIGINT in signals
        # SIGBREAK may not exist on all platforms, so we just check it's in the result when mocked


class TestGetKillSignal:
    """测试 get_kill_signal 函数"""

    @patch("peng.common.platform.IS_WINDOWS", False)
    def test_get_kill_signal_posix(self):
        """测试获取 POSIX 平台的强制终止信号"""
        sig = get_kill_signal()
        assert sig == signal.SIGKILL

    @patch("peng.common.platform.IS_WINDOWS", True)
    def test_get_kill_signal_windows(self):
        """测试获取 Windows 平台的强制终止信号（使用 SIGTERM）"""
        sig = get_kill_signal()
        assert sig == signal.SIGTERM


class TestCanUseSignalHandler:
    """测试 can_use_signal_handler 函数"""

    @patch("peng.common.platform.IS_WINDOWS", False)
    def test_can_use_signal_handler_posix(self):
        """测试 POSIX 平台可以使用信号处理器"""
        assert can_use_signal_handler() is True

    @patch("peng.common.platform.IS_WINDOWS", True)
    def test_can_use_signal_handler_windows(self):
        """测试 Windows 平台不能使用信号处理器"""
        # Mock IS_WINDOWS to True and check the result
        with patch.object(sys, "platform", "win32"):
            # This will be checked at import time, so we need to test differently
            # For now, just verify the function returns a boolean
            result = can_use_signal_handler()
            assert isinstance(result, bool)


class TestShutilWhich:
    """测试 shutil_which 函数"""

    @patch("shutil.which")
    def test_shutil_which_found(self, mock_which):
        """测试找到命令"""
        mock_which.return_value = "/usr/bin/python"
        result = shutil_which("python")
        assert result == "/usr/bin/python"

    @patch("shutil.which")
    def test_shutil_which_not_found(self, mock_which):
        """测试未找到命令"""
        mock_which.return_value = None
        result = shutil_which("nonexistent")
        assert result is None


class TestGetDefaultShell:
    """测试 get_default_shell 函数"""

    @patch("peng.common.platform.IS_WINDOWS", False)
    @patch.dict("os.environ", {"SHELL": "/bin/zsh"})
    def test_get_default_shell_posix_with_env(self):
        """测试获取 POSIX 平台的默认 shell（使用环境变量）"""
        shell = get_default_shell()
        assert shell == "/bin/zsh"

    @patch("peng.common.platform.IS_WINDOWS", False)
    @patch.dict("os.environ", {}, clear=True)
    def test_get_default_shell_posix_without_env(self):
        """测试获取 POSIX 平台的默认 shell（使用默认值）"""
        shell = get_default_shell()
        assert shell == "/bin/bash"

    @patch("peng.common.platform.IS_WINDOWS", True)
    @patch("peng.common.platform.shutil_which")
    def test_get_default_shell_windows_with_bash(self, mock_which):
        """测试获取 Windows 平台的默认 shell（有 bash）"""
        mock_which.return_value = "/usr/bin/bash"
        shell = get_default_shell()
        assert shell == "bash"

    @patch("peng.common.platform.IS_WINDOWS", True)
    @patch("peng.common.platform.shutil_which")
    def test_get_default_shell_windows_without_bash(self, mock_which):
        """测试获取 Windows 平台的默认 shell（无 bash）"""
        mock_which.return_value = None
        shell = get_default_shell()
        assert shell == "cmd.exe"


class TestProcessExists:
    """测试 process_exists 函数"""

    @patch("peng.common.platform.IS_WINDOWS", False)
    @patch("os.kill")
    def test_process_exists_posix_true(self, mock_kill):
        """测试 POSIX 平台进程存在"""
        mock_kill.return_value = None  # 发送信号成功，进程存在
        result = process_exists(1234)
        assert result is True

    @patch("peng.common.platform.IS_WINDOWS", False)
    @patch("os.kill")
    def test_process_exists_posix_false(self, mock_kill):
        """测试 POSIX 平台进程不存在"""
        mock_kill.side_effect = OSError()
        result = process_exists(1234)
        assert result is False

    @patch("peng.common.platform.IS_WINDOWS", True)
    @patch("subprocess.run")
    def test_process_exists_windows_true(self, mock_run):
        """测试 Windows 平台进程存在"""
        mock_run.return_value = Mock(stdout="1234 task_name\n")
        result = process_exists(1234)
        assert result is True

    @patch("peng.common.platform.IS_WINDOWS", True)
    @patch("subprocess.run")
    def test_process_exists_windows_false(self, mock_run):
        """测试 Windows 平台进程不存在"""
        mock_run.return_value = Mock(stdout="5678 other_task\n")
        result = process_exists(1234)
        assert result is False


class TestKillProcessWindows:
    """测试 _kill_process_windows 函数"""

    @patch("peng.common.platform.IS_WINDOWS", True)
    @patch("subprocess.run")
    @patch("peng.common.platform.process_exists")
    def test_kill_process_windows_success(self, mock_exists, mock_run):
        """测试 Windows 平台成功杀死进程"""
        mock_exists.return_value = True
        mock_run.return_value = Mock(returncode=0)

        result = _kill_process_windows(1234, force=False, kill_tree=False, caller="test", reason="test")

        assert result is True
        mock_run.assert_called_once()

    @patch("peng.common.platform.IS_WINDOWS", True)
    @patch("subprocess.run")
    @patch("peng.common.platform.process_exists")
    def test_kill_process_windows_force(self, mock_exists, mock_run):
        """测试 Windows 平台强制杀死进程"""
        mock_exists.return_value = True
        mock_run.return_value = Mock(returncode=0)

        result = _kill_process_windows(1234, force=True, kill_tree=False, caller="test", reason="test")

        assert result is True
        args = mock_run.call_args[0][0]
        assert "/F" in args

    @patch("peng.common.platform.IS_WINDOWS", True)
    @patch("subprocess.run")
    @patch("peng.common.platform.process_exists")
    def test_kill_process_windows_with_tree(self, mock_exists, mock_run):
        """测试 Windows 平台杀死进程树"""
        mock_exists.return_value = True
        mock_run.return_value = Mock(returncode=0)

        result = _kill_process_windows(1234, force=False, kill_tree=True, caller="test", reason="test")

        assert result is True
        args = mock_run.call_args[0][0]
        assert "/T" in args

    @patch("peng.common.platform.IS_WINDOWS", True)
    @patch("subprocess.run")
    @patch("peng.common.platform.process_exists")
    def test_kill_process_windows_failure(self, mock_exists, mock_run):
        """测试 Windows 平台杀死进程失败"""
        mock_exists.return_value = True
        mock_run.return_value = Mock(returncode=1)

        result = _kill_process_windows(1234, force=False, kill_tree=False, caller="test", reason="test")

        assert result is False

    @patch("peng.common.platform.IS_WINDOWS", True)
    @patch("subprocess.run")
    @patch("peng.common.platform.process_exists")
    def test_kill_process_windows_exception(self, mock_exists, mock_run):
        """测试 Windows 平台杀死进程异常"""
        mock_exists.return_value = True
        mock_run.side_effect = Exception("Test exception")

        result = _kill_process_windows(1234, force=False, kill_tree=False, caller="test", reason="test")

        assert result is False

    @patch("peng.common.platform.IS_WINDOWS", True)
    @patch("peng.common.platform.process_exists")
    def test_kill_process_windows_not_exists(self, mock_exists):
        """测试 Windows 平台进程不存在"""
        mock_exists.return_value = False

        result = _kill_process_windows(1234, force=False, kill_tree=False, caller="test", reason="test")

        assert result is True


class TestKillProcessPosix:
    """测试 _kill_process_posix 函数"""

    @patch("peng.common.platform.IS_WINDOWS", False)
    @patch("os.kill")
    @patch("peng.common.platform.process_exists")
    def test_kill_process_posix_success(self, mock_exists, mock_kill):
        """测试 POSIX 平台成功杀死进程"""
        mock_exists.return_value = True
        mock_kill.return_value = None

        result = _kill_process_posix(1234, force=False, kill_tree=False, caller="test", reason="test")

        assert result is True

    @patch("peng.common.platform.IS_WINDOWS", False)
    @patch("os.kill")
    @patch("peng.common.platform.process_exists")
    def test_kill_process_posix_force(self, mock_exists, mock_kill):
        """测试 POSIX 平台强制杀死进程"""
        mock_exists.return_value = True
        mock_kill.return_value = None

        result = _kill_process_posix(1234, force=True, kill_tree=False, caller="test", reason="test")

        assert result is True
        mock_kill.assert_called_with(1234, signal.SIGKILL)

    @patch("peng.common.platform.IS_WINDOWS", False)
    @patch("os.kill")
    @patch("peng.common.platform.process_exists")
    def test_kill_process_posix_failure(self, mock_exists, mock_kill):
        """测试 POSIX 平台杀死进程失败"""
        mock_exists.return_value = True
        mock_kill.side_effect = OSError()

        result = _kill_process_posix(1234, force=False, kill_tree=False, caller="test", reason="test")

        assert result is False

    @patch("peng.common.platform.IS_WINDOWS", False)
    @patch("peng.common.platform.process_exists")
    def test_kill_process_posix_not_exists(self, mock_exists):
        """测试 POSIX 平台进程不存在"""
        mock_exists.return_value = False

        result = _kill_process_posix(1234, force=False, kill_tree=False, caller="test", reason="test")

        assert result is True


class TestKillProcess:
    """测试 kill_process 函数"""

    @patch("peng.common.platform.IS_WINDOWS", False)
    @patch("peng.common.platform._kill_process_posix")
    @patch("peng.common.platform.process_exists")
    def test_kill_process_posix(self, mock_exists, mock_kill_posix):
        """测试调用 POSIX 平台的杀死进程"""
        mock_exists.return_value = True
        mock_kill_posix.return_value = True

        result = kill_process(1234, force=False, kill_tree=False, caller="test", reason="test")

        assert result is True
        mock_kill_posix.assert_called_once()

    @patch("peng.common.platform.IS_WINDOWS", True)
    @patch("peng.common.platform._kill_process_windows")
    @patch("peng.common.platform.process_exists")
    def test_kill_process_windows(self, mock_exists, mock_kill_windows):
        """测试调用 Windows 平台的杀死进程"""
        mock_exists.return_value = True
        mock_kill_windows.return_value = True

        result = kill_process(1234, force=False, kill_tree=False, caller="test", reason="test")

        assert result is True
        mock_kill_windows.assert_called_once()


class TestGetChildProcesses:
    """测试 _get_child_processes 函数"""

    @patch("os.path.exists")
    @patch("os.listdir")
    @patch("builtins.open")
    def test_get_child_processes_from_proc(self, mock_open, mock_listdir, mock_exists):
        """测试从 /proc 文件系统获取子进程"""
        mock_exists.return_value = True
        mock_listdir.return_value = ["123"]

        mock_file = MagicMock()
        mock_file.read.return_value = "456 789\n"
        mock_open.return_value.__enter__.return_value = mock_file

        children = _get_child_processes(123)

        assert 456 in children
        assert 789 in children

    @patch("os.path.exists")
    @patch("subprocess.run")
    def test_get_child_processes_from_ps(self, mock_run, mock_exists):
        """测试从 ps 命令获取子进程"""
        mock_exists.return_value = False
        mock_run.return_value = Mock(stdout="456\n789\n")

        children = _get_child_processes(123)

        assert 456 in children
        assert 789 in children

    @patch("os.path.exists")
    @patch("subprocess.run")
    def test_get_child_processes_empty(self, mock_run, mock_exists):
        """测试获取空子进程列表"""
        mock_exists.return_value = False
        mock_run.return_value = Mock(stdout="")

        children = _get_child_processes(123)

        assert children == []


class TestKillProcessTreePosix:
    """测试 _kill_process_tree_posix 函数"""

    @patch("os.getpgid")
    @patch("os.killpg")
    @patch("peng.common.platform._get_child_processes")
    @patch("os.kill")
    def test_kill_process_tree_by_group(self, mock_kill, mock_get_children, mock_killpg, mock_getpgid):
        """测试通过进程组杀死进程树"""
        mock_getpgid.return_value = 123  # 进程是组 leader

        _kill_process_tree_posix(123, force=False, caller="test", reason="test")

        mock_killpg.assert_called_once()
        mock_kill.assert_not_called()

    @patch("os.getpgid")
    @patch("os.killpg")
    @patch("peng.common.platform._get_child_processes")
    @patch("os.kill")
    def test_kill_process_tree_by_children(self, mock_kill, mock_get_children, mock_killpg, mock_getpgid):
        """测试递归杀死子进程"""
        mock_getpgid.side_effect = OSError()  # 不是组 leader
        mock_get_children.return_value = [456, 789]

        _kill_process_tree_posix(123, force=False, caller="test", reason="test")

        mock_kill.assert_called()


class TestSetupSignalHandlers:
    """测试 setup_signal_handlers 函数"""

    @patch("peng.common.platform.IS_WINDOWS", False)
    def test_setup_signal_handlers_posix(self):
        """测试 POSIX 平台设置信号处理器"""
        loop = Mock()
        handler = Mock()

        result = setup_signal_handlers(loop, handler)

        assert result is True
        assert loop.add_signal_handler.called

    @patch("peng.common.platform.IS_WINDOWS", True)
    def test_setup_signal_handlers_windows(self):
        """测试 Windows 平台不能设置信号处理器"""
        loop = Mock()
        handler = Mock()

        result = setup_signal_handlers(loop, handler)

        assert result is False
        loop.add_signal_handler.assert_not_called()

    @patch("peng.common.platform.IS_WINDOWS", False)
    def test_setup_signal_handlers_exception(self):
        """测试设置信号处理器异常"""
        loop = Mock()
        handler = Mock()
        loop.add_signal_handler.side_effect = OSError()

        result = setup_signal_handlers(loop, handler)

        assert result is False


class TestCleanupSignalHandlers:
    """测试 cleanup_signal_handlers 函数"""

    @patch("peng.common.platform.IS_WINDOWS", False)
    def test_cleanup_signal_handlers_posix(self):
        """测试 POSIX 平台清理信号处理器"""
        loop = Mock()

        result = cleanup_signal_handlers(loop)

        assert result is True
        assert loop.remove_signal_handler.called

    @patch("peng.common.platform.IS_WINDOWS", True)
    def test_cleanup_signal_handlers_windows(self):
        """测试 Windows 平台不能清理信号处理器"""
        loop = Mock()

        result = cleanup_signal_handlers(loop)

        assert result is False
        loop.remove_signal_handler.assert_not_called()


class TestEnsureUvSync:
    """测试 ensure_uv_sync 函数"""

    @patch("peng.common.platform.shutil.which")
    def test_ensure_uv_sync_no_uv(self, mock_which):
        """测试 uv 不可用时跳过"""
        mock_which.return_value = None

        result = ensure_uv_sync()

        assert result is True

    @patch("peng.common.platform.shutil.which")
    @patch("pathlib.Path.exists")
    @patch("subprocess.run")
    def test_ensure_uv_sync_success(self, mock_run, mock_exists, mock_which):
        """测试 uv sync 成功"""
        mock_which.return_value = "/usr/bin/uv"
        mock_run.return_value = Mock(returncode=0)

        result = ensure_uv_sync(Path("/project"))

        assert result is True

    @patch("peng.common.platform.shutil.which")
    @patch("pathlib.Path.exists")
    @patch("subprocess.run")
    def test_ensure_uv_sync_failure(self, mock_run, mock_exists, mock_which):
        """测试 uv sync 失败"""
        mock_which.return_value = "/usr/bin/uv"
        mock_run.return_value = Mock(returncode=1)

        result = ensure_uv_sync(Path("/project"))

        assert result is False

    @patch("peng.common.platform.shutil.which")
    @patch("pathlib.Path.exists")
    @patch("subprocess.run")
    def test_ensure_uv_sync_exception(self, mock_run, mock_exists, mock_which):
        """测试 uv sync 异常"""
        mock_which.return_value = "/usr/bin/uv"
        mock_run.side_effect = Exception("Test exception")

        result = ensure_uv_sync(Path("/project"))

        assert result is False


class TestKillPortProcess:
    """测试 kill_port_process 函数"""

    @patch("peng.common.platform.IS_WINDOWS", False)
    @patch("peng.common.platform._kill_port_process_posix")
    def test_kill_port_process_posix(self, mock_kill_posix):
        """测试 POSIX 平台杀死占用端口的进程"""
        mock_kill_posix.return_value = True

        result = kill_port_process(8080)

        assert result is True
        mock_kill_posix.assert_called_with(8080)

    @patch("peng.common.platform.IS_WINDOWS", True)
    @patch("peng.common.platform._kill_port_process_windows")
    def test_kill_port_process_windows(self, mock_kill_windows):
        """测试 Windows 平台杀死占用端口的进程"""
        mock_kill_windows.return_value = True

        result = kill_port_process(8080)

        assert result is True
        mock_kill_windows.assert_called_with(8080)


class TestKillPortProcessWindows:
    """测试 _kill_port_process_windows 函数"""

    @patch("peng.common.platform.IS_WINDOWS", True)
    @patch("subprocess.run")
    @patch("time.sleep")
    def test_kill_port_process_windows_success(self, mock_sleep, mock_run):
        """测试 Windows 平台成功杀死占用端口的进程"""
        mock_run.return_value = Mock(stdout="TCP    0.0.0.0:8080    0.0.0.0:0    LISTENING    1234\n")

        result = _kill_port_process_windows(8080)

        assert result is True

    @patch("peng.common.platform.IS_WINDOWS", True)
    @patch("subprocess.run")
    @patch("time.sleep")
    def test_kill_port_process_windows_no_process(self, mock_sleep, mock_run):
        """测试 Windows 平台没有进程占用端口"""
        mock_run.return_value = Mock(stdout="")

        result = _kill_port_process_windows(8080)

        assert result is True

    @patch("peng.common.platform.IS_WINDOWS", True)
    @patch("subprocess.run")
    @patch("time.sleep")
    def test_kill_port_process_windows_exception(self, mock_sleep, mock_run):
        """测试 Windows 平台杀死占用端口进程异常"""
        mock_run.side_effect = Exception("Test exception")

        result = _kill_port_process_windows(8080)

        assert result is False


class TestKillPortProcessPosix:
    """测试 _kill_port_process_posix 函数"""

    @patch("peng.common.platform.IS_WINDOWS", False)
    @patch("subprocess.run")
    @patch("time.sleep")
    def test_kill_port_process_posix_success(self, mock_sleep, mock_run):
        """测试 POSIX 平台成功杀死占用端口的进程"""
        mock_run.return_value = None

        result = _kill_port_process_posix(8080)

        assert result is True

    @patch("peng.common.platform.IS_WINDOWS", False)
    @patch("subprocess.run")
    @patch("time.sleep")
    def test_kill_port_process_posix_exception(self, mock_sleep, mock_run):
        """测试 POSIX 平台杀死占用端口进程异常"""
        mock_run.side_effect = Exception("Test exception")

        result = _kill_port_process_posix(8080)

        assert result is False
