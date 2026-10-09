"""
消息处理子系统 - 类型定义

定义消息处理相关的类型和数据结构，包括：
1. 消息状态和方向枚举
2. 进程状态枚举
3. 消息数据类（MessageInfo, MessageRecord）
4. Agent 对话消息类型（BaseMessage, UserMessage, AssistantMessage 等）
5. 回复链类型（MessageChain）

整合自：
- 原 types.py（消息处理类型）
- 原 messages.py（Agent 对话消息）
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any

from peng.config import get_agent_name, get_master_name

AGENT_NAME = get_agent_name()
MASTER_NAME = get_master_name()
MASTER_NAME_EMOJI = get_master_name()
__all__ = [
    # 枚举
    "MessageStatus",
    "ProcessState",
    "MessageRole",
    # 消息数据类
    "MessageInfo",
    "MessageRecord",
    "ProcessInfo",
    "ProcessingResult",
    # 回复链
    "ChainMessage",
    # 消息树
    "MessageTreeNode",
    "MessageTree",
    # Agent 对话消息
    "BaseMessage",
    "UserMessage",
    "AssistantMessage",
    "ToolResultMessage",
    "SystemMessage",
    # 类型别名
    "MessageHandler",
    "MessageCallback",
    "HeartbeatCallback",
]

# ==================== 消息状态枚举 ====================


class MessageStatus(Enum):
    """消息状态枚举

    状态流转：
    - unread -> read -> replied -> processing ->
      (accepting -> accepted | paused | interrupted | cancelled | failed) -> done
    - processing 可直接转为 done（无需验收的情况）
    """

    UNREAD = "unread"  # 未读
    READ = "read"  # 已读
    REPLIED = "replied"  # 已回复
    PROCESSING = "processing"  # 处理中
    ACCEPTING = "accepting"  # 验收中
    ACCEPTED = "accepted"  # 已验收
    PAUSED = "paused"  # 已暂停
    INTERRUPTED = "interrupted"  # 已中断
    CANCELLED = "cancelled"  # 已取消
    FAILED = "failed"  # 失败
    DONE = "done"  # 已完成
    ARCHIVED = "archived"  # 已归档

    @classmethod
    def terminal_states(cls) -> list[MessageStatus]:
        """获取终止状态列表"""
        return [cls.DONE, cls.FAILED, cls.CANCELLED, cls.ARCHIVED]

    @classmethod
    def active_states(cls) -> list[MessageStatus]:
        """获取活跃状态列表（需要处理的）"""
        return [cls.PROCESSING, cls.ACCEPTING]

    @classmethod
    def paused_states(cls) -> list[MessageStatus]:
        """获取可暂停状态列表"""
        return [cls.PROCESSING, cls.ACCEPTING, cls.PAUSED]

    def is_terminal(self) -> bool:
        """是否是终止状态"""
        return self in self.terminal_states()

    def is_active(self) -> bool:
        """是否是活跃状态"""
        return self in self.active_states()


class ProcessState(Enum):
    """进程状态枚举"""

    IDLE = "idle"
    RUNNING = "running"
    STOPPED = "stopped"
    ERROR = "error"


class MessageRole(Enum):
    """消息角色（Agent 对话）"""

    SYSTEM = "system"
    USER = "user"
    ASSISTANT = "assistant"
    TOOL = "tool"


# 向后兼容：从核心模块导入的 CallerType 已经可用
# class CallerType 已移动到 src/peng/types.py


# ==================== 消息数据类 ====================


@dataclass
class MessageInfo:
    """消息信息（简化版，用于处理器）"""

    msg_id: int
    content: str
    timestamp: str | None = None
    sender: str | None = None
    receiver: str | None = None
    status: MessageStatus = MessageStatus.UNREAD

    def __post_init__(self) -> None:
        """确保status是枚举类型"""
        if isinstance(self.status, str):
            self.status = MessageStatus(self.status)


@dataclass
class MessageRecord:
    """
    消息记录（完整版，与数据库字段对应）

    用于表示数据库中的消息记录，包含所有字段。
    """

    # 基本信息（必填字段）
    id: int
    timestamp: str
    sender: str
    receiver: str
    content: str
    created_at: str
    status: str = "unread"
    # 基本信息（可选字段，有默认值）
    title: str = ""

    # 可选时间戳
    read_at: str | None = None
    archived_at: str | None = None

    # 并发控制
    locked_by: int | None = None
    locked_at: str | None = None
    heartbeat_at: str | None = None
    process_id: int | None = None

    # 回复链
    reply_to: int | None = None
    chain_head_id: int | None = None
    chain_context: str | None = None

    # 调用链
    caller: str | None = None
    caller_session_id: str | None = None

    # 标记
    is_chain_head: int = 0
    is_cancelled: int = 0

    # 超时
    timeout: int = 0

    # 其他字段
    workdir: str = ""
    error: str | None = None
    is_paused: int = 0
    collapsed: int = 0
    is_archived: int = 0
    pending_delete: int = 0

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> MessageRecord:
        """从字典创建消息记录"""
        return cls(
            id=data.get("id", 0),
            timestamp=data.get("timestamp", ""),
            sender=data.get("sender", MASTER_NAME),
            receiver=data.get("receiver", AGENT_NAME),
            content=data.get("content", ""),
            title=data.get("title", ""),
            status=data.get("status", "unread"),
            created_at=data.get("created_at", ""),
            read_at=data.get("read_at"),
            archived_at=data.get("archived_at"),
            locked_by=data.get("locked_by"),
            locked_at=data.get("locked_at"),
            heartbeat_at=data.get("heartbeat_at"),
            process_id=data.get("process_id"),
            reply_to=data.get("reply_to"),
            chain_head_id=data.get("chain_head_id"),
            chain_context=data.get("chain_context"),
            caller=data.get("caller"),
            caller_session_id=data.get("caller_session_id"),
            is_chain_head=data.get("is_chain_head", 0) or 0,
            is_cancelled=data.get("is_cancelled", 0) or 0,
            timeout=data.get("timeout", 0) or 0,
            workdir=data.get("workdir", ""),
            error=data.get("error"),
            is_paused=data.get("is_paused", 0) or 0,
            collapsed=data.get("collapsed", 0) or 0,
            is_archived=data.get("is_archived", 0) or 0,
            pending_delete=data.get("pending_delete", 0) or 0,
        )


@dataclass
class ProcessInfo:
    """进程信息"""

    process_id: int
    message_id: int
    started_at: float = 0.0
    last_heartbeat: float = 0.0
    state: ProcessState = ProcessState.IDLE
    error: str | None = None


@dataclass
class ProcessingResult:
    """消息处理结果"""

    msg_id: int
    success: bool
    result: str
    error: str | None = None
    duration: float = 0.0


# ==================== 回复链类型 ====================


@dataclass
class ChainMessage:
    """回复链中的单条消息

    包含数据库中的所有字段，不进行过滤。
    """

    id: int
    timestamp: str
    sender: str
    receiver: str
    content: str
    title: str = ""
    workdir: str = ""
    status: str = "unread"
    created_at: str = ""
    read_at: str | None = None
    archived_at: str | None = None
    locked_by: int | None = None
    locked_at: str | None = None
    heartbeat_at: str | None = None
    process_id: int | None = None
    reply_to: int | None = None
    chain_head_id: int | None = None
    chain_context: str | None = None
    error: str | None = None
    caller: str | None = None
    caller_session_id: str | None = None
    is_chain_head: int = 0
    is_cancelled: int = 0
    timeout: int = 0
    is_paused: int = 0
    collapsed: int = 0
    is_archived: int = 0
    pending_delete: int = 0
    nest: str = ""

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ChainMessage:
        """从字典创建（提取所有字段）"""
        return cls(
            id=data.get("id", 0),
            timestamp=data.get("timestamp", ""),
            sender=data.get("sender", MASTER_NAME),
            receiver=data.get("receiver", AGENT_NAME),
            content=data.get("content", ""),
            title=data.get("title", ""),
            workdir=data.get("workdir", ""),
            status=data.get("status", "unread"),
            created_at=data.get("created_at", ""),
            read_at=data.get("read_at"),
            archived_at=data.get("archived_at"),
            locked_by=data.get("locked_by"),
            locked_at=data.get("locked_at"),
            heartbeat_at=data.get("heartbeat_at"),
            process_id=data.get("process_id"),
            reply_to=data.get("reply_to"),
            chain_head_id=data.get("chain_head_id"),
            chain_context=data.get("chain_context"),
            error=data.get("error"),
            caller=data.get("caller"),
            caller_session_id=data.get("caller_session_id"),
            is_chain_head=data.get("is_chain_head", 0) or 0,
            is_cancelled=data.get("is_cancelled", 0) or 0,
            timeout=data.get("timeout", 0) or 0,
            is_paused=data.get("is_paused", 0) or 0,
            collapsed=data.get("collapsed", 0) or 0,
            is_archived=data.get("is_archived", 0) or 0,
            pending_delete=data.get("pending_delete", 0) or 0,
            nest=data.get("nest", "") or "",
        )


@dataclass
class MessageTreeNode:
    """
    消息树节点

    表示消息树中的一个节点，可以是根消息或回复消息。
    """

    # 消息内容
    message: ChainMessage

    # 子节点（该消息的回复）
    children: list[MessageTreeNode] = field(default_factory=list)

    # 层级深度（0=根消息，1=一级回复，2=二级回复...）
    depth: int = 0

    def add_child(self, child: MessageTreeNode) -> None:
        """添加子节点"""
        child.depth = self.depth + 1
        self.children.append(child)

    def to_dict(self) -> dict[str, Any]:
        """转换为字典格式（用于API）

        返回所有字段，不进行过滤。
        """
        message_dict = {
            "id": self.message.id,
            "timestamp": self.message.timestamp,
            "sender": self.message.sender,
            "receiver": self.message.receiver,
            "content": self.message.content,
            "title": self.message.title,
            "workdir": self.message.workdir,
            "status": self.message.status,
            "created_at": self.message.created_at,
            "read_at": self.message.read_at,
            "archived_at": self.message.archived_at,
            "locked_by": self.message.locked_by,
            "locked_at": self.message.locked_at,
            "heartbeat_at": self.message.heartbeat_at,
            "process_id": self.message.process_id,
            "reply_to": self.message.reply_to,
            "chain_head_id": self.message.chain_head_id,
            "chain_context": self.message.chain_context,
            "error": self.message.error,
            "caller": self.message.caller,
            "caller_session_id": self.message.caller_session_id,
            "is_chain_head": self.message.is_chain_head,
            "is_cancelled": self.message.is_cancelled,
            "timeout": self.message.timeout,
            "is_paused": self.message.is_paused,
            "collapsed": self.message.collapsed,
            "is_archived": self.message.is_archived,
            "pending_delete": self.message.pending_delete,
            "nest": self.message.nest,
        }

        return {
            "message": message_dict,
            "children": [child.to_dict() for child in self.children],
            "depth": self.depth,
        }


@dataclass
class MessageTree:
    """
    消息树

    表示消息及其所有回复形成的树状结构。
    用于在交互模式下管理消息上下文。

    树结构说明：
    - 根节点：链首消息（reply_to is None）
    - 子节点：回复消息（reply_to指向父节点）
    - 支持多级回复（回复消息的回复）
    """

    # 根节点
    root: MessageTreeNode

    # 完整上下文（JSON 格式）
    context_json: str | None = None

    @property
    def head_id(self) -> int:
        """获取链首消息ID（即根节点消息ID）"""
        return self.root.message.id

    @property
    def all_messages(self) -> list[ChainMessage]:
        """获取所有消息（前序遍历）"""
        return self._collect_all_messages(self.root)

    @property
    def total_count(self) -> int:
        """获取消息总数"""
        return self._count_nodes(self.root)

    def _count_nodes(self, node: MessageTreeNode) -> int:
        """递归计算节点总数"""
        count = 1
        for child in node.children:
            count += self._count_nodes(child)
        return count

    def find_node(self, msg_id: int) -> MessageTreeNode | None:
        """
        根据消息ID查找节点

        Args:
            msg_id: 消息ID

        Returns:
            MessageTreeNode 或 None（如果未找到）
        """
        if self.root.message.id == msg_id:
            return self.root

        for child in self.root.children:
            result = self._find_node_recursive(child, msg_id)
            if result:
                return result

        return None

    def _find_node_recursive(self, node: MessageTreeNode, msg_id: int) -> MessageTreeNode | None:
        """递归查找节点"""
        if node.message.id == msg_id:
            return node

        for child in node.children:
            result = self._find_node_recursive(child, msg_id)
            if result:
                return result

        return None

    def get_path_to_root(self, msg_id: int) -> list[ChainMessage]:
        """
        获取从指定消息到根节点的路径

        Args:
            msg_id: 消息ID

        Returns:
            消息列表（从叶子到根）
        """
        node = self.find_node(msg_id)
        if not node:
            return []

        path = [node.message]

        # 需要向上遍历到根节点
        # 由于我们的树结构是单向的（只有children），需要通过reply_to反向查找
        current = node.message
        while current.reply_to:
            parent_node = self.find_node(current.reply_to)
            if parent_node:
                path.append(parent_node.message)
                current = parent_node.message
            else:
                break

        return path

    def build_context_text(
        self, msg_id: int | None = None, max_size: int = 4096, message_manager=None
    ) -> str:
        """
        构建上下文文本（用于显示和 LLM 处理）

        Args:
            msg_id: 指定消息ID（构建从该消息到根的路径），None则构建所有消息
            max_size: 最大字节数，默认 4096（4KB）
            message_manager: 消息管理器（可选），用于获取消息关联的事务ID

        Returns:
            上下文文本
        """
        # ANSI 紫色（用于高亮 reply_to ID）
        purple = "\033[95m"
        reset = "\033[0m"

        # 如果指定了消息ID，只构建该消息到根的路径
        if msg_id:
            path = self.get_path_to_root(msg_id)
            messages = path  # 从叶子到根
        else:
            # 否则构建所有消息（前序遍历）
            messages = self._collect_all_messages(self.root)

        # 构建消息行
        lines = []
        for msg in messages:
            # 格式：时间戳 #id [→reply_to_id] 发送者\t内容
            sender = self._get_sender_display(msg)
            timestamp = msg.timestamp or ""
            # 构建 reply_to 标识
            if msg.reply_to:
                reply_to_part = f"{purple}→ #{msg.reply_to}{reset} "
            else:
                reply_to_part = " "
            # 截断过长的内容
            content = msg.content
            if len(content) > 100:
                content = content[:97] + "..."

            # 获取消息关联的事务ID
            msg_id_part = f"#{msg.id}"
            if message_manager is not None:
                task_id = message_manager.get_task_id(msg.id)
                if task_id:
                    msg_id_part = f"#{msg.id} 事务#{task_id}"

            line = f"{timestamp} {msg_id_part}{reply_to_part}{sender}\t{content}"
            lines.append(line)

        # 如果只有1-2条消息，直接返回
        if len(lines) <= 2:
            return "\n".join(lines)

        # 检查总长度，如果超长则删除中间消息
        result = "\n".join(lines)
        while len(result.encode("utf-8")) > max_size and len(lines) > 2:
            # 删除最中间的消息（保留首尾）
            mid_index = len(lines) // 2
            lines.pop(mid_index)
            result = "\n".join(lines)

        return result

    def _collect_all_messages(self, node: MessageTreeNode) -> list[ChainMessage]:
        """前序遍历收集所有消息"""
        messages = [node.message]
        for child in node.children:
            messages.extend(self._collect_all_messages(child))
        return messages

    def _get_sender_display(self, msg: ChainMessage) -> str:
        """
        获取发送者显示名称

        Args:
            msg: 消息对象

        Returns:
            发送者显示名称（{AGENT_NAME} 或 {MASTER_NAME_EMOJI}）
        """
        from peng.message.names import is_agent_side

        sender = msg.sender or MASTER_NAME
        # 根据发送者名称判断（is_agent_side 兼容 鹏🐣·事务#N 等带事务标识形态）
        if is_agent_side(sender):
            return AGENT_NAME
        return MASTER_NAME_EMOJI

    @classmethod
    def from_messages(cls, messages: list[dict[str, Any]]) -> MessageTree | None:
        """
        从消息列表创建消息树

        按照以下规则构造：
        1. 回复消息id的父节点为reply_to消息id
        2. 同级分支按时间戳排序（新上旧下）

        Args:
            messages: 消息字典列表（按时间正序）

        Returns:
            MessageTree 或 None（如果列表为空）
        """
        if not messages:
            return None

        # 找到根消息（reply_to is None 或 reply_to == 0 或 is_chain_head == 1）
        root_message = None
        for msg in messages:
            reply_to = msg.get("reply_to")
            if reply_to is None or reply_to == 0 or msg.get("is_chain_head") == 1:
                root_message = ChainMessage.from_dict(msg)
                break

        if not root_message:
            return None

        # 创建根节点
        root_node = MessageTreeNode(message=root_message, depth=0)

        # 构建树结构
        node_map = {root_message.id: root_node}

        # 按reply_to分组收集子消息
        children_map: dict[int, list[dict[str, Any]]] = {}
        for msg in messages:
            msg_id = msg["id"]
            reply_to_id = msg.get("reply_to")
            if reply_to_id:
                # 收集到父节点的子消息列表
                if reply_to_id not in children_map:
                    children_map[reply_to_id] = []
                children_map[reply_to_id].append(msg)
        # 按时间戳排序子消息（新上旧下）
        for _reply_to_id, children in children_map.items():
            # 按时间戳降序排序（新的在上面）
            children.sort(key=lambda m: m.get("timestamp", ""), reverse=True)

        def add_children(parent_node_id, children):
            if not children:
                return
            parent_node = node_map[parent_node_id]
            for msg in children:
                msg_id = msg["id"]
                if msg_id not in node_map:
                    child_node = MessageTreeNode(message=ChainMessage.from_dict(msg))
                    parent_node.add_child(child_node)
                    node_map[msg_id] = child_node
                    add_children(msg_id, children_map.get(msg_id))

        add_children(root_message.id, children_map.get(root_message.id))
        return cls(root=root_node)

    def to_dict(self) -> dict[str, Any]:
        """转换为字典格式（用于API）"""
        return {
            "root": self.root.to_dict(),
            "total_count": self.total_count,
        }


# ==================== Agent 对话消息类型 ====================


@dataclass
class BaseMessage:
    """消息基类（Agent 对话）"""

    role: MessageRole
    content: str
    timestamp: datetime = field(default_factory=datetime.now)
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_api_format(self) -> dict:
        """转换为API格式"""
        return {
            "role": self.role.value,
            "content": self.content,
        }


@dataclass
class UserMessage(BaseMessage):
    """用户消息"""

    role: MessageRole = field(default=MessageRole.USER, init=False)


@dataclass
class AssistantMessage(BaseMessage):
    """助手消息"""

    role: MessageRole = field(default=MessageRole.ASSISTANT, init=False)
    tool_calls: list[Any] | None = None  # ToolCall 类型，避免循环导入

    def to_api_format(self) -> dict:
        result: dict[str, Any] = {"role": self.role.value}
        if self.content:
            result["content"] = self.content
        if self.tool_calls:
            result["tool_calls"] = [
                tc.to_api_format() if hasattr(tc, "to_api_format") else tc for tc in self.tool_calls
            ]
        return result


@dataclass
class ToolResultMessage(BaseMessage):
    """工具结果消息"""

    tool_call_id: str = ""
    tool_name: str = ""
    role: MessageRole = field(default=MessageRole.TOOL, init=False)

    def to_api_format(self) -> dict:
        return {
            "role": "tool",
            "tool_call_id": self.tool_call_id,
            "content": self.content,
        }


@dataclass
class SystemMessage(BaseMessage):
    """系统消息"""

    role: MessageRole = field(default=MessageRole.SYSTEM, init=False)


# ==================== 类型别名 ====================

MessageHandler = Callable[[str], Any]
MessageCallback = Callable[[int, bool, str], None]
HeartbeatCallback = Callable[[int], bool]
