"""
消息处理子系统

提供统一的消息处理框架，包括：
- 消息数据库：消息的持久化存储和管理
- Inbox监控器：检测新消息并通知处理
- 消息处理器：定义消息处理的标准接口
- 回复链管理：消息回复链的构建和上下文管理
- 调用链追踪：记录消息的调用来源

这个子系统的设计目标：
1. 与tools/chat保持架构一致（使用进程而非线程）
2. 避免线程泄漏问题
3. 提供统一的消息处理接口
4. 支持防冲突、超时处理、进度跟踪
5. 支持回复链管理和调用链追踪
"""

from .database import MessageDatabase
from .inbox_monitor import InboxMonitor
from .manager import MessageManager
from .types import (AssistantMessage, BaseMessage,  # Agent 对话消息; 消息数据类; 类型别名
                    ChainMessage, HeartbeatCallback, MessageCallback,
                    MessageHandler, MessageInfo, MessageRecord,
                    MessageRole, MessageStatus, ProcessInfo, ProcessingResult,
                    ProcessState, SystemMessage, ToolResultMessage,
                    UserMessage)

__all__ = [
    # 枚举
    "MessageRole",
    "MessageStatus",
    "ProcessState",
    # 消息数据类
    "ChainMessage",
    "MessageInfo",
    "MessageRecord",
    "ProcessInfo",
    "ProcessingResult",
    # Agent 对话消息
    "AssistantMessage",
    "BaseMessage",
    "SystemMessage",
    "ToolResultMessage",
    "UserMessage",
    # 类型别名
    "HeartbeatCallback",
    "MessageCallback",
    "MessageHandler",
    # 消息数据库
    "MessageDatabase",
    # 消息管理器
    "MessageManager",
    # Inbox监控器
    "InboxMonitor",
    # 显示格式化
    "Colors",
    "format_status_icon",
    # 消息处理器
    "MessageProcessor",
    "DefaultMessageProcessor",
    "SubprocessMessageProcessor",
    # 常量
    "DEFAULT_CHECK_INTERVAL",
    "DEFAULT_MSG_TIMEOUT",
]
