"""
WebSocket 管理

实现实时进程输出推送和远程终端

需求映射: docs/requests.md §9.10-9.11
"""

from __future__ import annotations

from peng.config import get_agent_name

AGENT_NAME = get_agent_name()

import asyncio  # noqa: E402
import json  # noqa: E402
import logging  # noqa: E402
import os  # noqa: E402
import signal  # noqa: E402
import struct  # noqa: E402
import time  # noqa: E402
import traceback  # noqa: E402

# 服务启动时间（用于前端检测服务重启后自动刷新）
_SERVER_START_TIME = time.time()
import uuid  # noqa: E402
from typing import Any  # noqa: E402
from io import StringIO  # noqa: E402

# Windows 兼容：Unix PTY 模块仅在 POSIX 平台可用
import sys
_UNIX_PTY_AVAILABLE = sys.platform != "win32"
_WIN_PTY_AVAILABLE = False
if _UNIX_PTY_AVAILABLE:
    import fcntl  # noqa: E402
    import pty  # noqa: E402
    import termios  # noqa: E402
else:
    # Windows: 尝试导入 pywinpty（ConPTY 后端）
    try:
        from winpty import PtyProcess as _WinPtyProcess  # noqa: E402
        _WIN_PTY_AVAILABLE = True
    except ImportError:
        _WIN_PTY_AVAILABLE = False

from peng.common.platform import IS_WINDOWS, kill_process, process_exists

from fastapi import WebSocket, WebSocketDisconnect, Query  # noqa: E402

from peng.config.loader import load_config  # noqa: E402
from peng.message.manager import MessageManager  # noqa: E402

logger = logging.getLogger(__name__)


class PtyProcess:
    """
    基于Python标准库pty的伪终端进程包装类

    使用 pty.openpty() 创建伪终端，os.fork() + os.execvp() 启动shell进程，
    提供 spawn()、write()、setwinsize()、isalive()、terminate() 等接口。

    注意：此类仅在 POSIX 平台可用。Windows 上不支持 PTY 功能。
    """

    def __init__(self, pid: int, master_fd: int, slave_fd: int):
        self.pid = pid
        self.master_fd = master_fd
        self._slave_fd = slave_fd  # 进程启动后关闭
        self._exit_status: int | None = None

    @classmethod
    def spawn(
        cls,
        command: str,
        encoding: str = "utf-8",
        dimensions: tuple[int, int] = (24, 80),
        env: dict[str, str] | None = None,
        cwd: str | None = None,
    ) -> PtyProcess:
        """
        创建伪终端并启动子进程

        Args:
            command: 要执行的命令（通常是shell路径）
            encoding: 编码方式
            dimensions: 终端大小 (rows, cols)
            env: 环境变量字典
            cwd: 工作目录（可选）

        Raises:
            OSError: 在 Windows 平台上调用时抛出（PTY 不支持 Windows）
        """
        if IS_WINDOWS:
            raise OSError("PTY 终端功能在 Windows 上不可用。请使用 WSL 或等待 ConPTY 支持。")

        master_fd, slave_fd = pty.openpty()

        # 设置终端大小
        rows, cols = dimensions
        winsize = struct.pack("HHHH", rows, cols, 0, 0)
        fcntl.ioctl(slave_fd, termios.TIOCSWINSZ, winsize)

        # 合并环境变量
        proc_env = os.environ.copy()
        if env:
            proc_env.update(env)

        pid = os.fork()
        if pid == 0:
            # 子进程
            try:
                os.close(master_fd)
                os.setsid()

                # 关键：将slave_fd设为控制终端（TIOCSCTTY）
                # 必须在 setsid() 之后调用，否则 slave 不会成为新 session 的控制终端
                # 缺少此步骤会导致 Ctrl+C/SIGINT 等信号无法传递到子进程
                TIOCSCTTY = 0x540E  # Linux x86_64
                fcntl.ioctl(slave_fd, TIOCSCTTY, 0)

                # 将slave_fd设置为子进程的stdin/stdout/stderr
                os.dup2(slave_fd, 0)
                os.dup2(slave_fd, 1)
                os.dup2(slave_fd, 2)

                if slave_fd > 2:
                    os.close(slave_fd)

                # 设置终端大小（子进程侧）
                fcntl.ioctl(0, termios.TIOCSWINSZ, winsize)

                # 🔧 Task#185：切换到指定工作目录
                if cwd:
                    os.chdir(cwd)

                # 🔧 修复：使用 execvpe 传递 proc_env 环境变量（execvp 不接受自定义 env）
                # 传递 -l（login shell）参数，确保加载 ~/.zshrc / ~/.bashrc 等用户配置
                os.execvpe(command, [command, "-l"], proc_env)
            except Exception:
                os._exit(1)
        else:
            # 父进程
            os.close(slave_fd)
            return cls(pid, master_fd, slave_fd)

    def write(self, data: str) -> int:
        """写入数据到伪终端"""
        return os.write(self.master_fd, data.encode("utf-8"))

    def setwinsize(self, rows: int, cols: int) -> None:
        """设置终端窗口大小"""
        winsize = struct.pack("HHHH", rows, cols, 0, 0)
        try:
            fcntl.ioctl(self.master_fd, termios.TIOCSWINSZ, winsize)
        except OSError as e:
            logger.warning(f"设置终端大小失败: {e}")

    def isalive(self) -> bool:
        """检查进程是否仍然存活"""
        if self._exit_status is not None:
            return False
        try:
            wpid, status = os.waitpid(self.pid, os.WNOHANG)
            if wpid == 0:
                return True
            self._exit_status = status
            return False
        except ChildProcessError:
            return False

    def sendcontrol(self, char: str) -> None:
        """发送控制字符（如 'c' 发送 Ctrl+C）"""
        control_map = {
            "c": b"\x03",  # Ctrl+C
            "d": b"\x04",  # Ctrl+D
            "z": b"\x1a",  # Ctrl+Z
            "\\": b"\x1c",  # Ctrl+\
        }
        data = control_map.get(char)
        if data:
            os.write(self.master_fd, data)

    def terminate(self, force: bool = False) -> None:
        """终止进程（使用跨平台 kill_process 封装）"""
        kill_process(
            self.pid,
            force=force,
            kill_tree=False,
            caller="PtyProcess.terminate",
            reason=f"force={force}",
        )

    def close(self) -> None:
        """关闭伪终端主设备"""
        try:
            if self.master_fd >= 0:
                os.close(self.master_fd)
        except OSError:
            pass


class WindowsPtyProcess:
    """
    基于 pywinpty 的 Windows 伪终端进程包装类

    使用 pywinpty.PtyProcess（ConPTY 后端）创建伪终端，
    提供与 PtyProcess 相同的接口，支持 Windows 平台。

    需要 pywinpty>=2.0.0 依赖。
    """

    def __init__(self, proc: Any):
        """
        Args:
            proc: pywinpty.PtyProcess 实例
        """
        self._proc = proc
        self._closed = False

    @property
    def pid(self) -> int:
        """获取子进程 PID（从 proc 的 pid 属性读取）"""
        return self._proc.pid

    @classmethod
    def spawn(
        cls,
        command: str,
        encoding: str = "utf-8",
        dimensions: tuple[int, int] = (24, 80),
        env: dict[str, str] | None = None,
    ) -> WindowsPtyProcess:
        """
        创建 Windows 伪终端并启动子进程

        Args:
            command: 要执行的命令（例如 'cmd.exe' 或 'powershell.exe'）
            encoding: 编码方式（保留兼容，暂不使用）
            dimensions: 终端大小 (rows, cols)
            env: 环境变量字典

        Raises:
            OSError: pywinpty 不可用或创建终端失败时抛出
        """
        if not _WIN_PTY_AVAILABLE:
            raise OSError(
                "Windows PTY 功能需要 pywinpty 库。"
                "请执行: pip install pywinpty 或 uv sync"
            )

        rows, cols = dimensions

        try:
            proc = _WinPtyProcess.spawn(
                command,
                env=env,
                dimensions=(rows, cols),
            )
        except Exception as e:
            raise OSError(f"无法启动终端进程 '{command}': {e}") from e

        logger.info(f"Windows PTY 进程已创建: command={command}, pid={proc.pid}, size={rows}x{cols}")

        return cls(proc)

    def write(self, data: str) -> int:
        """写入数据到伪终端"""
        if self._closed:
            return 0
        try:
            self._proc.write(data)
            return len(data)
        except Exception as e:
            logger.warning(f"Windows PTY 写入失败: {e}")
            return 0

    def setwinsize(self, rows: int, cols: int) -> None:
        """设置终端窗口大小"""
        if self._closed:
            return
        try:
            self._proc.setwinsize(rows, cols)
        except Exception as e:
            logger.warning(f"Windows PTY 设置窗口大小失败: {e}")

    def isalive(self) -> bool:
        """检查进程是否仍然存活"""
        if self._closed:
            return False
        try:
            return self._proc.isalive()
        except Exception:
            return False

    def sendcontrol(self, char: str) -> None:
        """发送控制字符（如 'c' 发送 Ctrl+C）"""
        if self._closed:
            return
        try:
            self._proc.sendcontrol(char)
        except Exception as e:
            logger.warning(f"Windows PTY 发送控制字符失败: {e}")

    def read(self, size: int = 65536) -> str | None:
        """
        读取终端输出

        注意：此方法可能会阻塞（内部使用 socket.recv）。
        在异步环境中应通过 run_in_executor 调用。

        Returns:
            读取到的文本，如果终端已关闭返回 None
        """
        if self._closed:
            return None
        try:
            data = self._proc.read(size=size)
            return data
        except EOFError:
            # 终端已关闭
            return None
        except Exception:
            return None

    def terminate(self, force: bool = False) -> None:
        """终止进程"""
        if self._closed:
            return
        try:
            self._proc.terminate(force=True)
        except Exception:
            pass
        # 也通过 kill_process 确保进程树被终止
        try:
            kill_process(
                self.pid,
                force=True,
                kill_tree=True,
                caller="WindowsPtyProcess.terminate",
                reason=f"force={force}",
            )
        except Exception:
            pass

    def close(self) -> None:
        """关闭伪终端"""
        if self._closed:
            return
        self._closed = True
        try:
            self._proc.close(force=True)
        except Exception:
            pass


