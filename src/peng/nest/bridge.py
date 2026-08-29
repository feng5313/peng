"""
Nest Bridge - peng 端管理来自 nest 的 WebSocket 连接

架构：
  nest(A电脑) ──WebSocket──> peng(B电脑后端:8899)

nest 主动连接 peng 后端的 WebSocket 服务器（不走 webui）。
peng 通过已建立的连接向 nest 发送命令并接收结果。

管理器负责：
1. 接受 nest 的 WebSocket 连接
2. 维护 nest_name -> websocket 映射
3. 提供发送请求并等待响应的接口（给 tools 使用）
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import time
import uuid
from typing import Any, AsyncIterator

import websockets

from peng.nest.protocol import NestRequest, NestResponse, NestError

logger = logging.getLogger(__name__)

# 🔧 自动代码更新防抖：记录每个nest上次推送时间，避免频繁重连时反复触发
_last_code_update_time: dict[str, float] = {}
_CODE_UPDATE_DEBOUNCE_SECONDS = 300  # 同一个nest 5分钟内不重复推送代码更新

# 🔧 服务启动保护：记录服务启动时间，restart期间禁止自动代码推送
_service_start_time: float = time.monotonic()
_SERVICE_START_COOLDOWN_SECONDS = 300  # 服务启动后5分钟内禁止自动代码推送
# suppress websockets.server ERROR logs
logging.getLogger("websockets.server").setLevel(logging.CRITICAL)

# 默认端口（peng 后端监听 nest 连接的端口）
DEFAULT_NEST_PORT = 8899


class NestConnection:
    """单个 nest 连接"""
    
    def __init__(self, conn_id: int, name: str, websocket, info: dict, display_key: str):
        self.conn_id = conn_id  # 唯一连接 ID
        self.name = name  # 原始注册名
        self.display_key = display_key  # 唯一显示键（自动加后缀区分同名）
        self.websocket = websocket
        self.info = info  # nest 上报的自身信息（platform, hostname, pid 等）
        self.connected_at = time.time()
        self._pending: dict[str, asyncio.Future] = {}  # request_id -> Future
        self._closing = False
        # 🔧 终端输出回调：{session_id: callable}
        self._terminal_output_callbacks: dict[str, Any] = {}
        # 🔧 网络流量统计（EWMA指数加权移动平均算法）
        self._bytes_sent: int = 0       # 累计发送字节数
        self._bytes_recv: int = 0       # 累计接收字节数
        self._last_traffic_ts: float = 0.0  # 上次采样时间（0表示未初始化）
        self._last_bytes_total: int = 0  # 上次采样的累计总字节数
        self._ewma_rate: float = 0.0    # EWMA平滑后的合并速率 (bytes/sec)
        # 🔧 流式响应队列：{request_id: [Queue, ...]}
        self._streaming_queues: dict[str, list[asyncio.Queue]] = {}
        self._ewma_tau: float = 5.0     # EWMA时间常数（秒），约63%权重在最近tau秒内
    
    async def send_request(self, request: NestRequest, timeout: float = 120.0) -> NestResponse:
        """发送请求并等待响应"""
        if self._closing:
            raise NestError(f"nest '{self.display_key}' 正在关闭")
        loop = asyncio.get_event_loop()
        future = loop.create_future()
        self._pending[request.id] = future
        
        try:
            data = json.dumps(request.to_dict(), ensure_ascii=True)
            self._bytes_sent += len(data.encode('utf-8'))  # 统计发送流量
            try:
                await self.websocket.send(data)
            except websockets.ConnectionClosed as e:
                logger.warning(f"send_request 发送失败(nest={self.display_key}, id={self.conn_id}): code={e.code}, reason={e.reason}")
                raise NestError(f"发送请求时WebSocket已关闭: code={e.code}")
            
            # 等待响应
            return await asyncio.wait_for(future, timeout=timeout)
        except asyncio.TimeoutError:
            raise NestError(f"请求超时 ({timeout}s): type={request.type}")
        finally:
            self._pending.pop(request.id, None)
    
    async def send_request_streaming(
        self, request: NestRequest, timeout: float = 120.0
    ) -> AsyncIterator[NestResponse]:
        """发送请求并流式接收响应（用于 bash stream 模式）
        
        与 send_request 不同，此方法会持续接收 stream 类型的响应，
        直到收到 done=True 的最终响应或超时。
        
        Yields:
            NestResponse: 逐条流式响应
        """
        if self._closing:
            raise NestError(f"nest '{self.display_key}' 正在关闭")
        
        queue: asyncio.Queue[NestResponse | None] = asyncio.Queue()
        
        # 注册流式队列，用于接收多条响应
        self._streaming_queues.setdefault(request.id, []).append(queue)
        
        try:
            data = json.dumps(request.to_dict(), ensure_ascii=True)
            self._bytes_sent += len(data.encode('utf-8'))
            await self.websocket.send(data)
            
            # 流式接收
            deadline = asyncio.get_event_loop().time() + timeout
            while True:
                remaining = deadline - asyncio.get_event_loop().time()
                if remaining <= 0:
                    raise NestError(f"流式请求超时 ({timeout}s): type={request.type}")
                
                try:
                    resp = await asyncio.wait_for(queue.get(), timeout=remaining)
                except asyncio.TimeoutError:
                    raise NestError(f"流式请求超时 ({timeout}s): type={request.type}")
                
                if resp is None:
                    # 结束信号
                    break
                
                yield resp
                
                if resp.done:
                    break
        finally:
            # 清理流式队列
            queues = self._streaming_queues.get(request.id, [])
            if queue in queues:
                queues.remove(queue)
            if not queues:
                self._streaming_queues.pop(request.id, None)
    
    def handle_response(self, response_data: dict):
        """处理收到的响应，匹配到对应的 pending request"""
        # 统计接收流量（估算JSON序列化后的字节数）
        try:
            recv_size = len(json.dumps(response_data, ensure_ascii=True).encode('utf-8'))
            self._bytes_recv += recv_size
        except Exception:
            pass
        
        # 🔧 处理终端输出推送消息
        msg_type = response_data.get("type")
        if msg_type == "terminal_output":
            session_id = response_data.get("session_id")
            output_data = response_data.get("data")
            callback = self._terminal_output_callbacks.get(session_id)
            if callback:
                asyncio.ensure_future(callback(session_id, output_data))
            return
        
        resp = NestResponse.from_dict(response_data)
        
        # 🔧 流式响应：分发到流式队列
        if resp.type == "stream":
            queues = self._streaming_queues.get(resp.id, [])
            if queues:
                for q in queues:
                    q.put_nowait(resp)
                if resp.done:
                    # 流式结束，发送 None 作为结束信号
                    for q in queues:
                        q.put_nowait(None)
                return
        
        # 普通响应：匹配到 pending future
        future = self._pending.pop(resp.id, None)
        if future and not future.done():
            future.set_result(resp)
        elif resp.id:
            logger.warning(f"[Bridge] 收到未匹配的响应: id={resp.id}, pending_ids={list(self._pending.keys())[:3]}, type={resp.type}")
    
    def register_terminal_callback(self, session_id: str, callback):
        """注册终端输出回调"""
        self._terminal_output_callbacks[session_id] = callback
    
    def unregister_terminal_callback(self, session_id: str):
        """注销终端输出回调"""
        self._terminal_output_callbacks.pop(session_id, None)
    
    def update_traffic_rates(self) -> None:
        """更新流量速率（EWMA指数加权移动平均算法，5秒时间常数平滑）
        
        算法原理：
        - 每次调用时计算当前即时速率 instant_rate = delta_bytes / delta_time
        - 用EWMA平滑：rate = (1 - alpha) * prev_rate + alpha * instant_rate
        - alpha = 1 - exp(-dt / tau)，tau=5秒为时间常数
        - tau=5s 意味着约63%权重在最近5秒内，适合网络流量平滑显示
        """
        now = time.time()
        total_bytes = self._bytes_sent + self._bytes_recv
        
        if self._last_traffic_ts > 0:
            dt = now - self._last_traffic_ts
            if dt > 0:
                delta_bytes = max(0, total_bytes - self._last_bytes_total)
                instant_rate = delta_bytes / dt
                # EWMA平滑：alpha 随采样间隔自适应
                alpha = 1.0 - math.exp(-dt / self._ewma_tau)
                self._ewma_rate = (1.0 - alpha) * self._ewma_rate + alpha * instant_rate
        
        self._last_traffic_ts = now
        self._last_bytes_total = total_bytes
    
    def get_traffic_snapshot(self) -> dict:
        """获取当前流量快照"""
        return {
            "display_key": self.display_key,
            "display_name": self.display_name,
            "platform": self.info.get("platform", ""),
            "linux_id": self.info.get("linux_id", ""),
            "linux_name": self.info.get("linux_name", ""),
            "total_rate": max(0.0, self._ewma_rate),
            "bytes_sent": self._bytes_sent,
            "bytes_recv": self._bytes_recv,
        }
    
    def close(self):
        """标记连接为关闭状态"""
        self._closing = True
        # 取消所有 pending 请求
        for fut in self._pending.values():
            if not fut.done():
                fut.set_exception(NestError(f"nest '{self.display_key}' 关闭"))
        # 清理所有终端回调
        self._terminal_output_callbacks.clear()
    
    @property
    def display_name(self) -> str:
        """显示名称"""
        return self.info.get("name", self.name)


class NestBridgeManager:
    """Nest 连接管理器（peng 端单例）
    
    支持同一名称的多个 nest 连接共存（同一 PC 可能运行多个 nest 实例）。
    同名连接自动加后缀区分（优先hostname，hostname相同加数字）。
    内部用列表管理所有连接，用 conn_id (int) 唯一标识每个连接。
    """
    
    _instance: NestBridgeManager | None = None
    
    def __init__(self):
        self._connections: list[NestConnection] = []
        self._next_id: int = 1  # 自增连接 ID
        self._accepting_connections: bool = True  # 是否接受远端nest连接（启动阶段只允许local）
    
    @classmethod
    def get_instance(cls) -> NestBridgeManager:
        if cls._instance is None:
            cls._instance = NestBridgeManager()
        return cls._instance
    
    def _generate_display_key(self, name: str, hostname: str) -> str:
        """为同名连接生成唯一显示键
        
        规则（所有nest统一）：
        - 第一个连接：保持原名（如 "local"）
        - 后续同名连接：加 /hostname 后缀（如 "local/epyc"）
        - hostname也相同时：加数字后缀（如 "local/epyc-2"）
        """
        # 收集所有同名连接的 display_key
        existing_keys = {c.display_key for c in self._connections if c.name == name}
        
        if not existing_keys:
            return name
        
        if name not in existing_keys:
            return name
        
        # 尝试用 hostname 作后缀
        if hostname:
            candidate = f"{name}/{hostname}"
            if candidate not in existing_keys:
                return candidate
            
            # hostname 也冲突了，加数字
            n = 2
            while f"{candidate}-{n}" in existing_keys:
                n += 1
            return f"{candidate}-{n}"
        else:
            # 没有 hostname，直接加数字
            n = 2
            while f"{name}-{n}" in existing_keys:
                n += 1
            return f"{name}-{n}"
    
    def register(self, name: str, websocket, info: dict) -> NestConnection:
        """注册一个新的 nest 连接（允许多个同名连接共存，自动加后缀区分）"""
        conn_id = self._next_id
        self._next_id += 1
        
        hostname = info.get("hostname", "")
        display_key = self._generate_display_key(name, hostname)
        
        conn = NestConnection(conn_id, name, websocket, info, display_key)
        self._connections.append(conn)
        logger.info(
            f"nest '{display_key}' 已连接 "
            f"(id={conn_id}, platform={info.get('platform', '?')}, "
            f"hostname={hostname or '?'}), 总连接数: {len(self._connections)}"
        )
        return conn
    
    def unregister(self, conn: NestConnection):
        """注销指定的 nest 连接"""
        if conn in self._connections:
            self._connections.remove(conn)
            conn.close()
            logger.info(f"nest '{conn.display_key}' (id={conn.conn_id}) 已断开, 剩余连接数: {len(self._connections)}")
    
    def get_connection(self, name_or_key: str) -> NestConnection | None:
        """获取指定名称或 display_key 的 nest 连接"""
        # 先精确匹配 display_key
        for conn in self._connections:
            if conn.display_key == name_or_key:
                return conn
        # 再匹配原始 name
        for conn in self._connections:
            if conn.name == name_or_key:
                return conn
        return None
    
    def get_default_connection(self) -> NestConnection | None:
        """获取默认 nest 连接"""
        if not self._connections:
            return None
        # 优先选择有桌面访问能力的连接（has_desktop=True）
        for conn in self._connections:
            if conn.info.get("has_desktop", True):
                return conn
        # 没有桌面能力的连接也返回（用于bash等不需要桌面的操作）
        return self._connections[0]
    
    def list_connections(self) -> list[dict]:
        """列出所有连接"""
        result = []
        for conn in self._connections:
            result.append({
                "conn_id": conn.conn_id,
                "name": conn.name,
                "display_key": conn.display_key,
                "display_name": conn.display_name,
                "platform": conn.info.get("platform", "?"),
                "hostname": conn.info.get("hostname", "?"),
                "linux_id": conn.info.get("linux_id", ""),
                "linux_name": conn.info.get("linux_name", ""),
                "pid": conn.info.get("pid"),
                "workdir": conn.info.get("workdir", ""),
                "connected_at": conn.connected_at,
            })
        return result
    
    def update_all_traffic_rates(self) -> None:
        """更新所有连接的流量速率（定期调用）"""
        for conn in self._connections:
            conn.update_traffic_rates()
    
    def get_traffic_snapshots(self) -> list[dict]:
        """获取所有连接的流量快照"""
        return [conn.get_traffic_snapshot() for conn in self._connections]
    
    async def send_code_update(self, nest_name: str | None = None) -> dict[str, Any]:
        """向远程nest发送代码更新通知，让其git pull并重启
        
        Args:
            nest_name: 目标nest名称，None则发送给所有远程nest
        
        Returns:
            发送结果 {nest_name: success/error}
        """
        results = {}
        targets = []
        
        # 获取本机hostname用于排除自身
        import socket
        local_hostname = socket.gethostname().lower()
        
        if nest_name:
            conn = self.get_connection(nest_name)
            if conn:
                targets.append(conn)
        else:
            # 发送给所有远程nest（排除与自身hostname相同的连接）
            targets = [c for c in self._connections if c.info.get("hostname", "").lower() != local_hostname]
        
        for conn in targets:
            try:
                request = NestRequest(
                    type="code_update",
                    params={"_skip_security": True},
                )
                response = await conn.send_request(request, timeout=30.0)
                if response.error:
                    results[conn.display_key] = {"status": "error", "error": response.error}
                    logger.error(f"代码更新通知失败({conn.display_key}): {response.error}")
                else:
                    result_data = response.data or {}
                    results[conn.display_key] = {"status": "success", **result_data}
                    logger.info(f"代码更新通知成功({conn.display_key})")
            except Exception as e:
                results[conn.display_key] = {"status": "error", "error": str(e)}
                logger.error(f"代码更新通知异常({conn.display_key}): {e}")
        
        return results


async def _handle_nest_connection(websocket):
    """处理单个 WebSocket 连接（websockets 库回调）
    
    三种连接类型：
    1. nest 注册：发来 register 消息，进入主循环转发响应
    2. 子进程转发：发来 forward_request 消息，转发到已连接的 nest
    3. CLI 命令：发来 cli_command 消息，在本地执行 task/msg 命令
    """
    try:
        # 第一步：等待消息确定连接类型
        raw = await asyncio.wait_for(websocket.recv(), timeout=30)
        msg = json.loads(raw)
        
        msg_type = msg.get("type")
        
        if msg_type == "register":
            # nest 客户端注册
            await _handle_nest_register(websocket, msg)
        elif msg_type == "forward_request":
            # 子进程转发请求
            await _handle_forward_request(websocket, msg)
        elif msg_type == "cli_command":
            # CLI 命令远程执行
            await _handle_cli_command(websocket, msg)
        else:
            await websocket.close(code=4001, reason="Expected register, forward_request or cli_command")
    
    except asyncio.TimeoutError:
        logger.warning("WebSocket 连接超时（未收到注册/转发消息）")
        try:
            await websocket.close(code=4002, reason="Timeout")
        except Exception:
            pass
    except websockets.ConnectionClosed:
        pass
    except Exception as e:
        logger.debug(f"WebSocket 处理异常: {e}")


async def _handle_nest_register(websocket, register_msg):
    """处理 nest 客户端注册"""
    nest_name = register_msg.get("name", "unknown")
    nest_info = register_msg.get("info", {})
    new_hostname = nest_info.get("hostname", "")
    new_pid = nest_info.get("pid")
    
    manager = NestBridgeManager.get_instance()
    
    # 🔧 启动阶段只允许 local 注册，拒绝远端连接（远端会自动重连）
    # 保证 local nest 在所有远端 nest 之前注册，排在连接列表第一位
    if not manager._accepting_connections and nest_name != "local":
        logger.info(f"nest '{nest_name}' 在启动阶段被拒绝，等待 local 注册完成后重连")
        await websocket.close(code=4003, reason="Server not ready, retry after local nest registered")
        return
    
    # 🔧 服务器端重复检测：同一台机器（同名+同hostname+同platform）上的重复连接
    # 保留最新的连接，关闭旧的重复连接
    if nest_name and new_hostname:
        duplicates = []
        for existing_conn in manager._connections:
            if (existing_conn.name == nest_name 
                and existing_conn.info.get("hostname") == new_hostname
                and existing_conn.info.get("platform") == nest_info.get("platform")):
                duplicates.append(existing_conn)
        
        if duplicates:
            # 按连接时间排序，关闭所有旧的（保留最新的 = 当前正在注册的）
            for dup in duplicates:
                dup_pid = dup.info.get("pid", "?")
                logger.info(
                    f"nest 重复检测：关闭旧连接 '{dup.display_key}' "
                    f"(conn_id={dup.conn_id}, pid={dup_pid})，"
                    f"新连接 pid={new_pid}"
                )
                try:
                    await dup.websocket.close(code=4010, reason="Duplicate connection replaced by newer instance")
                except Exception:
                    pass
                dup._closing = True
                manager.unregister(dup)
    
    conn = manager.register(nest_name, websocket, nest_info)
    
    # 发送确认
    await websocket.send(json.dumps({
        "type": "registered",
        "name": nest_name,
    }, ensure_ascii=True))
    
    # 🔧 远程nest注册成功后，自动推送代码更新（git pull + 重启）
    # 排除 local nest（本机无需更新），仅更新远程nest
    if nest_name != "local":
        async def _auto_code_update():
            """延迟发送代码更新，等待注册流程完全就绪"""
            try:
                now = time.monotonic()
                
                # 🔧 服务启动保护：restart期间禁止自动代码推送
                service_uptime = now - _service_start_time
                if service_uptime < _SERVICE_START_COOLDOWN_SECONDS:
                    logger.info(
                        f"[AutoCodeUpdate] 服务启动冷却期（{_SERVICE_START_COOLDOWN_SECONDS}秒），"
                        f"禁止自动代码推送（已运行 {service_uptime:.0f}秒）"
                    )
                    return
                
                # 🔧 防抖检查：同一nest短时间内不重复推送
                last_time = _last_code_update_time.get(nest_name, 0)
                if now - last_time < _CODE_UPDATE_DEBOUNCE_SECONDS:
                    logger.info(
                        f"[AutoCodeUpdate] nest '{nest_name}' 在 {_CODE_UPDATE_DEBOUNCE_SECONDS}秒内已推送过更新，"
                        f"跳过（距上次 {now - last_time:.0f}秒）"
                    )
                    return
                _last_code_update_time[nest_name] = now
                
                await asyncio.sleep(2)  # 等待2秒，确保nest端注册完成并准备就绪
                
                import subprocess
                import os as _os
                from pathlib import Path
                from datetime import datetime as _dt
                project_root = str(Path(__file__).resolve().parent.parent.parent.parent)
                
                # 用户操作频繁变更的文件，不算代码变更
                _skip_patterns = {"config/webui.json"}
                
                # ===== 第1步：获取最后代码变更时间 =====
                # 先看有变更的文件的修改时间，没有变更才查commit时间
                
                last_change_dt = None
                
                # 1a. 用git diff获取有变更的文件列表（工作区+暂存区），取这些文件的最新mtime
                # 排除webui.json（用户频繁变更不算）
                for diff_cmd in [["git", "diff", "--name-only"], ["git", "diff", "--cached", "--name-only"]]:
                    diff_result = subprocess.run(
                        diff_cmd, capture_output=True, text=True, timeout=10,
                        cwd=project_root,
                    )
                    if diff_result.stdout.strip():
                        for fpath in diff_result.stdout.strip().split('\n'):
                            if not fpath or any(skip in fpath for skip in _skip_patterns):
                                continue
                            full_path = _os.path.join(project_root, fpath)
                            try:
                                mt = _os.path.getmtime(full_path)
                                file_dt = _dt.fromtimestamp(mt)
                                if last_change_dt is None or file_dt > last_change_dt:
                                    last_change_dt = file_dt
                            except OSError:
                                pass
                
                # 1b. 如果git diff没有变更文件，查最后一次commit时间
                if last_change_dt is None:
                    log_result = subprocess.run(
                        ["git", "log", "-1", "--format=%aI"],
                        capture_output=True, text=True, timeout=10,
                        cwd=project_root,
                    )
                    commit_time_str = log_result.stdout.strip()
                    if commit_time_str:
                        try:
                            last_change_dt = _dt.fromisoformat(commit_time_str)
                            if last_change_dt.tzinfo:
                                last_change_dt = last_change_dt.replace(tzinfo=None)
                        except ValueError:
                            pass
                
                if not last_change_dt:
                    logger.warning("[AutoCodeUpdate] 无法确定最后代码变更时间，跳过")
                    return
                
                # ===== 第2步：检查是否有代码变更需要推送 =====
                # 包括：未commit的文件、未push的commit
                has_uncommitted = False
                has_unpushed = False
                
                # 检查未commit的代码变更
                status_result = subprocess.run(
                    ["git", "status", "--porcelain"],
                    capture_output=True, text=True, timeout=10,
                    cwd=project_root,
                )
                all_changes = status_result.stdout.strip().split('\n') if status_result.stdout.strip() else []
                code_changes = [
                    line for line in all_changes
                    if line and not any(skip in line for skip in _skip_patterns)
                ]
                if code_changes:
                    has_uncommitted = True
                
                # 检查未push的commit
                unpushed_result = subprocess.run(
                    ["git", "log", "origin/master..HEAD", "--oneline"],
                    capture_output=True, text=True, timeout=10,
                    cwd=project_root,
                )
                if unpushed_result.stdout.strip():
                    has_unpushed = True
                
                if not has_uncommitted and not has_unpushed:
                    logger.info("[AutoCodeUpdate] 无代码变更需要推送（已全部commit且已push），跳过")
                    return
                
                # ===== 第3步：安全检查 =====
                # 最后代码变更时间到当前时间之间，需要有 >= 2个成功完成的事务
                # 才允许commit+push+推送更新，确保peng端代码经过验证
                try:
                    from peng.task.database import TaskDatabase
                    task_db = TaskDatabase()
                    completed_tasks = task_db.list_tasks(status="completed")
                    recent_completed = []
                    for t in completed_tasks:
                        ca = t.get("completed_at")
                        if not ca:
                            continue
                        try:
                            task_dt = _dt.fromisoformat(ca)
                            if task_dt.tzinfo:
                                task_dt = task_dt.replace(tzinfo=None)
                            if task_dt >= last_change_dt:
                                recent_completed.append(t)
                        except ValueError:
                            continue
                    completed_count = len(recent_completed)
                    
                    if completed_count < 2:
                        logger.info(
                            f"[AutoCodeUpdate] 安全检查：最后代码变更后仅有 {completed_count}/2 个成功事务，"
                            f"暂不推送（需要2个成功事务验证代码稳定性）"
                        )
                        return
                    else:
                        logger.info(f"[AutoCodeUpdate] 安全检查通过：最后代码变更后有 {completed_count} 个成功事务")
                except Exception as verify_err:
                    logger.error(f"[AutoCodeUpdate] 安全检查异常，不推送: {verify_err}")
                    return
                
                # ===== 第4步：安全检查通过，auto commit + push + 推送更新 =====
                # 代码已验证稳定，可以安全提交和推送
                
                # 先提交所有未commit的变更（含webui.json，保持仓库干净）
                if has_uncommitted:
                    logger.info(f"[AutoCodeUpdate] 自动提交 {len(code_changes)} 个代码文件（含webui.json）...")
                    subprocess.run(["git", "add", "-A"], capture_output=True, text=True, timeout=30, cwd=project_root)
                    commit_result = subprocess.run(
                        ["git", "commit", "-m", "auto: 自动提交（代码更新推送前）"],
                        capture_output=True, text=True, timeout=30,
                        cwd=project_root,
                    )
                    if commit_result.returncode != 0:
                        logger.error(f"[AutoCodeUpdate] git commit 失败: {commit_result.stderr}")
                        return
                    logger.info(f"[AutoCodeUpdate] 自动提交成功: {commit_result.stdout.strip()[:200]}")
                
                # push到远程仓库
                if has_uncommitted or has_unpushed:
                    push_result = subprocess.run(
                        ["git", "push"],
                        capture_output=True, text=True, timeout=60,
                        cwd=project_root,
                    )
                    if push_result.returncode != 0:
                        logger.error(f"[AutoCodeUpdate] git push 失败: {push_result.stderr}")
                        return
                    logger.info(f"[AutoCodeUpdate] git push 成功: {push_result.stdout.strip()[:200]}")
                
                # 推送更新到远程nest
                logger.info(f"[AutoCodeUpdate] 开始推送更新到远程nest '{conn.display_key}'...")
                results = await manager.send_code_update(conn.display_key)
                for name, result in results.items():
                    status = result.get("status", "unknown") if isinstance(result, dict) else result
                    if status == "success":
                        logger.info(f"[AutoCodeUpdate] 远程nest '{name}' 代码更新推送成功")
                        # 如果远程nest有被stash的本地修改，发消息提醒master
                        if result.get("has_stash") and (result.get("stash_files") or result.get("stash_diff")):
                            stash_files = result.get("stash_files", "")
                            stash_diff = result.get("stash_diff", "")
                            warning_msg = (
                                f"⚠️ 远程nest '{name}' 在代码更新时发现有本地修改被stash！\n"
                                f"被stash的文件:\n{stash_files}\n"
                            )
                            if stash_diff:
                                warning_msg += f"diff内容:\n```\n{stash_diff}\n```"
                            logger.warning(f"[AutoCodeUpdate] {warning_msg}")
                            # 发消息提醒master
                            try:
                                from peng.config import get_master_name
                                from peng.message.cli_service import send_message
                                send_message(
                                    content=warning_msg,
                                    from_="peng",
                                    to=get_master_name(),
                                )
                            except Exception as msg_err:
                                logger.error(f"[AutoCodeUpdate] 发送stash提醒消息失败: {msg_err}")
                    else:
                        error = result.get("error", str(result)) if isinstance(result, dict) else str(result)
                        logger.warning(f"[AutoCodeUpdate] 远程nest '{name}' 代码更新推送失败: {error}")
            except Exception as e:
                logger.error(f"[AutoCodeUpdate] 自动代码更新异常: {e}", exc_info=True)
        asyncio.create_task(_auto_code_update())
    
    try:
        # 主循环：接收 nest 发来的响应
        async for raw_msg in websocket:
            try:
                msg = json.loads(raw_msg)
                # 🔧 事务#1598：处理 nest 端发来的 workdir 更新 register 消息
                # （同一连接上的重新注册，仅更新 info，不创建新连接）
                # 并回复 registered 确认（nest 端主循环会跳过该确认消息）
                if msg.get("type") == "register":
                    new_info = msg.get("info", {})
                    if new_info:
                        conn.info.update(new_info)
                        _new_workdir = new_info.get("workdir")
                        if _new_workdir:
                            logger.info(f"nest '{conn.display_key}' workdir 已更新: {_new_workdir}")
                    try:
                        await websocket.send(json.dumps({
                            "type": "registered",
                            "name": conn.name,
                        }, ensure_ascii=True))
                    except Exception:
                        break
                    continue
                conn.handle_response(msg)
            except json.JSONDecodeError:
                logger.warning(f"nest '{nest_name}' 发送了无效 JSON")
            except Exception as e:
                logger.debug(f"nest '{nest_name}' 处理消息异常: {e}")
        # 🔧 async for 正常退出 = 对端发送了 Close 帧，记录日志便于排查频繁断连
        logger.info(f"nest '{nest_name}' (id={conn.conn_id}) WebSocket 正常关闭（对端发起 Close 帧）, connected_at={conn.connected_at:.0f}, duration={time.time()-conn.connected_at:.1f}s")
    except websockets.ConnectionClosed as e:
        logger.warning(f"nest '{nest_name}' (id={conn.conn_id}) WebSocket关闭: code={e.code}, reason={e.reason}, duration={time.time()-conn.connected_at:.1f}s")
    except Exception as e:
        logger.error(f"nest '{nest_name}' (id={conn.conn_id}) 主循环异常: {type(e).__name__}: {e}")
    finally:
        manager.unregister(conn)


async def _handle_forward_request(websocket, forward_msg):
    """处理子进程转发的请求（支持通过 nest_name 指定目标 nest）"""
    request_data = forward_msg.get("request", {})
    timeout = forward_msg.get("timeout", 120.0)
    nest_name = forward_msg.get("nest_name")  # 子进程指定的目标 nest
    use_stream = forward_msg.get("stream", False)
    
    manager = NestBridgeManager.get_instance()
    
    # 优先使用指定名称的连接，否则使用默认连接
    conn = None
    if nest_name:
        conn = manager.get_connection(nest_name)
        if not conn:
            await websocket.send(json.dumps({
                "type": "error",
                "error": f"nest '{nest_name}' 未连接（可用: "
                         + ", ".join(c.display_key for c in manager._connections) + ")",
            }, ensure_ascii=True))
            await websocket.close()
            return
    if not conn:
        conn = manager.get_default_connection()
    
    if not conn:
        await websocket.send(json.dumps({
            "type": "error",
            "error": "没有可用的 nest 连接",
        }, ensure_ascii=True))
        await websocket.close()
        return
    
    try:
        request = NestRequest(
            type=request_data.get("type", ""),
            params=request_data.get("params", {}),
            id=request_data.get("id"),
        )
        
        if use_stream:
            # 流式模式：逐条转发 stream 响应
            async for resp in conn.send_request_streaming(request, timeout=timeout):
                await websocket.send(json.dumps(resp.to_dict(), ensure_ascii=True))
            await websocket.close()
        else:
            # 非流式模式（原有逻辑）
            response = await conn.send_request(request, timeout=timeout)
            await websocket.send(json.dumps(response.to_dict(), ensure_ascii=True))
    except Exception as e:
        await websocket.send(json.dumps({
            "type": "error",
            "error": str(e),
        }, ensure_ascii=True))
    finally:
        await websocket.close()


def _inject_workdir_from_nest(args: list[str]) -> list[str]:
    """如果 args 中有 -n/--nest 但没有 -w/--workdir，从 NestBridgeManager 获取 workdir 并注入
    
    Args:
        args: CLI 命令参数列表
        
    Returns:
        可能追加了 -w <workdir> 的参数列表
    """
    # 检查是否已有 -w/--workdir
    has_workdir = False
    nest_name = None
    
    i = 0
    while i < len(args):
        arg = args[i]
        if arg in ("-w", "--workdir"):
            has_workdir = True
            break
        elif arg in ("-n", "--nest") and i + 1 < len(args):
            nest_name = args[i + 1]
            i += 1
        i += 1
    
    # 如果没有 nest 或已有 workdir，不做处理
    if not nest_name or has_workdir:
        return args
    
    # 从 NestBridgeManager 获取该 nest 的 workdir
    try:
        bridge = NestBridgeManager.get_instance()
        conn = bridge.get_connection(nest_name)
        if conn:
            nest_workdir = conn.info.get("workdir", "")
            if nest_workdir:
                return args + ["-w", nest_workdir]
    except Exception:
        pass
    
    return args


async def _handle_cli_command(websocket, cli_msg):
    """处理 CLI 命令远程执行请求
    
    请求格式:
    {
        "type": "cli_command",
        "command": "task" | "msg" | "fly",
        "args": ["tree", "1"],
        "cwd": "/path/to/project" (可选)
    }
    
    响应格式:
    {
        "type": "result",
        "stdout": "...",
        "stderr": "...",
        "returncode": 0
    }
    """
    command = cli_msg.get("command", "")
    args = cli_msg.get("args", [])
    cwd = cli_msg.get("cwd")
    
    if command not in ("task", "msg", "fly"):
        await websocket.send(json.dumps({
            "type": "error",
            "error": f"不支持的命令: {command}，只支持 task、msg 和 fly",
        }, ensure_ascii=True))
        await websocket.close()
        return
    
    # 构建 CLI 命令：用 --local 强制本地模式执行
    from peng.config import get_project_root
    project_root = get_project_root()
    
    # 确定正确的 python 和脚本路径
    import sys
    if sys.platform == "win32":
        venv_python = str(project_root / ".venv" / "Scripts" / "python.exe")
    else:
        venv_python = str(project_root / ".venv" / "bin" / "python")
    
    import os
    if not os.path.exists(venv_python):
        venv_python = sys.executable
    
    script_path = str(project_root / "bin" / command)
    
    # 如果指定了 -n/--nest 但没有 -w/--workdir，从远端 nest 获取 workdir 并注入
    args = _inject_workdir_from_nest(args)
    
    cmd = [venv_python, script_path, "--local"] + args
    
    # 设置颜色环境变量，确保 rich/ANSI 颜色输出
    # 同时设置 PYTHONUNBUFFERED=1 禁用 Python 输出缓冲，确保流式输出实时传递
    import os as _os
    _color_env = _os.environ.copy()
    _color_env.update({
        "TERM": "xterm-256color",
        "COLORTERM": "truecolor",
        "FORCE_COLOR": "1",
        "CLICOLOR_FORCE": "1",
        "PYTHONUNBUFFERED": "1",
    })
    
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=cwd or str(project_root),
            env=_color_env,
        )
        
        # 流式读取子进程输出，实时通过 WebSocket 发送给客户端
        # 使用 read(4096) 而非 readline()，避免因缺少换行符导致缓冲阻塞
        async def _stream_output(stream, output_type):
            """实时读取子进程输出并发送（按块读取，不等待换行符）"""
            while True:
                chunk = await stream.read(4096)
                if not chunk:
                    break
                try:
                    await websocket.send(json.dumps({
                        "type": "stream",
                        "output_type": output_type,
                        "data": chunk.decode("utf-8", errors="replace"),
                    }, ensure_ascii=True))
                except Exception:
                    break
        
        # 并发读取 stdout 和 stderr
        import asyncio as _aio
        await _aio.wait_for(
            _aio.gather(
                _stream_output(proc.stdout, "stdout"),
                _stream_output(proc.stderr, "stderr"),
                proc.wait(),
            ),
            timeout=300,
        )
        
        # 发送最终结果（包含 returncode）
        await websocket.send(json.dumps({
            "type": "result",
            "returncode": proc.returncode,
        }, ensure_ascii=True))
    except asyncio.TimeoutError:
        # 超时时kill子进程，释放其占用的PG连接
        try:
            proc.kill()
            await proc.wait()
        except Exception:
            pass
        await websocket.send(json.dumps({
            "type": "error",
            "error": "CLI 命令执行超时（300秒）",
        }, ensure_ascii=True))
    except Exception as e:
        # 异常时也确保kill子进程
        try:
            proc.kill()
            await proc.wait()
        except Exception:
            pass
        await websocket.send(json.dumps({
            "type": "error",
            "error": f"CLI 命令执行失败: {e}",
        }, ensure_ascii=True))
    finally:
        await websocket.close()


async def start_nest_server(host: str = "0.0.0.0", port: int = DEFAULT_NEST_PORT):
    """启动 peng 后端的 nest 接入服务器"""
    logger.info(f"Starting nest server on {host}:{port}")
    
    async with websockets.serve(
        _handle_nest_connection, host, port,
        max_size=50 * 1024 * 1024,  # 50MB，允许截屏等大消息通过（默认1MB太小）
        ping_interval=None,  # 禁用自动ping，由应用层心跳保活（WSL等虚拟网络环境TCP连接易断）
        ping_timeout=None,   # 禁用ping超时
    ):
        logger.info(f"Nest server listening on {host}:{port}")
        # 永远等待，直到被取消
        await asyncio.Future()
