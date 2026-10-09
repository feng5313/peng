"""
后台服务模式

实现 bin/fly --service 后台服务模式：
- 开启消息监控（13 秒间隔，可配置）
- 开启心跳监控（7 秒间隔，可配置）
- 不主动创建新消息
- 低消耗值守

需求映射：docs/requests.md §1.2-1.3, §2.6, §3.2
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import signal
import sys
import threading
import time
import traceback
from pathlib import Path
from typing import Any

from peng.common import jsonp as json
from peng.common.paths import get_project_root, chown_to_project_owner, ensure_dir_with_owner
from peng.config import get_agent_name

logger = logging.getLogger(__name__)

# Agent 名称
AGENT_NAME = get_agent_name()

# 停止信号文件配置（硬编码文件名）
STOP_SIGNAL_FILENAME = "fly_stop_service_signal_0x7f3d9c2a"


def _open_nest_log_with_recovery(nest_log: str):
    """打开 nest connect 日志文件，遇到属主异常的历史残留文件时自愈

    自愈策略（逐级降级，保证 local nest 不因日志权限问题无法启动）：
    1. 正常打开（root 时由 open_log_with_owner 顺带修复属主）
    2. PermissionError：属主异常的残留文件（如曾被 root 命令创建）→
       删除重建（项目属主对 logs 目录有写权限，可删除目录内任意文件）
    3. 仍失败（目录权限也异常）：改用系统临时目录文件兜底

    Args:
        nest_log: 日志文件路径

    Returns:
        已打开的文件对象
    """
    from peng.common.paths import open_log_with_owner

    try:
        return open_log_with_owner(nest_log)
    except PermissionError:
        logger.warning(f"nest日志 {nest_log} 无写入权限，尝试删除重建...")

    try:
        os.unlink(nest_log)
        return open_log_with_owner(nest_log)
    except OSError as e:
        logger.warning(f"nest日志 {nest_log} 删除重建失败({e})，改用临时文件兜底")

    import tempfile

    fallback = Path(tempfile.gettempdir()) / "peng_nest_connect_fallback.log"
    return open(fallback, "a", encoding="utf-8", errors="replace")


def _is_windows_service() -> bool:
    """检测当前进程是否运行在 Windows 服务会话（Session 0）中"""
    if sys.platform != "win32":
        return False
    try:
        import ctypes

        kernel32 = ctypes.windll.kernel32
        # 获取当前进程的会话ID
        pid = kernel32.GetCurrentProcessId()
        session_id = ctypes.c_ulong()
        result = ctypes.windll.kernel32.ProcessIdToSessionId(pid, ctypes.byref(session_id))
        if result:
            return session_id.value == 0  # Session 0 = 服务会话
    except Exception:
        pass
    return False


def _start_nest_in_user_session(cmd: list[str], cwd: str, log_file: str) -> bool:
    """
    在用户交互会话中启动进程（用于Windows服务模式下启动nest connect）

    Windows服务运行在Session 0，子进程无法访问桌面（截屏/窗口枚举）。
    此函数使用当前登录用户的会话来启动进程，使其能访问桌面。
    """
    import subprocess

    # 方案：使用 schtasks 创建临时计划任务，以 INTERACTIVE 模式运行
    # 这比 CreateProcessAsUser 简单得多且不需要复杂权限

    # 更简单的方案：使用 cmd /c start 在用户会话启动
    # Windows 服务可以通过 sysinterals 的 psexec 或计划任务实现
    # 但最可靠的方式是写一个临时 bat 文件，用 ShellExecuteW 启动

    try:
        import ctypes

        # 写一个临时启动脚本
        bat_path = str(Path(log_file).parent / "nest_connect_start.bat")
        cmd_str = " ".join(f'"{c}"' if " " in c else c for c in cmd)
        with open(bat_path, "w", encoding="utf-8") as f:
            f.write("@echo off\n")
            f.write(f'cd /d "{cwd}"\n')
            f.write(f'echo [%date% %time%] Starting nest connect >> "{log_file}" 2>&1\n')
            f.write(f'{cmd_str} >> "{log_file}" 2>&1\n')

        # 使用 ShellExecuteW 在用户桌面会话中启动
        # 关键：不使用 "runas"，使用 "open"，这样会在用户会话中启动
        # SW_HIDE = 0 隐藏窗口（nest不需要控制台窗口）
        ret = ctypes.windll.shell32.ShellExecuteW(
            None,  # hwnd
            "open",  # 不用runas（不提权）
            "cmd",
            f'/c "{bat_path}"',
            cwd,
            0,  # SW_HIDE
        )

        if ret <= 32:
            logger.warning(f"ShellExecuteW 返回 {ret}，回退到普通 Popen")
            # 回退：用普通Popen启动（虽然截屏可能不行，但至少nest能连接）
            with open(log_file, "a", encoding="utf-8", errors="replace") as log_f:
                subprocess.Popen(cmd, stdout=log_f, stderr=subprocess.STDOUT, cwd=cwd)
            return True

        logger.info(f"ShellExecuteW 启动成功 (返回值: {ret})")
        return True

    except Exception as e:
        logger.warning(f"在用户会话启动失败: {e}，回退到普通Popen")
        try:
            with open(log_file, "a", encoding="utf-8", errors="replace") as log_f:
                subprocess.Popen(cmd, stdout=log_f, stderr=subprocess.STDOUT, cwd=cwd)
            return True
        except Exception as e2:
            logger.error(f"回退启动也失败: {e2}")
            return False


def get_temp_dir() -> Path:
    """获取临时数据目录（项目 data 目录）"""
    return get_project_root() / "data"


def get_stop_signal_file() -> Path:
    """获取停止信号文件路径"""
    return get_temp_dir() / STOP_SIGNAL_FILENAME


class BackgroundService:
    """
    后台服务模式

    实现低消耗值守，监控新消息和心跳状态。
    """

    # 默认配置
    DEFAULT_INBOX_INTERVAL = 13  # 消息监控间隔（秒）
    DEFAULT_HEARTBEAT_INTERVAL = 7  # 心跳监控间隔（秒）
    DEFAULT_ARCHIVE_DAYS = 7  # 归档天数

    def __init__(
        self,
        config: dict[str, Any] | None = None,
        db_path: Path | None = None,
    ):
        """
        初始化后台服务

        Args:
            config: 配置字典（从 peng.json 读取）
            db_path: 数据库路径
        """
        self.config = config or {}
        self.db_path = db_path

        # 从配置读取间隔
        monitor_config = self.config.monitor
        self.inbox_interval = monitor_config.inbox_monitor_interval
        self.heartbeat_interval = monitor_config.heartbeat_monitor_interval
        self.archive_days = monitor_config.archive_after_days
        self.auto_start_pending_tasks = monitor_config.auto_start_pending_tasks

        # 监控器实例
        self._inbox_monitor = None
        self._heartbeat_monitor = None

        # 运行状态
        self._running = False
        self._stop_event = asyncio.Event()

        # 停止信号监控线程（独立干净线程）
        self._stop_signal_thread: threading.Thread | None = None
        self._stop_signal_event = threading.Event()

        # 心跳监控相关
        self._last_heartbeat = time.time()
        self._restart_pending = False

        # 内置 local nest 相关
        self._local_nest_pid: int | None = None  # 内置 local nest 子进程 PID
        self._builtin_nest_connector: Any | None = None  # 内置 local nest 连接器（用于重启）

    def _run_stop_signal_monitor(self) -> None:
        """
        停止信号文件监控线程函数（在独立线程中运行）

        每 1 秒检查一次停止信号文件，检测到后通知主线程优雅关闭。
        同时监控心跳，如果心跳超时则通知主线程重启。

        注意：不再使用 os._exit(0) 强制退出，而是设置标志让主线程处理清理。
        """
        logger.info("🔍 停止信号监控线程已启动")

        while not self._stop_signal_event.is_set():
            # 等待 1 秒或直到被唤醒
            if self._stop_signal_event.wait(timeout=1.0):
                break

            # 检查停止信号文件
            signal_file = get_stop_signal_file()
            if signal_file.exists():
                logger.info("🛑 检测到停止信号文件，通知主线程停止...")
                # 删除信号文件（避免重复触发）
                try:
                    signal_file.unlink()
                except Exception as e:
                    logger.error(
                        f"❌ _run_stop_signal_monitor() 删除信号文件失败: {e}\n{traceback.format_exc()}"
                    )
                # 优雅关闭：设置标志，让主线程处理清理
                self._restart_pending = False
                self._stop_event.set()
                logger.info("🔍 停止信号监控线程已退出（停止信号）")
                return

        logger.info("🔍 停止信号监控线程已退出")

    def _start_stop_signal_monitor(self) -> None:
        """启动停止信号监控线程"""
        if self._stop_signal_thread and self._stop_signal_thread.is_alive():
            logger.warning("停止信号监控线程已在运行")
            return

        # 创建并启动独立线程
        self._stop_signal_thread = threading.Thread(
            target=self._run_stop_signal_monitor,
            name="StopSignalMonitor",
            daemon=True,
        )
        self._stop_signal_thread.start()

    def _stop_stop_signal_monitor(self) -> None:
        """停止停止信号监控线程"""
        if self._stop_signal_thread and self._stop_signal_thread.is_alive():
            self._stop_signal_event.set()
            self._stop_signal_thread.join(timeout=2.0)
            logger.info("停止信号监控线程已停止")

    def should_restart(self) -> bool:
        """检查是否需要重启"""
        return self._restart_pending

    async def start(self, webui=False, webui_host=None, webui_port=None) -> None:
        """启动后台服务"""
        if self._running:
            logger.warning("后台服务已在运行中")
            return

        self._running = True
        self._stop_event.clear()
        self._stop_signal_event.clear()
        self._restart_pending = False

        # 清理残留的停止信号文件（防止上次异常退出遗留导致新服务立即停止）
        stale_signal = get_stop_signal_file()
        if stale_signal.exists():
            try:
                stale_signal.unlink()
                logger.warning(f"⚠️ 清理了残留的停止信号文件: {stale_signal}")
            except Exception as e:
                logger.warning(f"清理残留停止信号文件失败（可忽略）: {e}")

        from peng.config.loader import get_default_heartbeat_timeout

        logger.info("=" * 50)
        logger.info(f"🚀 {AGENT_NAME} 后台服务启动")
        logger.info(f"  - 消息监控间隔：{self.inbox_interval}秒")
        logger.info(f"  - 心跳监控间隔：{self.heartbeat_interval}秒")
        logger.info(f"  - 心跳超时时间：{get_default_heartbeat_timeout()}秒")
        logger.info(f"  - 归档天数：{self.archive_days}天")
        logger.info("=" * 50)

        try:
            # 启动停止信号监控线程（独立干净线程）
            self._start_stop_signal_monitor()

            # 初始化监控器（DB 连接可能暂时不可用，重试等待）
            for _init_retry in range(5):
                try:
                    await self._init_monitors()
                    break
                except Exception as _init_err:
                    logger.warning(f"监控器初始化失败（第{_init_retry + 1}次）: {_init_err}")
                    if _init_retry < 4:
                        import asyncio as _aio

                        await _aio.sleep(5)
                    else:
                        raise
            from peng.message.manager import MessageManager
            from peng.task.manager import TaskManager
            from peng.webui import run_webui

            self.message_manager = MessageManager()
            self.task_manager = TaskManager()

            # 初始化 nest 客户端（连接远程鹏窝服务）
            try:
                from peng.config.loader import load_config as _load_config

                _cfg = _load_config()
                _nest_cfg = _cfg.nest
                if _nest_cfg.enabled:
                    from peng.nest.client import init_nest_clients

                    init_nest_clients()
                    logger.info(f"🪹 Nest 客户端已初始化（默认: {_nest_cfg.default_nest}）")
            except Exception as e:
                logger.warning(f"初始化 nest 客户端失败（非致命）: {e}")

            # 启动 Nest 接入服务器 + 内置 local nest
            # 【重要】启动阶段只允许 local nest 注册，拒绝远端连接
            # 等 local 注册成功后再开放给远端，保证 local 排在连接列表第一位
            nest_task = None
            try:
                from peng.nest.bridge import start_nest_server, NestBridgeManager

                # 🔧 启动阶段拒绝远端连接（远端会自动重连）
                NestBridgeManager.get_instance()._accepting_connections = False
                nest_task = asyncio.create_task(start_nest_server())
                logger.info("🪹 Nest 接入服务器已启动（仅接受 local 注册）")
            except Exception as e:
                logger.warning(f"启动 Nest 接入服务器失败（非致命）: {e}")

            # 启动内置 local nest 子进程（不依赖 config/nest.json，CLI 已支持 --name + --host/--port 直接启动）
            # 名称固定为 "local"，host=localhost，port=8899，任何情况都必须成功启动
            # 【重要】nest server 必须先于 nest connect 启动，否则 connect 启动时 server 未就绪
            # 【重要】必须用子进程在用户会话中启动，否则 Session 0 下无法截屏/操作桌面
            _LOCAL_NEST_NAME = "local"

            # 清理旧的内置实例（只杀 name=local 的，不影响外部 nest install 安装的实例）
            try:
                from peng.nest.cli import _is_nest_process_running, _kill_all_nest_processes

                if _is_nest_process_running(name=_LOCAL_NEST_NAME):
                    logger.info(f"🪹 检测到旧的内嵌 nest({_LOCAL_NEST_NAME}) 进程，清理后重新启动")
                    _kill_all_nest_processes(name=_LOCAL_NEST_NAME)
                    import time as _time

                    _time.sleep(2)
            except Exception:
                pass

            try:
                from peng.nest.cli import _get_python_cmd, _get_nest_script
                import subprocess as _sp

                nest_cmd = _get_python_cmd() + [
                    str(_get_nest_script()),
                    "connect",
                    "--name",
                    _LOCAL_NEST_NAME,
                    "--host",
                    "localhost",
                    "--port",
                    "8899",
                    "--skip-single-instance-check",
                ]
                project_root = get_project_root()
                log_dir = project_root / "logs"
                ensure_dir_with_owner(log_dir)
                nest_log = str(log_dir / "nest_connect.log")
                chown_to_project_owner(nest_log)

                if sys.platform == "win32" and _is_windows_service():
                    # Windows 服务运行在 Session 0，子进程无法访问桌面（截屏/窗口枚举）
                    # 尝试在用户会话中启动 nest connect
                    logger.info(
                        "🪹 检测到Windows服务模式(Session 0)，尝试在用户会话启动nest connect..."
                    )
                    _start_nest_in_user_session(nest_cmd, str(project_root), nest_log)
                else:
                    # 非服务模式或Linux：正常启动子进程
                    nest_log_f = _open_nest_log_with_recovery(nest_log)
                    popen_kwargs = dict(
                        stdout=nest_log_f,
                        stderr=_sp.STDOUT,
                        cwd=str(project_root),
                        close_fds=True,
                    )
                    proc = _sp.Popen(nest_cmd, **popen_kwargs)
                    self._local_nest_pid = proc.pid
                    logger.info(f"🪹 内置 local nest 已启动 (PID: {proc.pid})")
            except Exception as e:
                logger.warning(f"启动内置 local nest 失败（非致命）: {e}")

            # 等待 local nest 注册成功（最多等待15秒）
            # 【重要】启动阶段拒绝远端连接，local 注册成功后开放给远端
            _local_registered = False
            try:
                from peng.nest.bridge import NestBridgeManager

                _mgr = NestBridgeManager.get_instance()
                for _ in range(30):  # 30次 x 0.5秒 = 15秒
                    for conn in _mgr._connections:
                        if conn.name == "local" and not conn._closing:
                            _local_registered = True
                            break
                    if _local_registered:
                        break
                    await asyncio.sleep(0.5)
                if _local_registered:
                    logger.info(f"🪹 内置 local nest 已注册成功，开放远端连接")
                else:
                    logger.warning(f"🪹 内置 local nest 注册超时（15秒），开放远端连接")
                # 🔧 无论 local 是否注册成功，都开放远端连接（远端会自动重连）
                _mgr._accepting_connections = True
            except Exception as e:
                logger.warning(f"等待 local nest 注册失败（非致命）: {e}")
                try:
                    from peng.nest.bridge import NestBridgeManager

                    NestBridgeManager.get_instance()._accepting_connections = True
                except Exception:
                    pass

            # 启动事务定时器（自动重启设置了timer字段的事务）
            try:
                from peng.task.timer import start_timer

                start_timer(scan_interval=5)
                logger.info("⏰ 事务定时器已启动（每5秒扫描一次）")
            except Exception as e:
                logger.warning(f"启动事务定时器失败（非致命）: {e}")

            # 启动监控事务（不再需要poll_all_processes_background)
            tasks = [
                asyncio.create_task(self._inbox_monitor_loop()),
                asyncio.create_task(self._heartbeat_monitor_loop()),
                asyncio.create_task(self._archive_loop()),
                asyncio.create_task(self._lsp_installation_loop()),
                asyncio.create_task(self._local_nest_monitor_loop()),
                asyncio.create_task(self._temp_cleanup_loop()),
            ]
            if webui:
                # 使用传入的 host 和 port
                config = load_config()
                final_host = webui_host if webui_host else config.get("host", "0.0.0.0")
                final_port = webui_port if webui_port else config.get("port", 6688)
                logger.info(f"🌐 Web UI 将在 http://{final_host}:{final_port} 启动")
                # 传递 BackgroundService 的停止事件给 run_webui
                webui_task = asyncio.create_task(
                    run_webui(host=final_host, port=final_port, shutdown_event=self._stop_event)
                )
                tasks.append(webui_task)

            # 等待停止信号或 webui 事务完成
            try:
                if webui:
                    # 同时等待 webui_task 和 stop_event，stop_event 触发时主动关闭 webui
                    stop_task = asyncio.create_task(self._stop_event.wait())
                    done, pending = await asyncio.wait(
                        [webui_task, stop_task],
                        return_when=asyncio.FIRST_COMPLETED,
                    )
                    stop_task.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await stop_task
                    if self._stop_event.is_set():
                        # 停止信号触发，立刻 cancel webui_task
                        logger.info("🛑 停止信号触发，正在关闭 WebUI...")
                        try:
                            from peng.webui.server import _uvicorn_server

                            if _uvicorn_server:
                                _uvicorn_server.should_exit = True
                        except Exception:
                            pass
                        webui_task.cancel()
                    try:
                        await webui_task
                    except asyncio.CancelledError:
                        logger.info("✅ WebUI 事务被取消")
                else:
                    # 没有 webui，只等待停止事件
                    await self._stop_event.wait()
            except Exception as e:
                logger.error(f"❌ BackgroundService.start() 异常: {e}", exc_info=True)
            finally:
                # 确保清理代码总是被执行
                logger.info("✅ 开始取消所有监控事务...")
                # 取消所有事务
                for task in tasks:
                    task.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await task
                logger.info("✅ 所有监控事务已取消")

                # 清理内置 local nest 子进程
                try:
                    if self._local_nest_pid:
                        import signal as _signal

                        try:
                            os.kill(self._local_nest_pid, _signal.SIGTERM)
                            logger.info(
                                f"🪹 已终止内置 local nest 子进程 (PID: {self._local_nest_pid})"
                            )
                        except (ProcessLookupError, OSError):
                            pass
                        self._local_nest_pid = None
                    # 无论哪种模式启动的，都用 _kill_all_nest_processes 兜底清理
                    from peng.nest.cli import _kill_all_nest_processes

                    killed = _kill_all_nest_processes(name="local")
                    if killed:
                        logger.info(f"🪹 已清理 {killed} 个残留的内置 local nest 进程")
                except Exception as e:
                    logger.warning(f"清理内置 local nest 子进程失败（非致命）: {e}")

                logger.info("✅ BackgroundService.start() 退出")

            # 取消所有事务
            for task in tasks:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task
        except Exception as e:
            logger.error(f"服务停止失败: {e}\n{traceback.format_exc()}")
        finally:
            # 停止监控器
            await self._stop_monitors()
            # 停止停止信号监控线程
            self._stop_stop_signal_monitor()

            self._running = False
            if self._restart_pending:
                logger.info("🔄 服务将自动重启...")
            else:
                logger.info("🛑 后台服务已停止")

    def stop(self) -> None:
        """停止后台服务"""
        logger.info("🛑 收到停止信号...")
        self._stop_event.set()

    async def _init_monitors(self) -> None:
        """初始化监控器"""
        try:
            from peng.message.inbox_monitor import InboxMonitor
            from peng.task.heartbeat_monitor import HeartbeatMonitor

            # 初始化消息监控（PostgreSQL 使用连接池，不需要 db_path）
            self._inbox_monitor = InboxMonitor(
                check_interval=self.inbox_interval,
            )

            # 初始化心跳监控（PostgreSQL 使用连接池，不需要 db_path）
            # 注意：无论是否使用PostgreSQL，都需要初始化心跳监控
            self._heartbeat_monitor = HeartbeatMonitor(
                check_interval=self.heartbeat_interval,
                archive_days=self.archive_days,
                auto_start_pending_tasks=self.auto_start_pending_tasks,
            )

            logger.info("✅ 监控器初始化完成")

        except Exception as e:
            tb = traceback.format_exc()
            logger.error(f"❌ 监控器初始化失败：{e}\n{traceback.format_exc()}\n{tb}")
            raise

    async def _stop_monitors(self) -> None:
        """停止监控器"""
        if self._inbox_monitor:
            await self._inbox_monitor.stop()
        # HeartbeatMonitor 目前没有异步 stop 方法
        logger.info("✅ 监控器已停止")

        # 注释：不再使用 ProcessManager，改用 multiprocessing.Process
        # 子进程会在父进程退出时自动终止（因为不是 daemon）

    async def _inbox_monitor_loop(self) -> None:
        """消息监控循环"""
        logger.info(f"📬 消息监控启动 (间隔：{self.inbox_interval}秒)")

        try:
            if self._inbox_monitor:
                await self._inbox_monitor.start()
            # 只要服务在运行，就保持这个事务存活
            while self._running and not self._stop_event.is_set():
                await asyncio.sleep(1)
        except asyncio.CancelledError:
            pass
        except Exception as e:
            tb = traceback.format_exc()
            logger.error(f"消息监控异常：{e}\n{traceback.format_exc()}\n{tb}")

    async def _heartbeat_monitor_loop(self) -> None:
        """心跳监控循环"""
        logger.info(f"💓 心跳监控启动 (间隔：{self.heartbeat_interval}秒)")

        while self._running and not self._stop_event.is_set():
            try:
                if self._heartbeat_monitor:
                    # 心跳监控的同步检查
                    await self._heartbeat_monitor.check_all()
                await asyncio.sleep(self.heartbeat_interval)
            except asyncio.CancelledError:
                break
            except Exception as e:
                tb = traceback.format_exc()
                logger.error(f"心跳监控异常：{e}\n{traceback.format_exc()}\n{tb}")
                await asyncio.sleep(5)

    async def _archive_loop(self) -> None:
        """归档循环（每小时检查一次）"""
        logger.info(f"📦 归档事务启动 (归档超过 {self.archive_days} 天的消息)")

        # 每小时检查一次归档
        archive_check_interval = 3600

        while self._running and not self._stop_event.is_set():
            try:
                await asyncio.sleep(archive_check_interval)
                # TODO: 实现归档逻辑

            except asyncio.CancelledError:
                break
            except Exception as e:
                tb = traceback.format_exc()
                logger.error(f"归档事务异常：{e}\n{traceback.format_exc()}\n{tb}")

    async def _temp_cleanup_loop(self) -> None:
        """临时文件和日志清理循环

        定期自动清理：
        1. data/temp、data/shot 中超过 cleanup_temp_days 天（默认2天）的文件
        2. logs 目录总大小超过 logs_max_size_mb（默认800MB）时，删除最老的日志
        3. 启动时清理已废弃的 data/tmp、data/wechat_attachments、data/clawbot 目录
        """
        try:
            from peng.config.loader import load_config

            config = load_config()
            cleanup_cfg = config.webui if hasattr(config, "webui") else None
        except Exception:
            cleanup_cfg = None

        if cleanup_cfg is not None:
            cleanup_temp_days = getattr(cleanup_cfg, "cleanup_temp_days", 2)
            logs_max_size_mb = getattr(cleanup_cfg, "logs_max_size_mb", 800)
        else:
            cleanup_temp_days = 2
            logs_max_size_mb = 800
        cleanup_interval = 3600  # 每小时检查一次

        project_root = get_project_root()

        logger.info(
            f"🧹 临时文件清理事务启动 "
            f"(temp/shot超过{cleanup_temp_days}天清理, logs超过{logs_max_size_mb}MB清理)"
        )

        # 启动时清理废弃目录
        await asyncio.to_thread(self._cleanup_deprecated_dirs, project_root)

        # 首次启动时也执行一次清理
        await asyncio.to_thread(
            self._do_temp_cleanup, project_root, cleanup_temp_days, logs_max_size_mb
        )

        while self._running and not self._stop_event.is_set():
            try:
                await asyncio.sleep(cleanup_interval)
                await asyncio.to_thread(
                    self._do_temp_cleanup, project_root, cleanup_temp_days, logs_max_size_mb
                )
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"临时文件清理异常：{e}\n{traceback.format_exc()}")

    @staticmethod
    def _cleanup_deprecated_dirs(project_root: Path) -> None:
        """清理已废弃的目录（data/tmp, data/wechat_attachments, data/clawbot）"""
        deprecated_dirs = [
            project_root / "data" / "tmp",
            project_root / "data" / "wechat_attachments",
            project_root / "data" / "clawbot",
        ]
        for d in deprecated_dirs:
            if d.exists() and d.is_dir():
                try:
                    import shutil

                    file_count = sum(1 for _ in d.rglob("*") if _.is_file())
                    if file_count == 0:
                        d.rmdir()
                        logger.info(f"🧹 已清理废弃空目录: {d}")
                    else:
                        logger.info(f"⚠️ 废弃目录非空({file_count}个文件)，跳过删除: {d}")
                except Exception as e:
                    logger.warning(f"清理废弃目录失败 {d}: {e}")

    @staticmethod
    def _do_temp_cleanup(project_root: Path, temp_days: int, logs_max_mb: int) -> None:
        """执行临时文件和日志清理"""
        import shutil
        from datetime import datetime, timedelta

        now = datetime.now()
        cutoff = now - timedelta(days=temp_days)

        # 1. 清理 data/temp 和 data/shot 中的过期文件
        for dir_name in ["data/temp", "data/shot"]:
            target_dir = project_root / dir_name
            if not target_dir.exists():
                continue
            removed = 0
            freed_bytes = 0
            for f in target_dir.rglob("*"):
                if not f.is_file():
                    continue
                try:
                    mtime = datetime.fromtimestamp(f.stat().st_mtime)
                    if mtime < cutoff:
                        size = f.stat().st_size
                        f.unlink()
                        removed += 1
                        freed_bytes += size
                except Exception:
                    pass
            if removed > 0:
                freed_mb = freed_bytes / (1024 * 1024)
                logger.info(f"🧹 {dir_name}: 清理了 {removed} 个过期文件，释放 {freed_mb:.1f}MB")

        # 2. 控制 logs 目录总大小
        logs_dir = project_root / "logs"
        if not logs_dir.exists():
            return

        max_bytes = logs_max_mb * 1024 * 1024
        log_files = []
        total_size = 0
        for f in logs_dir.rglob("*.log*"):
            if f.is_file():
                try:
                    size = f.stat().st_size
                    mtime = f.stat().st_mtime
                    total_size += size
                    log_files.append((f, size, mtime))
                except Exception:
                    pass

        if total_size > max_bytes and log_files:
            # 按修改时间排序，最老的在前面
            log_files.sort(key=lambda x: x[2])
            removed = 0
            freed_bytes = 0
            for f, size, mtime in log_files:
                if total_size - freed_bytes <= max_bytes:
                    break
                try:
                    f.unlink()
                    freed_bytes += size
                    removed += 1
                except Exception:
                    pass
            if removed > 0:
                freed_mb = freed_bytes / (1024 * 1024)
                total_mb = (total_size - freed_bytes) / (1024 * 1024)
                logger.info(
                    f"🧹 logs: 删除了 {removed} 个旧日志，释放 {freed_mb:.1f}MB，"
                    f"当前总大小 {total_mb:.1f}MB"
                )

    async def _lsp_installation_loop(self) -> None:
        """LSP依赖项自动安装循环 - 在后台自动检查并安装LSP语言服务器"""
        logger.info("📦 LSP依赖项自动安装事务启动")
        try:
            from peng.tools.lsp.dependencies import LspDependencyManager, InstallStatus
            from peng.webui.websocket import broadcast_toast

            await asyncio.sleep(5)  # 延迟启动，避免阻塞服务启动
            manager = LspDependencyManager()

            async def progress_callback(
                name: str, status: InstallStatus, error_message: str | None
            ) -> None:
                if status == InstallStatus.INSTALLING:
                    await broadcast_toast("info", "正在安装", f"{name} 安装中...", 2000)
                elif status == InstallStatus.INSTALLED:
                    logger.info(f"✅ {name} 安装成功")
                    await broadcast_toast("success", "安装成功", f"{name} 已安装", 3000)
                elif status == InstallStatus.INSTALL_FAILED:
                    logger.error(f"❌ {name} 安装失败: {error_message}")
                    await broadcast_toast(
                        "error", "安装失败", f"{name} 失败: {error_message}", 5000
                    )

            results = await manager.check_and_install_all(progress_callback)
            installed_count = sum(1 for s in results.values() if s)
            failed_count = len(results) - installed_count

            logger.info(
                f"📊 LSP依赖项安装完成: {installed_count}成功/{failed_count}失败/{len(results)}总计"
            )
        except asyncio.CancelledError:
            logger.info("LSP依赖项安装事务已取消")
        except Exception as e:
            logger.error(f"❌ LSP依赖项安装事务异常: {e}", exc_info=True)

    async def _local_nest_monitor_loop(self) -> None:
        """内置 local nest 健康监控循环

        检查 bridge 中是否有可用连接，没有则立刻重启内置 local nest。
        如果重启失败，异步等待3秒再重试，防止重启过于频繁。
        """
        MONITOR_INTERVAL = 15  # 检查间隔（秒）
        INITIAL_DELAY = 5  # 初始延迟（秒）
        RESTART_COOLDOWN = 3  # 重启失败后的冷却时间（秒）

        logger.info("🪹 内置 local nest 健康监控启动")
        _restarting = False  # 防止并发重启

        try:
            await asyncio.sleep(INITIAL_DELAY)

            while not self._stop_event.is_set():
                try:
                    await asyncio.wait_for(self._stop_event.wait(), timeout=MONITOR_INTERVAL)
                    break
                except asyncio.TimeoutError:
                    pass

                if self._stop_event.is_set():
                    break

                # 🔧 检查本机是否有可用的内置 local nest 连接
                # 只匹配本机hostname的连接，远程nest（如epyc）不算
                has_local_connection = False
                try:
                    import socket as _socket
                    from peng.nest.bridge import NestBridgeManager

                    _local_hostname = _socket.gethostname().lower()
                    mgr = NestBridgeManager.get_instance()
                    for conn in mgr._connections:
                        if not conn._closing:
                            conn_host = conn.info.get("hostname", "").lower()
                            if conn_host == _local_hostname:
                                has_local_connection = True
                                break
                except Exception:
                    has_local_connection = True  # 无法确认，保守认为健康

                if has_local_connection:
                    continue

                # 没有可用连接 - 立刻重启
                if _restarting:
                    continue  # 上次重启还在进行中，跳过

                _restarting = True
                logger.warning("🪹 没有可用的 nest 连接，立刻重启内置 local nest")

                success = await self._restart_builtin_nest()

                if success:
                    _restarting = False
                else:
                    # 重启失败，异步等待冷却后重置标志
                    async def _cooldown_reset():
                        await asyncio.sleep(RESTART_COOLDOWN)
                        nonlocal _restarting
                        _restarting = False
                        logger.warning(
                            f"🪹 重启失败冷却结束（{RESTART_COOLDOWN}s），将在下次检查时重试"
                        )

                    asyncio.create_task(_cooldown_reset())

        except asyncio.CancelledError:
            logger.info("🪹 内置 local nest 健康监控已取消")
        except Exception as e:
            logger.error(f"❌ 内置 local nest 健康监控异常: {e}", exc_info=True)

    async def _restart_builtin_nest(self) -> bool:
        """重启内置 local nest（子进程模式，与启动时一致）

        Returns:
            True 表示重启成功启动，False 表示重启失败
        """
        try:
            # 清理所有旧的 local nest 进程（子进程 + 进程内 connector）
            try:
                from peng.nest.cli import _kill_all_nest_processes

                _kill_all_nest_processes(name="local")
            except Exception:
                pass

            # 停止旧的进程内 connector（如果有）
            if self._builtin_nest_connector is not None:
                self._builtin_nest_connector.stop()
                self._builtin_nest_connector = None

            # 用子进程方式重启（与启动时 line 345-385 一致）
            from peng.nest.cli import _get_python_cmd, _get_nest_script
            import subprocess as _sp

            nest_cmd = _get_python_cmd() + [
                str(_get_nest_script()),
                "connect",
                "--name", "local",
                "--host", "localhost",
                "--port", "8899",
                "--skip-single-instance-check",
            ]
            project_root = get_project_root()
            log_dir = project_root / "logs"
            ensure_dir_with_owner(log_dir)
            nest_log = str(log_dir / "nest_connect.log")

            nest_log_f = _open_nest_log_with_recovery(nest_log)
            popen_kwargs = dict(
                stdout=nest_log_f,
                stderr=_sp.STDOUT,
                cwd=str(project_root),
                close_fds=True,
            )
            proc = _sp.Popen(nest_cmd, **popen_kwargs)
            self._local_nest_pid = proc.pid
            logger.info(f"🪹 内置 local nest 已重启（子进程模式, PID: {proc.pid}）")
            return True
        except Exception as e:
            logger.error(f"❌ 重启内置 local nest 失败: {e}", exc_info=True)
            return False


def load_config(config_path: Path | None = None) -> dict[str, Any]:
    """加载配置文件"""
    if config_path is None:
        project_root = Path(__file__).resolve().parent.parent.parent.parent
        config_path = project_root / "config" / "peng.json"

    if not config_path.exists():
        logger.warning(f"配置文件不存在：{config_path}")
        return {}

    with open(config_path, encoding="utf-8") as f:
        return json.load(f)


def get_data_path(filename: str) -> Path:
    """获取 data 目录下的文件路径"""
    from peng.config import get_path

    return get_path("data") / filename


def get_pid_file(port: int | None = None) -> Path:
    """
    获取服务 PID 文件路径

    Args:
        port: 服务端口，None 时使用默认端口 6688

    Returns:
        PID 文件路径（按端口区分）
    """
    actual_port = port or 6688
    return get_data_path(f"fly_service_{actual_port}.pid")


def _check_process_exists(pid: int) -> bool:
    """检查进程是否存在（跨平台）"""
    from peng.common.platform import process_exists

    return process_exists(pid)


def _get_port_from_cmdline(pid: int) -> int | None:
    """
    从进程命令行获取端口（跨平台）

    Args:
        pid: 进程 ID

    Returns:
        端口号，无法解析时返回 None
    """
    import re
    import subprocess

    from peng.common.platform import IS_WINDOWS

    try:
        cmdline = ""

        if IS_WINDOWS:
            # Windows: 使用 wmic 获取命令行
            result = subprocess.run(
                ["wmic", "process", "where", f"ProcessId={pid}", "get", "CommandLine=/VALUE"],
                capture_output=True,
                text=True,
                timeout=5,
            )
            if result.returncode == 0:
                # 解析 wmic 输出
                for line in result.stdout.split("\n"):
                    line = line.strip()
                    if line.startswith("CommandLine="):
                        cmdline = line.split("=", 1)[1]
                        break
        else:
            # POSIX: 使用 ps 获取命令行
            result = subprocess.run(
                ["ps", "-p", str(pid), "-o", "cmd=", "--width", "500"],
                capture_output=True,
                text=True,
                timeout=5,
            )
            if result.returncode == 0:
                cmdline = result.stdout.strip()

        if not cmdline:
            return None

        # 格式1: --listen=host:port 或 --listen=port
        match = re.search(r"--listen=(\S+)", cmdline)
        if match:
            listen_value = match.group(1)
            if ":" in listen_value:
                return int(listen_value.rsplit(":", 1)[1])
            return int(listen_value)

        # 格式2: --listen host:port 或 --listen port（空格分隔）
        match = re.search(r"--listen\s+(\S+)", cmdline)
        if match:
            listen_value = match.group(1)
            if ":" in listen_value:
                return int(listen_value.rsplit(":", 1)[1])
            return int(listen_value)

        # 默认端口 6688（有 --service 但无 --listen 参数）
        if "--service" in cmdline:
            return 6688

        return None
    except (subprocess.SubprocessError, ValueError, IndexError, subprocess.TimeoutExpired):
        return None


def _get_running_service_pids(port: int | None = None) -> list[int]:
    """
    获取指定端口上正在运行的 fly 服务进程 PID（跨平台）

    Args:
        port: 服务端口，None 时使用默认端口 6688

    Returns:
        正在运行的服务 PID 列表（排除当前进程）
    """
    import subprocess

    from peng.common.platform import IS_WINDOWS, process_exists

    pids = []
    current_pid = os.getpid()
    target_port = port or 6688

    # 方法1：检查 PID 文件
    pid_file = get_pid_file(target_port)
    if pid_file.exists():
        try:
            file_pid = int(pid_file.read_text().strip())
            if file_pid != current_pid and process_exists(file_pid):
                # 验证该进程确实运行在目标端口
                proc_port = _get_port_from_cmdline(file_pid)
                if proc_port == target_port:
                    pids.append(file_pid)
        except (OSError, ValueError):
            pass

    # 方法2：查找所有 fly 服务进程
    if IS_WINDOWS:
        # Windows: 使用 wmic 查找 python 进程
        try:
            result = subprocess.run(
                ["wmic", "process", "where", "name='python.exe'", "get", "ProcessId,CommandLine"],
                capture_output=True,
                text=True,
                timeout=10,
            )
            if result.returncode == 0:
                for line in result.stdout.split("\n"):
                    if "bin/fly" in line and "--service" in line:
                        # 提取 PID
                        parts = line.strip().split()
                        for part in reversed(parts):
                            try:
                                pid = int(part)
                                if pid != current_pid and pid not in pids and process_exists(pid):
                                    # 检查该进程是否运行在目标端口
                                    proc_port = _get_port_from_cmdline(pid)
                                    if proc_port == target_port:
                                        pids.append(pid)
                                break
                            except ValueError:
                                continue
        except (FileNotFoundError, subprocess.SubprocessError, subprocess.TimeoutExpired):
            pass
    else:
        # POSIX: 通过 pgrep 查找
        try:
            result = subprocess.run(
                ["pgrep", "-f", "fly.*--service"],
                capture_output=True,
                text=True,
                timeout=5,
            )
            if result.returncode == 0 and result.stdout.strip():
                for line in result.stdout.strip().split("\n"):
                    try:
                        pid = int(line.strip())
                        if pid != current_pid and pid not in pids and process_exists(pid):
                            # 检查该进程是否运行在目标端口
                            proc_port = _get_port_from_cmdline(pid)
                            if proc_port == target_port:
                                pids.append(pid)
                    except ValueError:
                        pass
        except (FileNotFoundError, subprocess.SubprocessError, subprocess.TimeoutExpired):
            pass

    return pids


def _kill_all_service_processes(port: int | None = None) -> int:
    """
    强制终止指定端口上的 fly 服务进程（排除当前进程）

    Args:
        port: 服务端口，None 时使用默认端口 6688

    Returns:
        终止的进程数量
    """
    from peng.common.platform import kill_process

    target_port = port or 6688
    killed = 0
    pids = _get_running_service_pids(target_port)

    for pid in pids:
        try:
            logger.info(f"🔪 正在终止端口 {target_port} 的服务进程 (PID: {pid})...")

            # 【修复Bug #进程终止竞态】检查进程是否还存在（避免 ProcessLookupError）
            if not _check_process_exists(pid):
                logger.info(f"进程 {pid} 已不存在，跳过终止")
                continue

            # 使用跨平台封装：先 SIGTERM，必要时 SIGKILL
            kill_process(
                pid,
                force=False,
                kill_tree=False,
                caller="_kill_all_service_processes",
                reason=f"port={target_port}",
            )

            # 等待进程退出
            import time

            for _ in range(5):  # 最多等待5秒
                time.sleep(1)
                if not _check_process_exists(pid):
                    break
            else:
                # 超时，强制终止
                logger.warning(f"进程 {pid} 未响应 SIGTERM，使用 SIGKILL...")
                if _check_process_exists(pid):
                    kill_process(
                        pid,
                        force=True,
                        kill_tree=False,
                        caller="_kill_all_service_processes",
                        reason=f"port={target_port}, force kill after timeout",
                    )
                    time.sleep(1)

            if not _check_process_exists(pid):
                killed += 1
                logger.info(f"✅ 已终止进程 {pid}")
            else:
                logger.warning(f"⚠️ 进程 {pid} 仍然存在，终止失败")
        except (OSError, ProcessLookupError) as e:
            # 进程可能已自然退出（竞态条件），记录 info 而非 warning
            logger.info(f"进程 {pid} 终止操作跳过: {e}")

    # 清理该端口的 PID 文件
    pid_file = get_pid_file(target_port)
    pid_file.unlink(missing_ok=True)

    return killed


def _get_all_service_pids() -> list[int]:
    """
    获取所有正在运行的 fly 服务进程 PID（所有端口）

    Returns:
        正在运行的服务 PID 列表（排除当前进程）
    """
    import subprocess

    from peng.common.platform import IS_WINDOWS

    pids = []
    current_pid = os.getpid()

    if IS_WINDOWS:
        # Windows: 使用 wmic 查找 fly 服务进程
        # 查找 python.exe、pythonw.exe 和 fly.exe 进程
        for proc_name in ("python.exe", "pythonw.exe", "fly.exe"):
            try:
                result = subprocess.run(
                    [
                        "wmic",
                        "process",
                        "where",
                        f"name='{proc_name}'",
                        "get",
                        "ProcessId,CommandLine",
                    ],
                    capture_output=True,
                    text=True,
                    timeout=10,
                )
                if result.returncode == 0:
                    for line in result.stdout.split("\n"):
                        line = line.strip()
                        if "fly" in line and "service" in line:
                            # 提取 PID（wmic 输出的最后一列）
                            parts = line.split()
                            for part in reversed(parts):
                                try:
                                    pid = int(part)
                                    if (
                                        pid != current_pid
                                        and pid not in pids
                                        and _check_process_exists(pid)
                                    ):
                                        pids.append(pid)
                                    break
                                except ValueError:
                                    continue
            except (FileNotFoundError, subprocess.SubprocessError, subprocess.TimeoutExpired):
                pass
    else:
        # POSIX: 通过 pgrep 查找
        try:
            result = subprocess.run(
                ["pgrep", "-f", "fly service"],
                capture_output=True,
                text=True,
                timeout=10,  # 防止垃圾进程卡死项目
            )
            if result.returncode == 0 and result.stdout.strip():
                for line in result.stdout.strip().split("\n"):
                    try:
                        pid = int(line.strip())
                        if pid != current_pid and pid not in pids and _check_process_exists(pid):
                            pids.append(pid)
                    except ValueError:
                        pass
        except (FileNotFoundError, AttributeError):
            # Windows 不支持 pgrep
            pass

    return pids


def kill_all_services() -> int:
    """
    强制终止所有端口的 fly 服务进程（排除当前进程）

    Returns:
        终止的进程数量
    """
    import time

    from peng.common.platform import kill_process

    killed = 0
    pids = _get_all_service_pids()

    if not pids:
        return 0

    print(f"🔪 发现 {len(pids)} 个服务进程，正在终止...")

    for pid in pids:
        try:
            print(f"  正在终止 PID: {pid}...")
            kill_process(
                pid,
                force=False,
                kill_tree=False,
                caller="kill_all_services",
                reason="kill all services",
            )
        except (OSError, ProcessLookupError) as e:
            print(f"  ⚠️ 终止 PID {pid} 失败: {e}", file=sys.stderr)

    # 等待进程退出
    time.sleep(1)

    # 检查并强制终止未响应的进程
    for pid in pids:
        if _check_process_exists(pid):
            try:
                print(f"  强制终止 PID: {pid} (未响应 SIGTERM)...")
                kill_process(
                    pid,
                    force=True,
                    kill_tree=False,
                    caller="kill_all_services",
                    reason="force kill after timeout",
                )
            except (OSError, ProcessLookupError):
                pass

    time.sleep(0.5)

    # 统计最终结果
    for pid in pids:
        if not _check_process_exists(pid):
            killed += 1

    # 清理所有 PID 文件
    data_dir = get_data_path(".")
    for pid_file in data_dir.glob("fly_service_*.pid"):
        pid_file.unlink(missing_ok=True)

    print(f"✅ 已终止 {killed} 个服务进程")
    return killed


def stop_service() -> int:
    """
    停止后台服务（跨平台方案：使用信号文件 + 超时强制终止）

    改进：
    1. 先发送信号文件，等待优雅退出（最多10秒）
    2. 超时后强制终止进程
    3. 清理所有临时文件

    Returns:
        退出码 (0=成功，1=失败)
    """
    import subprocess
    import time

    signal_file = get_stop_signal_file()
    pid_file = get_pid_file()

    try:
        # 获取当前运行的 PID
        current_pid = None
        if pid_file.exists():
            try:
                current_pid = int(pid_file.read_text().strip())
            except (OSError, ValueError):
                pass

        # 检查服务是否在运行
        service_running = False
        if current_pid and _check_process_exists(current_pid):
            service_running = True
        else:
            # 尝试通过 pgrep 检查（仅 Unix）
            try:
                result = subprocess.run(
                    ["pgrep", "-f", "fly.*--service"],
                    capture_output=True,
                    text=True,
                    timeout=10,  # 防止垃圾进程卡死项目
                )
                if result.returncode == 0 and result.stdout.strip():
                    # 可能有多个匹配，取第一个
                    pids = result.stdout.strip().split("\n")
                    if pids:
                        try:
                            current_pid = int(pids[0])
                            service_running = True
                        except ValueError:
                            pass
            except (FileNotFoundError, AttributeError):
                # Windows 不支持 pgrep
                pass

        if not service_running:
            print("❌ 服务未运行")
            logger.warning("服务未运行")
            # 清理残留文件
            pid_file.unlink(missing_ok=True)
            signal_file.unlink(missing_ok=True)
            return 1

        # 创建停止信号文件
        signal_file.touch()
        print(f"🛑 正在停止服务 (PID: {current_pid})...")
        logger.info(f"已发送停止信号 (PID: {current_pid})...")

        # 等待进程优雅退出
        # Windows 平台信号文件机制无效，直接强制终止
        from peng.common.platform import IS_WINDOWS

        max_wait_seconds = 0 if IS_WINDOWS else 10
        stopped_gracefully = False
        for i in range(max_wait_seconds):
            time.sleep(1)
            if current_pid and not _check_process_exists(current_pid):
                stopped_gracefully = True
                print("✅ 服务已优雅停止")
                break
            if not pid_file.exists():
                stopped_gracefully = True
                print("✅ 服务已优雅停止")
                break

        if stopped_gracefully:
            logger.info("✅ 服务已优雅停止")
        else:
            # 超时或 Windows 平台直接强制终止
            if IS_WINDOWS:
                print("⚡ Windows 平台直接强制终止进程...")
                logger.info("Windows 平台直接强制终止进程")
            else:
                print(f"⚠️ 服务未在 {max_wait_seconds} 秒内响应，尝试强制终止...")
                logger.warning(f"⚠️ 服务未在 {max_wait_seconds} 秒内响应，尝试强制终止...")
            if current_pid and _check_process_exists(current_pid):
                try:
                    from peng.common.platform import kill_process

                    kill_process(
                        current_pid,
                        force=True,
                        kill_tree=False,
                        caller="stop_service",
                        reason="force stop after timeout",
                    )
                    time.sleep(1)
                    print(f"✅ 已强制终止进程 {current_pid}")
                    logger.info(f"✅ 已强制终止进程 {current_pid}")
                except (OSError, ProcessLookupError) as e:
                    print(f"❌ 强制终止失败: {e}")
                    logger.warning(f"强制终止失败: {e}\n{traceback.format_exc()}")

        # 清理所有临时文件
        pid_file.unlink(missing_ok=True)
        signal_file.unlink(missing_ok=True)
        logger.debug("已清理 PID 和信号文件")

        return 0

    except Exception as e:
        tb = traceback.format_exc()
        logger.error(f"停止服务失败：{e}\n{traceback.format_exc()}\n{tb}")
        return 1


def restart_service(argv: list[str]) -> int:
    """
    重启后台服务

    Args:
        argv: 命令行参数（包含 --service 等）

    Returns:
        退出码
    """
    import subprocess
    import time

    logger.info("🔄 重启后台服务...")

    # 1. 停止现有服务
    stop_result = stop_service()
    if stop_result == 0:
        # 等待服务完全停止
        time.sleep(2)
        logger.info("旧服务已停止，启动新服务...")
    else:
        logger.info("无旧服务运行，直接启动新服务...")

    # 2. 启动新服务
    project_root = Path(__file__).resolve().parent.parent.parent.parent

    from peng.common.platform import IS_WINDOWS

    if IS_WINDOWS:
        # Windows: 优先用 fly.exe（uv sync 安装的 entry point）
        fly_exe = project_root / ".venv" / "Scripts" / "fly.exe"
        if fly_exe.exists():
            cmd = [str(fly_exe), "service"]
        else:
            # 回退到 python -m 方式
            import sys

            cmd = [sys.executable, "-m", "peng.ui.fly", "service"]
    else:
        # Linux: 用 bin/fly 脚本
        fly_bin = project_root / "bin" / "fly"
        cmd = [str(fly_bin), "service"]
    logger.info(f"启动命令：{' '.join(cmd)}")

    try:
        kwargs = {}
        if IS_WINDOWS:
            # Windows: 不弹窗，输出到日志
            log_dir = project_root / "logs"
            log_dir.mkdir(exist_ok=True)
            service_log = log_dir / "service_startup.log"
            log_f = open(service_log, "a")
            kwargs["creationflags"] = (
                subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW
            )
            kwargs["stdout"] = log_f
            kwargs["stderr"] = log_f
        else:
            kwargs["start_new_session"] = True
            kwargs["stdout"] = subprocess.DEVNULL
            kwargs["stderr"] = subprocess.DEVNULL

        subprocess.Popen(cmd, **kwargs)
        time.sleep(3)

        pid_file = get_pid_file()
        if pid_file.exists():
            pid = pid_file.read_text().strip()
            logger.info(f"✅ 服务已重启 (PID: {pid})")
            return 0
        else:
            logger.warning("服务启动中，请稍后检查状态")
            return 0

    except Exception as e:
        tb = traceback.format_exc()
        logger.error(f"启动服务失败：{e}\n{traceback.format_exc()}\n{tb}")
        return 1


async def run_service(
    webui_enabled: bool = False,
    webui_host: str | None = None,
    webui_port: int | None = None,
    force_start: bool = False,
) -> int:
    """
    运行后台服务（整合launcher崩溃恢复系统）

    Args:
        webui_enabled: 是否启用 Web UI
        webui_host: Web UI 监听地址（覆盖配置文件）
        webui_port: Web UI 监听端口（覆盖配置文件）
        force_start: 是否强制启动（先终止已有服务）

    Returns:
        退出码
    """
    import os

    # 确定最终端口
    final_port = webui_port or 6688

    # 启动前检查是否已有服务在运行（同一端口）
    running_pids = _get_running_service_pids(final_port)
    if running_pids:
        if force_start:
            logger.info(
                f"🔄 强制启动：发现端口 {final_port} 上有 {len(running_pids)} 个服务进程，正在终止..."
            )
            killed = _kill_all_service_processes(final_port)
            logger.info(f"✅ 已终止 {killed} 个旧服务进程")
            # 等待进程完全退出
            await asyncio.sleep(1)
        else:
            # 使用简洁的错误提示，不输出 traceback
            print(f"❌ 端口 {final_port} 上已有服务在运行 (PID: {running_pids})")
            print(f"💡 使用 './bin/fly kill --port {final_port}' 释放端口")
            print(f"   或使用 './bin/fly service --force' 强制启动")
            return 1

    # 写入 PID 文件（按端口区分）
    pid_file = get_pid_file(final_port)
    ensure_dir_with_owner(pid_file.parent)
    pid_file.write_text(str(os.getpid()))
    chown_to_project_owner(pid_file)
    logger.info(f"PID 文件已创建：{pid_file} (PID: {os.getpid()})")

    # 加载配置
    from peng.config.loader import load_config

    config = load_config()

    # 检查配置中是否启用 webui
    webui_config = config.webui

    # 决定是否启用 webui：命令行参数优先，否则使用配置文件
    if not webui_enabled:
        webui_enabled = webui_config.enabled

    # 确定 webui 的 host 和 port：命令行参数优先
    final_host = webui_host if webui_host else webui_config.host
    final_port = webui_port if webui_port else webui_config.port

    # 创建launcher实例（整合崩溃恢复系统）
    from peng.service.launcher import ServiceLauncher

    launcher = ServiceLauncher(port=final_port, service_name="peng")

    # 定义service_func：包装BackgroundService启动逻辑
    async def service_func():
        """BackgroundService启动函数（会被launcher包装）"""
        # 创建服务实例
        service = BackgroundService(config=config)

        # 启动BackgroundService
        await service.start(webui=webui_enabled, webui_host=final_host, webui_port=final_port)

    # 使用launcher运行服务（包含崩溃恢复）
    try:
        success = await launcher.run_with_health_check(service_func)
        logger.info(f"launcher.run_with_health_check() 返回: {success}")
    except KeyboardInterrupt:
        # 让 KeyboardInterrupt 正常传播，由外层处理
        raise
    except Exception as e:
        logger.error(f"launcher运行异常: {e}", exc_info=True)
        success = False
    finally:
        # 清理PID文件
        try:
            pid_file.unlink(missing_ok=True)
            logger.info("PID 文件已清理")
        except Exception as e:
            logger.error(f"❌ 清理PID文件失败: {e}", exc_info=True)

    # 返回结果
    # 如果launcher返回True，说明服务成功启动并正常运行
    # 如果launcher返回False，说明服务启动失败（可能已经执行了崩溃恢复）
    # systemd的Restart=on-failure会自动重启失败的服务
    return 0 if success else 1


def main() -> int:
    """服务入口函数"""

    # 初始化日志系统（使用统一的日志配置）
    from peng.common.logging_config import setup_logging

    setup_logging()

    # 不设置 sys.excepthook，让 asyncio.run() 正常处理 KeyboardInterrupt
    # 这样 Ctrl+C 能够干净退出

    try:
        return asyncio.run(run_service())
    except KeyboardInterrupt:
        logger.info("🛑 收到 Ctrl+C，正在停止服务...")
        # asyncio.run 会自动取消所有事务并等待清理完成
        # 不需要额外的清理代码
        return 0


if __name__ == "__main__":
    sys.exit(main())
