"""
平台兼容性工具模块

提供跨平台的：
- 虚拟环境路径解析
- 信号处理
- 进程管理
- Shell RC 文件加载
"""

from __future__ import annotations

import logging
import os
import signal
import subprocess
import sys
import traceback
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# 平台检测
IS_WINDOWS = sys.platform == "win32"
IS_POSIX = not IS_WINDOWS


def is_wsl() -> bool:
    """
    检测是否在 WSL (Windows Subsystem for Linux) 环境中运行

    Returns:
        True 表示在 WSL 环境中
    """
    # 方法1：检查 /proc/version 文件
    try:
        with open("/proc/version", "r") as f:
            version_info = f.read().lower()
            if "microsoft" in version_info or "wsl" in version_info:
                return True
    except (OSError, IOError):
        pass

    # 方法2：检查环境变量
    if "WSL_DISTRO_NAME" in os.environ or "WSLENV" in os.environ:
        return True

    return False


def get_venv_python(project_root: Path) -> Path:
    """
    获取虚拟环境的 Python 解释器路径（跨平台）

    Args:
        project_root: 项目根目录

    Returns:
        Python 解释器路径
    """
    if IS_WINDOWS:
        return project_root / ".venv" / "Scripts" / "python.exe"
    return project_root / ".venv" / "bin" / "python"


def get_venv_bin_dir(project_root: Path) -> Path:
    """
    获取虚拟环境的 bin/Scripts 目录路径（跨平台）

    Args:
        project_root: 项目根目录

    Returns:
        bin/Scripts 目录路径
    """
    if IS_WINDOWS:
        return project_root / ".venv" / "Scripts"
    return project_root / ".venv" / "bin"


def get_shell_rc_files() -> list[Path]:
    """
    获取当前平台的 Shell RC 文件列表

    Returns:
        RC 文件路径列表（Windows 返回空列表）
    """
    if IS_WINDOWS:
        # Windows 没有 shell rc 文件
        # 用户需要手动设置环境变量或使用系统环境变量
        return []

    return [
        Path.home() / ".zshrc",
        Path.home() / ".bashrc",
    ]


def get_termination_signals() -> tuple[int, ...]:
    """
    获取进程终止信号（跨平台）

    Returns:
        可用的终止信号元组
    """
    if IS_WINDOWS:
        # Windows 只支持有限的信号
        return (signal.SIGINT, signal.SIGBREAK)
    return (signal.SIGINT, signal.SIGTERM)


def get_kill_signal() -> int:
    """
    获取强制杀死进程的信号（跨平台）

    Returns:
        强制终止信号
    """
    if IS_WINDOWS:
        # Windows 没有 SIGKILL，使用 SIGTERM 作为替代
        # 实际强制终止需要使用 taskkill 或 TerminateProcess
        return signal.SIGTERM
    return signal.SIGKILL


def can_use_signal_handler() -> bool:
    """
    检查是否可以使用 asyncio signal handler

    Returns:
        True 表示可以使用 add_signal_handler
    """
    return IS_POSIX


def kill_process(
    pid: int,
    force: bool = False,
    kill_tree: bool = True,
    caller: str = "unknown",
    reason: str = "unknown",
) -> bool:
    """
    杀死进程（跨平台）

    Args:
        pid: 进程 ID
        force: 是否强制杀死
        kill_tree: 是否杀死整个进程树（包括子进程）
        caller: 调用者（用于日志追踪）
        reason: 杀死原因（用于日志追踪）

    Returns:
        是否成功
    """
    # 先检查进程是否存在，不存在则直接返回True（视为成功）
    if not process_exists(pid):
        logger.warning(
            f"[{caller}] 进程 {pid} 已不存在，跳过杀死操作 - "
            f"reason={reason}\n调用堆栈:\n{traceback.format_stack()[-5:]}"
        )
        return True

    if IS_WINDOWS:
        return _kill_process_windows(pid, force, kill_tree, caller, reason)
    return _kill_process_posix(pid, force, kill_tree, caller, reason)