class ConnectionManager:
    """WebSocket 连接管理器"""

    def __init__(self) -> None:
        self.active_connections: list[WebSocket] = []
        # 为每个连接维护一个发送锁，防止并发发送导致AssertionError
        self._connection_locks: dict[int, asyncio.Lock] = {}
        # 主事件循环引用（首个连接时捕获，供工作线程通过 run_coroutine_threadsafe 安全调度协程）
        self._loop: asyncio.AbstractEventLoop | None = None

    async def connect(self, websocket: WebSocket) -> None:
        """接受新连接"""
        await websocket.accept()
        # 捕获主事件循环引用（仅在首次连接时设置）
        if self._loop is None:
            self._loop = asyncio.get_running_loop()
        self.active_connections.append(websocket)
        # 为新连接创建锁
        self._connection_locks[id(websocket)] = asyncio.Lock()
        logger.info(f"WebSocket 连接建立，当前连接数: {len(self.active_connections)}")

    async def disconnect(self, websocket: WebSocket) -> None:
        """断开连接"""
        removed = False
        if websocket in self.active_connections:
            self.active_connections.remove(websocket)
            removed = True
        # 🔧 Task#1725：只有当连接确实在活跃列表中时才清锁！
        # 否则出现灾难竞态：reload时浏览器先accept新连接(创建锁)，
        # 后处理旧连接关闭帧(pop锁)——若新旧对象id因地址复用相同，
        # 旧disconnect会删掉新连接的锁 → 新连接所有send_to永久静默丢弃。
        if removed:
            ws_id = id(websocket)
            self._connection_locks.pop(ws_id, None)
        logger.info(f"WebSocket 连接断开，当前连接数: {len(self.active_connections)}")

    async def send_to(self, websocket: WebSocket, message: str | dict[str, Any]) -> None:
        """发送消息给指定连接（带锁保护，防止并发发送冲突）"""
        if isinstance(message, dict):
            message = json.dumps(message, ensure_ascii=False)
        logger.debug(f"WebSocket 发送消息: {message[:100]}...")

        # 获取连接的锁，如果连接已断开则跳过
        ws_id = id(websocket)
        lock = self._connection_locks.get(ws_id)
        if lock is None:
            # 🔧 Task#1725：静默丢弃是诊断黑洞！锁丢失说明连接管理状态已损坏，
            # 必须大声日志（曾导致页面收不到任何实时输出却无任何痕迹）
            logger.warning(
                f"⚠️ WebSocket连接锁不存在，发送被丢弃(连接状态损坏?): ws_id={ws_id}, "
                f"type={message[:60] if isinstance(message, str) else 'dict'}"
            )
            return

        # 使用锁保护发送操作，防止并发发送导致AssertionError
        # 🔧 Task#1725：send必须带超时！半死连接（TCP半开/对端不读导致发送缓冲满）
        # 会让send_text永久挂起——watch_loop顺序await每个客户端的回调，
        # 一个僵尸连接就能卡死整个输出推送循环（所有客户端零增量、前端循环重订阅）。
        async with lock:
            try:
                await asyncio.wait_for(websocket.send_text(message), timeout=5.0)
            except asyncio.TimeoutError:
                # 🔧 Task#1725：发送超时=死连接，立即清理，绝不让它拖垮watch_loop
                logger.warning(
                    f"WebSocket发送超时(5s)，判定为死连接并清理: ws_id={ws_id}"
                )
                # 仅当连接确实活跃时才清理（防误删地址复用的新连接的锁）
                if websocket in self.active_connections:
                    self.active_connections.remove(websocket)
                    self._connection_locks.pop(ws_id, None)
                try:
                    await websocket.close()
                except Exception:
                    pass
            except RuntimeError as e:
                # WebSocket已关闭或响应已完成，清理连接
                err_msg = str(e)
                if (
                    "close message has been sent" in err_msg
                    or "not connected" in err_msg
                    or "after sending 'websocket.close'" in err_msg
                    or "response already completed" in err_msg
                ):
                    logger.debug(f"WebSocket已关闭或响应完成，清理连接: {e}")
                    if websocket in self.active_connections:
                        self.active_connections.remove(websocket)
                        self._connection_locks.pop(ws_id, None)
                else:
                    raise
            except WebSocketDisconnect:
                # WebSocket断开是预期行为，使用DEBUG级别
                logger.debug("WebSocket连接已断开，跳过发送消息")
                # 从活跃连接列表中移除已断开的连接并清理锁
                if websocket in self.active_connections:
                    self.active_connections.remove(websocket)
                    self._connection_locks.pop(ws_id, None)
            except AssertionError as e:
                # websockets 库内部状态异常（并发发送导致 waiter 状态不一致）
                # 视为连接已断开，清理连接
                logger.debug(
                    f"WebSocket 内部状态异常 (AssertionError): {e}, "
                    f"msg={message[:100]}, traceback={traceback.format_exc()}"
                )
                # 从活跃连接列表中移除已断开的连接并清理锁
                if websocket in self.active_connections:
                    self.active_connections.remove(websocket)
                    self._connection_locks.pop(ws_id, None)
                # 主动关闭 WebSocket 连接
                try:
                    await websocket.close()
                except Exception:
                    pass  # 忽略关闭失败
            except Exception as e:
                # 其他异常才打印WARNING
                logger.warning(
                    f"WebSocket发送消息失败: type={type(e).__name__}, str={str(e)}, repr={repr(e)}, "
                    f"msg={message[:100]}, traceback={traceback.format_exc()}"
                )
                # 从活跃连接列表中移除已断开的连接并清理锁
                if websocket in self.active_connections:
                    self.active_connections.remove(websocket)
                self._connection_locks.pop(ws_id, None)
                # 关键修复：主动关闭WebSocket连接，让主循环触发WebSocketDisconnect异常
                try:
                    await websocket.close()
                except Exception:
                    pass  # 忽略关闭失败

    async def broadcast(self, message: str | dict[str, Any]) -> None:
        """
        广播消息到所有活跃的WebSocket连接（使用asyncio.gather并行发送）

        Args:
            message: 要广播的消息（字符串或字典）
        """
        if not self.active_connections:
            return
        if isinstance(message, dict):
            message = json.dumps(message, ensure_ascii=False)
        # 使用 asyncio.gather 并行发送到所有连接
        # return_exceptions=True 避免单连接失败影响其他连接
        tasks = [self.send_to(ws, message) for ws in list(self.active_connections)]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        # 记录发送失败（debug级别，send_to内部已处理异常）
        for i, result in enumerate(results):
            if isinstance(result, Exception):
                logger.debug(f"广播发送到连接 {i} 失败: {result}")


# 全局连接管理器
manager = ConnectionManager()


# ==================== 终端会话管理（支持页面刷新后恢复） ====================


class TerminalSession:
    """终端会话类，管理pty进程和连接"""

    def __init__(self, session_id: str, pty_proc: PtyProcess, read_task: asyncio.Task, nest: str = "local"):
        self.session_id = session_id
        self.pty_proc = pty_proc  # PtyProcess对象
        self.child = pty_proc  # 兼容性别名，供外部引用 session.child.pid
        self.read_task = read_task
        self.created_at = asyncio.get_event_loop().time()
        self.websockets: list[WebSocket] = []  # 附加到此会话的WebSocket连接列表
        self.nest = nest  # 🔧 Task#84：记住终端所属的nest
        # 🔧 优化：使用StringIO代替字符串拼接（方案4）
        self.screen_buffer = StringIO()  # 屏幕内容缓冲区，使用StringIO提高性能
        self._buffer_size = 0  # 缓冲区大小跟踪
        self._max_buffer_size = 10 * 1024 * 1024  # 最大缓冲区10MB
        self.cursor_x: int = 0  # 光标列位置（从0开始）
        self.cursor_y: int = 0  # 光标行位置（从0开始）
        # 🔧 修复：需求#7 - 序列化屏幕快照（由前端xterm-addon-serialize生成）
        # 用于页面刷新后正确恢复终端内容（替代原始pty输出流，避免乱码）
        self.serialized_screen: str = ""  # xterm序列化后的屏幕快照

    def add_websocket(self, websocket: WebSocket):
        """添加WebSocket连接到会话"""
        if websocket not in self.websockets:
            self.websockets.append(websocket)

    def _add_to_buffer(self, data: str):
        """
        添加数据到缓冲区（使用StringIO优化）

        Args:
            data: 要添加的数据
        """
        self.screen_buffer.write(data)
        self._buffer_size += len(data)

        # 超过限制时截断
        if self._buffer_size > self._max_buffer_size:
            # 读取所有内容
            all_content = self.screen_buffer.getvalue()
            # 保留后5MB
            keep_content = all_content[-5 * 1024 * 1024 :]
            # 重置StringIO
            self.screen_buffer = StringIO()
            self.screen_buffer.write(keep_content)
            self._buffer_size = len(keep_content)

    def remove_websocket(self, websocket: WebSocket):
        """从会话中移除WebSocket连接"""
        if websocket in self.websockets:
            self.websockets.remove(websocket)

    def is_active(self) -> bool:
        """检查会话是否仍然活跃（pty进程存活）"""
        try:
            return self.pty_proc.isalive()
        except Exception as e:
            logger.warning(f"检查会话活跃性失败: session_id={self.session_id}, error={e}")
            return False

    async def broadcast(self, message: str | dict[str, Any]):
        """广播消息到所有WebSocket连接"""
        if isinstance(message, dict):
            message = json.dumps(message, ensure_ascii=False)

        # 遍历副本，避免遍历过程中修改列表
        for ws in list(self.websockets):
            try:
                await ws.send_text(message)
            except RuntimeError as e:
                # WebSocket已关闭，移除此连接（但保持pty实例运行）
                if "not connected" in str(e) or "close message has been sent" in str(e):
                    logger.debug(f"WebSocket已关闭，移除此连接: {e}")
                    self.remove_websocket(ws)
                else:
                    raise
            except WebSocketDisconnect:
                # WebSocket断开，移除此连接（但保持pty实例运行）
                logger.debug("WebSocket连接已断开，移除此连接")
                self.remove_websocket(ws)
            except Exception as e:
                # 其他发送错误，移除此连接（但保持pty实例运行）
                # 这可能是网络临时故障，前端稍后会重连
                logger.warning(
                    f"发送到WebSocket失败: type={type(e).__name__}, str={str(e)}, msg={message[:100] if message else '(empty)'}"
                )
                self.remove_websocket(ws)

    def update_serialized_screen(self, serialized: str):
        """
        更新序列化屏幕快照（由前端定期同步）

        Args:
            serialized: xterm-addon-serialize序列化后的屏幕内容
        """
        self.serialized_screen = serialized

    def get_current_screen(self) -> tuple[str, int, int]:
        """
        获取当前屏幕内容和光标位置

        返回序列化屏幕快照（由前端xterm-addon-serialize生成）。
        如果没有序列化数据，返回空字符串（不再回退到原始buffer，
        因为原始buffer包含终端查询响应等不可逆序列，直接写入xterm会导致乱码）。

        Returns:
            (screen, cursor_x, cursor_y) 屏幕内容和光标位置
        """
        if self.serialized_screen:
            return self.serialized_screen, self.cursor_x, self.cursor_y
        # 不再回退到原始buffer，避免发送终端查询响应等不可逆序列
        return "", self.cursor_x, self.cursor_y


