"""
Channel 管理器

管理所有消息通道（微信、飞书等），提供统一的消息收发接口。

职责：
1. 管理多个Channel实例的生命周期
2. 将Channel收到的消息转发到MessageManager
3. 将MessageManager的回复通过对应Channel发送
4. 提供统一的启停和状态查询接口
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from peng.channels.base import ChannelBase, ChannelMessage, ChannelStatus, ChannelType

logger = logging.getLogger(__name__)

__all__ = ["ChannelManager"]


class ChannelManager:
    """通道管理器

    统一管理所有消息通道，提供：
    - 通道注册和生命周期管理
    - 消息路由（Channel → MessageManager → Channel）
    - 状态查询
    """

    def __init__(self) -> None:
        self._channels: dict[ChannelType, ChannelBase] = {}
        self._on_message_callback: Any = None  # 外部消息回调（MessageManager注册）
        self._started = False

    def register_channel(self, channel: ChannelBase) -> None:
        """注册通道

        Args:
            channel: Channel实例
        """
        self._channels[channel.channel_type] = channel
        # 注册消息回调到该Channel
        channel.set_message_callback(self._handle_channel_message)
        logger.info(f"已注册通道: {channel.channel_type.value}")

    def unregister_channel(self, channel_type: ChannelType) -> None:
        """注销通道

        Args:
            channel_type: 通道类型
        """
        if channel_type in self._channels:
            del self._channels[channel_type]
            logger.info(f"已注销通道: {channel_type.value}")

    def set_message_callback(self, callback: Any) -> None:
        """设置消息回调（由MessageManager注册）

        当从任何Channel收到消息时，调用此回调。

        Args:
            callback: 回调函数，接收ChannelMessage参数
        """
        self._on_message_callback = callback

    def _handle_channel_message(self, message: ChannelMessage) -> None:
        """处理Channel收到的消息

        由各Channel内部调用，将消息转发到外部回调（MessageManager）。
        """
        logger.info(
            f"收到[{message.channel_type.value}]消息: "
            f"sender={message.sender_name or message.sender_id}, "
            f"is_group={message.is_group}, "
            f"content={message.content[:50]}..."
        )

        if self._on_message_callback:
            try:
                self._on_message_callback(message)
            except Exception as e:
                logger.error(f"消息回调执行失败: {e}", exc_info=True)
        else:
            logger.warning("收到消息但无外部回调注册")

    async def start_all(self) -> None:
        """启动所有已注册的通道"""
        if self._started:
            logger.warning("ChannelManager已在运行")
            return

        logger.info(f"正在启动所有通道 ({len(self._channels)}个)...")

        for channel_type, channel in self._channels.items():
            try:
                await channel.start()
                # 检查通道实际状态（start()可能内部设ERROR但不抛异常）
                if channel.status in (ChannelStatus.CONNECTED, ChannelStatus.CONNECTING):
                    logger.info(f"✅ 通道 {channel_type.value} 启动成功 (status={channel.status.value})")
                else:
                    logger.error(
                        f"❌ 通道 {channel_type.value} 启动失败: status={channel.status.value}"
                    )
            except Exception as e:
                logger.error(f"❌ 通道 {channel_type.value} 启动失败: {e}", exc_info=True)

        self._started = True
        logger.info("所有通道启动完成")

    async def stop_all(self) -> None:
        """停止所有已注册的通道"""
        if not self._started:
            return

        logger.info("正在停止所有通道...")

        for channel_type, channel in self._channels.items():
            try:
                await channel.stop()
                logger.info(f"✅ 通道 {channel_type.value} 已停止")
            except Exception as e:
                logger.error(f"❌ 通道 {channel_type.value} 停止失败: {e}", exc_info=True)

        self._started = False
        logger.info("所有通道已停止")

    async def start_channel(self, channel_type: ChannelType) -> None:
        """启动指定通道"""
        if channel_type not in self._channels:
            raise ValueError(f"未注册的通道类型: {channel_type.value}")

        channel = self._channels[channel_type]
        await channel.start()

    async def stop_channel(self, channel_type: ChannelType) -> None:
        """停止指定通道"""
        if channel_type not in self._channels:
            raise ValueError(f"未注册的通道类型: {channel_type.value}")

        channel = self._channels[channel_type]
        await channel.stop()

    async def send_message(
        self,
        channel_type: ChannelType,
        receiver_id: str,
        content: str,
        msg_type: str = "text",
        **kwargs: Any,
    ) -> bool:
        """通过指定通道发送消息

        Args:
            channel_type: 通道类型
            receiver_id: 接收者ID
            content: 消息内容
            msg_type: 消息类型
            **kwargs: 额外参数

        Returns:
            True 表示发送成功
        """
        if channel_type not in self._channels:
            logger.error(f"未注册的通道类型: {channel_type.value}")
            return False

        channel = self._channels[channel_type]
        if not channel.is_connected:
            logger.error(f"通道 {channel_type.value} 未连接")
            return False

        try:
            return await channel.send_message(receiver_id, content, msg_type, **kwargs)
        except Exception as e:
            logger.error(
                f"发送消息失败 [channel={channel_type.value}, "
                f"receiver={receiver_id}]: {e}",
                exc_info=True,
            )
            return False

    async def send_message_auto(
        self,
        receiver_id: str,
        content: str,
        preferred_channel: ChannelType | None = None,
        msg_type: str = "text",
        **kwargs: Any,
    ) -> bool:
        """自动选择通道发送消息

        优先使用preferred_channel，如果不可用则尝试其他通道。

        Args:
            receiver_id: 接收者ID
            content: 消息内容
            preferred_channel: 首选通道（可选）
            msg_type: 消息类型
            **kwargs: 额外参数

        Returns:
            True 表示发送成功
        """
        # 如果指定了首选通道，优先尝试
        if preferred_channel and preferred_channel in self._channels:
            channel = self._channels[preferred_channel]
            if channel.is_connected:
                return await self.send_message(
                    preferred_channel, receiver_id, content, msg_type, **kwargs
                )

        # 尝试所有已连接的通道
        for channel_type, channel in self._channels.items():
            if channel.is_connected:
                try:
                    return await self.send_message(
                        channel_type, receiver_id, content, msg_type, **kwargs
                    )
                except Exception:
                    continue

        logger.error("无可用的消息通道")
        return False

    def get_channel(self, channel_type: ChannelType) -> ChannelBase | None:
        """获取指定类型的通道"""
        return self._channels.get(channel_type)

    def get_all_status(self) -> dict[str, dict[str, Any]]:
        """获取所有通道状态"""
        return {
            ct.value: channel.get_status_info()
            for ct, channel in self._channels.items()
        }

    @property
    def is_running(self) -> bool:
        """是否正在运行"""
        return self._started

    @property
    def active_channels(self) -> list[ChannelType]:
        """获取所有已连接的通道"""
        return [ct for ct, ch in self._channels.items() if ch.is_connected]
