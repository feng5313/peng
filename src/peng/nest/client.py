"""
Nest Client - peng 端通过已连接的 nest 发送命令

架构变更：peng 不再主动连接 nest。
nest 主动连接 peng 的 /ws/nest 端点后，peng 通过 NestBridgeManager
找到对应的连接，发送命令并等待响应。
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import traceback
import uuid
from typing import Any

from peng.nest.protocol import NestRequest, NestResponse, NestError

logger = logging.getLogger(__name__)

# 🔧 nest 连接暂时不可用时的自动重试配置
_RETRYABLE_PATTERNS = re.compile(r"关闭|正在关闭|未连接|WebSocket已关闭|ConnectionClosed")
_MAX_RETRIES = 3
_RETRY_DELAY_BASE = 1.0  # 基础重试延迟（秒），实际递增：1s, 2s, 3s

# 🔧 持续重试配置：nest 不可用时重试，避免丢失大模型 token
# 🔧 事务#1737: 不再无限重试！确定性失败（如响应超过帧上限触发1009断连）
# 会陷入"重连→重发→再超限"死循环，必须设硬上限让调用方感知失败
_RETRY_MAX_DELAY = 30  # 单次重试最大延迟
_RETRY_HEARTBEAT_INTERVAL = 25  # 每 25 秒写一次 output 心跳，防止任务超时
_RETRY_MAX_ATTEMPTS = 20  # 最大重试次数（20次 × 最长30s退避 ≈ 上限约10分钟）
_RETRY_MAX_TOTAL_SECONDS = 600  # 重试总时长上限（秒）

# 全局默认 nest 名称
_default_nest_name: str | None = None

# 🔧 nest 请求默认超时（从 peng.json 的 nest.timeout 读取，事务#1598）
# 缓存读取结果避免每次请求重复解析配置；读取失败保持代码默认值 120s
_configured_timeout: float | None = None
_DEFAULT_REQUEST_TIMEOUT = 120.0


def get_request_timeout() -> float:
    """获取 nest 请求默认超时时间（秒）

    优先读 peng.json 的 nest.timeout；未配置或读取失败则返回 120.0。
    结果缓存，配置文件变化由 load_config 的 mtime 缓存机制感知。
    """
    global _configured_timeout
    if _configured_timeout is not None:
        return _configured_timeout
    try:
        from peng.config.loader import load_config

        config = load_config()
        nest_cfg = getattr(config, "nest", None)
        timeout = getattr(nest_cfg, "timeout", None) if nest_cfg else None
        if isinstance(timeout, (int, float)) and timeout > 0:
            _configured_timeout = float(timeout)
        else:
            _configured_timeout = _DEFAULT_REQUEST_TIMEOUT
    except Exception:
        _configured_timeout = _DEFAULT_REQUEST_TIMEOUT
    return _configured_timeout

# 当前 task_id（由工具层设置，用于重试时写 output 心跳）
_current_task_id: int | None = None


def set_default_nest(name: str) -> None:
    """设置默认 nest 名称"""
    global _default_nest_name
    _default_nest_name = name


def get_default_nest_name() -> str | None:
    """获取默认 nest 名称"""
    return _default_nest_name


def set_current_task_id(task_id: int | None) -> None:
    """设置当前 task_id（由工具层调用，用于重试时写 output 心跳保活）"""
    global _current_task_id
    _current_task_id = task_id


def _get_connection(name: str | None = None):
    """获取 nest 连接（仅主进程有效，子进程 NestBridgeManager 无连接）"""
    from peng.nest.bridge import NestBridgeManager
    manager = NestBridgeManager.get_instance()

    # 快速检测：如果管理器没有任何连接，说明在子进程中，立即返回 None
    # 由调用方决定是否走 _forward_via_server
    if not manager._connections:
        return None

    if name:
        conn = manager.get_connection(name)
    else:
        conn = manager.get_default_connection()
        if not conn:
            conn = manager.get_connection(_default_nest_name or "local")
    return conn


def _check_connection_available(name: str | None = None) -> bool:
    """检查 nest 连接是否可用（不抛出异常）"""
    conn = _get_connection(name)
    return conn is not None and not conn._closing


async def _forward_via_server(
    request: NestRequest, timeout: float = 120.0, nest_name: str | None = None
) -> NestResponse:
    """
    子进程模式：通过 peng 后端 nest server（8899）转发请求
    
    返回 NestResponse（成功或包含业务错误的响应）。
    仅在连接层面失败时抛出异常（连接拒绝、超时等）。
    """
    import asyncio
    import websockets

    nest_server_port = 8899
    try:
        from peng.config.loader import load_config

        config = load_config()
        nest_cfg = getattr(config, "nest", None)
        if nest_cfg:
            nest_server_port = getattr(nest_cfg, "port", 8899)
    except Exception:
        pass

    uri = f"ws://localhost:{nest_server_port}"

    try:
        async with websockets.connect(uri, max_size=50 * 1024 * 1024) as ws:
            forward_msg = json.dumps(
                {
                    "type": "forward_request",
                    "request": request.to_dict(),
                    "timeout": timeout,
                    "nest_name": nest_name,
                },
                ensure_ascii=True,
            )
            await ws.send(forward_msg)

            raw = await asyncio.wait_for(ws.recv(), timeout=timeout)
            resp_data = json.loads(raw)

            if resp_data.get("type") == "error":
                # bridge 层面错误（如"没有可用的 nest 连接"）→ 连接错误，可重试
                raise NestError(resp_data.get("error", "转发请求失败"))

            # nest 返回的响应（即使包含业务 error 字段，也是正常响应）
            return NestResponse.from_dict(resp_data)
    except NestError:
        raise
    except asyncio.TimeoutError:
        raise NestError(f"转发请求超时 ({timeout}s)")
    except websockets.ConnectionClosed as e:
        raise NestError(f"转发连接异常: {e}")
    except ConnectionRefusedError as e:
        raise NestError(f"无法连接到 peng 后端 nest server ({uri}): {e}")
    except Exception as e:
        raise NestError(f"转发请求异常: {e}")


def _is_retryable_error(error: Exception) -> bool:
    """判断是否为可重试的连接错误（nest 短暂断连、正在关闭等）"""
    error_msg = str(error)
    return bool(_RETRYABLE_PATTERNS.search(error_msg))


async def send_request(
    request_type: str,
    params: dict[str, Any] | None = None,
    timeout: float | None = None,
    nest_name: str | None = None,
) -> NestResponse:
    """发送请求到 nest 并等待响应

    两种模式：
    1. 主进程：通过 NestBridgeManager 找到已连接的 nest
    2. 子进程：直接连接 peng 后端 nest server（8899）转发请求

    🔧 自动重试：当遇到连接断开/关闭等暂时性错误时，会短暂等待后重试，
    因为 nest 通常会在秒级内重新连接。

    🔧 timeout=None 时使用 peng.json 的 nest.timeout（默认 120s，事务#1598）
    """
    if timeout is None:
        timeout = get_request_timeout()
    request = NestRequest(
        type=request_type,
        params=params or {},
    )

    # 🔧 检测是否在子进程中（NestBridgeManager 无连接）
    # 子进程直接走 _forward_via_server，避免浪费 6 秒重试 _get_connection
    from peng.nest.bridge import NestBridgeManager
    mgr = NestBridgeManager.get_instance()
    if not mgr._connections:
        return await _forward_with_retry(request, timeout, nest_name)

    # 主进程模式：通过 NestBridgeManager 查找连接
    # 🔧 减少重试次数到 1 次（1 秒），因为 _forward_with_retry 会接管后续重试
    last_error = None
    bridge_max_retries = 1

    for attempt in range(bridge_max_retries + 1):
        try:
            conn = _get_connection(nest_name)
            if conn is None:
                # 连接已丢失（可能刚被 unregister），立即走 forward
                break
            return await conn.send_request(request, timeout=timeout)
        except NestError as e:
            last_error = e
            if _is_retryable_error(e) and attempt < bridge_max_retries:
                delay = _RETRY_DELAY_BASE * (attempt + 1)
                logger.info(
                    f"nest 请求失败(可重试, attempt={attempt + 1}/{bridge_max_retries}): {e}, "
                    f"{delay:.1f}s 后重试..."
                )
                await asyncio.sleep(delay)
                request = NestRequest(type=request_type, params=params or {})
                continue
            elif not _is_retryable_error(e):
                raise
            else:
                break  # 可重试但重试次数已用完
        except Exception as e:
            last_error = e
            break  # 非 NestError 异常，跳出循环

    # fallback 到 forward 模式
    return await _forward_with_retry(request, timeout, nest_name, last_error)


async def _forward_with_retry(
    request: NestRequest,
    timeout: float,
    nest_name: str | None = None,
    last_error: Exception | None = None,
) -> NestResponse:
    """通过 _forward_via_server 发送请求，无限重试直到成功
    
    🔧 nest 不可用时无限重试（指数退避 1s→30s 封顶），永不放弃，
    避免丢失大模型重要信息（如超长文件写入的巨量 token）。
    
    每次重试都写 output_manager 提示，重试等待期间每 25 秒写心跳，防止任务心跳超时。
    """
    import time
    
    start_time = time.monotonic()
    attempt = 0
    error = last_error
    last_heartbeat_time = start_time

    def _write_output(msg: str):
        """写 output_manager（每次调用都写）"""
        tid = _current_task_id
        if tid:
            try:
                from peng.task.output_manager import get_output_manager
                get_output_manager().write_output(tid, "system", msg + "\n")
            except Exception:
                pass

    def _write_heartbeat(msg: str):
        """写 output 心跳保活（等待期间每 25 秒写一次）"""
        nonlocal last_heartbeat_time
        now = time.monotonic()
        if now - last_heartbeat_time >= _RETRY_HEARTBEAT_INTERVAL:
            last_heartbeat_time = now
            _write_output(msg)

    while True:
        attempt += 1
        try:
            return await _forward_via_server(request, timeout, nest_name=nest_name)
        except NestError as e:
            error = e
            # 🔧 只有连接类错误才重试（如 nest 正在重启、连接被拒绝等）
            # 业务错误（如"文件未找到"、"old_string 匹配失败"）立即抛出，不重试
            if not _is_retryable_error(e):
                raise

        elapsed = time.monotonic() - start_time

        # 🔧 事务#1737: 重试必须有上限——确定性失败（如响应超过WebSocket帧上限
        # 触发 code=1009 断连）会导致"重连→重发→再超限"无限死循环，
        # 把整个nest打挂且调用方永久卡死。达到上限后抛错让调用方感知失败。
        if attempt >= _RETRY_MAX_ATTEMPTS or elapsed >= _RETRY_MAX_TOTAL_SECONDS:
            raise NestError(
                f"nest 请求重试 {attempt} 次后放弃(已用 {elapsed:.0f}s, "
                f"上限 {_RETRY_MAX_ATTEMPTS} 次/{_RETRY_MAX_TOTAL_SECONDS}s): {error}"
            )

        # 计算下一次重试延迟（指数退避：1, 2, 4, 8, 16, 30, 30, ...）
        delay = min(2 ** (attempt - 1), _RETRY_MAX_DELAY)
        elapsed = time.monotonic() - start_time
        
        logger.warning(
            f"nest 请求失败(attempt={attempt}), {delay:.0f}s 后重试 "
            f"(已用 {elapsed:.0f}s): {error}"
        )
        
        # 🔧 每次重试都写 output_manager 提示（不节流）
        _write_output(
            f"⏳ nest 不可用，第 {attempt} 次重试，{delay:.0f}s 后重试 "
            f"(已用 {elapsed:.0f}s): {error}"
        )
        last_heartbeat_time = time.monotonic()
        
        # 分段 sleep，等待期间每 25 秒写心跳
        slept = 0.0
        while slept < delay:
            sleep_chunk = min(_RETRY_HEARTBEAT_INTERVAL, delay - slept)
            await asyncio.sleep(sleep_chunk)
            slept += sleep_chunk
            _write_heartbeat(
                f"⏳ nest 不可用，持续等待中 (第 {attempt} 次重试, 已等待 {elapsed + slept:.0f}s)"
            )
        
        # 更新 request id 避免冲突
        request = NestRequest(type=request.type, params=request.params)


# ==================== 便捷方法 ====================


async def ping(nest_name: str | None = None) -> dict[str, Any]:
    """Ping nest"""
    response = await send_request("ping", timeout=5, nest_name=nest_name)
    if response.error:
        raise NestError(f"Ping 失败: {response.error}")
    return response.data


async def bash(
    command: str,
    cwd: str | None = None,
    timeout: int = 120000,
    env: dict | None = None,
    nest_name: str | None = None,
) -> NestResponse:
    """执行 bash 命令"""
    params: dict[str, Any] = {"command": command, "timeout": timeout}
    if cwd:
        params["cwd"] = cwd
    if env:
        params["env"] = env
    # 🔧 将 bash 超时传递给 send_request（+10s 缓冲），
    # 否则 send_request 默认 120s 会在 nest 执行完之前就超时，
    # 导致长时间运行的命令被误报为"转发连接异常"
    ws_timeout = timeout / 1000 + 10
    return await send_request("bash", params, timeout=ws_timeout, nest_name=nest_name)


async def read_file(
    file_path: str,
    offset: int = 1,
    limit: int = 2000,
    nest_name: str | None = None,
) -> NestResponse:
    """读取文件"""
    return await send_request(
        "read",
        {
            "path": file_path,
            "offset": offset,
            "limit": limit,
        },
        nest_name=nest_name,
    )


async def write_file(
    file_path: str,
    content: str,
    nest_name: str | None = None,
) -> NestResponse:
    """写入文件"""
    return await send_request(
        "write",
        {
            "path": file_path,
            "content": content,
        },
        nest_name=nest_name,
    )


async def write_bytes(
    file_path: str,
    data_b64: str,
    mode: str = "create",
    mtime: float | None = None,
    nest_name: str | None = None,
) -> NestResponse:
    """分块写入二进制文件（base64，nest 端二进制安全写入）"""
    params: dict[str, Any] = {"path": file_path, "data_b64": data_b64, "mode": mode}
    if mtime is not None:
        params["mtime"] = mtime
    return await send_request("write_bytes", params, nest_name=nest_name)


async def read_bytes(
    file_path: str,
    offset: int = 0,
    chunk_size: int = 1048576,
    nest_name: str | None = None,
) -> NestResponse:
    """字节级分块读取文件（base64 返回，二进制安全）"""
    return await send_request(
        "read_bytes",
        {
            "path": file_path,
            "offset": offset,
            "chunk_size": chunk_size,
            "encoding": "base64",
        },
        nest_name=nest_name,
    )


async def list_tree(path: str, nest_name: str | None = None) -> NestResponse:
    """递归列出目录树（sync 同步用）"""
    return await send_request("list_tree", {"path": path}, nest_name=nest_name)


async def delete_path(path: str, nest_name: str | None = None) -> NestResponse:
    """删除文件或目录（sync --delete 用）"""
    return await send_request("delete", {"path": path}, nest_name=nest_name)


async def edit_file(
    file_path: str,
    old_string: str,
    new_string: str,
    replace_all: bool = False,
    nest_name: str | None = None,
) -> NestResponse:
    """编辑文件"""
    params = {
        "path": file_path,
        "oldString": old_string,
        "newString": new_string,
    }
    if replace_all:
        params["replace_all"] = True
    return await send_request("edit", params, nest_name=nest_name)


async def line_edit_file(
    file_path: str,
    operation: str,
    start_line: int | None = None,
    end_line: int | None = None,
    line: int | None = None,
    content: str | None = None,
    position: str = "after",
    nest_name: str | None = None,
) -> NestResponse:
    """按行编辑文件"""
    params: dict[str, Any] = {
        "path": file_path,
        "operation": operation,
    }
    if start_line is not None:
        params["start_line"] = start_line
    if end_line is not None:
        params["end_line"] = end_line
    if line is not None:
        params["line"] = line
    if content is not None:
        params["content"] = content
    if position != "after":
        params["position"] = position
    return await send_request("line_edit", params, nest_name=nest_name)


async def glob(
    pattern: str,
    path: str | None = None,
    nest_name: str | None = None,
) -> NestResponse:
    """Glob 搜索"""
    params: dict[str, Any] = {"pattern": pattern}
    if path:
        params["path"] = path
    return await send_request("glob", params, nest_name=nest_name)


async def grep(
    pattern: str,
    path: str | None = None,
    glob: str | None = None,
    ignore_case: bool = False,
    max_matches: int = 100,
    nest_name: str | None = None,
) -> NestResponse:
    """Grep 搜索"""
    params: dict[str, Any] = {"pattern": pattern}
    if path:
        params["path"] = path
    if glob:
        params["glob"] = glob
    if ignore_case:
        params["ignore_case"] = ignore_case
    if max_matches != 100:
        params["max_matches"] = max_matches
    return await send_request("grep", params, nest_name=nest_name)


async def list_windows(query: str = "", nest_name: str | None = None) -> NestResponse:
    """列出窗口"""
    params: dict[str, Any] = {}
    if query:
        params["query"] = query
    return await send_request("list_windows", params, nest_name=nest_name)


async def screenshot(
    window: str | None = None,
    region: str | None = None,
    fmt: str = "png",
    nest_name: str | None = None,
) -> NestResponse:
    """截屏"""
    params: dict[str, Any] = {"format": fmt}
    if window:
        params["window"] = window
    if region:
        params["region"] = region
    return await send_request("screenshot", params, nest_name=nest_name)


async def input_action(
    action_type: str,
    params: dict[str, Any],
    nest_name: str | None = None,
) -> NestResponse:
    """输入操作"""
    params["action_type"] = action_type
    return await send_request("input", params, nest_name=nest_name)


async def python_exec(
    code: str,
    nest_name: str | None = None,
    timeout: float = 30.0,
) -> NestResponse:
    """在 nest 端用 exec 执行 Python 语句（修改状态）"""
    return await send_request("python_exec", {"code": code}, timeout=timeout, nest_name=nest_name)


async def python_eval(
    code: str,
    nest_name: str | None = None,
    timeout: float = 30.0,
) -> NestResponse:
    """在 nest 端用 eval 执行 Python 表达式（查询状态）"""
    return await send_request("python_eval", {"code": code}, timeout=timeout, nest_name=nest_name)


# ==================== 初始化 ====================


def init_nest_clients() -> None:
    """初始化 nest 客户端（从配置读取默认 nest）"""
    try:
        from peng.config.loader import load_config

        config = load_config()
        nest_config = getattr(config, "nest", None)
        if nest_config:
            default = getattr(nest_config, "default_nest", None)
            if default:
                set_default_nest(default)
                logger.info(f"默认 nest 设置为: {default}")
    except Exception as e:
        logger.debug(f"初始化 nest 客户端配置失败（可忽略）: {e}")


# ==================== 兼容旧 NestClient 接口 ====================


class _NestClientCompat:
    """
    兼容旧 NestClient 接口的包装器

    旧代码: client = get_nest_client(); await client.bash(...)
    返回 NestResponse，tools 用 result.data.get() 访问
    但部分旧代码用 result.get()，所以提供 __getitem__/get 兼容

    支持通过 set_nest_name() 指定目标 nest，由工具层通过 get_context('nest') 设置。
    """

    def __init__(self):
        self._nest_name: str | None = None
        self._task_id: int | None = None

    def set_nest_name(self, name: str | None) -> None:
        """设置目标 nest 名称（由工具层通过 get_context('nest') 设置）"""
        self._nest_name = name

    def set_task_id(self, task_id: int | None) -> None:
        """设置当前 task_id（由工具层通过 get_context('task_id') 设置，用于重试保活）"""
        self._task_id = task_id
        set_current_task_id(task_id)

    @property
    def connected(self) -> bool:
        from peng.nest.bridge import NestBridgeManager

        manager = NestBridgeManager.get_instance()
        if self._nest_name:
            return manager.get_connection(self._nest_name) is not None
        return manager.get_default_connection() is not None

    async def connect(self) -> bool:
        return self.connected

    async def send_request(
        self, request_type: str, params: dict = None, timeout: float = 120.0
    ) -> NestResponse:
        return await send_request(request_type, params or {}, timeout, nest_name=self._nest_name)

    async def ping(self) -> dict:
        return await ping(nest_name=self._nest_name)

    async def bash(
        self, command: str, cwd: str = None, timeout: int = 120000, env: dict = None, **kw
    ) -> dict:
        resp = await bash(command, cwd, timeout, env, nest_name=self._nest_name)
        if resp.error:
            raise NestError(resp.error)
        return resp.data if resp and resp.data else {}

    async def read_file(
        self, file_path: str, offset: int = 1, limit: int = 2000, **kw
    ) -> NestResponse:
        return await read_file(file_path, offset, limit, nest_name=self._nest_name)

    async def read(self, file_path: str, offset: int = 1, limit: int = 2000, **kw) -> NestResponse:
        """read_file 的别名，供工具层调用"""
        return await self.read_file(file_path, offset, limit, **kw)

    async def write_file(self, file_path: str, content: str, **kw) -> NestResponse:
        return await write_file(file_path, content, nest_name=self._nest_name)

    async def write_bytes(
        self,
        file_path: str,
        data_b64: str,
        mode: str = "create",
        mtime: float | None = None,
        **kw,
    ) -> NestResponse:
        """分块写入二进制文件（base64）"""
        return await write_bytes(file_path, data_b64, mode=mode, mtime=mtime, nest_name=self._nest_name)

    async def read_bytes(
        self, file_path: str, offset: int = 0, chunk_size: int = 1048576, **kw
    ) -> NestResponse:
        """字节级分块读取文件（base64 返回）"""
        return await read_bytes(file_path, offset, chunk_size, nest_name=self._nest_name)

    async def list_tree(self, path: str, **kw) -> NestResponse:
        """递归列出目录树（sync 同步用）"""
        return await list_tree(path, nest_name=self._nest_name)

    async def delete_path(self, path: str, **kw) -> NestResponse:
        """删除文件或目录（sync --delete 用）"""
        return await delete_path(path, nest_name=self._nest_name)

    async def write(self, file_path: str, content: str, **kw) -> NestResponse:
        """write_file 的别名，供工具层调用"""
        return await self.write_file(file_path, content, **kw)

    async def edit_file(
        self, file_path: str, old_string: str, new_string: str, replace_all: bool = False, **kw
    ) -> NestResponse:
        return await edit_file(
            file_path, old_string, new_string, replace_all, nest_name=self._nest_name
        )

    async def edit(
        self, file_path: str, old_string: str, new_string: str, replace_all: bool = False, **kw
    ) -> NestResponse:
        """edit_file 的别名，供工具层调用"""
        return await self.edit_file(file_path, old_string, new_string, replace_all, **kw)

    async def line_edit(
        self, file_path: str, operation: str, **kw
    ) -> NestResponse:
        """\u6309\u884c\u7f16\u8f91\u6587\u4ef6\uff08\u5220\u9664/\u66ff\u6362/\u63d2\u5165\uff09\uff0c\u4f9b\u5de5\u5177\u5c42\u8c03\u7528"""
        return await line_edit_file(file_path, operation, nest_name=self._nest_name, **kw)

    async def glob(self, pattern: str, path: str = None, **kw) -> NestResponse:
        return await glob(pattern, path, nest_name=self._nest_name)

    async def grep(
        self, pattern: str, path: str = None, glob: str = None, ignore_case: bool = False, **kw
    ) -> NestResponse:
        return await grep(pattern, path, glob, ignore_case, nest_name=self._nest_name)

    async def list_windows(self, query: str = "", **kw) -> NestResponse:
        return await list_windows(query, nest_name=self._nest_name)

    async def screenshot(
        self, window: str = None, region: str = None, fmt: str = "png", save_path: str = None, **kw
    ) -> NestResponse:
        """截屏 - 支持指定 save_path 让 nest 端保存文件"""
        params: dict[str, Any] = {"format": fmt}
        if window:
            params["window"] = window
        if region:
            params["region"] = region
        if save_path:
            params["save_path"] = save_path
        return await send_request("screenshot", params, nest_name=self._nest_name)

    async def input_action(self, action_type: str, params: dict = None, **kw) -> NestResponse:
        return await input_action(action_type, params or {}, nest_name=self._nest_name)

    async def send_input(
        self, actions: list = None, window: str = None, activate: bool = False, **kw
    ) -> NestResponse:
        """send_input - 供 screen.py InputTool 调用"""
        params: dict[str, Any] = {}
        if actions:
            params["actions"] = actions
        if window:
            params["window"] = window
        if activate:
            params["activate"] = activate
        return await send_request("input", params, nest_name=self._nest_name)

    async def python_exec(self, code: str, **kw) -> NestResponse:
        """在 nest 端用 exec 执行 Python 语句"""
        return await python_exec(code, nest_name=self._nest_name)

    async def python_eval(self, code: str, **kw) -> NestResponse:
        """在 nest 端用 eval 执行 Python 表达式"""
        return await python_eval(code, nest_name=self._nest_name)


def get_nest_client():
    """获取 nest 客户端（兼容旧接口）"""
    return _NestClientCompat()