class TerminalSessionManager:
    """终端会话管理器，管理所有活跃的终端会话"""

    def __init__(self):
        self.sessions: dict[str, TerminalSession] = {}  # {session_id: TerminalSession}

    def create_session(
        self, session_id: str, pty_proc: PtyProcess, read_task: asyncio.Task, nest: str = "local"
    ) -> TerminalSession:
        """创建新的终端会话"""
        session = TerminalSession(session_id, pty_proc, read_task, nest=nest)
        self.sessions[session_id] = session
        logger.info(f"✅ 终端会话已创建: session_id={session_id}, pid={pty_proc.pid}, nest={nest}")
        return session

    def get_session(self, session_id: str) -> TerminalSession | None:
        """获取指定的终端会话"""
        return self.sessions.get(session_id)

    def cleanup_session(self, session_id: str):
        """清理指定的终端会话（终止pty进程）"""
        if session_id not in self.sessions:
            return

        session = self.sessions[session_id]
        logger.info(f"🧹 清理终端会话: session_id={session_id}, pid={session.pty_proc.pid}")

        # 取消读取事务
        if session.read_task and not session.read_task.done():
            session.read_task.cancel()

        # 移除异步读取器（仅 POSIX PtyProcess 有 master_fd）
        try:
            loop = asyncio.get_event_loop()
            if hasattr(session.pty_proc, 'master_fd'):
                loop.remove_reader(session.pty_proc.master_fd)
        except (OSError, ValueError):
            pass  # reader可能已经移除

        # 终止pty进程
        if session.pty_proc.isalive():
            try:
                session.pty_proc.sendcontrol("c")
                session.pty_proc.terminate(force=True)
            except Exception as e:
                logger.warning(f"终止pty进程失败: {e}")

        # 关闭pty主设备
        session.pty_proc.close()

        del self.sessions[session_id]

    def cleanup_inactive_sessions(self, max_age: float = 3600.0):
        """清理不活跃的会话（超过最大存活时间且没有WebSocket连接）"""
        current_time = asyncio.get_event_loop().time()
        sessions_to_remove = []

        for session_id, session in self.sessions.items():
            # 没有WebSocket连接且超过最大存活时间
            if len(session.websockets) == 0:
                age = current_time - session.created_at
                if age > max_age:
                    sessions_to_remove.append(session_id)

        for session_id in sessions_to_remove:
            self.cleanup_session(session_id)

        if sessions_to_remove:
            logger.info(f"🧹 清理了 {len(sessions_to_remove)} 个不活跃的终端会话")


# 全局终端会话管理器
terminal_session_manager = TerminalSessionManager()


# 全局暂停事务管理（阶段9）
paused_tasks: dict[int, dict[str, Any]] = {}
"""
全局暂停事务字典：{task_id: {'context': dict, 'signal': 'paused'|'resume'}}
- context: 暂停时保存的关键上下文
- signal: 暂停信号变量，'paused'表示暂停，'resume'表示恢复
"""


def add_paused_task(task_id: int, context: dict[str, Any]) -> None:
    """
    添加暂停事务到全局列表

    Args:
        task_id: 事务ID
        context: 暂停时的关键上下文
    """
    paused_tasks[task_id] = {"context": context, "signal": "paused"}
    logger.info(f"⏸️ 已添加暂停事务 #{task_id}")


def remove_paused_task(task_id: int) -> None:
    """
    从全局列表移除暂停事务

    Args:
        task_id: 事务ID
    """
    if task_id in paused_tasks:
        del paused_tasks[task_id]
        logger.info(f"▶️ 已移除暂停事务 #{task_id}")


def set_resume_signal(task_id: int) -> None:
    """
    设置恢复信号，让暂停的事务继续执行

    Args:
        task_id: 事务ID
    """
    if task_id in paused_tasks:
        paused_tasks[task_id]["signal"] = "resume"
        logger.info(f"▶️ 已设置恢复信号给事务 #{task_id}")


def get_pause_signal(task_id: int) -> str | None:
    """
    获取暂停信号

    Args:
        task_id: 事务ID

    Returns:
        信号值：'paused'|'resume'|None（事务不在暂停列表）
    """
    if task_id in paused_tasks:
        return paused_tasks[task_id]["signal"]
    return None


def get_paused_task_context(task_id: int) -> dict[str, Any] | None:
    """
    获取暂停事务的上下文

    Args:
        task_id: 事务ID

    Returns:
        暂停时的关键上下文，如果事务不存在则返回None
    """
    if task_id in paused_tasks:
        return paused_tasks[task_id].get("context")
    return None


async def broadcast_paused_tasks_update() -> None:
    """
    广播暂停事务更新到所有WebSocket连接
    """
    await manager.broadcast({"type": "paused_tasks_update", "tasks": list(paused_tasks.keys())})


async def _send_output_to_websocket(websocket: WebSocket, output_data: dict[str, Any]) -> None:
    """
    将输出发送到 WebSocket（作为 OutputReader 的回调）

    如果发送失败，立即重新抛出异常，让read_history中断
    """
    try:
        # 🔧 Task#1725：文件重写重置信号（行号格式迁移后通知前端重新拉取）
        if output_data.get("type") == "reset":
            await manager.send_to(
                websocket,
                {
                    "type": "output_reset",
                    "taskId": output_data.get("task_id"),
                },
            )
            return
        await manager.send_to(
            websocket,
            {
                "type": "output",
                "content": output_data.get("content", ""),
                "source": output_data.get("source", "output"),
                "taskId": output_data.get("task_id"),
                "messageId": output_data.get("message_id"),
                "timestamp": output_data.get("timestamp"),
                "fileLineNo": output_data.get("fileLineNo"),
            },
        )
    except Exception as e:
        logger.error(
            f"❌ 发送输出到 WebSocket 失败: type={type(e).__name__}, "
            f"msg={str(output_data)[:200]}, error={e}\n{traceback.format_exc()}"
        )
        # 关键：重新抛出异常，让read_history立即中断
        raise


def _detect_line_no_in_field(batch_lines: list[str], file_line_start: int) -> bool:
    """检测批次行是否为新格式（第3字段=行号）。

    🔧 Task#1725：新格式 {ts}\t{source}\t{lineNo}\t{content} 的行内行号 == 物理行号。
    🔧 Task#1725 v2：严格递增判定（对历史行号偏移免疫）。
    1694.out等经历过多次runner重启的文件，行内行号与物理行号存在恒定偏移
    （如差4），旧判定 parts[2]==str(物理行号) 会误判False→前端按旧格式解析
    →taskId=行号→Task#1435丢弃首批→缓存稀疏→翻页半黑/全黑。
    新判定：批次内第3字段严格递增1（首尾差==行数-1）→ 行号（新格式）；
    旧格式第3字段是固定task_id（恒定不变），两种模式互斥、判定稳健。

    🔧 Fix #1751：差值众数判定 —— 多进程并发写（webui服务与fly service
    各自持有内存计数器漂移）会在文件中产生交错的行号跳变行（实测task-2.out
    物理行137451行号却是95387）。跳变行打破"严格递增1"导致整批判定为旧格式
    → 前端按行号解析taskId → 缓存错key → 切换事务不渲染/翻页黑屏。
    新算法：diffs = 第3字段 - 物理行号(file_line_start+i)，取众数，
    众数占比≥75% 即判定新格式（容忍少数跳变行）。
    """
    if not batch_lines or file_line_start <= 0:
        return False
    diffs: list[int] = []
    for i, bl in enumerate(batch_lines[:20]):  # 采样前20行足够
        parts = bl.rstrip("\n").split("\t", 3)
        if len(parts) < 4:
            return False
        try:
            nums_i = int(parts[2])
        except ValueError:
            return False
        diffs.append(nums_i - (file_line_start + i))
    if len(diffs) < 2:
        return False
    if len(set(diffs)) == 1:
        # 恒定偏移或0（新格式行号==物理行号）
        return True
    # 差值众数（Counter.most_common），容忍少数跳变行
    from collections import Counter
    most_cnt = Counter(diffs).most_common(1)[0][1]
    return most_cnt / len(diffs) >= 0.75


