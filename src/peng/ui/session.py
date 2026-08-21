"""
聊天会话管理模块

管理交互模式下的消息回复链和调用链。

功能：
1. 会话状态管理
2. 回复链处理
3. 调用链记录
4. 继续最近消息功能
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any

from peng.config import get_agent_name, get_master_name
from peng.message.manager import MessageManager

logger = logging.getLogger(__name__)
AGENT_NAME = get_agent_name()
MASTER_NAME = get_master_name()


class SessionState(Enum):
    """会话状态"""

    ACTIVE = "active"  # 活跃中
    PAUSED = "paused"  # 已暂停
    CANCELLED = "cancelled"  # 已取消
    COMPLETED = "completed"  # 已完成


@dataclass
class ChatSession:
    """
    聊天会话

    管理交互模式下的消息回复链和调用链。

    用法：
        session = ChatSession(caller="chat")

        # 创建链首消息
        msg_id = session.create_chain_head("请帮我重构代码")

        # 添加回复
        reply_id = session.add_reply("需要支持回复链")

        # 获取完整上下文
        context = session.get_chain_context()

        # 开始新链
        session.start_new_chain()
    """

    # 调用来源
    caller: str = "chat"

    # 会话ID（用于区分不同会话）
    session_id: str = field(default_factory=lambda: str(uuid.uuid4())[:8])

    # 当前链首消息ID
    current_chain_head_id: int | None = None

    # 会话状态
    state: SessionState = SessionState.ACTIVE

    # 消息管理器（延迟初始化）
    _message_manager: MessageManager | None = field(default=None, repr=False)

    # 数据库路径（可选，用于测试）
    db_path: Path | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        """初始化消息管理器"""
        if self._message_manager is None:
            self._message_manager = MessageManager()

    @property
    def message_manager(self) -> MessageManager:
        """获取消息管理器"""
        if self._message_manager is None:
            self._message_manager = MessageManager()
        return self._message_manager

    def create_chain_head(
        self,
        content: str,
        timeout: int = 0,
    ) -> int:
        """
        创建链首消息

        Args:
            content: 消息内容
            timeout: 超时时间（秒）

        Returns:
            消息ID
        """
        logger.info(f"[ChatSession.create_chain_head] 创建链首消息，content={content[:50]}")
        msg_id, _ = self.message_manager.create_message(
            content=content,
            sender=get_master_name(),
            receiver=AGENT_NAME,
            caller=self.caller,
            caller_session_id=self.session_id,
            timeout=timeout,
        )
        self.current_chain_head_id = msg_id
        self.state = SessionState.ACTIVE
        logger.info(
            f"[ChatSession.create_chain_head] 链首消息ID={msg_id}, current_chain_head_id={self.current_chain_head_id}"
        )

        logger.debug(f"创建链首消息: {msg_id}, caller={self.caller}")
        return msg_id

    def add_reply(
        self,
        content: str,
    ) -> int:
        """
        添加回复到当前链

        Args:
            content: 回复内容

        Returns:
            新消息ID

        Raises:
            ValueError: 如果没有当前链首
        """
        logger.info(
            f"[ChatSession.add_reply] 添加回复，content={content[:50]}, current_chain_head_id={self.current_chain_head_id}"
        )
        if not self.current_chain_head_id:
            # 没有链首，自动创建
            logger.warning("[ChatSession.add_reply] 没有链首，自动创建新链首")
            return self.create_chain_head(content)

        msg_id, _ = self.message_manager.create_message(
            content=content,
            sender=get_master_name(),
            receiver=AGENT_NAME,
            caller=self.caller,
            caller_session_id=self.session_id,
            reply_to=self.current_chain_head_id,
        )

        logger.info(
            f"[ChatSession.add_reply] 回复消息ID={msg_id}, reply_to={self.current_chain_head_id}"
        )
        logger.debug(f"添加回复消息: {msg_id}, reply_to={self.current_chain_head_id}")
        return msg_id

    def get_chain_context(self) -> str | None:
        """
        获取当前回复链的完整上下文

        Returns:
            上下文文本，或 None 如果没有链首
        """
        if not self.current_chain_head_id:
            return None

        # 使用 database 层的 build_context_history
        return self.message_manager.db.build_context_history(self.current_chain_head_id)

    def start_new_chain(self) -> None:
        """开始新的回复链（清除当前链首）"""
        logger.warning(
            f"[ChatSession.start_new_chain] 清除current_chain_head_id={self.current_chain_head_id}"
        )
        self.current_chain_head_id = None
        self.state = SessionState.ACTIVE
        logger.info("[ChatSession.start_new_chain] current_chain_head_id已清除，开始新的回复链")
        logger.debug("开始新的回复链")

    def pause(self) -> bool:
        """
        暂停当前会话

        Returns:
            是否成功
        """
        if self.state == SessionState.ACTIVE:
            self.state = SessionState.PAUSED
            logger.debug("会话已暂停")
            return True
        return False

    def resume(self) -> bool:
        """
        恢复会话

        Returns:
            是否成功
        """
        if self.state == SessionState.PAUSED:
            self.state = SessionState.ACTIVE
            logger.debug("会话已恢复")
            return True
        return False

    def cancel(self) -> bool:
        """
        取消当前会话

        Returns:
            是否成功
        """
        if self.state in (SessionState.ACTIVE, SessionState.PAUSED):
            self.state = SessionState.CANCELLED
            logger.debug("会话已取消")
            return True
        return False

    def is_active(self) -> bool:
        """检查会话是否活跃"""
        return self.state == SessionState.ACTIVE

    def is_paused(self) -> bool:
        """检查会话是否暂停"""
        return self.state == SessionState.PAUSED

    def is_cancelled(self) -> bool:
        """检查会话是否取消"""
        return self.state == SessionState.CANCELLED

    def get_latest_chain_head(
        self,
        status: str | None = None,
        callers: list[str] | None = None,
    ) -> dict[str, Any] | None:
        """
        获取最近的链首消息

        Args:
            status: 状态过滤（如 'processing'），None 表示不限状态
            callers: 调用者列表（如 ['fly', 'chat']），None 表示使用 self.caller

        Returns:
            消息字典或 None
        """
        if callers is None:
            callers = [self.caller]

        # 遍历所有 caller，找到最新的消息
        latest_msg = None
        latest_time = None

        for caller in callers:
            chains = self.message_manager.get_active_chains(
                caller=caller,
                status=status,
            )
            if chains:
                msg = chains[0]
                msg_time = msg.get("updated_at") or msg.get("created_at")
                if msg_time and (latest_time is None or msg_time > latest_time):
                    latest_msg = msg
                    latest_time = msg_time

        return latest_msg

    def continue_latest(
        self,
        status: str | None = None,
        callers: list[str] | None = None,
    ) -> bool:
        """
        继续最近的消息链

        查找最近的消息（不限状态）并设置为当前链首。

        Args:
            status: 状态过滤，None 表示不限状态
            callers: 调用者列表，None 表示使用 self.caller

        Returns:
            是否找到并设置了链首
        """
        latest = self.get_latest_chain_head(status=status, callers=callers)
        if latest:
            self.current_chain_head_id = latest["id"]
            self.state = SessionState.ACTIVE
            logger.debug(f"继续消息链: {self.current_chain_head_id}")
            return True
        return False

    def to_dict(self) -> dict[str, Any]:
        """导出为字典"""
        return {
            "caller": self.caller,
            "session_id": self.session_id,
            "current_chain_head_id": self.current_chain_head_id,
            "state": self.state.value,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ChatSession:
        """从字典创建"""
        return cls(
            caller=data.get("caller", "chat"),
            session_id=data.get("session_id", str(uuid.uuid4())[:8]),
            current_chain_head_id=data.get("current_chain_head_id"),
            state=SessionState(data.get("state", "active")),
        )
