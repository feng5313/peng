"""
Nest（鹏窝）- 后台控制服务模块

架构：nest 主动连接 peng 的 /ws/nest 端点，peng 通过已建立的连接发送命令。

模块结构：
- protocol.py: 消息协议定义
- security.py: 安全控制
- local.py: 本地执行器（nest 端）
- server.py: NestConnector - nest 端 WebSocket 客户端（主动连接 + 自动重连）
- bridge.py: NestBridgeManager - peng 端管理已连接的 nest
- client.py: 便捷方法 - peng 端 tools 通过 bridge 发送命令
- cli.py: 命令行界面
"""

from peng.nest.protocol import (
    NestRequest,
    NestResponse,
    NestError,
    RequestType,
    ResponseType,
)
from peng.nest.security import NestSecurity

__all__ = [
    "NestRequest",
    "NestResponse",
    "NestError",
    "RequestType",
    "ResponseType",
    "NestSecurity",
]