async def _send_history_output_async(
    websocket: WebSocket,
    reader: Any,
    client_id: int,
    task_ids: list[int],
    scrollback: int,
    before_timestamp: str | None = None,
    since_time: str | None = None,
    subscription_epoch: int | None = None,
    page_start: int | None = None,
    page_end: int | None = None,
) -> None:
    """
    异步发送历史输出（后台任务）- 🚀 性能优化版

    核心优化：不再 readlines() 整个大文件！
    - totalLines: 用行偏移索引 O(1) 获取（🔧 Task#1725）
    - page_start/page_end: 行号范围 seek 直读，O(页大小)（🔧 Task#1725 翻页）
    - since_time: 用 find_timestamp_line_pos 定位后只读取增量
    - before_timestamp: 扫描定位后只取窗口行（旧翻页协议，兼容保留）
    - 首次订阅: 从文件末尾读 scrollback 行
    - 所有文件IO在 asyncio.to_thread 中执行
    """
    try:
        # 收集文件路径（带task_id，供行号索引使用）
        file_entries: list[tuple[int, Path]] = []
        if task_ids:
            for tid in sorted(task_ids):
                fp = reader.get_output_file_path(tid)
                if fp.exists():
                    file_entries.append((tid, fp))
        else:
            for fp in reader.output_dir.iterdir():
                if fp.name.startswith("task-") and fp.name.endswith(".out"):
                    try:
                        file_entries.append((int(fp.name[5:-4]), fp))
                    except ValueError:
                        pass
            file_entries.sort(key=lambda x: x[0])

        if not file_entries:
            logger.info("[WebSocket后台任务] 没有输出文件")
            return

        # 🚀 所有文件IO在线程池中执行
        def _do_work():
            # --- 模式0: page_start/page_end 行号翻页（🔧 Task#1725 O(1)）---
            if page_start and page_end:
                all_batch_lines = []
                grand_total = 0
                has_more = page_start > 1
                file_line_start = page_start
                count = 0
                for tid, fp in file_entries:
                    lines = reader.read_line_range_indexed(tid, fp, page_start, page_end)
                    total = reader.get_total_lines(tid, fp)
                    grand_total += total
                    all_batch_lines.extend(lines)
                    count += len(lines)
                file_line_end = file_line_start + max(0, count - 1) if count else 0
                # 行内行号检测：新格式第3字段=lineNo（==物理行号）
                line_no_in_field = _detect_line_no_in_field(all_batch_lines, file_line_start)
                logger.info(
                    f"[WebSocket后台任务] 行号翻页完成: [{page_start},{page_end}] → {count}行, hasMore={has_more}, lineNoInField={line_no_in_field}"
                )
                return ("page_range", all_batch_lines, grand_total, has_more, file_line_start, file_line_end, line_no_in_field)

            # --- 模式1: since_time 增量获取 ---
            # 🚀 复用 update_subscription 已算好的订阅位置，不再重复扫描文件
            if since_time:
                all_batch_lines = []
                grand_total = 0
                for tid, fp in file_entries:
                    byte_pos = 0
                    subs = reader._subscriptions.get(client_id, {})
                    byte_pos = subs.get(tid, 0)
                    total = reader.get_total_lines(tid, fp)  # 🔧 Task#1725: O(1)
                    grand_total += total
                    if byte_pos > 0 and byte_pos < fp.stat().st_size:
                        lines, _, _ = reader.read_lines_from_byte_pos(fp, byte_pos)
                        all_batch_lines.extend(lines)
                logger.info(f"[WebSocket后台任务] 增量读取完成: {len(all_batch_lines)} 行, 总行数={grand_total}, since_time={since_time}")
                return ("incremental", all_batch_lines, grand_total)

            # --- 模式2: before_timestamp 翻页（旧协议，兼容保留）---
            # 🚀 用 read_lines_before_pos 反向读取，不逐行扫描整个文件
            if before_timestamp:
                all_batch_lines = []
                grand_total = 0
                has_more = False
                # 🔧 Task#1725 修复多文件行号覆盖bug：按文件分段正确累加行号
                seg_line_start = 0
                file_line_start = 0
                file_line_end = 0
                for tid, fp in file_entries:
                    cutoff_pos, cutoff_line, total = reader.find_timestamp_line_pos(fp, before_timestamp, mode="gte")
                    grand_total += total
                    if cutoff_line > 1:
                        has_more = True
                    if cutoff_pos > 0:
                        lines, _ = reader.read_lines_before_pos(fp, cutoff_pos, scrollback)
                        if lines:
                            seg_line_start = max(1, cutoff_line - len(lines) + 1)
                            if not file_line_start:
                                file_line_start = seg_line_start
                            file_line_end = cutoff_line
                        all_batch_lines.extend(lines)
                logger.info(f"[WebSocket后台任务] 翻页读取完成: {len(all_batch_lines)} 行, hasMore={has_more}")
                return ("older_history", all_batch_lines, grand_total, has_more, file_line_start, file_line_end)

            # --- 模式3: 首次订阅 ---
            all_batch_lines = []
            grand_total = 0
            for tid, fp in file_entries:
                lines, total, start_line_no = reader.read_last_n_lines(fp, scrollback)
                grand_total += total
                all_batch_lines.extend(lines)
            has_more = grand_total > scrollback
            logger.info(f"[WebSocket后台任务] 首次订阅读取完成: {len(all_batch_lines)} 行, 总行数={grand_total}")
            return ("first_subscription", all_batch_lines, grand_total, has_more)

        result = await asyncio.to_thread(_do_work)
        mode = result[0]

        # 🔧 Task#1435：检查 epoch 是否过期（用户快速切换事务后，旧任务的数据不应发送）
        if subscription_epoch is not None and reader.is_subscription_stale(client_id, subscription_epoch):
            logger.info(
                f"[WebSocket后台任务] 订阅已过期(epoch={subscription_epoch})，丢弃本次历史发送: task_ids={task_ids}"
            )
            return

        # --- 公共：更新订阅位置到文件末尾 ---
        async def _update_positions():
            for tid, fp in file_entries:
                if fp.exists():
                    file_size = fp.stat().st_size
                    try:
                        await reader.update_read_position(client_id, tid, file_size)
                    except (ValueError, IndexError):
                        pass

        if mode == "page_range":
            _, batch_lines, grand_total, has_more, fl_start, fl_end, line_no_in_field = result
            await manager.send_to(websocket, {
                "type": "output_batch",
                "outputs": batch_lines,
                "count": len(batch_lines),
                "batchNum": 1,
                "totalBatches": 1,
                "hasMore": has_more,
                "isPageRange": True,
                "lineNoInField": line_no_in_field,
                "isOlderHistory": False,
                "isIncremental": False,
                "totalLines": grand_total,
                "fileLineStart": fl_start,
                "fileLineEnd": fl_end,
            })
            logger.info(f"[WebSocket后台任务] 发送行号翻页批次：{len(batch_lines)} 行 [{fl_start}-{fl_end}]/{grand_total}")
            return

        if mode == "incremental":
            _, batch_lines, grand_total = result
            if not batch_lines:
                logger.info(f"[WebSocket后台任务] since_time={since_time} 后无新输出")
                await _update_positions()
                return
            count = len(batch_lines)
            logger.info(f"[WebSocket后台任务] 增量输出：{count} 行（since_time={since_time}）")
            await manager.send_to(websocket, {
                "type": "output_batch",
                "outputs": batch_lines,
                "count": count,
                "batchNum": 1,
                "totalBatches": 1,
                "hasMore": False,
                "isOlderHistory": False,
                "isIncremental": True,
                "lineNoInField": _detect_line_no_in_field(batch_lines, max(1, grand_total - count + 1)),
                "totalLines": grand_total,
                "fileLineStart": max(1, grand_total - count + 1),
                "fileLineEnd": grand_total,
            })
            await _update_positions()
            return

        elif mode == "older_history":
            _, batch_lines, grand_total, has_more, fl_start, fl_end = result
            await manager.send_to(websocket, {
                "type": "output_batch",
                "outputs": batch_lines,
                "count": len(batch_lines),
                "batchNum": 1,
                "totalBatches": 1,
                "hasMore": has_more,
                "isOlderHistory": True,
                "lineNoInField": _detect_line_no_in_field(batch_lines, fl_start),
                "totalLines": grand_total,
                "fileLineStart": fl_start,
                "fileLineEnd": fl_end,
            })
            logger.info(f"[WebSocket后台任务] 发送老历史批次：{len(batch_lines)} 行，hasMore={has_more}, lines [{fl_start}-{fl_end}]/{grand_total}")
            return

        elif mode == "first_subscription":
            _, batch_lines, grand_total, has_more = result
            count = len(batch_lines)
            start_line = max(1, grand_total - count + 1)
            await manager.send_to(websocket, {
                "type": "output_batch",
                "outputs": batch_lines,
                "count": count,
                "batchNum": 1,
                "totalBatches": 1,
                "hasMore": has_more,
                "isOlderHistory": False,
                "lineNoInField": _detect_line_no_in_field(batch_lines, start_line),
                "totalLines": grand_total,
                "fileLineStart": start_line,
                "fileLineEnd": grand_total,
            })
            logger.info(f"[WebSocket后台任务] 发送首批历史：{count} 行，hasMore={has_more}, totalLines={grand_total}")

        # 更新订阅位置到文件末尾
        await _update_positions()

    except Exception as e:
        logger.error(f"[WebSocket后台任务] 异步发送历史输出失败: {e}\n{traceback.format_exc()}")


