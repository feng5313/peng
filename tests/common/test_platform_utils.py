"""
platform.py 单元测试模块

测试平台兼容性工具模块的所有公共接口：
- 虚拟环境路径解析
- 信号处理
- 进程管理
- Shell RC 文件加载
- 端口进程杀死

禁止发送/回复消息/创建事务
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from peng.common.platform import (
    IS_POSIX,
    IS_WINDOWS,
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
    """测试平台检测相关功能"""

    def test_is_windows_type(self):
        """测试 IS_WINDOWS 是布尔类型"""
        assert isinstance(IS_WINDOWS, bool)

    def test_is_posix_type(self):
        """测试 IS_POSIX 是布尔类型"""
        assert isinstance(IS_POSIX, bool)

    def test_platform_consistency(self):
        """测试平台检测的一致性（Windows 和 POSIX 互斥）"""
        assert IS_WINDOWS != IS_POSIX  # 两者应该互斥

    def test_is_windows_matches_sys_platform(self):
        """测试 IS_WINDOWS 与 sys.platform 一致"""
        expected = sys.platform == "win32"
        assert IS_WINDOWS == expected


class TestVenvPathResolution:
    """测试虚拟环境路径解析功能"""

    def test_get_venv_python_posix(self):
        """测试 POSIX 系统下获取虚拟环境 Python 路径"""
        with patch("peng.common.platform.IS_WINDOWS", False):
            project_root = Path("/home/user/project")
            result = get_venv_python(project_root)
            expected = project_root / ".venv" / "bin" / "python"
            assert result == expected

    def test_get_venv_python_windows(self):
        """测试 Windows 系统下获取虚拟环境 Python 路径"""
        with patch("peng.common.platform.IS_WINDOWS", True):
            project_root = Path("C:\\Users\\user\\project")
            result = get_venv_python(project_root)
            expected = project_root / ".venv" / "Scripts" / "python.exe"
            assert result == expected

    def test_get_venv_bin_dir_posix(self):
        """测试 POSIX 系统下获取虚拟环境 bin 目录路径"""
        with patch("peng.common.platform.IS_WINDOWS", False):
            project_root = Path("/home/user/project")
            result = get_venv_bin_dir(project_root)
            expected = project_root / ".venv" / "bin"
            assert result == expected

    def test_get_venv_bin_dir_windows(self):
        """测试 Windows 系统下获取虚拟环境 Scripts 目录路径"""
        with patch("peng.common.platform.IS_WINDOWS", True):
            project_root = Path("C:\\Users\\user\\project")
            result = get_venv_bin_dir(project_root)
            expected = project_root / ".venv" / "Scripts"
            assert result == expected


class TestShellRCFiles:
    """测试 Shell RC 文件加载功能"""

    def test_get_shell_rc_files_windows(self):
        """测试 Windows 系统下返回空列表"""
        with patch("peng.common.platform.IS_WINDOWS", True):
            result = get_shell_rc_files()
            assert result == []

    def test_get_shell_rc_files_posix(self):
        """测试 POSIX 系统下返回 RC 文件列表"""
        with patch("peng.common.platform.IS_WINDOWS", False):
            result = get_shell_rc_files()
            assert len(result) == 2
            assert Path.home() / ".zshrc" in result
            assert Path.home() / ".bashrc" in result


class TestTerminationSignals:
    """测试进程终止信号功能"""

    def test_get_termination_signals_windows(self):
        """测试 Windows 系统下返回的信号"""
        with patch("peng.common.platform.IS_WINDOWS", True):
            result = get_termination_signals()
            assert signal.SIGINT in result
            assert signal.SIGBREAK in result

    def test_get_termination_signals_posix(self):
        """测试 POSIX 系统下返回的信号"""
        with patch("peng.common.platform.IS_WINDOWS", False):
            result = get_termination_signals()
            assert signal.SIGINT in result
            assert signal.SIGTERM in result

    def test_get_kill_signal_windows(self):
        """测试 Windows 系统下返回 SIGTERM（因为没有 SIGKILL）"""
        with patch("peng.common.platform.IS_WINDOWS", True):
            result = get_kill_signal()
            assert result == signal.SIGTERM

    def test_get_kill_signal_posix(self):
        """测试 POSIX 系统下返回 SIGKILL"""
        with patch("peng.common.platform.IS_WINDOWS", False):
            result = get_kill_signal()
            assert result == signal.SIGKILL

    def test_can_use_signal_handler_posix(self):
        """测试 POSIX 系统下可以使用信号处理器"""
        with patch("peng.common.platform.IS_WINDOWS", False):
            result = can_use_signal_handler()
            assert result is True

    def test_can_use_signal_handler_windows(self):
        """测试 Windows 系统下不能使用信号处理器"""
        with patch("peng.common.platform.IS_WINDOWS", True):
            result = can_use_signal_handler()
            assert result is False


class TestSignalHandlers:
    """测试信号处理器设置和清理功能"""

    def test_setup_signal_handlers_unsupported_platform(self):
        """测试在不支持的平台（Windows）上设置信号处理器"""
        with patch("peng.common.platform.can_use_signal_handler", return_value=False):
            loop = MagicMock()
            handler = MagicMock()
            result = setup_signal_handlers(loop, handler)
            assert result is False
            loop.add_signal_handler.assert_not_called()

    def test_setup_signal_handlers_success(self):
        """测试成功设置信号处理器"""
        with patch("peng.common.platform.can_use_signal_handler", return_value=True):
            with patch("peng.common.platform.get_termination_signals") as mock_get_signals:
                mock_get_signals.return_value = (signal.SIGINT, signal.SIGTERM)
                loop = MagicMock()
                handler = MagicMock()
                result = setup_signal_handlers(loop, handler)
                assert result is True
                assert loop.add_signal_handler.call_count == 2

    def test_setup_signal_handlers_with_custom_signals(self):
        """测试使用自定义信号设置信号处理器"""
        with patch("peng.common.platform.can_use_signal_handler", return_value=True):
            loop = MagicMock()
            handler = MagicMock()
            custom_signals = (signal.SIGUSR1,)
            result = setup_signal_handlers(loop, handler, signals=custom_signals)
            assert result is True
            loop.add_signal_handler.assert_called_once_with(signal.SIGUSR1, handler)

    def test_setup_signal_handlers_exception(self):
        """测试设置信号处理器时发生异常"""
        with patch("peng.common.platform.can_use_signal_handler", return_value=True):
            with patch("peng.common.platform.get_termination_signals") as mock_get_signals:
                mock_get_signals.return_value = (signal.SIGINT,)
                loop = MagicMock()
                loop.add_signal_handler.side_effect = NotImplementedError()
                handler = MagicMock()
                result = setup_signal_handlers(loop, handler)
                assert result is False

    def test_cleanup_signal_handlers_unsupported_platform(self):
        """测试在不支持的平台（Windows）上清理信号处理器"""
        with patch("peng.common.platform.can_use_signal_handler", return_value=False):
            loop = MagicMock()
            result = cleanup_signal_handlers(loop)
            assert result is False
            loop.remove_signal_handler.assert_not_called()

    def test_cleanup_signal_handlers_success(self):
        """测试成功清理信号处理器"""
        with patch("peng.common.platform.can_use_signal_handler", return_value=True):
            with patch("peng.common.platform.get_termination_signals") as mock_get_signals:
                mock_get_signals.return_value = (signal.SIGINT, signal.SIGTERM)
                loop = MagicMock()
                result = cleanup_signal_handlers(loop)
                assert result is True
                assert loop.remove_signal_handler.call_count == 2

    def test_cleanup_signal_handlers_with_custom_signals(self):
        """测试使用自定义信号清理信号处理器"""
        with patch("peng.common.platform.can_use_signal_handler", return_value=True):
            loop = MagicMock()
            custom_signals = (signal.SIGUSR1,)
            result = cleanup_signal_handlers(loop, signals=custom_signals)
            assert result is True
            loop.remove_signal_handler.assert_called_once_with(signal.SIGUSR1)

    def test_cleanup_signal_handlers_exception(self):
        """测试清理信号处理器时发生异常"""
        with patch("peng.common.platform.can_use_signal_handler", return_value=True):
            with patch("peng.common.platform.get_termination_signals") as mock_get_signals:
                mock_get_signals.return_value = (signal.SIGINT,)
                loop = MagicMock()
                loop.remove_signal_handler.side_effect = NotImplementedError()
                result = cleanup_signal_handlers(loop)
                assert result is False


class TestProcessExists:
    """测试进程存在检查功能"""

    def test_process_exists_posix_current_process(self):
        """测试 POSIX 系统下检查当前进程存在"""
        with patch("peng.common.platform.IS_WINDOWS", False):
            current_pid = os.getpid()
            result = process_exists(current_pid)
            assert result is True

    def test_process_exists_posix_nonexistent_process(self):
        """测试 POSIX 系统下检查不存在的进程"""
        with patch("peng.common.platform.IS_WINDOWS", False):
            # 使用一个极不可能存在的 PID
            result = process_exists(999999999)
            assert result is False

    def test_process_exists_windows_mock(self):
        """测试 Windows 系统下检查进程存在（mock）"""
        with patch("peng.common.platform.IS_WINDOWS", True):
            with patch("subprocess.run") as mock_run:
                mock_run.return_value = MagicMock(
                    returncode=0, stdout="python.exe  12345"
                )
                result = process_exists(12345)
                assert result is True

    def test_process_exists_windows_exception(self):
        """测试 Windows 系统下检查进程存在时发生异常"""
        with patch("peng.common.platform.IS_WINDOWS", True):
            with patch("subprocess.run") as mock_run:
                mock_run.side_effect = Exception("Test exception")
                result = process_exists(12345)
                assert result is False


class TestKillProcess:
    """测试进程杀死功能"""

    def test_kill_process_nonexistent_process(self):
        """测试杀死不存在的进程（直接返回 True）"""
        with patch("peng.common.platform.process_exists", return_value=False):
            result = kill_process(999999999, caller="test", reason="test")
            assert result is True

    def test_kill_process_windows(self):
        """测试 Windows 系统下杀死进程"""
        with patch("peng.common.platform.IS_WINDOWS", True):
            with patch("peng.common.platform.process_exists", return_value=True):
                with patch("subprocess.run") as mock_run:
                    mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
                    result = kill_process(12345, force=True, kill_tree=True, caller="test", reason="test")
                    assert result is True
                    mock_run.assert_called_once()
                    call_args = mock_run.call_args[0][0]
                    assert "taskkill" in call_args
                    assert "/F" in call_args
                    assert "/T" in call_args

    def test_kill_process_windows_failure(self):
        """测试 Windows 系统下杀死进程失败"""
        with patch("peng.common.platform.IS_WINDOWS", True):
            with patch("peng.common.platform.process_exists", return_value=True):
                with patch("subprocess.run") as mock_run:
                    mock_run.return_value = MagicMock(returncode=1, stdout="", stderr="Error")
                    result = kill_process(12345, caller="test", reason="test")
                    assert result is False

    def test_kill_process_windows_exception(self):
        """测试 Windows 系统下杀死进程时发生异常"""
        with patch("peng.common.platform.IS_WINDOWS", True):
            with patch("peng.common.platform.process_exists", return_value=True):
                with patch("subprocess.run") as mock_run:
                    mock_run.side_effect = Exception("Test exception")
                    result = kill_process(12345, caller="test", reason="test")
                    assert result is False

    def test_kill_process_posix_single(self):
        """测试 POSIX 系统下杀死单个进程"""
        with patch("peng.common.platform.IS_WINDOWS", False):
            with patch("peng.common.platform.process_exists", return_value=True):
                with patch("os.kill") as mock_kill:
                    result = kill_process(12345, kill_tree=False, caller="test", reason="test")
                    assert result is True
                    mock_kill.assert_called_once_with(12345, signal.SIGTERM)

    def test_kill_process_posix_force(self):
        """测试 POSIX 系统下强制杀死进程"""
        with patch("peng.common.platform.IS_WINDOWS", False):
            with patch("peng.common.platform.process_exists", return_value=True):
                with patch("os.kill") as mock_kill:
                    result = kill_process(12345, force=True, kill_tree=False, caller="test", reason="test")
                    assert result is True
                    mock_kill.assert_called_once_with(12345, signal.SIGKILL)

    def test_kill_process_posix_with_tree(self):
        """测试 POSIX 系统下杀死进程树"""
        with patch("peng.common.platform.IS_WINDOWS", False):
            with patch("peng.common.platform.process_exists", return_value=True):
                with patch("peng.common.platform._kill_process_tree_posix") as mock_kill_tree:
                    result = kill_process(12345, kill_tree=True, caller="test", reason="test")
                    assert result is True
                    mock_kill_tree.assert_called_once()

    def test_kill_process_posix_failure(self):
        """测试 POSIX 系统下杀死进程失败"""
        with patch("peng.common.platform.IS_WINDOWS", False):
            with patch("peng.common.platform.process_exists", return_value=True):
                with patch("os.kill") as mock_kill:
                    mock_kill.side_effect = ProcessLookupError("Process not found")
                    result = kill_process(12345, kill_tree=False, caller="test", reason="test")
                    assert result is False


class TestKillProcessTreePosix:
    """测试 POSIX 系统下进程树杀死功能"""

    def test_kill_process_tree_as_group_leader(self):
        """测试作为进程组 leader 时杀死整个进程组"""
        with patch("os.getpgid") as mock_getpgid:
            with patch("os.killpg") as mock_killpg:
                mock_getpgid.return_value = 12345  # PID == PGID，是 leader
                with patch("peng.common.platform._get_child_processes", return_value=[]):
                    from peng.common.platform import _kill_process_tree_posix
                    _kill_process_tree_posix(12345, force=False, caller="test", reason="test")
                    mock_killpg.assert_called_once_with(12345, signal.SIGTERM)

    def test_kill_process_tree_with_children(self):
        """测试有子进程时递归杀死"""
        with patch("os.getpgid") as mock_getpgid:
            mock_getpgid.side_effect = OSError()  # 无法获取进程组
            with patch("peng.common.platform._get_child_processes") as mock_get_children:
                mock_get_children.return_value = [12346, 12347]
                with patch("os.kill") as mock_kill:
                    from peng.common.platform import _kill_process_tree_posix
                    _kill_process_tree_posix(12345, force=True, caller="test", reason="test")
                    # 应该杀死主进程
                    assert mock_kill.called

    def test_get_child_processes_proc_method(self):
        """测试通过 /proc 文件系统获取子进程"""
        # 这个测试在 Linux 上才有意义
        if not IS_POSIX:
            pytest.skip("仅在 POSIX 系统上测试")

        from peng.common.platform import _get_child_processes
        # 获取 init 进程的子进程（通常会有）
        result = _get_child_processes(1)
        assert isinstance(result, list)

    def test_get_child_processes_ps_method(self):
        """测试通过 ps 命令获取子进程"""
        with patch("os.listdir") as mock_listdir:
            mock_listdir.side_effect = OSError()  # /proc 方法失败，回退到 ps
            with patch("subprocess.run") as mock_run:
                mock_run.return_value = MagicMock(
                    returncode=0, stdout="12346\n12347\n"
                )
                from peng.common.platform import _get_child_processes
                result = _get_child_processes(12345)
                assert 12346 in result
                assert 12347 in result


class TestDefaultShell:
    """测试默认 shell 获取功能"""

    def test_get_default_shell_windows_no_bash(self):
        """测试 Windows 系统下没有 bash 时返回 cmd.exe"""
        with patch("peng.common.platform.IS_WINDOWS", True):
            with patch("peng.common.platform.shutil_which", return_value=None):
                result = get_default_shell()
                assert result == "cmd.exe"

    def test_get_default_shell_windows_with_bash(self):
        """测试 Windows 系统下有 bash 时返回 bash"""
        with patch("peng.common.platform.IS_WINDOWS", True):
            with patch("peng.common.platform.shutil_which", return_value="/usr/bin/bash"):
                result = get_default_shell()
                assert result == "bash"

    def test_get_default_shell_posix(self):
        """测试 POSIX 系统下返回 SHELL 环境变量或默认 bash"""
        with patch("peng.common.platform.IS_WINDOWS", False):
            with patch.dict(os.environ, {"SHELL": "/bin/zsh"}, clear=False):
                result = get_default_shell()
                assert result == "/bin/zsh"

    def test_get_default_shell_posix_no_env(self):
        """测试 POSIX 系统下没有 SHELL 环境变量时返回默认 bash"""
        with patch("peng.common.platform.IS_WINDOWS", False):
            with patch.dict(os.environ, {}, clear=True):
                result = get_default_shell()
                assert result == "/bin/bash"


class TestShutilWhich:
    """测试命令查找功能"""

    def test_shutil_which_existing_command(self):
        """测试查找存在的命令"""
        result = shutil_which("python")
        # python 应该存在
        assert result is None or isinstance(result, str)

    def test_shutil_which_nonexistent_command(self):
        """测试查找不存在的命令"""
        result = shutil_which("nonexistent_command_xyz123")
        assert result is None


class TestEnsureUvSync:
    """测试 uv sync 功能"""

    def test_ensure_uv_sync_no_uv(self):
        """测试 uv 不可用时返回 True（跳过）"""
        with patch("shutil.which", return_value=None):
            result = ensure_uv_sync()
            assert result is True

    def test_ensure_uv_sync_no_project_root(self):
        """测试找不到项目根目录时返回 True（跳过）"""
        with patch("shutil.which", return_value="/usr/bin/uv"):
            with patch.object(Path, "exists", return_value=False):
                # 模拟找不到 pyproject.toml
                result = ensure_uv_sync()
                assert result is True

    def test_ensure_uv_sync_success(self):
        """测试 uv sync 成功"""
        with patch("shutil.which", return_value="/usr/bin/uv"):
            with patch("subprocess.run") as mock_run:
                mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
                result = ensure_uv_sync(Path("/tmp/test_project"))
                assert result is True
                mock_run.assert_called_once()
                call_args = mock_run.call_args[0][0]
                assert "uv" in call_args
                assert "sync" in call_args

    def test_ensure_uv_sync_failure(self):
        """测试 uv sync 失败"""
        with patch("shutil.which", return_value="/usr/bin/uv"):
            with patch("subprocess.run") as mock_run:
                mock_run.return_value = MagicMock(returncode=1, stdout="", stderr="Error")
                result = ensure_uv_sync(Path("/tmp/test_project"))
                assert result is False

    def test_ensure_uv_sync_exception(self):
        """测试 uv sync 发生异常"""
        with patch("shutil.which", return_value="/usr/bin/uv"):
            with patch("subprocess.run") as mock_run:
                mock_run.side_effect = Exception("Test exception")
                result = ensure_uv_sync(Path("/tmp/test_project"))
                assert result is False


class TestKillPortProcess:
    """测试端口进程杀死功能"""

    def test_kill_port_process_posix(self):
        """测试 POSIX 系统下杀死占用端口的进程"""
        with patch("peng.common.platform.IS_WINDOWS", False):
            with patch("subprocess.run") as mock_run:
                mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
                result = kill_port_process(8080)
                assert result is True
                mock_run.assert_called_once()

    def test_kill_port_process_posix_exception(self):
        """测试 POSIX 系统下杀死占用端口的进程时发生异常"""
        with patch("peng.common.platform.IS_WINDOWS", False):
            with patch("subprocess.run") as mock_run:
                mock_run.side_effect = Exception("Test exception")
                result = kill_port_process(8080)
                assert result is False

    def test_kill_port_process_windows_no_process(self):
        """测试 Windows 系统下没有进程占用端口"""
        with patch("peng.common.platform.IS_WINDOWS", True):
            with patch("subprocess.run") as mock_run:
                mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
                result = kill_port_process(8080)
                assert result is True

    def test_kill_port_process_windows_with_process(self):
        """测试 Windows 系统下有进程占用端口"""
        with patch("peng.common.platform.IS_WINDOWS", True):
            with patch("subprocess.run") as mock_run:
                # netstat 输出包含端口占用
                mock_run.return_value = MagicMock(
                    returncode=0,
                    stdout=f"TCP    0.0.0.0:8080    0.0.0.0:0    LISTENING    12345\n",
                    stderr=""
                )
                # taskkill 成功
                mock_run.side_effect = [
                    MagicMock(returncode=0, stdout=f"TCP    0.0.0.0:8080    0.0.0.0:0    LISTENING    12345\n", stderr=""),
                    MagicMock(returncode=0, stdout="", stderr=""),
                ]
                result = kill_port_process(8080)
                assert result is True

    def test_kill_port_process_windows_exception(self):
        """测试 Windows 系统下杀死占用端口的进程时发生异常"""
        with patch("peng.common.platform.IS_WINDOWS", True):
            with patch("subprocess.run") as mock_run:
                mock_run.side_effect = Exception("Test exception")
                result = kill_port_process(8080)
                assert result is False


class TestEdgeCases:
    """测试边界情况和异常处理"""

    def test_kill_process_with_invalid_pid(self):
        """测试使用无效 PID 杀死进程"""
        # 进程不存在应该返回 True
        result = kill_process(-1, caller="test", reason="test")
        assert result is True

    def test_process_exists_with_invalid_pid(self):
        """测试检查无效 PID 的进程存在性"""
        result = process_exists(-1)
        assert result is False

    def test_kill_port_process_with_invalid_port(self):
        """测试杀死占用无效端口的进程"""
        # 端口应该是整数
        with pytest.raises((TypeError, ValueError), match=None):
            kill_port_process(-1)  # 负数端口

    def test_get_venv_python_with_relative_path(self):
        """测试使用相对路径获取虚拟环境 Python"""
        project_root = Path(".")
        result = get_venv_python(project_root)
        assert ".venv" in str(result)

    def test_setup_signal_handlers_with_empty_signals(self):
        """测试使用空信号元组设置信号处理器"""
        with patch("peng.common.platform.can_use_signal_handler", return_value=True):
            loop = MagicMock()
            handler = MagicMock()
            result = setup_signal_handlers(loop, handler, signals=())
            assert result is True
            loop.add_signal_handler.assert_not_called()

    def test_cleanup_signal_handlers_with_empty_signals(self):
        """测试使用空信号元组清理信号处理器"""
        with patch("peng.common.platform.can_use_signal_handler", return_value=True):
            loop = MagicMock()
            result = cleanup_signal_handlers(loop, signals=())
            assert result is True
            loop.remove_signal_handler.assert_not_called()
