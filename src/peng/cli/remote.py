"""
CLI 远程模式支持模块

bin/task 和 bin/msg 共享的远程模式检测和通信逻辑。

连接优先级（由调用方控制）：
1. 本地 peng service（localhost:PENG_PORT）→ 本地模式直接执行
2. config/nest.json 中默认 nest 的 host → 通过 WebSocket 远程执行

远程模式通信流程：
1. 读取 config/nest.json 的 default 字段获取默认 nest
2. 从默认 nest 配置中获取 host 和 port
3. 通过 WebSocket 连接到 host:port（超时3秒）
4. 发送 cli_command 请求，传递命令和参数
5. 接收结果，输出到 stdout/stderr，返回 exitcode

本地模式（默认）：
- 本地服务运行中 → 本地模式直接执行
- --local 参数强制本地模式
- 无本地服务且无远程 nest → 报错退出
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from typing import Any


def _get_project_root() -> Path:
    """获取项目根目录"""
    return Path(__file__).resolve().parent.parent.parent.parent


def _load_nest_config() -> dict:
    """加载 config/nest.json 配置"""
    config_path = _get_project_root() / "config" / "nest.json"
    if not config_path.exists():
        return {}
    try:
        with open(config_path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def is_local_service_running(timeout: float = 1.0) -> bool:
    """检测本地 peng service 是否在运行
    
    通过尝试连接 localhost:PENG_PORT (默认6688) 来判断。
    
    Returns:
        True 表示本地 service 正在运行，False 表示未运行
    """
    import os
    import socket
    
    port = int(os.environ.get("PENG_PORT", "6688"))
    try:
        with socket.create_connection(("localhost", port), timeout=timeout):
            return True
    except (socket.timeout, ConnectionRefusedError, OSError):
        return False


def get_self_nest_name() -> str | None:
    """获取本机 nest 身份标识
    
    优先级：
    1. config/nest.json 的 "self" 字段（nest install 时写入）
    2. 如果 self 缺失，从 instances 中推断：
       a. 如果只有一个 instance → 它就是当前 nest
       b. 如果有 instance 且其 host 解析为本机 → 该 instance 就是当前 nest
       c. 如果有 default 且其 host 解析为本机 → default 就是当前 nest
    
    用于 bin/msg 等本机 CLI 工具在非 local nest 上运行时正确设置 nest 字段。
    
    Returns:
        本机 nest 名称（如 "wsl"、"local" 等），无配置时返回 None
    """
    config = _load_nest_config()
    
    # 优先使用显式的 self 字段
    self_name = config.get("self")
    if self_name:
        return self_name
    
    # self 字段缺失，尝试从 instances 推断
    instances = config.get("instances", {})
    if not instances:
        return None
    
    # 如果只有一个 instance，它很可能就是当前 nest
    if len(instances) == 1:
        return next(iter(instances))
    
    # 多个 instance：查找 host 解析为本机的 instance
    for name, inst in instances.items():
        host = inst.get("host")
        if host and is_nest_host_local(host):
            return name
    
    # 都不匹配，返回 None
    return None


def get_default_nest_name() -> str | None:
    """获取默认 nest 的名称
    
    Returns:
        默认 nest 名称 或 None（无默认配置时）
    """
    config = _load_nest_config()
    default_name = config.get("default")
    if not default_name:
        return None
    
    instances = config.get("instances", {})
    instance = instances.get(default_name)
    if not instance:
        return None
    
    host = instance.get("host")
    if not host:
        return None
    
    return default_name


def is_nest_host_local(host: str) -> bool:
    """检测给定的 host 是否解析为本机地址
    
    通过解析 host 的 IP 地址，与本机所有网络接口 IP 比较。
    支持以下情况：
    - localhost / 127.0.0.1 / ::1（回环地址）
    - 127.0.1.1（某些 Linux 发行版的本机地址）
    - 本机任意网卡的 IP 地址（如 192.168.x.x）
    
    Args:
        host: 主机名或 IP 地址
    
    Returns:
        True 表示 host 解析为本机，False 表示非本机
    """
    import socket
    
    if host in ("localhost", "127.0.0.1", "::1"):
        return True
    
    try:
        # 解析 host 的所有 IP 地址
        addr_infos = socket.getaddrinfo(host, None, socket.AF_UNSPEC, socket.SOCK_STREAM)
        host_ips = {addr[4][0] for addr in addr_infos}
    except socket.gaierror:
        return False
    
    # 获取本机所有网络接口的 IP 地址
    local_ips = set()
    
    # 回环地址
    local_ips.add("127.0.0.1")
    local_ips.add("::1")
    
    # 通过 hostname 获取本机 IP
    try:
        local_hostname = socket.gethostname()
        addr_infos = socket.getaddrinfo(local_hostname, None, socket.AF_UNSPEC, socket.SOCK_STREAM)
        for addr in addr_infos:
            local_ips.add(addr[4][0])
    except socket.gaierror:
        pass
    
    # 通过 UDP socket 获取本机出口 IP（不实际发送数据）
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        local_ips.add(s.getsockname()[0])
        s.close()
    except Exception:
        pass
    
    # IPv6 同理
    try:
        s = socket.socket(socket.AF_INET6, socket.SOCK_DGRAM)
        s.connect(("2001:4860:4860::8888", 80))
        local_ips.add(s.getsockname()[0])
        s.close()
    except Exception:
        pass
    
    return bool(host_ips & local_ips)


def is_default_nest_local() -> bool:
    """检测默认 nest 的 host 是否为本机
    
    Returns:
        True 表示默认 nest 的 host 解析为本机，False 表示非本机或无默认 nest
    """
    host_port = get_default_nest_host_port()
    if not host_port:
        return False
    return is_nest_host_local(host_port[0])


def get_default_nest_host_port() -> tuple[str, int] | None:
    """获取默认 nest 的 host:port
    
    Returns:
        (host, port) 或 None（无默认配置时）
    """
    config = _load_nest_config()
    default_name = config.get("default")
    if not default_name:
        return None
    
    instances = config.get("instances", {})
    instance = instances.get(default_name)
    if not instance:
        return None
    
    host = instance.get("host")
    if not host:
        return None
    
    port = instance.get("port", 8899)
    return (host, port)


async def _try_connect_and_execute(command: str, args: list[str], host: str, port: int, timeout: float = 1.0) -> dict[str, Any] | None:
    """尝试连接远程 peng service 并执行 CLI 命令
    
    Args:
        command: "task" 或 "msg"
        args: 命令参数列表
        host: peng service 地址
        port: peng service 端口
        timeout: 连接超时（秒）
    
    Returns:
        returncode（int，执行成功时）或 None（连接失败时）
    """
    try:
        import websockets
    except ImportError:
        return None
    
    uri = f"ws://{host}:{port}"
    
    try:
        # 使用 async with 管理连接生命周期（websockets 13.x 要求）
        # 连接超时通过 asyncio.wait_for 包裹 connect() 协程实现
        ws = await asyncio.wait_for(
            websockets.connect(uri, max_size=50*1024*1024).__aenter__(),
            timeout=timeout,
        )
    except (asyncio.TimeoutError, Exception):
        return None
    
    try:
        # 发送 CLI 命令请求
        request = {
            "type": "cli_command",
            "command": command,
            "args": args,
        }
        await ws.send(json.dumps(request, ensure_ascii=True))
        
        # 流式接收响应：stream 消息实时打印，result/error 消息为最终结果
        returncode = 1
        while True:
            raw = await asyncio.wait_for(ws.recv(), timeout=300)
            response = json.loads(raw)
            msg_type = response.get("type")
            
            if msg_type == "stream":
                # 实时输出到 stdout/stderr
                data = response.get("data", "")
                output_type = response.get("output_type", "stdout")
                if data:
                    if output_type == "stderr":
                        _write_output(sys.stderr, data)
                    else:
                        _write_output(sys.stdout, data)
            elif msg_type == "result":
                returncode = response.get("returncode", 1)
                break
            elif msg_type == "error":
                _write_output(sys.stderr, response.get("error", "未知错误") + "\n")
                returncode = 1
                break
            else:
                # 兼容旧版：非流式响应
                if "stdout" in response:
                    _write_output(sys.stdout, response.get("stdout", ""))
                if "stderr" in response:
                    _write_output(sys.stderr, response.get("stderr", ""))
                returncode = response.get("returncode", 1)
                break
        
        return returncode
    except (asyncio.TimeoutError, Exception):
        return None
    finally:
        try:
            await ws.__aexit__(None, None, None)
        except Exception:
            pass


def should_use_remote(command: str, args: list[str]) -> int | None:
    """判断是否应使用远程模式，并执行远程连接。
    
    连接优先级：
    1. 本地 peng service (localhost:PENG_PORT) → 本地模式，返回 None
    2. config/nest.json 中默认 nest 的 host → 通过 WebSocket 远程执行
    3. 两者都不可用 → 报错退出
    
    Args:
        command: "task" 或 "msg"
        args: 命令参数列表（含 --local 时视为强制本地）
    
    Returns:
        exitcode（远程模式执行成功时）或 None（应使用本地模式时）
    """
    force_local = "--local" in args
    clean_args = [a for a in args if a != "--local"]
    
    # 强制本地模式
    if force_local:
        return None
    
    # 优先检测本地服务
    if is_local_service_running():
        return None
    
    # 无本地服务，尝试远程模式
    host_port = get_default_nest_host_port()
    if not host_port:
        print("无本地服务，且无默认nest配置。", file=sys.stderr)
        sys.exit(1)
    
    host, port = host_port
    
    # 尝试通过 WebSocket 连接远程 peng service
    try:
        result = asyncio.run(_try_connect_and_execute(command, clean_args, host, port))
    except Exception:
        result = None
    
    if result is not None:
        # 流式模式下输出已实时打印，result 就是 returncode
        return result
    
    # 远程连接也失败
    print(f"无本地服务，且无法连接默认nest({host}:{port})。", file=sys.stderr)
    sys.exit(1)


def _write_output(stream, data: str) -> None:
    """安全输出到指定流"""
    try:
        stream.write(data)
        stream.flush()
    except UnicodeEncodeError:
        try:
            stream.buffer.write(data.encode('utf-8'))
            stream.flush()
        except Exception:
            pass


def try_remote_mode(command: str, args: list[str], force_local: bool = False) -> int | None:
    """尝试远程模式执行 CLI 命令
    
    Args:
        command: "task" 或 "msg"
        args: 命令参数列表（不含 --local）
        force_local: 是否强制本地模式（--local 参数）
    
    Returns:
        exitcode（远程模式执行成功时）或 None（应使用本地模式时）
    """
    if force_local:
        return None
    
    host_port = get_default_nest_host_port()
    if not host_port:
        return None
    
    host, port = host_port
    
    # 注意：is_nest_host_local 检查在旧的"远程优先"模式下用于避免重复本地执行。
    # 在新的"本地优先"模式下，调用方已确认无本地服务，因此这里不再跳过。
    # 但 msg 命令仍需要通过远程模式获取 NestBridgeManager 中的 nest workdir。
    
    try:
        result = asyncio.run(_try_connect_and_execute(command, args, host, port))
    except Exception:
        return None
    
    if result is None:
        # 连接失败，静默回退到本地模式
        return None
    
    # 流式模式下输出已实时打印，result 就是 returncode
    return result