async def websocket_endpoint(websocket: WebSocket) -> None:
    """
    WebSocket 端点

    处理进程输出的实时推送
    """
    from peng.ui.output_reader import get_output_reader

    await manager.connect(websocket)
    reader = get_output_reader()

    # 注册客户端并获取 client_id
    client_id = await reader.register_client(
        lambda data: _send_output_to_websocket(websocket, data)
    )

    try:
        # 发送欢迎消息（带服务启动时间，前端用于检测服务重启后自动刷新）
        await manager.send_to(
            websocket,
            {
                "type": "connected",
                "message": f"已连接到{AGENT_NAME} 输出流",
                "server_start_time": _SERVER_START_TIME,
            },
        )

        while True:
            # 关键修复：检查WebSocket是否还在活跃连接中
            # 如果不在，说明已断开（被send_to移除），立即退出
            if websocket not in manager.active_connections:
                logger.debug("WebSocket已不在活跃连接列表中，退出主循环")
                break

            # 等待客户端消息
            data = await websocket.receive_text()

            # 处理心跳
            if data == "ping":
                await manager.send_to(websocket, {"type": "pong"})
                continue

            # 处理 JSON 命令
            try:
                cmd = json.loads(data)
                if cmd.get("type") == "subscribe":
                    message_id = cmd.get("messageId")
                    task_ids = cmd.get("taskIds", [])
                    skip_history = cmd.get("skipHistory", False)
                    before_timestamp = cmd.get("beforeTimestamp", None)  # 用于"上一页"加载更多老历史
                    since_time = cmd.get("sinceTime", None)  # 🔧 Task#219：增量获取切换期间的新输出
                    # 🔧 Task#1725：O(1) 行号翻页 —— 按行号范围直接seek读取
                    page_start = cmd.get("pageStart", None)
                    page_end = cmd.get("pageEnd", None)
                    logger.info(
                        f"处理订阅请求: messageId={message_id}, taskIds={task_ids}, skipHistory={skip_history}, beforeTimestamp={before_timestamp}, sinceTime={since_time}, pageStart={page_start}, pageEnd={page_end}"
                    )

                    if message_id:
                        # 更新此客户端的订阅
                        # 🔧 Task#219：传递 since_time 让后端从该时间点开始读取
                        # 🔧 Task#1435：返回值改为 (is_new_subscription, epoch)
                        is_new_subscription, subscription_epoch = await reader.update_subscription(
                            client_id, task_ids, skip_history, since_time=since_time
                        )

                        logger.info(f"is_new_subscription={is_new_subscription}")

                        # 加载配置获取 scrollback 大小
                        from peng.config.loader import load_config
                        from peng.webui.routes._common import DEFAULT_OUTPUT_HISTORY_LIMIT  # noqa: E402

                        try:
                            config = load_config()
                            scrollback = config.webui.get("xterm", {}).get(
                                "scrollback", DEFAULT_OUTPUT_HISTORY_LIMIT
                            )
                        except Exception:
                            scrollback = DEFAULT_OUTPUT_HISTORY_LIMIT

                        # 🔧 Task#1725：O(1) 行号翻页（优先级最高）—— 按行号范围seek直读
                        if page_start and page_end:
                            logger.info(f"[WebSocket] 行号翻页: [{page_start}, {page_end}]")
                            asyncio.create_task(
                                _send_history_output_async(
                                    websocket,
                                    reader,
                                    client_id,
                                    task_ids,
                                    scrollback,
                                    page_start=int(page_start),
                                    page_end=int(page_end),
                                    subscription_epoch=subscription_epoch,
                                )
                            )
                        # 🔧 如果提供了 beforeTimestamp，说明是"上一页"请求更多老历史
                        elif before_timestamp:
                            logger.info(f"[WebSocket] 请求更老历史: beforeTimestamp={before_timestamp}")
                            # 发送头部消息
                            await manager.send_to(
                                websocket,
                                {
                                    "type": "output_header",
                                    "messageId": message_id,
                                    "taskIds": task_ids,
                                    "scrollback": scrollback,
                                },
                            )
                            # 异步发送更老的历史
                            asyncio.create_task(
                                _send_history_output_async(
                                    websocket,
                                    reader,
                                    client_id,
                                    task_ids,
                                    scrollback,
                                    before_timestamp=before_timestamp,
                                    subscription_epoch=subscription_epoch,
                                )
                            )
                        elif since_time and is_new_subscription:
                            # 🔧 Task#219：增量获取切换期间的新输出
                            # 前端切换回已查看过的事务时，用 sinceTime 获取上次离开后的新输出
                            logger.info(f"[WebSocket] 增量获取: sinceTime={since_time}")
                            asyncio.create_task(
                                _send_history_output_async(
                                    websocket,
                                    reader,
                                    client_id,
                                    task_ids,
                                    scrollback,
                                    since_time=since_time,
                                    subscription_epoch=subscription_epoch,
                                )
                            )
                        elif is_new_subscription and not skip_history:
                            # 发送历史输出头部功能链接
                            # 发送头部消息（包含功能链接信息）
                            await manager.send_to(
                                websocket,
                                {
                                    "type": "output_header",
                                    "messageId": message_id,
                                    "taskIds": task_ids,
                                    "scrollback": scrollback,
                                },
                            )

                            # 🔧 关键修复：创建后台任务异步发送历史输出，不阻塞WebSocket主循环
                            # 这解决了快速点击事务时卡死的问题
                            asyncio.create_task(
                                _send_history_output_async(
                                    websocket,
                                    reader,
                                    client_id,
                                    task_ids,
                                    scrollback,
                                    subscription_epoch=subscription_epoch,
                                )
                            )
                            logger.info(f"[WebSocket] 已创建后台任务异步发送历史输出")

                        # 发送订阅确认
                        await manager.send_to(
                            websocket,
                            {
                                "type": "subscribed",
                                "messageId": message_id,
                                "taskIds": task_ids,
                            },
                        )
                    else:
                        # 清除此客户端的订阅
                        await reader.update_subscription(client_id, None, skip_history=False)

            except json.JSONDecodeError:
                pass  # 忽略非 JSON 消息

    except WebSocketDisconnect:
        await manager.disconnect(websocket)
    except RuntimeError as e:
        # 处理"WebSocket is not connected"错误
        if "not connected" in str(e):
            logger.debug(f"WebSocket连接已断开: {e}")
            await manager.disconnect(websocket)
        else:
            raise
    except Exception as e:
        logger.error(f"❌ WebSocket 异常: {e}\n{traceback.format_exc()}")
        await manager.disconnect(websocket)
    finally:
        # 注销客户端（会自动清除其订阅）
        await reader.unregister_client(client_id)


async def broadcast_output(
    content: str,
    source: str = "output",
    task_id: int | None = None,
    message_id: int | None = None,
) -> None:
    """
    广播进程输出（已废弃，现在由 output_reader 通过订阅机制推送）

    保留此函数以兼容旧代码调用

    Args:
        content: 输出内容
        source: 输出来源（terminal/task/process）
        task_id: 关联的事务 ID（可选）
        message_id: 关联的消息 ID（可选）
    """
    # 现在由 output_reader 的监控循环通过订阅机制推送
    # 此函数保留为空实现，供旧代码调用
    pass


async def broadcast_status(status: str, details: dict[str, Any] | None = None) -> None:
    """
    广播状态更新

    Args:
        status: 状态（started/stopped/paused/error）
        details: 详细信息
    """
    await manager.broadcast(
        {
            "type": "status",
            "status": status,
            "details": details or {},
        }
    )


async def broadcast_task_update(
    task_id: int, action: str, details: dict[str, Any] | None = None, source: str = "unknown"
) -> None:
    """
    广播事务更新

    Args:
        task_id: 事务 ID
        action: 操作（created/updated/completed/paused/cancelled）
        details: 详细信息
        source: 事务来源（webui/cli/api/unknown）
    """
    await manager.broadcast(
        {
            "type": "task_update",
            "task_id": task_id,
            "action": action,
            "source": source,
            "details": details or {},
        }
    )


async def broadcast_message_update(
    msg_id: int, action: str, details: dict[str, Any] | None = None, source: str = "unknown"
) -> None:
    """
    广播消息更新

    Args:
        msg_id: 消息 ID
        action: 操作（created/updated/completed）
        details: 详细信息
        source: 消息来源（webui/cli/api/unknown）
    """
    logger.info(
        f"广播消息更新: msg_id={msg_id}, action={action}, source={source}, active_connections={len(manager.active_connections)}"
    )
    await manager.broadcast(
        {
            "type": "message_update",
            "msg_id": msg_id,
            "action": action,
            "source": source,
            "details": details or {},
        }
    )


async def broadcast_toast(
    toast_type: str,
    title: str,
    message: str,
    duration: int = 3000,
) -> None:
    """
    广播toast通知到所有WebSocket连接

    Args:
        toast_type: toast类型（success/error/info/warning）
        title: 标题
        message: 消息内容
        duration: 显示时长（毫秒）
    """
    logger.info(f"广播toast通知: {title} - {message}")
    await manager.broadcast(
        {
            "type": "toast",
            "toast_type": toast_type,
            "title": title,
            "message": message,
            "duration": duration,
        }
    )


