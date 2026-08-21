"""
Channel 抽象基类

定义所有消息通道的标准接口，包括：
- 启动/停止
- 发送消息
- 接收消息回调
- 连接状态检查
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable

logger = logging.getLogger(__name__)

__all__ = [
    "ChannelType",
    "ChannelMessage",
    "ChannelBase",
    "ChannelStatus",
]


class ChannelType(Enum):
    """通道类型枚举"""

    WECHAT = "wechat"
    FEISHU = "feishu"


class ChannelStatus(Enum):
    """通道状态"""

    DISCONNECTED = "disconnected"
    CONNECTING = "connecting"
    CONNECTED = "connected"
    ERROR = "error"


@dataclass
class ChannelMessage:
    """通道消息（统一格式）

    用于在各Channel和MessageManager之间传递消息。
    """

    # 消息内容
    content: str
    # 发送者ID（平台相关：wxid / open_id）
    sender_id: str
    # 发送者名称（可选）
    sender_name: str = ""
    # 接收者ID（可选，群聊时为群ID）
    receiver_id: str = ""
    # 消息类型（text/image/file等）
    msg_type: str = "text"
    # 平台原始消息数据
    raw_data: dict[str, Any] = field(default_factory=dict)
    # 所属通道类型
    channel_type: ChannelType = ChannelType.WECHAT
    # 平台消息ID
    platform_msg_id: str = ""
    # 是否群消息
    is_group: bool = False
    # 群名（如果是群消息）
    group_name: str = ""
    # @机器人列表（群聊中被@的用户ID列表）
    at_users: list[str] = field(default_factory=list)
    # 回复消息ID（如果这条消息是回复某条消息）
    reply_to_platform_msg_id: str = ""

    def to_dict(self) -> dict[str, Any]:
        """转换为字典"""
        return {
            "content": self.content,
            "sender_id": self.sender_id,
            "sender_name": self.sender_name,
            "receiver_id": self.receiver_id,
            "msg_type": self.msg_type,
            "channel_type": self.channel_type.value,
            "platform_msg_id": self.platform_msg_id,
            "is_group": self.is_group,
            "group_name": self.group_name,
            "at_users": self.at_users,
        }


class ChannelBase(ABC):
    """消息通道抽象基类

    所有Channel实现必须继承此类并实现所有抽象方法。

    使用流程：
    1. 创建Channel实例
    2. 注册消息回调（on_message）
    3. 启动Channel（start）
    4. 发送/接收消息
    5. 停止Channel（stop）
    """

    def __init__(self, channel_type: ChannelType, config: dict[str, Any]) -> None:
        """
        Args:
            channel_type: 通道类型
            config: 通道配置（从peng.json的channels节点读取）
        """
        self.channel_type = channel_type
        self.config = config
        self._status = ChannelStatus.DISCONNECTED
        self._on_message: Callable[[ChannelMessage], None] | None = None
        self._logger = logging.getLogger(f"peng.channels.{channel_type.value}")

    @property
    def status(self) -> ChannelStatus:
        """获取通道状态"""
        return self._status

    @property
    def is_connected(self) -> bool:
        """是否已连接"""
        return self._status == ChannelStatus.CONNECTED

    def set_message_callback(self, callback: Callable[[ChannelMessage], None]) -> None:
        """设置消息接收回调

        当从平台收到消息时，调用此回调函数。

        Args:
            callback: 消息回调函数，接收ChannelMessage参数
        """
        self._on_message = callback

    def _notify_message(self, message: ChannelMessage) -> None:
        """通知收到新消息（内部方法）

        子类收到平台消息后调用此方法触发回调。
        """
        if self._on_message:
            try:
                self._on_message(message)
            except Exception as e:
                self._logger.error(f"消息回调执行失败: {e}", exc_info=True)
        else:
            self._logger.warning(f"收到消息但无回调注册: {message.content[:50]}...")

    @abstractmethod
    async def start(self) -> None:
        """启动通道

        初始化连接、注册回调等。
        启动成功后 status 应为 CONNECTED。
        """

    @abstractmethod
    async def stop(self) -> None:
        """停止通道

        断开连接、清理资源等。
        """

    @abstractmethod
    async def send_message(
        self,
        receiver_id: str,
        content: str,
        msg_type: str = "text",
        **kwargs: Any,
    ) -> bool:
        """发送消息

        Args:
            receiver_id: 接收者ID（wxid / open_id / chat_id）
            content: 消息内容
            msg_type: 消息类型（text/image/file等）
            **kwargs: 额外参数

        Returns:
            True 表示发送成功，False 表示失败
        """

    @abstractmethod
    async def check_connection(self) -> bool:
        """检查连接状态

        Returns:
            True 表示连接正常，False 表示连接异常
        """

    def get_status_info(self) -> dict[str, Any]:
        """获取通道状态信息"""
        return {
            "channel_type": self.channel_type.value,
            "status": self._status.value,
            "is_connected": self.is_connected,
        }
