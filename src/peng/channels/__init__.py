"""
Channel 消息通道模块

提供统一的多平台消息收发能力，包括：
- ChannelBase: 通道抽象基类
- ChannelManager: 通道管理器
- WechatChannel: 微信通道（Gewechat适配器）
- FeishuChannel: 飞书通道（飞书开放平台）

消息流向：
微信用户 → Gewechat回调 → WechatChannel → MessageManager → 处理/回复
飞书用户 → 飞书事件订阅 → FeishuChannel → MessageManager → 处理/回复
"""

from peng.channels.base import ChannelBase, ChannelMessage, ChannelStatus, ChannelType
from peng.channels.feishu import FeishuChannel
from peng.channels.integration import (
    channel_message_to_message_record,
    get_channel_manager,
    init_channel_system,
)
from peng.channels.manager import ChannelManager
from peng.channels.wechat import WechatChannel

__all__ = [
    # 基类
    "ChannelBase",
    "ChannelMessage",
    "ChannelStatus",
    "ChannelType",
    # Channel实现
    "FeishuChannel",
    "WechatChannel",
    # 管理器
    "ChannelManager",
    # 集成
    "init_channel_system",
    "get_channel_manager",
    "channel_message_to_message_record",
]