async def terminal_websocket_endpoint(
    websocket: WebSocket,
    session_id: str | None = Query(None, description="终端会话ID，如果提供则附加到现有会话"),
    cols: int | None = Query(None, description="初始列数（创建新会话时使用）"),
    rows: int | None = Query(None, description="初始行数（创建新会话时使用）"),
    nest: str | None = Query(None, description="远程nest名称（指定后在远程nest上创建终端）"),
) -> None:
    """
    终端 WebSocket 端点

    实现远程终端功能，支持终端交互和会话恢复。
    支持通过nest参数在远程计算机上创建终端。

    Args:
        websocket: WebSocket连接
        session_id: 可选的会话ID，如果提供则附加到现有会话，否则创建新会话
        cols: 初始列数（创建新会话时使用，默认80）
        rows: 初始行数（创建新会话时使用，默认24）
        nest: 远程nest名称（可选，指定后在远程nest上创建终端）
    """
    # 🔧 如果指定了远程nest，走远程终端路径
    if nest and nest != "local":
        await _remote_terminal_endpoint(websocket, session_id, cols, rows, nest)
        return

    # Windows 平台：检查 pywinpty 是否可用
    if IS_WINDOWS and not _WIN_PTY_AVAILABLE:
        await websocket.accept()
        await websocket.send_text(
            json.dumps({
                "type": "error",
                "message": "Windows 终端功能需要 pywinpty 库，请执行: pip install pywinpty",
            })
        )
        await websocket.close()
        return

    await websocket.accept()

    # 读取配置
    try:
        config = load_config()
        if IS_WINDOWS:
            # Windows 默认使用 PowerShell
            default_shell = config.webui.get("terminal", {}).get("shell", "powershell.exe")
        else:
            default_shell = config.webui.get("terminal", {}).get("shell", "/bin/zsh")
        session_timeout = config.webui.get("terminal", {}).get("session_timeout", 3600)
        idle_timeout = config.webui.get("terminal", {}).get("idle_timeout", 3600)
    except Exception:
        default_shell = "powershell.exe" if IS_WINDOWS else "/bin/zsh"
        session_timeout = 3600
        idle_timeout = 3600

    # 🔧 修复：需求#414 - 设置默认窗口大小
    terminal_cols = cols if cols is not None else 80
    terminal_rows = rows if rows is not None else 24
    logger.info(f"终端窗口大小: {terminal_cols}x{terminal_rows}")

    # 检查是否附加到现有会话
    session = None
    if session_id:
        session = terminal_session_manager.get_session(session_id)
        if session:
            logger.info(
                f"✅ 附加到现有终端会话: session_id={session_id}, pid={session.child.pid}, 连接数={len(session.websockets)}"
            )
            session.add_websocket(websocket)
            logger.info(
                f"✅ WebSocket已添加到会话: session_id={session_id}, 连接数={len(session.websockets)}"
            )
        else:
            # 🔧 修复：需求#412 - 会话不存在时，关闭WebSocket并返回错误
            # 不应该创建新会话，应该让前端关闭对应的终端标签页
            logger.info(f"⚠️ 会话不存在: session_id={session_id}，关闭WebSocket")
            try:
                await websocket.send_text(
                    json.dumps({"type": "error", "message": "会话不存在", "session_id": session_id})
                )
            except Exception as e:
                logger.warning(f"发送错误消息失败: {e}")
            await websocket.close()
            return

    # 情况1：附加到现有会话
    if session is not None:
        try:
            # 🔧 修复：需求#415 - 更新窗口大小
            logger.info(
                f"📡 更新终端窗口大小: session_id={session_id}, 大小={terminal_rows}x{terminal_cols}"
            )
            session.pty_proc.setwinsize(terminal_rows, terminal_cols)

            # 🔧 修复：需求#7 - 发送序列化屏幕内容到新连接的WebSocket
            # 使用JSON协议发送，前端使用deserialize()恢复终端状态
            screen, cursor_x, cursor_y = session.get_current_screen()
            if screen:
                logger.info(f"📡 发送序列化屏幕内容: session_id={session_id}, 长度={len(screen)}")
                try:
                    await websocket.send_text(
                        json.dumps(
                            {
                                "type": "screen_restore",
                                "screen": screen,
                                "cursor_x": cursor_x,
                                "cursor_y": cursor_y,
                            }
                        )
                    )
                    logger.info(f"📡 序列化屏幕内容发送成功")
                except Exception as e:
                    logger.error(f"📡 发送序列化屏幕内容失败: {e}", exc_info=True)
            else:
                # 没有序列化屏幕，发送空恢复消息（让前端知道需要清空终端）
                logger.info(f"📡 无序列化屏幕内容，发送空恢复: session_id={session_id}")
                try:
                    await websocket.send_text(
                        json.dumps(
                            {"type": "screen_restore", "screen": "", "cursor_x": 0, "cursor_y": 0}
                        )
                    )
                except Exception as e:
                    logger.error(f"📡 发送空恢复消息失败: {e}", exc_info=True)
            logger.info(f"📡 开始接收用户输入并发送到终端: session_id={session_id}")
            # 接收用户输入并发送到终端
            while True:
                data = await websocket.receive_text()

                # 处理JSON命令（如resize、screen_sync）
                try:
                    cmd = json.loads(data)
                    if isinstance(cmd, dict) and cmd.get("type") == "resize":
                        # 调整终端大小
                        cols = cmd.get("cols", 80)
                        rows = cmd.get("rows", 24)
                        logger.info(f"调整终端大小: {cols}x{rows}")
                        session.pty_proc.setwinsize(rows, cols)
                    elif isinstance(cmd, dict) and cmd.get("type") == "screen_sync":
                        # 🔧 修复：需求#7 - 接收前端同步的序列化屏幕快照
                        screen_data = cmd.get("screen", "")
                        if screen_data:
                            session.update_serialized_screen(screen_data)
                    elif isinstance(cmd, dict) and cmd.get("type") == "close":
                        # 前端主动关闭终端
                        logger.info(f"前端主动关闭终端: session_id={session_id}")
                        # 🔧 修复：需求#402 - 调用 cleanup_session 清理pty进程
                        terminal_session_manager.cleanup_session(session_id)
                        # 🔧 Task#185：广播terminal_closed事件到所有webui实例
                        await manager.broadcast({
                            "type": "terminal_event",
                            "event": "terminal_closed",
                            "session_id": session_id,
                        })
                        break
                    else:
                        # 普通输入，写入终端
                        if isinstance(cmd, (int, float, bool, type(None))):
                            user_input = str(cmd)
                        elif isinstance(cmd, str):
                            user_input = cmd
                        else:
                            logger.warning(
                                f"收到意外的JSON数据类型: {type(cmd)}, data={repr(data)}"
                            )
                            continue
                        # 写入pty
                        try:
                            session.pty_proc.write(user_input)
                        except Exception as e:
                            logger.error(f"写入 pty 失败: {e}")
                except json.JSONDecodeError:
                    # 不是JSON命令，直接写入pty（用户输入）
                    try:
                        session.pty_proc.write(data)
                    except Exception as e:
                        logger.error(f"写入 pty 失败: {e}")

        except WebSocketDisconnect:
            logger.info(f"终端 WebSocket 连接已断开: session_id={session_id}")
        except Exception as e:
            logger.error(f"终端 WebSocket 异常: {e}\n{traceback.format_exc()}")
        finally:
            # 🔧 修复：WebSocket断开时，只从会话中移除WebSocket连接
            # 不立即关闭终端（让定期清理任务来清理空闲会话）
            session.remove_websocket(websocket)
            logger.info(
                f"✅ WebSocket已从会话中移除: session_id={session_id}, 剩余连接数={len(session.websockets)}"
            )

    # 情况2：创建新会话
    else:
        try:
            # 生成新的session_id
            session_id = str(uuid.uuid4())

            # 🔧 修复：需求#414 - 使用前端传递的窗口大小
            logger.info(
                f"🚀 创建终端进程: session_id={session_id}, 大小={terminal_rows}x{terminal_cols}, "
                f"platform={'Windows' if IS_WINDOWS else 'POSIX'}"
            )

            # 根据平台选择 PTY 实现
            if IS_WINDOWS:
                pty_proc = WindowsPtyProcess.spawn(
                    default_shell,
                    encoding="utf-8",
                    dimensions=(terminal_rows, terminal_cols),
                    env={
                        "TERM": "xterm-256color",
                    },
                )
            else:
                pty_proc = PtyProcess.spawn(
                    default_shell,
                    encoding="utf-8",
                    dimensions=(terminal_rows, terminal_cols),  # 🔧 修复：使用前端传递的窗口大小
                    env={
                        "TERM": "xterm-256color",
                        "LANG": "zh_CN.UTF-8",  # 🔧 修复：需求#100 - 设置UTF-8 locale，解决btop报错
                        "LC_ALL": "zh_CN.UTF-8",  # 🔧 修复：需求#100 - 强制使用UTF-8 locale
                    },
                )
            logger.info(
                f"✅ pty进程已创建，PID: {pty_proc.pid}, session_id={session_id}, 大小={terminal_rows}x{terminal_cols}"
            )

            read_task_started = asyncio.Event()

            async def read_pty_output():
                """读取pty输出并发送到WebSocket（跨平台）"""
                loop = asyncio.get_event_loop()
                read_task_started.set()

                if IS_WINDOWS:
                    # Windows: 使用 run_in_executor 轮询读取
                    # ConPTY 不支持 asyncio 原生 add_reader
                    import concurrent.futures
                    executor = concurrent.futures.ThreadPoolExecutor(max_workers=1)

                    async def _windows_read_loop():
                        """Windows PTY 异步读取循环"""
                        while True:
                            try:
                                # 使用线程池执行阻塞读取
                                data = await loop.run_in_executor(
                                    executor, pty_proc.read, 65536
                                )
                                if data:
                                    if session:
                                        session._add_to_buffer(data)
                                        await session.broadcast(data)
                                else:
                                    # 没有数据，短暂休眠避免空转
                                    await asyncio.sleep(0.02)
                                    # 检查进程是否仍然存活
                                    if not pty_proc.isalive():
                                        logger.info(f"Windows PTY 进程已退出: session_id={session_id}")
                                        if session:
                                            await session.broadcast(
                                                {"type": "process_exited", "session_id": session_id}
                                            )
                                        break
                            except Exception as e:
                                logger.error(f"Windows PTY 读取失败: {e}")
                                if session:
                                    await session.broadcast(
                                        {"type": "process_exited", "session_id": session_id}
                                    )
                                break

                    await _windows_read_loop()
                    executor.shutdown(wait=False)
                else:
                    # POSIX: 使用 loop.add_reader 原生异步IO
                    def on_pty_readable():
                        """pty主设备可读时的回调"""
                        try:
                            data = os.read(pty_proc.master_fd, 32768)
                            if data:
                                text = data.decode("utf-8", errors="replace")
                                # 🔧 优化：使用_add_to_buffer方法（方案4）
                                if session:
                                    session._add_to_buffer(text)
                                    # 广播到所有WebSocket连接（调度到事件循环）
                                    asyncio.ensure_future(session.broadcast(text))
                            else:
                                # EOF，进程已退出
                                logger.info(f"pty输出EOF，进程已退出: session_id={session_id}")
                                try:
                                    loop.remove_reader(pty_proc.master_fd)
                                except (OSError, ValueError):
                                    pass
                                if session:
                                    asyncio.ensure_future(
                                        session.broadcast(
                                            {"type": "process_exited", "session_id": session_id}
                                        )
                                    )
                        except OSError as e:
                            logger.error(f"读取 pty 输出失败: {e}")
                            try:
                                loop.remove_reader(pty_proc.master_fd)
                            except (OSError, ValueError):
                                pass
                            if session:
                                asyncio.ensure_future(
                                    session.broadcast(
                                        {"type": "process_exited", "session_id": session_id}
                                    )
                                )

                    # 注册原生异步读取器
                    loop.add_reader(pty_proc.master_fd, on_pty_readable)
                    logger.info(
                        f"✅ pty异步读取器已注册: session_id={session_id}, master_fd={pty_proc.master_fd}"
                    )

            # 启动读取事务
            read_task = asyncio.create_task(read_pty_output())
            # 等待读取器注册完成
            await read_task_started.wait()

            # 创建会话
            session = terminal_session_manager.create_session(
                session_id=session_id, pty_proc=pty_proc, read_task=read_task, nest="local"
            )
            session.add_websocket(websocket)

            # 🔧 修复：发送session_id给前端，让前端保存
            try:
                await websocket.send_text(
                    json.dumps({"type": "session_created", "session_id": session_id})
                )
            except Exception as e:
                logger.warning(f"发送session_id失败（WebSocket可能已断开）: {e}")
                # WebSocket已断开，清理会话后直接返回
                terminal_session_manager.cleanup_session(session_id)
                return

            # 🔧 Task#185：广播terminal_created事件到所有webui实例
            await manager.broadcast({
                "type": "terminal_event",
                "event": "terminal_created",
                "session_id": session_id,
                "nest": "local",
                "title": "local-1",
            })

            try:
                # 接收客户端消息
                while True:
                    data = await websocket.receive_text()

                    # 处理JSON命令（如resize、screen_sync）
                    try:
                        cmd = json.loads(data)
                        if isinstance(cmd, dict) and cmd.get("type") == "resize":
                            cols = cmd.get("cols", 80)
                            rows = cmd.get("rows", 24)
                            logger.info(f"调整终端大小: {cols}x{rows}")
                            pty_proc.setwinsize(rows, cols)
                        elif isinstance(cmd, dict) and cmd.get("type") == "screen_sync":
                            # 🔧 修复：需求#7 - 接收前端同步的序列化屏幕快照
                            screen_data = cmd.get("screen", "")
                            if screen_data:
                                session.update_serialized_screen(screen_data)
                        elif isinstance(cmd, dict) and cmd.get("type") == "close":
                            logger.info(f"前端主动关闭终端: session_id={session_id}")
                            terminal_session_manager.cleanup_session(session_id)
                            # 🔧 Task#185：广播terminal_closed事件到所有webui实例
                            await manager.broadcast({
                                "type": "terminal_event",
                                "event": "terminal_closed",
                                "session_id": session_id,
                            })
                            break
                        else:
                            # 普通输入，写入pty
                            if isinstance(cmd, (int, float, bool, type(None))):
                                user_input = str(cmd)
                            elif isinstance(cmd, str):
                                user_input = cmd
                            else:
                                logger.warning(
                                    f"收到意外的JSON数据类型: {type(cmd)}, data={repr(data)}"
                                )
                                continue
                            try:
                                pty_proc.write(user_input)
                            except Exception as e:
                                logger.error(f"写入 pty 失败: {e}")
                    except json.JSONDecodeError:
                        try:
                            pty_proc.write(data)
                        except Exception as e:
                            logger.error(f"写入 pty 失败: {e}")

            except WebSocketDisconnect:
                logger.info(f"终端 WebSocket 连接已断开: session_id={session_id}")
            except Exception as e:
                logger.error(f"终端 WebSocket 异常: {e}\n{traceback.format_exc()}")
            finally:
                # 🔧 修复：WebSocket断开时，只从会话中移除WebSocket连接
                # 不立即清理终端（让定期清理任务来清理空闲会话）
                session.remove_websocket(websocket)
                logger.info(
                    f"✅ WebSocket已从会话中移除: session_id={session_id}, 剩余连接数={len(session.websockets)}"
                )

        except Exception as e:
            logger.error(f"创建 pty 终端失败: {e}\n{traceback.format_exc()}")
            try:
                await websocket.close()
            except Exception:
                pass


