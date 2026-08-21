"""
Nest Connector - 主动连接 peng 服务的 WebSocket 客户端

架构变更：nest 不再监听端口，而是主动连接 peng 的 /ws/nest 端点。
连接建立后，peng 通过此连接向 nest 发送命令，nest 执行后返回结果。

特性：
- 自动重连（定时重试）
- 多实例支持（可同时连接多个 peng）
- 心跳保活
- 优雅关闭
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import platform
import signal
import socket
import sys
import time
import traceback
from pathlib import Path
from typing import Any

from peng.nest.protocol import NestRequest, NestResponse, RequestType

logger = logging.getLogger(__name__)


def _json_dumps(obj) -> str:
    """安全的JSON序列化"""
    return json.dumps(obj, ensure_ascii=True)


def _safe_print(msg: str, **kwargs) -> None:
    """安全打印，处理 Windows GBK 编码问题"""
    try:
        print(msg, **kwargs)
    except UnicodeEncodeError:
        safe_msg = msg.encode("ascii", errors="replace").decode("ascii")
        print(safe_msg, **kwargs)


def _get_project_root() -> Path:
    """获取项目根目录"""
    return Path(__file__).resolve().parent.parent.parent.parent


class NestConnector:
    """Nest 连接器 - 主动连接 peng 服务"""
    
    def __init__(
        self,
        name: str,
        host: str = "localhost",
        port: int = 8899,
        reconnect_interval: float = 5.0,
        security=None,
    ):
        self.name = name
        self.host = host
        self.port = port
        self.reconnect_interval = reconnect_interval
        self.security = security
        self._running = False
        self._ws = None
        self._executor = None
        self._request_count = 0
        self._stop_event: asyncio.Event | None = None
        self._last_workdir: str = ""  # 缓存上次的workdir用于检测变化
        # 🔧 守护模式属性
        self.daemon = False  # 是否启用守护模式
        self.ssh = None  # 远程控制SSH命令
        self.daemon_disconnect_timeout = 120  # 连续断开N秒后触发修复
        self._disconnect_since: float | None = None  # 断连开始时间
        self._daemon_repairing = False  # 是否正在执行守护修复
        
        # 自动补全 nest.json 中的 self 字段（确保 bin/msg 等工具能识别本机 nest 身份）
        self._ensure_self_in_config()
    
    def _get_executor(self):
        if self._executor is None:
            from peng.nest.local import LocalExecutor
            self._executor = LocalExecutor(security=self.security)
        return self._executor
    
    def _get_ws_url(self) -> str:
        """构建 WebSocket URL"""
        return f"ws://{self.host}:{self.port}"
    
    def _read_workdir_from_config(self) -> str:
        """从 config/nest.json 读取当前 instance 的 workdir"""
        try:
            nest_cfg_path = _get_project_root() / "config" / "nest.json"
            if nest_cfg_path.exists():
                with open(nest_cfg_path, encoding="utf-8") as f:
                    nest_cfg = json.load(f)
                inst = nest_cfg.get("instances", {}).get(self.name, {})
                return inst.get("workdir", "")
        except Exception:
            pass
        return ""

    def _ensure_self_in_config(self) -> None:
        """确保 config/nest.json 中有正确的 self 字段
        
        如果 self 字段缺失或与当前 nest 名称不符，自动补全。
        这使得 bin/msg 等本机 CLI 工具能正确识别当前 nest 身份。
        """
        try:
            nest_cfg_path = _get_project_root() / "config" / "nest.json"
            config = {}
            if nest_cfg_path.exists():
                with open(nest_cfg_path, encoding="utf-8") as f:
                    config = json.load(f)
            
            current_self = config.get("self")
            if current_self == self.name:
                return  # 已正确
            
            # 补全或修正 self 字段
            config["self"] = self.name
            nest_cfg_path.parent.mkdir(parents=True, exist_ok=True)
            nest_cfg_path.write_text(
                json.dumps(config, indent=2, ensure_ascii=False) + "\n",
                encoding="utf-8",
            )
            _safe_print(f"[Nest] 已补全 nest.json self={self.name}")
            logger.info(f"已补全 nest.json self={self.name}")
        except Exception as e:
            logger.warning(f"补全 nest.json self 字段失败: {e}")

    @staticmethod
    def _get_stop_signal_file() -> Path:
        """获取 nest connect 停止信号文件路径"""
        return _get_project_root() / "data" / "nest_stop_signal"
    
    def _check_stop_signal(self) -> bool:
        """检查是否存在停止信号文件"""
        try:
            signal_file = self._get_stop_signal_file()
            if signal_file.exists():
                # 检查信号文件是否过期（超过60秒视为过期，避免残留文件阻止启动）
                mtime = signal_file.stat().st_mtime
                if time.time() - mtime < 60:
                    return True
                else:
                    # 过期文件，清理
                    signal_file.unlink(missing_ok=True)
        except Exception:
            pass
        return False
    
    @staticmethod
    def create_stop_signal():
        """创建停止信号文件（fly stop 调用，让所有 nest connect 进程主动退出）"""
        from peng.common.paths import ensure_dir_with_owner, chown_to_project_owner
        signal_file = NestConnector._get_stop_signal_file()
        ensure_dir_with_owner(signal_file.parent)
        signal_file.write_text(str(os.getpid()))
        chown_to_project_owner(signal_file)
        return signal_file
    
    @staticmethod
    def remove_stop_signal():
        """删除停止信号文件"""
        try:
            NestConnector._get_stop_signal_file().unlink(missing_ok=True)
        except Exception:
            pass
    
    async def start(self) -> None:
        """启动连接器（主循环，含自动重连）"""
        try:
            import websockets
        except ImportError:
            logger.error("websockets 库未安装")
            _safe_print("[ERROR] websockets 库未安装", file=sys.stderr)
            return
        
        self._running = True
        ws_url = self._get_ws_url()
        
        _safe_print(f"[Nest] 连接器启动: {self.name} -> {ws_url}")
        logger.info(f"Nest 连接器启动: {self.name} -> {ws_url}")
        
        # 设置信号处理
        loop = asyncio.get_running_loop()
        stop_event = asyncio.Event()
        self._stop_event = stop_event
        
        # 保存原始信号处理器，退出时恢复
        original_sigint = signal.getsignal(signal.SIGINT)
        original_sigterm = signal.getsignal(signal.SIGTERM)
        
        if sys.platform == "win32":
            signal.signal(signal.SIGINT, lambda sig, frame: loop.call_soon_threadsafe(stop_event.set))
            signal.signal(signal.SIGTERM, lambda sig, frame: loop.call_soon_threadsafe(stop_event.set))
        else:
            try:
                loop.add_signal_handler(signal.SIGINT, stop_event.set)
                loop.add_signal_handler(signal.SIGTERM, stop_event.set)
            except (NotImplementedError, OSError):
                pass
        
        try:
            while self._running and not stop_event.is_set():
                try:
                    async with websockets.connect(
                        ws_url,
                        ping_interval=None,  # 禁用自动ping，由应用层心跳保活
                        ping_timeout=None,
                        max_size=50 * 1024 * 1024,
                        close_timeout=5,
                    ) as ws:
                        self._ws = ws
                        _safe_print(f"[Nest] 已连接: {ws_url}")
                        logger.info(f"Nest 已连接: {ws_url}")
                        
                        # 🔧 守护模式：连接成功时重置断连计时
                        self._disconnect_since = None
                        if self._daemon_repairing:
                            self._daemon_repairing = False
                            _safe_print("[Nest][Daemon] 远端服务已恢复连接")
                            logger.info("[Daemon] 远端服务已恢复连接")
                        
                        # 发送注册消息
                        # 从 config/nest.json 的 instance 中读取 workdir
                        _workdir = self._read_workdir_from_config()
                        self._last_workdir = _workdir

                        register_msg = {
                            "type": "register",
                            "name": self.name,
                            "info": {
                                "name": self.name,
                                "platform": platform.system(),
                                "hostname": socket.gethostname(),
                                "pid": os.getpid(),
                                "project_root": str(_get_project_root()),
                                "workdir": _workdir or str(_get_project_root()),
                                "has_desktop": True,  # nest connect 进程标记自己有桌面访问能力
                            },
                        }
                        # Linux: 读取 /etc/os-release 获取发行版信息
                        if sys.platform.startswith("linux"):
                            try:
                                os_release = {}
                                with open("/etc/os-release", encoding="utf-8") as f:
                                    for line in f:
                                        line = line.strip()
                                        if "=" in line:
                                            k, _, v = line.partition("=")
                                            os_release[k] = v.strip('"')
                                register_msg["info"]["linux_id"] = os_release.get("ID", "")
                                register_msg["info"]["linux_name"] = os_release.get("NAME", "Linux")
                                register_msg["info"]["linux_version"] = os_release.get("VERSION_ID", "")
                            except Exception:
                                pass
                        # Windows: 检测是否在Session 0（服务会话，无桌面访问）
                        if sys.platform == "win32":
                            try:
                                import ctypes
                                session_id = ctypes.c_ulong()
                                ctypes.windll.kernel32.ProcessIdToSessionId(
                                    os.getpid(), ctypes.byref(session_id)
                                )
                                register_msg["info"]["has_desktop"] = (session_id.value != 0)
                                register_msg["info"]["session_id"] = session_id.value
                            except Exception:
                                pass
                        await ws.send(_json_dumps(register_msg))
                        
                        # 等待注册确认
                        reg_resp = await asyncio.wait_for(ws.recv(), timeout=10)
                        reg_data = json.loads(reg_resp)
                        if reg_data.get("type") != "registered":
                            logger.error(f"注册失败: {reg_data}")
                            await asyncio.sleep(self.reconnect_interval)
                            continue
                        
                        _safe_print(f"[Nest] 注册成功: {self.name}")
                        logger.info(f"Nest 注册成功: {self.name}")
                        
                        # 主消息循环
                        # 🔧 使用 asyncio.wait 同时等待消息和 stop_event，确保 Ctrl+C 能响应
                        # websockets 16.x 兼容：recv() 在连接关闭时可能返回空或抛异常
                        # 🔧 添加 15秒超时心跳，防止 WSL/Hyper-V 等虚拟网络环境杀空闲 TCP 连接
                        _last_heartbeat = time.time()
                        try:
                            while self._running and not stop_event.is_set():
                                # 创建接收消息的 task
                                recv_task = asyncio.ensure_future(ws.recv())
                                stop_task = asyncio.ensure_future(stop_event.wait())
                                
                                done, pending = await asyncio.wait(
                                    {recv_task, stop_task},
                                    timeout=15,  # 最多等15秒，超时发心跳
                                    return_when=asyncio.FIRST_COMPLETED,
                                )
                                
                                # 取消未完成的 task
                                for task in pending:
                                    task.cancel()
                                    try:
                                        await task
                                    except (asyncio.CancelledError, Exception):
                                        pass
                                
                                if stop_event.is_set() or not self._running:
                                    break
                                
                                # 🔧 每15秒发送应用层心跳，防止虚拟网络环境杀空闲TCP连接
                                now = time.time()
                                if now - _last_heartbeat >= 15:
                                    _last_heartbeat = now
                                    try:
                                        await ws.ping()
                                    except Exception:
                                        break
                                
                                # 处理收到的消息
                                if recv_task in done:
                                    try:
                                        message = recv_task.result()
                                        # 🔧 websockets 16.x: 空消息表示连接已关闭
                                        if not message:
                                            logger.info("Nest 收到空消息（连接关闭），准备重连...")
                                            break
                                        # 🔧 事务#1598：跳过 bridge 发来的 registered 确认消息
                                        # （workdir 更新 register 的确认），它不是请求，
                                        # 不能进 _handle_message（否则被当成无效请求返回错误）
                                        try:
                                            _msg_type = json.loads(message).get("type")
                                        except Exception:
                                            _msg_type = None
                                        if _msg_type == "registered":
                                            logger.debug("收到 bridge 的 register 确认（跳过）")
                                            continue
                                        await self._handle_message(ws, message)
                                        
                                        # 🔧 检查 workdir 配置是否变化，变化则重新注册同步到 host
                                        _new_workdir = self._read_workdir_from_config()
                                        if _new_workdir != self._last_workdir:
                                            self._last_workdir = _new_workdir
                                            logger.info(f"检测到workdir变化: {_new_workdir}")
                                            # 发送更新注册消息
                                            # 🔧 事务#1598：不再 await ws.recv() 等确认！
                                            # 该写法会错误挪用下一条正常请求（如read），
                                            # 导致请求被吞、bridge侧future悬空120s超时
                                            # （#1596 read 120s超时的根因）
                                            update_msg = {
                                                "type": "register",
                                                "name": self.name,
                                                "info": {
                                                    "name": self.name,
                                                    "platform": platform.system(),
                                                    "hostname": socket.gethostname(),
                                                    "pid": os.getpid(),
                                                    "project_root": str(_get_project_root()),
                                                    "workdir": _new_workdir or str(_get_project_root()),
                                                    "has_desktop": True,
                                                },
                                            }
                                            await ws.send(_json_dumps(update_msg))
                                            # 确认由主循环的 "registered" 跳过逻辑异步接收
                                    except websockets.ConnectionClosed as e:
                                        logger.warning(f"Nest 连接断开: code={e.code}, reason={e.reason}")
                                        if e.code == 4010:
                                            # 被新实例替代，主动退出进程（防止连接风暴）
                                            logger.info("被新实例替代(code=4010)，主动退出")
                                            _safe_print("[Nest] 被新实例替代，主动退出")
                                            self._running = False
                                        else:
                                            _safe_print(f"[Nest] 连接断开 (code={e.code})，{self.reconnect_interval}s 后重连...")
                                        break
                        except Exception as e:
                            logger.warning(f"Nest 主循环异常: {e}")
                            _safe_print(f"[Nest] 主循环异常: {e}")
                        
                except asyncio.TimeoutError:
                    logger.warning(f"Nest 注册超时: {ws_url}")
                    _safe_print(f"[Nest] 注册超时，{self.reconnect_interval}s 后重连...")
                except ConnectionRefusedError:
                    logger.warning(f"Nest 连接被拒绝: {ws_url}")
                    _safe_print(f"[Nest] 连接被拒绝，{self.reconnect_interval}s 后重连...")
                except Exception as e:
                    logger.warning(f"Nest 连接异常: {e}")
                    _safe_print(f"[Nest] 连接异常: {e}，{self.reconnect_interval}s 后重连...")
                
                self._ws = None
                
                # 🔧 守护模式：记录断连开始时间
                if self.daemon and self._disconnect_since is None:
                    self._disconnect_since = time.time()
                
                # 检查停止信号文件（fly stop 写入此文件让 nest connect 主动退出）
                if self._check_stop_signal():
                    logger.info("检测到停止信号文件，主动退出")
                    break
                
                # 🔧 守护模式：检测断连超时，触发自动修复
                if self.daemon and self.ssh and self._disconnect_since and not self._daemon_repairing:
                    disconnect_duration = time.time() - self._disconnect_since
                    if disconnect_duration >= self.daemon_disconnect_timeout:
                        logger.warning(f"[Daemon] 远端服务连续断开 {disconnect_duration:.0f}s，触发守护修复")
                        self._daemon_repairing = True
                        # 在后台执行守护修复（不阻塞重连循环）
                        asyncio.ensure_future(self._daemon_repair())
                
                # 等待重连间隔（可被 stop_event 打断）
                try:
                    await asyncio.wait_for(stop_event.wait(), timeout=self.reconnect_interval)
                    break  # stop_event 被设置，退出
                except asyncio.TimeoutError:
                    pass  # 超时，继续重连
        finally:
            # 恢复原始信号处理器
            self._running = False
            self._stop_event = None
            try:
                signal.signal(signal.SIGINT, original_sigint)
            except (ValueError, OSError):
                pass
            try:
                signal.signal(signal.SIGTERM, original_sigterm)
            except (ValueError, OSError):
                pass
            # 关闭残留的 WebSocket 连接
            if self._ws is not None:
                try:
                    await self._ws.close()
                except Exception:
                    pass
                self._ws = None
        
        _safe_print(f"[Nest] 连接器已停止: {self.name}")
        logger.info(f"Nest 连接器已停止: {self.name}")
    
    async def _daemon_repair(self) -> None:
        """守护模式：远端服务失效后自动修复
        
        修复流程：
        1. 如果本地没有peng服务，自动fly install安装
        2. 通过本地peng服务发送消息给本地：用SSH查看修复远端
        3. 远端恢复后，如果是自动安装的本地服务，则fly uninstall
        """
        try:
            _safe_print("[Nest][Daemon] 开始执行守护修复流程...")
            logger.info("[Daemon] 开始执行守护修复流程")
            
            # 检查本地是否有peng服务运行
            local_fly_running = False
            try:
                from peng.common.platform import subprocess_run_no_window
                result = subprocess_run_no_window(
                    ["fly", "status"],
                    capture_output=True, text=True, timeout=10,
                    cwd=str(_get_project_root()),
                )
                local_fly_running = result.returncode == 0
            except Exception:
                pass
            
            auto_installed = False
            if not local_fly_running:
                _safe_print("[Nest][Daemon] 本地无peng服务，自动安装...")
                logger.info("[Daemon] 本地无peng服务，尝试自动安装fly")
                try:
                    from peng.common.platform import subprocess_run_no_window
                    result = subprocess_run_no_window(
                        ["fly", "install"],
                        capture_output=True, text=True, timeout=120,
                        cwd=str(_get_project_root()),
                    )
                    if result.returncode == 0:
                        auto_installed = True
                        _safe_print("[Nest][Daemon] fly install 成功")
                        logger.info("[Daemon] fly install 成功")
                    else:
                        _safe_print(f"[Nest][Daemon] fly install 失败: {result.stderr[:200]}")
                        logger.error(f"[Daemon] fly install 失败: {result.stderr[:200]}")
                except Exception as e:
                    _safe_print(f"[Nest][Daemon] fly install 异常: {e}")
                    logger.error(f"[Daemon] fly install 异常: {e}")
            
            # 发送消息给本地peng服务，请求远程修复
            repair_message = (
                f"用 {self.ssh} 查看分析远程peng服务失效原因并修复。"
                f"如果是代码改坏可以尝试暂存代码并回退到上一个版本，"
                f"并用 msg --to peng 通知远程服务自行修复暂存代码。"
            )
            _safe_print(f"[Nest][Daemon] 发送修复消息: {repair_message[:80]}...")
            logger.info(f"[Daemon] 发送修复消息")
            
            try:
                from peng.common.platform import subprocess_run_no_window
                msg_cmd = ["msg", "--local", "--to", "peng", repair_message]
                subprocess_run_no_window(msg_cmd, capture_output=True, text=True, timeout=30,
                             cwd=str(_get_project_root()))
            except Exception as e:
                _safe_print(f"[Nest][Daemon] 发送修复消息失败: {e}")
                logger.error(f"[Daemon] 发送修复消息失败: {e}")
            
            # 注意：自动卸载逻辑在连接恢复后处理（_check_daemon_cleanup）
            self._auto_installed_fly = auto_installed
            
        except Exception as e:
            _safe_print(f"[Nest][Daemon] 守护修复流程异常: {e}")
            logger.error(f"[Daemon] 守护修复流程异常: {e}")
    
    def stop(self) -> None:
        """停止连接器"""
        self._running = False
        # 触发 stop_event 以唤醒可能在等待的 asyncio 循环
        if self._stop_event is not None:
            try:
                loop = asyncio.get_running_loop()
                loop.call_soon_threadsafe(self._stop_event.set)
            except RuntimeError:
                # 没有运行中的事件循环，直接设置
                self._stop_event.set()
    
    async def _safe_send(self, ws, data: str) -> None:
        """安全发送 WebSocket 消息，使用锁防止并发send冲突"""
        if not hasattr(self, '_send_lock'):
            self._send_lock = asyncio.Lock()
        try:
            async with self._send_lock:
                await ws.send(data)
        except Exception:
            logger.debug("ws.send 失败（连接可能已断开），跳过")
    
    async def _handle_message(self, ws, message: str | bytes) -> None:
        """处理来自 peng 的命令"""
        try:
            if isinstance(message, bytes):
                message = message.decode("utf-8")
            data = json.loads(message)
        except Exception as e:
            error_resp = NestResponse.make_error("unknown", f"无效的JSON: {e}")
            await self._safe_send(ws, _json_dumps(error_resp.to_dict()))
            return
        
        try:
            request = NestRequest.from_dict(data)
        except Exception as e:
            error_resp = NestResponse.make_error(data.get("id", "unknown"), f"无效请求: {e}")
            await self._safe_send(ws, _json_dumps(error_resp.to_dict()))
            return
        
        self._request_count += 1
        # stat/ping等高频请求用DEBUG级别，避免大量日志IO
        log_level = logging.DEBUG if request.type in ("stat", "ping") else logging.INFO
        logger.log(log_level, f"收到命令 #{self._request_count}: type={request.type}")
        
        params = dict(request.params)
        params["_request_id"] = request.id
        
        # 🔧 为 terminal_create 请求注入推送回调
        if request.type == "terminal_create":
            async def _push_terminal_output(session_id: str, output_data: str | None):
                """将PTY输出推送到peng端"""
                try:
                    if output_data is not None:
                        msg = {
                            "type": "terminal_output",
                            "session_id": session_id,
                            "data": output_data,
                        }
                    else:
                        msg = {
                            "type": "terminal_output",
                            "session_id": session_id,
                            "data": None,  # 进程退出信号
                        }
                    await ws.send(_json_dumps(msg))
                except Exception as e:
                    logger.debug(f"推送终端输出失败: {e}")
            params["_push_callback"] = _push_terminal_output
        
        executor = self._get_executor()
        
        # 🔧 代码更新通知：收到peng端的code_update请求，执行git pull并重启
        if request.type == "code_update":
            try:
                logger.info("[CodeUpdate] 收到代码更新通知，开始更新...")
                from peng.common.platform import subprocess_run_no_window
                project_root = str(_get_project_root())
                
                # 先记录更新前的commit hash
                hash_before = subprocess_run_no_window(
                    ["git", "rev-parse", "HEAD"],
                    capture_output=True, text=True, timeout=10,
                    cwd=project_root,
                ).stdout.strip()
                
                # git stash 保存本地修改，避免pull冲突
                # 原则上不应有nest端代码变动，stash后不恢复，但会通知peng端
                stash_result = subprocess_run_no_window(
                    ["git", "stash", "--include-untracked"],
                    capture_output=True, text=True, timeout=30,
                    cwd=project_root,
                )
                stash_output = stash_result.stdout.strip()
                # stash_output为空说明git stash没有实际保存内容，不应视为有stash
                has_stash = bool(stash_output) and "No local changes" not in stash_output and stash_result.returncode == 0
                stash_files = ""
                stash_diff = ""
                if has_stash:
                    # 记录被stash的文件列表
                    show_result = subprocess_run_no_window(
                        ["git", "stash", "show", "--stat"],
                        capture_output=True, text=True, timeout=10,
                        cwd=project_root,
                    )
                    stash_files = show_result.stdout.strip()
                    # 获取被stash的完整diff内容
                    diff_result = subprocess_run_no_window(
                        ["git", "stash", "show", "-p"],
                        capture_output=True, text=True, timeout=30,
                        cwd=project_root,
                    )
                    stash_diff = diff_result.stdout.strip()[:5000]  # 限制长度避免消息过大
                    # 如果stash_files和stash_diff都为空，说明没有实际被stash的内容
                    if not stash_files and not stash_diff:
                        has_stash = False
                        logger.info(f"[CodeUpdate] git stash执行成功但无实际文件变更，忽略stash")
                    else:
                        logger.warning(f"[CodeUpdate] 发现nest端有本地修改被stash！文件:\n{stash_files}\ndiff:\n{stash_diff[:500]}")
                
                # git pull
                result = subprocess_run_no_window(
                    ["git", "pull"],
                    capture_output=True, text=True, timeout=60,
                    cwd=project_root,
                )
                pull_output = result.stdout + result.stderr
                
                if result.returncode != 0:
                    logger.error(f"[CodeUpdate] git pull失败: {pull_output}")
                    # pull失败，恢复stash
                    if has_stash:
                        subprocess_run_no_window(["git", "stash", "pop"], capture_output=True, text=True, timeout=10, cwd=project_root)
                    error_resp = NestResponse.make_error(request.id, f"git pull失败: {pull_output[:500]}")
                    await self._safe_send(ws, _json_dumps(error_resp.to_dict()))
                    return
                
                # 检查是否有实际代码变化
                hash_after = subprocess_run_no_window(
                    ["git", "rev-parse", "HEAD"],
                    capture_output=True, text=True, timeout=10,
                    cwd=project_root,
                ).stdout.strip()
                
                if hash_before == hash_after:
                    logger.info(f"[CodeUpdate] 代码已是最新，无需重启 (hash={hash_before[:8]})")
                    success_resp = NestResponse.make_result(request.id, {
                        "message": "代码已是最新，无需重启",
                        "pull_output": pull_output[:500],
                        "restarted": False,
                        "has_stash": has_stash,
                        "stash_files": stash_files[:500] if stash_files else "",
                        "stash_diff": stash_diff if stash_diff else "",
                    })
                    await self._safe_send(ws, _json_dumps(success_resp.to_dict()))
                    return
                
                logger.info(f"[CodeUpdate] 代码已更新: {hash_before[:8]} -> {hash_after[:8]}")
                
                # 发送成功响应
                success_resp = NestResponse.make_result(request.id, {
                    "message": f"代码更新成功，即将重启",
                    "pull_output": pull_output[:500],
                    "restarted": True,
                    "has_stash": has_stash,
                    "stash_files": stash_files[:500] if stash_files else "",
                    "stash_diff": stash_diff if stash_diff else "",
                })
                await self._safe_send(ws, _json_dumps(success_resp.to_dict()))
                
                # 延迟1秒后重启
                _safe_print("[CodeUpdate] 代码更新完成，1秒后重启...")
                logger.info("[CodeUpdate] 代码更新完成，准备重启")
                await asyncio.sleep(1)
                
                # 重启自身进程
                os.execv(sys.executable, [sys.executable] + sys.argv)
                
            except Exception as e:
                logger.error(f"[CodeUpdate] 代码更新失败: {e}")
                error_resp = NestResponse.make_error(request.id, f"代码更新失败: {e}")
                await self._safe_send(ws, _json_dumps(error_resp.to_dict()))
            return
        
        # 🔧 所有请求都用独立task并行处理，避免长任务阻塞消息接收循环
        async def _handle_request():
            try:
                result = await executor.handle_request(request.type, params)
                if isinstance(result, NestResponse):
                    result.id = request.id
                    await self._safe_send(ws, _json_dumps(result.to_dict()))
                elif hasattr(result, '__aiter__'):
                    # 流式响应：逐条发送 stream 消息
                    async for stream_resp in result:
                        stream_resp.id = request.id
                        await self._safe_send(ws, _json_dumps(stream_resp.to_dict()))
                else:
                    error_resp = NestResponse.make_error(request.id, "执行器返回意外类型")
                    await self._safe_send(ws, _json_dumps(error_resp.to_dict()))
            except Exception as e:
                logger.error(f"请求执行失败 [{request.type}]: {e}\n{traceback.format_exc()}")
                error_resp = NestResponse.make_error(request.id, f"执行失败: {e}")
                await self._safe_send(ws, _json_dumps(error_resp.to_dict()))
        asyncio.create_task(_handle_request())