def _kill_process_windows(pid: int, force: bool, kill_tree: bool, caller: str, reason: str) -> bool:
    """Windows 进程终止"""
    try:
        # Windows 使用 taskkill
        args = ["taskkill", "/PID", str(pid)]
        if force:
            args.insert(1, "/F")
        if kill_tree:
            args.insert(1, "/T")  # 杀死进程树

        logger.info(
            f"[Windows] 准备杀死进程 {pid} - caller={caller}, reason={reason}, "
            f"force={force}, kill_tree={kill_tree}"
        )

        result = subprocess.run(
            args,
            capture_output=True,
            timeout=5,
        )

        if result.returncode == 0:
            logger.info(f"[Windows] 成功杀死进程 {pid} - caller={caller}, reason={reason}")
        else:
            logger.error(
                f"[Windows] 杀死进程 {pid} 失败 - caller={caller}, reason={reason}, "
                f"returncode={result.returncode}, stdout={result.stdout.decode('utf-8', errors='ignore')}, "
                f"stderr={result.stderr.decode('utf-8', errors='ignore')}"
            )

        return result.returncode == 0
    except Exception as e:
        logger.error(
            f"[Windows] 杀死进程 {pid} 异常 - caller={caller}, reason={reason}, "
            f"异常: {e}\n调用堆栈:\n{traceback.format_stack()[-5:]}"
        )
        return False


def _kill_process_posix(pid: int, force: bool, kill_tree: bool, caller: str, reason: str) -> bool:
    """POSIX 进程终止"""
    sig_name = "SIGKILL" if force else "SIGTERM"

    logger.info(
        f"[POSIX] 准备杀死进程 {pid} - caller={caller}, reason={reason}, "
        f"force={force}({sig_name}), kill_tree={kill_tree}"
    )

    try:
        if kill_tree:
            # 先尝试杀死整个进程组
            _kill_process_tree_posix(pid, force, caller, reason)
        else:
            # 只杀死单个进程
            if force:
                os.kill(pid, signal.SIGKILL)
            else:
                os.kill(pid, signal.SIGTERM)
        
        # 验证进程是否真的被杀死了（防止 os.killpg 返回后进程未实际退出）
        if process_exists(pid):
            logger.warning(
                f"[POSIX] 进程 {pid} 仍然存在，发送{sig_name}强制杀死 - caller={caller}, reason={reason}"
            )
            os.kill(pid, signal.SIGKILL)
        else:
            logger.info(f"[POSIX] 成功杀死进程 {pid} - caller={caller}, reason={reason}")
            return True
        return True
    except (OSError, ProcessLookupError) as e:
        logger.error(
            f"[POSIX] 杀死进程 {pid} 失败 - caller={caller}, reason={reason}, "
            f"异常: {e}({type(e).__name__})\n调用堆栈:\n{traceback.format_stack()[-5:]}"
        )
        return False


def _protected_daemon_pids() -> set[int]:
    """已注册的本地daemon进程PID集合（kill_tree时排除）

    🔒 daemon进程不跟随task进程结束，只能自行结束或stop/kill结束
    （stop内置命令 / task kill -d / task killall / task kill <task_id>）。
    延迟导入避免platform ↔ daemon_registry循环依赖。
    """
    try:
        from peng.common.daemon_registry import list_daemons, normalize_nest

        return {
            int(e.get("pid") or 0)
            for e in list_daemons().values()
            if normalize_nest(e.get("nest")) is None
        }
    except Exception:
        return set()