# ==================== 远程Nest终端（支持多浏览器实例广播） ====================


class RemoteTerminalSession:
    """
    远程Nest终端会话，支持多浏览器实例同时连接

    当远程PTY有输出时，广播到所有连接的WebSocket实例。
    输出队列和转发任务由会话统一管理，确保多个WebSocket共享同一输出源。
    参照本地终端 TerminalSession 的多WebSocket模式设计。
    """

    def __init__(self, session_id: str, remote_session_id: str, nest_name: str):
        self.session_id = session_id
        self.remote_session_id = remote_session_id
        self.nest_name = nest_name
        self.websockets: list[WebSocket] = []
        self.serialized_screen: str = ""
        # 每个WebSocket的发送锁，防止并发发送冲突
        self._ws_locks: dict[int, asyncio.Lock] = {}
        # 输出队列：远程PTY输出 -> 队列 -> 广播到所有WebSocket
        self.output_queue: asyncio.Queue = asyncio.Queue()
        # 输出转发任务（整个会话共享一个）
        self._forward_task: asyncio.Task | None = None
        # NestBridge连接引用（用于注销回调）
        self._nest_conn: Any = None

    def add_websocket(self, websocket: WebSocket):
        """添加WebSocket连接到会话"""
        if websocket not in self.websockets:
            self.websockets.append(websocket)
            self._ws_locks[id(websocket)] = asyncio.Lock()

    def remove_websocket(self, websocket: WebSocket):
        """从会话中移除WebSocket连接"""
        if websocket in self.websockets:
            self.websockets.remove(websocket)
        ws_id = id(websocket)
        if ws_id in self._ws_locks:
            del self._ws_locks[ws_id]

    @property
    def ws_count(self) -> int:
        """当前WebSocket连接数"""
        return len(self.websockets)

    async def broadcast(self, message: str | dict[str, Any]):
        """广播消息到所有WebSocket连接（并行发送）"""
        if isinstance(message, dict):
            message = json.dumps(message, ensure_ascii=False)

        if not self.websockets:
            return

        # 遍历副本，避免遍历过程中修改列表
        disconnected = []
        for ws in list(self.websockets):
            lock = self._ws_locks.get(id(ws))
            if lock is None:
                continue
            try:
                async with lock:
                    await ws.send_text(message)
            except RuntimeError as e:
                if "not connected" in str(e) or "close message has been sent" in str(e):
                    logger.debug(f"远程终端WebSocket已关闭，移除此连接: {e}")
                    disconnected.append(ws)
                else:
                    raise
            except WebSocketDisconnect:
                logger.debug("远程终端WebSocket连接已断开，移除此连接")
                disconnected.append(ws)
            except Exception as e:
                logger.warning(
                    f"远程终端广播发送失败: type={type(e).__name__}, str={str(e)}, "
                    f"msg={message[:100] if message else '(empty)'}"
                )
                disconnected.append(ws)

        for ws in disconnected:
            self.remove_websocket(ws)

    def update_serialized_screen(self, serialized: str):
        """更新序列化屏幕快照"""
        self.serialized_screen = serialized

    async def _on_terminal_output(self, sid: str, data: str | None):
        """远程PTY输出回调（放入队列）"""
        await self.output_queue.put((sid, data))

    def start_forward_task(self, nest_conn: Any):
        """启动输出转发任务（仅在首次创建会话时调用）"""
        self._nest_conn = nest_conn

        async def _forward_output():
            """将远程终端输出广播到所有连接的前端WebSocket"""
            try:
                while True:
                    sid, data = await self.output_queue.get()
                    if data is None:
                        # 进程退出 - 广播到所有连接
                        logger.info(f"远程PTY进程退出，广播退出消息: session_id={self.session_id}")
                        await self.broadcast(json.dumps({
                            "type": "process_exited",
                            "session_id": self.session_id,
                        }))
                        break
                    else:
                        # 广播输出文本到所有WebSocket（与本地终端格式一致）
                        await self.broadcast(data)
            except asyncio.CancelledError:
                pass
            except Exception as e:
                logger.debug(f"远程终端输出转发异常: {e}")

        self._forward_task = asyncio.create_task(_forward_output())

    def stop_forward_task(self):
        """停止输出转发任务"""
        if self._forward_task and not self._forward_task.done():
            self._forward_task.cancel()
        if self._nest_conn:
            try:
                self._nest_conn.unregister_terminal_callback(self.remote_session_id)
            except Exception:
                pass