def _kill_process_tree_posix(pid: int, force: bool, caller: str, reason: str) -> None:
    """
    杀死整个进程树（POSIX）

    递归查找并杀死所有子进程，然后杀死主进程。
    🔒 排除已注册的本地daemon进程（daemon生命周期独立，不跟随task进程结束）。

    Args:
        pid: 进程 ID
        force: 是否强制杀死（使用 SIGKILL）
        caller: 调用者
        reason: 杀死原因
    """
    import logging

    logger = logging.getLogger(__name__)

    # 获取要使用的信号
    sig = signal.SIGKILL if force else signal.SIGTERM
    sig_name = "SIGKILL" if force else "SIGTERM"

    # 首先尝试杀死整个进程组（如果进程是自己进程组的 leader）
    try:
        # 获取进程组 ID
        pgid = os.getpgid(pid)
        if pgid == pid:
            # 进程是进程组 leader，可以杀死整个进程组
            os.killpg(pgid, sig)
            logger.info(
                f"[POSIX] 已杀死进程组 {pgid} (信号: {sig_name}) - caller={caller}, reason={reason}"
            )
            return
    except (OSError, ProcessLookupError):
        pass  # 进程不存在或无法获取进程组，继续使用递归方式

    # 递归获取所有子进程
    child_pids = _get_child_processes(pid)

    # 🔒 排除已注册的本地daemon进程（daemon生命周期独立，不跟随task进程结束）
    protected = _protected_daemon_pids()
    if protected:
        filtered = [p for p in child_pids if p not in protected]
        if len(filtered) != len(child_pids):
            skipped = [p for p in child_pids if p in protected]
            logger.info(
                f"[POSIX] 进程树杀死时跳过已注册daemon进程: {skipped} "
                f"- caller={caller}, reason={reason}"
            )
        child_pids = filtered

    if child_pids:
        logger.info(
            f"[POSIX] 进程 {pid} 有 {len(child_pids)} 个子进程，准备递归杀死: "
            f"{child_pids} - caller={caller}, reason={reason}"
        )

    # 先杀死所有子进程（深度优先）
    for child_pid in child_pids:
        try:
            _kill_process_tree_posix(child_pid, force, caller, reason)
        except (OSError, ProcessLookupError):
            pass  # 子进程可能已经死了

    # 最后杀死主进程
    try:
        os.kill(pid, sig)
        logger.info(
            f"[POSIX] 已杀死进程 {pid} (信号: {sig_name}) - caller={caller}, reason={reason}"
        )
    except (OSError, ProcessLookupError):
        # 进程不存在，向上传播异常让调用者知道
        raise


def _get_child_processes(pid: int) -> list[int]:
    """
    获取进程的所有子进程 ID（POSIX）

    通过读取 /proc 文件系统获取子进程列表。

    Args:
        pid: 进程 ID

    Returns:
        子进程 ID 列表
    """
    child_pids = []

    try:
        # 读取 /proc/{pid}/task/{tid}/children 获取子进程
        # 注意：这需要 Linux 3.5+ 内核
        task_dir = f"/proc/{pid}/task"
        if os.path.exists(task_dir):
            for tid in os.listdir(task_dir):
                children_file = f"{task_dir}/{tid}/children"
                if os.path.exists(children_file):
                    try:
                        with open(children_file) as f:
                            children = f.read().strip().split()
                            child_pids.extend(int(c) for c in children if c)
                    except (OSError, ValueError):
                        pass
    except OSError:
        pass

    # 如果上面的方法失败，尝试使用 ps 命令
    if not child_pids:
        try:
            result = subprocess.run(
                ["ps", "-o", "pid=", "--ppid", str(pid)],
                capture_output=True,
                text=True,
                timeout=2,
            )
            if result.returncode == 0:
                for line in result.stdout.strip().split("\n"):
                    line = line.strip()
                    if line:
                        try:
                            child_pids.append(int(line))
                        except ValueError:
                            pass
        except (subprocess.TimeoutExpired, OSError):
            pass

    return child_pids


def process_exists(pid: int) -> bool:
    """
    检查进程是否存在（跨平台）

    Args:
        pid: 进程 ID

    Returns:
        True 表示进程存在
    """
    if IS_WINDOWS:
        try:
            result = subprocess.run(
                ["tasklist", "/FI", f"PID eq {pid}", "/NH"],
                capture_output=True,
                text=True,
                timeout=5,
            )
            return str(pid) in result.stdout
        except Exception as e:
            import traceback

            logger.error(
                f"[ERROR] process_exists() - platform.py:286: 异常 - {e}\n{traceback.format_exc()}"
            )
            return False
    else:
        try:
            os.kill(pid, 0)  # 发送信号 0 只检查进程是否存在
            # 额外检查：排除僵尸进程（Z状态）
            # 僵尸进程虽然存在于进程表中，但已经无法运行，应视为"不存在"
            try:
                with open(f"/proc/{pid}/stat", "r") as f:
                    stat_content = f.read()
                # 格式: pid (comm) state ...
                # state 是第三个字段，Z表示僵尸进程
                parts = stat_content.split()
                if len(parts) >= 3:
                    state = parts[2]
                    if state.startswith("Z"):
                        return False  # 僵尸进程，视为不存在
            except (FileNotFoundError, ProcessLookupError, PermissionError):
                # /proc 不可访问时回退到默认行为
                pass
            return True
        except OSError:
            return False


def subprocess_run_no_window(args, **kwargs):
    """subprocess.run 的跨平台封装，Windows上隐藏控制台窗口
    
    在 Windows 上，subprocess.run() 默认会创建新的控制台窗口（弹出cmd黑框）。
    后台服务中使用 subprocess 必须设置 CREATE_NO_WINDOW 标志来隐藏窗口。
    
    用法与 subprocess.run() 完全相同，只是自动处理 Windows 差异：
        result = subprocess_run_no_window(["git", "pull"], capture_output=True, text=True, cwd=project_root)
    
    Args:
        args: 命令参数列表
        **kwargs: 传递给 subprocess.run() 的其他参数
        
    Returns:
        subprocess.CompletedProcess 实例
    """
    if IS_WINDOWS:
        # Windows: 添加 CREATE_NO_WINDOW 标志，防止弹出cmd窗口
        kwargs.setdefault("creationflags", 0)
        kwargs["creationflags"] |= 0x08000000  # CREATE_NO_WINDOW
    return subprocess.run(args, **kwargs)


def get_default_shell() -> str:
    """
    获取默认 shell（跨平台）

    Returns:
        shell 名称或路径
    """
    if IS_WINDOWS:
        # 检查是否有 WSL 或 Git Bash
        if shutil_which("bash"):
            return "bash"
        return "cmd.exe"
    return os.environ.get("SHELL", "/bin/bash")


def shutil_which(cmd: str) -> str | None:
    """
    查找命令路径（跨平台封装）

    Args:
        cmd: 命令名

    Returns:
        命令完整路径或 None
    """
    import shutil

    return shutil.which(cmd)


def setup_signal_handlers(loop: Any, handler: Any, signals: tuple[int, ...] | None = None) -> bool:
    """
    设置信号处理器（跨平台）

    Args:
        loop: asyncio 事件循环
        handler: 信号处理函数
        signals: 要处理的信号（默认使用 get_termination_signals()）

    Returns:
        是否成功设置
    """
    if not can_use_signal_handler():
        return False

    if signals is None:
        signals = get_termination_signals()

    try:
        for sig in signals:
            loop.add_signal_handler(sig, handler)
        return True
    except (NotImplementedError, OSError):
        return False


def cleanup_signal_handlers(loop: Any, signals: tuple[int, ...] | None = None) -> bool:
    """
    清理信号处理器（跨平台）

    Args:
        loop: asyncio 事件循环
        signals: 要清理的信号

    Returns:
        是否成功清理
    """
    if not can_use_signal_handler():
        return False

    if signals is None:
        signals = get_termination_signals()

    try:
        for sig in signals:
            loop.remove_signal_handler(sig)
        return True
    except (NotImplementedError, OSError):
        return False