class RemoteTerminalSessionManager:
    """远程终端会话管理器，管理所有活跃的远程终端会话"""

    def __init__(self):
        self.sessions: dict[str, RemoteTerminalSession] = {}

    def create_session(self, session_id: str, remote_session_id: str, nest_name: str) -> RemoteTerminalSession:
        """创建新的远程终端会话"""
        session = RemoteTerminalSession(session_id, remote_session_id, nest_name)
        self.sessions[session_id] = session
        logger.info(
            f"✅ 远程终端会话已创建: session_id={session_id}, "
            f"remote_id={remote_session_id}, nest={nest_name}"
        )
        return session

    def get_session(self, session_id: str) -> RemoteTerminalSession | None:
        """获取指定的远程终端会话"""
        return self.sessions.get(session_id)

    def remove_session(self, session_id: str) -> RemoteTerminalSession | None:
        """移除会话记录并停止转发任务"""
        session = self.sessions.pop(session_id, None)
        if session:
            session.stop_forward_task()
        return session


# 全局远程终端会话管理器
remote_terminal_session_manager = RemoteTerminalSessionManager()

# 🔧 兼容旧代码：保持 _remote_terminal_sessions 字典引用
# 新代码应使用 remote_terminal_session_manager
_remote_terminal_sessions: dict[str, dict] = {}


async def _remote_terminal_endpoint(
    websocket: WebSocket,
    session_id: str | None,
    cols: int | None,
    rows: int | None,
    nest_name: str,
) -> None:
    """
    远程Nest终端WebSocket端点（支持多浏览器实例）

    通过NestBridge将终端输入/输出转发到远程nest上的PTY。
    支持多个浏览器实例同时连接同一远程终端，输出广播到所有连接。
    输出转发由 RemoteTerminalSession 统一管理，多个WebSocket共享同一输出源。

    Args:
        websocket: WebSocket连接
        session_id: 可选的会话ID（提供则附加到已有会话）
        cols: 初始列数
        rows: 初始行数
        nest_name: 远程nest名称
    """
    from peng.nest.bridge import NestBridgeManager
    from peng.nest.protocol import NestRequest, NestError

    await websocket.accept()

    # 获取nest连接
    nest_manager = NestBridgeManager.get_instance()
    conn = nest_manager.get_connection(nest_name)
    if not conn:
        await websocket.send_text(json.dumps({
            "type": "error",
            "message": f"Nest '{nest_name}' 未连接",
        }))
        await websocket.close()
        return

    terminal_cols = cols or 80
    terminal_rows = rows or 24

    remote_session_id = None
    session: RemoteTerminalSession | None = None

    try:
        # 检查是否附加到已有会话（多浏览器实例或页面刷新恢复）
        existing_session = remote_terminal_session_manager.get_session(session_id or "")
        if existing_session and existing_session.nest_name == nest_name:
            # 附加到已有会话（多浏览器实例共享同一远程终端）
            session = existing_session
            remote_session_id = session.remote_session_id
            session.add_websocket(websocket)
            logger.info(
                f"✅ 附加到已有远程终端会话: session_id={session_id}, "
                f"remote_id={remote_session_id}, nest={nest_name}, "
                f"当前连接数={session.ws_count}"
            )

            # 发送恢复消息
            await websocket.send_text(json.dumps({
                "type": "session_created",
                "session_id": session_id,
            }))

            # 发送序列化屏幕内容（与本地终端行为一致）
            serialized = session.serialized_screen
            if serialized:
                logger.info(
                    f"📡 发送远程终端序列化屏幕内容: session_id={session_id}, 长度={len(serialized)}"
                )
                await websocket.send_text(json.dumps({
                    "type": "screen_restore",
                    "screen": serialized,
                    "cursor_x": 0,
                    "cursor_y": 0,
                }))
            else:
                logger.info(f"📡 远程终端无序列化屏幕内容: session_id={session_id}")
        elif session_id:
            # 🔧 Task#185：前端提供了session_id但session不存在（peng重启后）
            # 不创建新终端，返回错误让前端关闭标签页
            logger.info(f"⚠️ 远程终端会话不存在（可能peng已重启）: session_id={session_id}")
            await websocket.send_text(json.dumps({
                "type": "error",
                "message": "会话不存在",
                "session_id": session_id,
            }))
            await websocket.close()
            return
        else:
            # 创建新的远程终端会话（前端没有提供session_id，是新建终端请求）
            # 读取配置获取shell
            try:
                config = load_config()
                terminal_shell = config.webui.get("terminal", {}).get("shell")
                default_shell = terminal_shell if terminal_shell else None
            except Exception:
                default_shell = None

            # 发送 terminal_create 请求到远程nest
            create_request = NestRequest(
                type="terminal_create",
                params={
                    "shell": default_shell,
                    "cols": terminal_cols,
                    "rows": terminal_rows,
                    "env": {
                        "TERM": "xterm-256color",
                        "LANG": "zh_CN.UTF-8",
                        "LC_ALL": "zh_CN.UTF-8",
                    },
                    "_skip_security": True,
                },
            )

            response = await conn.send_request(create_request, timeout=30.0)

            if response.error:
                await websocket.send_text(json.dumps({
                    "type": "error",
                    "message": f"创建远程终端失败: {response.error}",
                }))
                await websocket.close()
                return

            remote_session_id = response.data.get("session_id")
            if not remote_session_id:
                await websocket.send_text(json.dumps({
                    "type": "error",
                    "message": "远程终端创建返回数据异常",
                }))
                await websocket.close()
                return

            # 生成前端session_id并创建会话
            front_session_id = str(uuid.uuid4())
            session = remote_terminal_session_manager.create_session(
                session_id=front_session_id,
                remote_session_id=remote_session_id,
                nest_name=nest_name,
            )
            session.add_websocket(websocket)

            # 🔧 兼容旧代码
            _remote_terminal_sessions[front_session_id] = {
                "nest": nest_name,
                "remote_session_id": remote_session_id,
                "serialized_screen": "",
            }

            # 注册输出回调（会话级别，所有WebSocket共享）
            conn.register_terminal_callback(remote_session_id, session._on_terminal_output)

            # 启动输出转发任务（整个会话共享一个，广播到所有WebSocket）
            session.start_forward_task(conn)

            # 发送session_created给前端
            await websocket.send_text(json.dumps({
                "type": "session_created",
                "session_id": front_session_id,
            }))

            session_id = front_session_id
            logger.info(
                f"远程终端会话已创建: front_id={session_id}, remote_id={remote_session_id}, nest={nest_name}"
            )

        # 主消息循环：接收前端输入，转发到远程nest
        try:
            while True:
                data = await websocket.receive_text()
                try:
                    cmd = json.loads(data)
                    if isinstance(cmd, dict) and cmd.get("type") == "resize":
                        c = cmd.get("cols", 80)
                        r = cmd.get("rows", 24)
                        resize_req = NestRequest(
                            type="terminal_resize",
                            params={
                                "session_id": remote_session_id,
                                "cols": c,
                                "rows": r,
                                "_skip_security": True,
                            },
                        )
                        await conn.send_request(resize_req, timeout=5.0)
                    elif isinstance(cmd, dict) and cmd.get("type") == "close":
                        close_req = NestRequest(
                            type="terminal_close",
                            params={
                                "session_id": remote_session_id,
                                "_skip_security": True,
                            },
                        )
                        await conn.send_request(close_req, timeout=5.0)
                        # 清理会话（停止转发任务，注销回调，移除所有WebSocket）
                        remote_terminal_session_manager.remove_session(session_id)
                        _remote_terminal_sessions.pop(session_id, None)
                        logger.info(f"用户关闭远程终端，清理会话: session_id={session_id}")
                        # 🔧 Task#185：广播terminal_closed事件到所有webui实例
                        await manager.broadcast({
                            "type": "terminal_event",
                            "event": "terminal_closed",
                            "session_id": session_id,
                        })
                        break
                    elif isinstance(cmd, dict) and cmd.get("type") == "screen_sync":
                        # 保存序列化屏幕数据，用于刷新页面后恢复显示
                        screen_data = cmd.get("screen", "")
                        if screen_data and session:
                            session.update_serialized_screen(screen_data)
                            # 🔧 兼容旧代码
                            if session_id in _remote_terminal_sessions:
                                _remote_terminal_sessions[session_id]["serialized_screen"] = screen_data
                    else:
                        # 普通输入 -> 写入远程PTY
                        user_input = cmd if isinstance(cmd, str) else str(cmd)
                        write_req = NestRequest(
                            type="terminal_write",
                            params={
                                "session_id": remote_session_id,
                                "data": user_input,
                                "_skip_security": True,
                            },
                        )
                        await conn.send_request(write_req, timeout=10.0)
                except json.JSONDecodeError:
                    # 不是JSON，直接作为输入写入远程PTY
                    write_req = NestRequest(
                        type="terminal_write",
                        params={
                            "session_id": remote_session_id,
                            "data": data,
                            "_skip_security": True,
                        },
                    )
                    await conn.send_request(write_req, timeout=10.0)
        except WebSocketDisconnect:
            logger.info(f"远程终端 WebSocket 连接已断开: session_id={session_id}")
        except Exception as e:
            logger.error(f"远程终端异常: {e}\n{traceback.format_exc()}")
        finally:
            # 🔧 多浏览器支持：只移除当前WebSocket，不关闭远程终端
            if session:
                session.remove_websocket(websocket)
                remaining = session.ws_count
                logger.info(
                    f"✅ 远程终端WebSocket已从会话中移除: session_id={session_id}, "
                    f"剩余连接数={remaining}"
                )
                # 所有WebSocket都断开时，停止转发任务以释放资源
                # 但不关闭远程PTY（允许后续重连）
                if remaining == 0:
                    session.stop_forward_task()
                    logger.info(
                        f"✅ 所有浏览器已断开远程终端: session_id={session_id}, 已停止输出转发"
                    )

    except NestError as e:
        logger.error(f"远程终端Nest错误: {e}")
        try:
            await websocket.send_text(json.dumps({
                "type": "error",
                "message": f"Nest通信错误: {e}",
            }))
        except Exception:
            pass
    except Exception as e:
        logger.error(f"远程终端异常: {e}\n{traceback.format_exc()}")
    finally:
        try:
            await websocket.close()
        except Exception:
            pass