def ensure_uv_sync(project_root: Path | None = None) -> bool:
    """确保依赖已同步（静默运行 uv sync）
    
    用于启动时自动同步依赖，避免环境不一致。
    
    使用标记文件（.venv/.uv_sync_stamp）缓存 uv sync 结果：
    当 uv.lock 的 mtime 未变化时跳过 subprocess 调用，大幅减少启动时间。
    
    Args:
        project_root: 项目根目录，默认自动检测
    
    Returns:
        True 表示成功或跳过，False 表示失败
    """
    import json as _json
    
    if project_root is None:
        # 自动检测项目根目录
        current = Path(__file__).resolve()
        for parent in current.parents:
            if (parent / "pyproject.toml").exists():
                project_root = parent
                break
        else:
            return True  # 找不到项目根目录，跳过
    
    venv_dir = project_root / ".venv"
    venv_cfg = venv_dir / "pyvenv.cfg"
    lock_file = project_root / "uv.lock"
    stamp_file = venv_dir / ".uv_sync_stamp"
    
    # ── 快速路径：检查标记文件，跳过不必要的 uv sync ──
    if venv_cfg.exists() and lock_file.exists() and stamp_file.exists():
        try:
            stamp_data = _json.loads(stamp_file.read_text())
            lock_mtime = lock_file.stat().st_mtime_ns
            if stamp_data.get("lock_mtime_ns") == lock_mtime:
                return True  # uv.lock 未变化，跳过 uv sync
        except Exception:
            pass  # 标记文件损坏，继续执行 uv sync
    
    # ── 慢速路径：执行 uv sync ──
    import shutil
    
    if not shutil.which("uv"):
        return True  # uv 不可用，跳过
    
    # 如果 pyvenv.cfg 缺失，说明 venv 损坏，尝试 uv sync 重建
    # 注意：不主动删除 .venv，因为 Windows 上可能有文件被锁定
    # uv sync 本身能处理损坏的 venv
    
    try:
        result = subprocess.run(
            ["uv", "sync"],
            cwd=str(project_root),
            capture_output=True,
            timeout=300,  # uv sync 允许5分钟
        )
        if result.returncode != 0:
            stderr = result.stderr.decode('utf-8', errors='ignore')
            logger.error(f"uv sync 失败: {stderr}")
            # 如果 uv sync 失败且 venv 损坏，提示用户手动修复
            if not venv_cfg.exists():
                logger.error(f"[ERROR] venv 损坏且 uv sync 失败，请手动执行: rd /s /q .venv && uv sync")
            return False
        
        # 写入标记文件（记录 uv.lock 的 mtime）
        if lock_file.exists():
            try:
                stamp_file.write_text(_json.dumps({
                    "lock_mtime_ns": lock_file.stat().st_mtime_ns,
                }))
            except Exception:
                pass  # 写入失败不影响功能
        
        return True
    except Exception as e:
        import traceback
        logger.error(f"[ERROR] ensure_uv_sync() 异常: {e}\n{traceback.format_exc()}")
        return False


def kill_port_process(port: int) -> bool:
    """
    强制杀死占用指定端口的进程（跨平台）

    Args:
        port: 端口号

    Returns:
        True 表示成功或没有进程占用，False 表示失败
    """
    if IS_WINDOWS:
        return _kill_port_process_windows(port)
    return _kill_port_process_posix(port)


def _kill_port_process_windows(port: int) -> bool:
    """Windows 平台杀死占用端口的进程"""
    try:
        # 使用 netstat 查找占用端口的进程 PID
        result = subprocess.run(
            ["netstat", "-ano"],
            capture_output=True,
            text=True,
            timeout=10,
        )

        pids = set()
        for line in result.stdout.split("\n"):
            # 匹配 TCP 和 UDP 监听/建立的连接
            # 格式: TCP    0.0.0.0:6688    0.0.0.0:0    LISTENING    12345
            if f":{port}" in line:
                parts = line.split()
                if len(parts) >= 5:
                    try:
                        pid = int(parts[-1])
                        pids.add(pid)
                    except ValueError:
                        pass

        if not pids:
            return True  # 没有进程占用该端口

        # 使用 taskkill 强制杀死所有找到的进程
        import time

        for pid in pids:
            try:
                subprocess.run(
                    ["taskkill", "/F", "/PID", str(pid)],
                    capture_output=True,
                    timeout=5,
                )
            except Exception as e:
                import traceback

                logger.error(
                    f"[ERROR] _kill_port_process_windows() - platform.py:473: 异常 - {e}\n{traceback.format_exc()}"
                )

        time.sleep(1)  # 等待进程完全退出
        return True
    except Exception as e:
        import traceback

        logger.error(
            f"[ERROR] _kill_port_process_windows() - platform.py:478: 异常 - {e}\n{traceback.format_exc()}"
        )
        return False


def _kill_port_process_posix(port: int) -> bool:
    """POSIX 平台（Linux/macOS）杀死占用端口的进程"""
    import time

    try:
        # 使用 lsof 查找并杀死占用端口的进程
        # -t: 只输出 PID
        # -i:PORT: 查找占用指定端口的进程
        subprocess.run(
            f"lsof -ti:{port} | xargs -r kill -9 2>/dev/null",
            shell=True,
            capture_output=True,
            timeout=10,
        )

        time.sleep(1)  # 等待进程完全退出
        return True
    except Exception as e:
        import traceback

        logger.error(
            f"[ERROR] _kill_port_process_posix() - platform.py:500: 异常 - {e}\n{traceback.format_exc()}"
        )
        return False
