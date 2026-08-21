"""
消息管理模块 - 消息管理器

提供消息的高级管理接口，包括：
- 消息创建和处理
- 回复链管理
- 调用链管理
- 超时管理
- 验收管理
- 并发控制

作为 MessageDatabase 的高级封装，提供更便捷的 API。
"""

from __future__ import annotations

import logging
import traceback
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from peng.common import jsonp as json
from peng.config import get_agent_name, get_master_name

from .database import MessageDatabase
from .types import MessageRecord, MessageTree

AGENT_NAME = get_agent_name()

# 状态中文映射
STATUS_CHINESE = {
    "unread": "未读",
    "read": "已读",
    "replied": "已回复",
    "processing": "处理中",
    "accepting": "验收中",
    "accepted": "已验收",
    "paused": "已暂停",
    "interrupted": "已中断",
    "cancelled": "已取消",
    "failed": "失败",
    "done": "已完成",
    "archived": "已归档",
}

# 状态图标映射
STATUS_ICON = {
    "unread": "📩",
    "read": "📧",
    "replied": "💬",
    "processing": "⚙️",
    "accepting": "🔍",
    "accepted": "✅",
    "paused": "⏸️",
    "interrupted": "⚠️",
    "cancelled": "❌",
    "failed": "❗",
    "done": "✅",
    "archived": "📦",
}

if TYPE_CHECKING:
    from peng.task import TaskManager

logger = logging.getLogger(__name__)


from peng.common import MetaSingleton


class MessageManager(metaclass=MetaSingleton):
    """
    消息管理器

    提供消息的高级管理接口，封装 MessageDatabase 的底层操作。

    架构设计：
    - CLI 层的唯一入口
    - 持有 TaskManager 引用
    - 通过 TaskManager 访问 ProcessManager（MessageManager → TaskManager → ProcessManager）
    """

    # 类级别缓存：提示词配置（避免重复读取文件）
    _prompts_cache: dict[str, Any] | None = None
    _prompts_cache_loaded: bool = False

    def __init__(
        self,
    ):
        """
        初始化消息管理器

        注意：PostgreSQL 不需要数据库路径，使用连接池。

        Args:
            task_manager: 事务管理器（可选）
        """
        from peng.task.manager import TaskManager

        self.db = MessageDatabase()
        self._task_manager = TaskManager()

    def set_task_manager(self, task_manager: TaskManager) -> None:
        """设置事务管理器"""
        self._task_manager = task_manager

    def get_task_manager(self) -> TaskManager | None:
        """获取事务管理器"""
        return self._task_manager

    # ==================== 消息 CRUD ====================

    def create_message(self, **kwargs: Any) -> tuple[int, dict[str, Any] | None]:
        """
        创建消息（统一入口）

        所有消息发送都应该通过此方法，确保：
        1. 上下文正确更新
        2. 链首处理正确重启

        常用字段：content, sender, receiver, caller, caller_session_id,
        reply_to, workdir, timeout, check_duplicate, status, id, nest

        Returns:
            (msg_id, result): 消息 ID 和 消息信息
            - 如果找到重复消息，返回0,duplicate_msg,且duplicate_msg["is_duplicate"] == True
        """
        msg_id, msg = self.db.create_message(**kwargs)
        return (msg_id, msg)

    def get_message(self, msg_id: int) -> dict[str, Any] | None:
        """
        获取消息

        Args:
            msg_id: 消息ID

        Returns:
            消息字典
        """
        return self.db.get_message(msg_id)

    def get_message_record(self, msg_id: int) -> MessageRecord | None:
        """
        获取消息记录（类型化）

        Args:
            msg_id: 消息ID

        Returns:
            MessageRecord 或 None
        """
        data = self.db.get_message(msg_id)
        if data is None:
            return None
        return MessageRecord.from_dict(data)

    def get_task_id(self, msg_id: int) -> int | None:
        """
        获取消息关联的事务ID

        Args:
            msg_id: 消息ID

        Returns:
            事务ID，如果没有关联事务则返回 None
        """
        if self._task_manager is None:
            return None
        tasks = self._task_manager.list_tasks(message_id=msg_id, root_only=True)
        if tasks:
            return tasks[0].get("id")
        return None

    def get_unread_messages(self) -> list[dict[str, Any]]:
        """获取未读消息列表"""
        return self.db.get_unread()

    def get_messages_by_caller(self, caller: str) -> list[dict[str, Any]]:
        """
        按调用者获取消息

        Args:
            caller: 调用者标识

        Returns:
            消息列表
        """
        # 获取链首消息
        return self.db.get_chain_head_messages(caller=caller)

    def reply_to_message(
        self,
        msg_id: int,
        content: str,
    ) -> int | None:
        """
        回复消息

        发送回复给master，作为对指定消息的回复。

        Args:
            msg_id: 要回复的消息ID
            content: 回复内容

        Returns:
            新消息ID，失败返回 None
        """
        try:
            new_msg_id, _ = self.db.create_message(
                content=content,
                sender=AGENT_NAME,
                receiver=get_master_name(),
                reply_to=msg_id,
            )
            logger.info(f"已回复消息 #{msg_id}，新消息ID: {new_msg_id}")
            return new_msg_id
        except Exception as e:
            logger.error(f"回复消息 #{msg_id} 失败: {e}\n{traceback.format_exc()}")
            return None

    # ==================== 消息处理流程 ====================

    async def handle_new_message(
        self,
        msg_id: int,
        target: Callable[..., Any] | None = None,
        args: tuple = (),
        kwargs: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """
        处理新消息
        Args:
            msg_id: 消息ID
            target: 进程入口函数（可选，用于事务执行）
            args: 位置参数
            kwargs: 关键字参数

        Returns:
            处理结果，包含 success, message, task_id, error 等字段
        """
        logger.info(f"[MessageManager] handle_new_message 开始: msg_id={msg_id}")

        result = {
            "msg_id": msg_id,
            "success": False,
            "message": None,
            "action": None,
            "task_id": None,
            "process_id": None,
            "error": None,
        }

        logger.info(f"[MessageManager] result初始化完成")

        try:
            # 获取消息详情
            msg = self.get_message(msg_id)
            if msg is None:
                result["error"] = f"消息 #{msg_id} 不存在"
                logger.warning(result["error"])
                return result

            content = msg.get("content", "")
            logger.info(f"[MessageManager] 消息 #{msg_id} 直接创建事务处理")
            # 检查是否是回复消息，且链首消息已有事务
            reply_to = msg.get("reply_to")
            if reply_to:
                return await self.handle_reply(msg_id)
            # 检查当前消息本身是否有关联事务
            # 【原则】对于已经存在事务的消息，任何时候都不应该发起新事务
            if msg.get("status") != "unread":
                all_tasks = self._task_manager.list_tasks()
                msg_existing_task = None
                for task in all_tasks:
                    if task.get("message_id") == msg_id:
                        msg_existing_task = task
                        break

                if msg_existing_task:
                    task_id = msg_existing_task.get("id")
                    task_status = msg_existing_task.get("status")
                    if task_status not in ["running", "accepting"]:
                        await self.resume_message(msg_id)  # 保留 context
                    result["task_id"] = task_id
                    result["success"] = True
                    result["message"] = msg
                    return result

            # 直接创建事务
            logger.info(f"[MessageManager] 准备创建事务: msg_id={msg_id}")
            task_id = self._task_manager.create_task(
                message_id=msg_id,
                content=content,
            )
            if not task_id:
                result["error"] = "创建事务失败"
                logger.error(f"消息 #{msg_id} 创建事务失败")
                return result
            dependencies = self._task_manager.db.get_dependencies(task_id)
            if not dependencies:
                await self.restart_message(msg_id=msg_id)
            result["task_id"] = task_id
            result["success"] = True
            result["message"] = msg
            logger.info(f"✅ 消息 #{msg_id} 已创建事务 #{task_id}")
            return result

        except Exception as e:
            result["error"] = str(e)
            logger.error(f"处理消息 #{msg_id} 失败: {e}\n{traceback.format_exc()}")
            return result

    def start_processing(self, msg_id: int) -> bool:

        return self.update_message_status(msg_id, "processing")

    def complete_message(self, msg_id: int, force: bool = False) -> bool:
        """
        完成消息处理

        Args:
            msg_id: 消息ID
            force: 是否强制完成（保留参数兼容性，当前不使用）

        Returns:
            是否成功
        """
        # 获取当前状态
        msg = self.db.get_message(msg_id)
        if not msg:
            return False

        old_status = msg.get("status", "")
        if old_status == "done":
            return True  # 幂等操作

        # 终态不允许转换（archived）
        terminal_states = ["archived"]
        if old_status in terminal_states:
            logger.warning(f"消息 ID={msg_id} 状态为 {old_status}（终态），不允许完成到 done")
            return False

        # 允许从任何状态转换到 done（除终态）
        success = self.update_message_status(msg_id, "done")

        if success:
            logger.info(f"[MessageManager] 消息 #{msg_id} 状态已更新为 done")

            # 同步更新关联事务的状态
            if self._task_manager:
                tasks = self._task_manager.list_tasks()
                for task in tasks:
                    if task.get("message_id") == msg_id:
                        # 更新事务状态为已完成
                        self._task_manager.complete_task(task["id"], result="消息处理完成")
                        logger.info(f"事务 #{task['id']} 状态同步为 completed")
                        break

        return success

    def archive_message(self, msg_id: int) -> bool:
        """
        归档消息

        Args:
            msg_id: 消息ID

        Returns:
            是否成功
        """
        return self.db.mark_archived(msg_id)

    def unarchive_message(self, msg_id: int) -> bool:
        """
        取消归档消息

        Args:
            msg_id: 消息ID

        Returns:
            是否成功
        """
        return self.db.unarchive_message(msg_id)

    def delete_message(self, msg_id: int, force=False):
        """递归删除指定 ID 的消息及其子消息，并删除关联的事务树"""
        # PostgreSQL模式下不检查文件存在
        db = self.db
        msg = db.get_message(msg_id)

        failed = {
            "success": False,
            "msg_id": msg_id,
            "deleted_messages": 0,
            "deleted_tasks": 0,
        }
        if not msg:
            failed["error"] = f"消息#{msg_id}不存在。"
            return failed

        # 步骤1：先递归删除事务树（必须在删除消息之前）
        # 🔧 Task#1346：传 _delete_message=False，由步骤2统一删除消息
        # 避免在 task 递归删除中重复删除根消息（旧代码删除两次）
        deleted_tasks = 0
        task_manager = self.get_task_manager()
        if task_manager:
            tasks = task_manager.get_tasks_by_message(msg_id)
            for task in tasks:
                deleted_tasks += task_manager.db.delete_task_recursive(task["id"], force, _delete_message=False)
            if deleted_tasks > 0:
                logger.info(f"删除消息 {msg_id} 关联的 {deleted_tasks} 个事务（含子事务）")

        # 步骤2：递归删除消息及其子消息
        deleted_messages = db.delete_message_recursive(msg_id, force)
        return {
            "success": False,
            "msg_id": msg_id,
            "deleted_messages": deleted_messages,
            "deleted_tasks": deleted_tasks,
        }

    def set_pending_delete(self, msg_id: int) -> bool:
        """设置消息待删除标记"""
        return self.db.set_pending_delete(msg_id)

    def get_message_tree(self, msg_id: int) -> MessageTree | None:
        """
        获取消息树

        Args:
            msg_id: 消息ID（可以是树根或树中任意消息）

        Returns:
            MessageTree 或 None
        """
        messages = self.db.get_reply_chain(msg_id)
        if not messages:
            return None
        return MessageTree.from_messages(messages)

    def get_message_trees_batch(self, chain_head_ids: list[int]) -> dict[int, MessageTree]:
        """
        批量获取多个链首消息的消息树（一次SQL完成）

        替代循环调用 get_message_tree，解决 /api/messages 的 N+1 查询问题。

        Args:
            chain_head_ids: 链首消息ID列表

        Returns:
            dict[chain_head_id, MessageTree] — 仅为有消息的链构建树
        """
        chains = self.db.get_reply_chains_batch(chain_head_ids)
        result: dict[int, MessageTree] = {}
        for head_id, messages in chains.items():
            tree = MessageTree.from_messages(messages)
            if tree is not None:
                result[head_id] = tree
        return result

    def get_chain_head(self, msg_id: int) -> dict[str, Any] | None:
        """
        获取链首消息

        Args:
            msg_id: 消息ID

        Returns:
            链首消息字典
        """
        return self.db.get_chain_head(msg_id)

    def get_active_chains(
        self,
        caller: str | None = None,
        status: str | None = None,
        include_archived: bool = False,
    ) -> list[dict[str, Any]]:
        """
        获取活跃回复链

        Args:
            caller: 调用者过滤
            status: 状态过滤
            include_archived: 是否包含已归档消息

        Returns:
            链首消息列表
        """
        return self.db.get_chain_head_messages(
            caller=caller, status=status, include_archived=include_archived
        )

    # ==================== 回复链自动处理（需求12-17整改） ====================

    async def handle_reply(
        self, msg_id: int, reply_to: int | None = None, chain_head_id: int | None = None
    ) -> dict[str, Any]:
        """
        处理新回复消息（需求12整改）

        当有新回复时，更新链首上下文，如果链首处理已结束则重启处理。

        Args:
            msg_id: 回复消息ID

        Returns:
            处理结果
        """
        result = {
            "reply_id": msg_id,
            "chain_head_id": None,
            "context_updated": False,
            "success": False,
            "error": None,
        }

        try:
            # 获取回复链
            chain = self.get_chain_head(msg_id)
            if chain is None:
                result["error"] = "无法获取回复链"
                return result

            head_id = chain["id"]
            result["chain_head_id"] = head_id

            # 检查链首状态
            head_status = chain.get("status")
            if head_status != "processing":
                # 链首处理已结束，恢复处理（保留 context）
                result["success"] = await self.resume_message(head_id)
                logger.info(f"恢复链首 #{head_id} 的处理")
            else:
                # 链首消息状态为processing，但需验证关联task是否真的在运行
                should_resume = False
                resume_reason = ""
                tasks = self._task_manager.list_tasks(message_id=head_id, root_only=True) if self._task_manager else []
                if not tasks:
                    should_resume = True
                    resume_reason = "无关联task"
                else:
                    task = tasks[0]
                    task_id = task.get("id")
                    task_status = task.get("status", "")
                    process_id = task.get("process_id", 0) or 0
                    # 检查task是否在终态（completed/failed/cancelled/paused/interrupted）
                    terminal_statuses = ("completed", "failed", "cancelled", "paused", "interrupted")
                    if task_status in terminal_statuses:
                        should_resume = True
                        resume_reason = f"task#{task_id}状态为{task_status}"
                    elif process_id > 0:
                        # task状态为running，检查runner进程是否存活
                        try:
                            import os
                            os.kill(process_id, 0)
                        except (ProcessLookupError, OSError, PermissionError):
                            should_resume = True
                            resume_reason = f"task#{task_id}的runner进程pid={process_id}已不存在"
                    # else: process_id=0 且 task running，可能是刚启动还未设置pid

                if should_resume:
                    # 链首虽然标记为processing但task实际已结束，恢复处理
                    result["success"] = await self.resume_message(head_id)
                    logger.info(f"链首 #{head_id} 标记为processing但{resume_reason}，恢复处理")
                else:
                    # 链首确实在处理中，无需额外操作
                    result["success"] = True
                    logger.info(f"链首 #{head_id} 正在处理中")
            # 回复消息由根事务处理，run_stream中自动收集processing的回复消息
            self.update_message_status(msg_id=msg_id, new_status="processing")
            return result

        except Exception as e:
            import traceback

            result["error"] = str(e)
            logger.exception(f"处理新回复失败: {e}\n{traceback.format_exc()}")
            return result

    def get_message_with_context(self, msg_id: int) -> dict[str, Any] | None:
        """
        获取消息（优先包含完整上下文）

        Args:
            msg_id: 消息ID

        Returns:
            消息字典（包含上下文）
        """
        msg = self.get_message(msg_id)
        if msg is None:
            return None

        # 如果有上下文，直接返回
        if msg.get("chain_context"):
            return msg

        # 否则构建上下文（使用 database 层的 build_context_history）
        chain_head_id = msg.get("chain_head_id")
        if chain_head_id:
            context = self.db.build_context_history(chain_head_id)
            if context:
                msg["chain_context"] = context

        return msg

    # ==================== 调用链管理 ====================

    def set_caller(self, msg_id: int, caller: str) -> bool:
        """
        设置消息调用者

        Args:
            msg_id: 消息ID
            caller: 调用者

        Returns:
            是否成功
        """
        try:
            return self.db.update_message(msg_id, caller=caller) is not None
        except Exception as e:
            logger.error(f"set_caller failed: {e}")
            return False

    def get_caller(self, msg_id: int) -> str | None:
        """
        获取调用来源

        Args:
            msg_id: 消息ID

        Returns:
            调用来源
        """
        msg = self.db.get_message(msg_id)
        if msg is None:
            return None
        return msg.get("caller")

    def set_caller_session(self, msg_id: int, session_id: str) -> bool:
        """
        设置调用者会话ID

        Args:
            msg_id: 消息ID
            session_id: 会话ID

        Returns:
            是否成功
        """
        try:
            return self.db.update_message(msg_id, caller_session_id=session_id)
        except Exception as e:
            logger.warning(f"设置调用者会话ID失败: {e}\n{traceback.format_exc()}")
            return False

    # ==================== 超时管理 ====================

    def set_timeout(self, msg_id: int, timeout: int) -> bool:
        """
        设置超时时间

        Args:
            msg_id: 消息ID
            timeout: 超时时间（秒）

        Returns:
            是否成功
        """
        return self.db.set_timeout(msg_id, timeout)

    def get_timed_out_messages(self) -> list[dict[str, Any]]:
        """
        获取超时消息

        Returns:
            超时消息列表
        """
        try:
            return self.db.get_timeout_based_messages()
        except Exception as e:
            logger.warning(f"获取超时消息失败: {e}\n{traceback.format_exc()}")
            return []

    # ==================== 并发控制 ====================

    def lock_message(self, msg_id: int, process_id: int) -> bool:
        """
        锁定消息

        Args:
            msg_id: 消息ID
            process_id: 进程ID

        Returns:
            是否成功
        """
        return self.db.lock_message(msg_id, process_id)

    def unlock_message(self, msg_id: int, process_id: int) -> bool:
        """
        解锁消息

        Args:
            msg_id: 消息ID
            process_id: 进程ID

        Returns:
            是否成功
        """
        return self.db.unlock_message(msg_id, process_id)

    def update_heartbeat(self, msg_id: int, process_id: int) -> bool:
        """
        更新心跳

        Args:
            msg_id: 消息ID
            process_id: 进程ID

        Returns:
            是否成功
        """
        return self.db.update_heartbeat(msg_id, process_id)

    def release_expired_locks(self, timeout_seconds: int = 300) -> int:
        """
        释放过期锁

        Args:
            timeout_seconds: 超时时间（秒）

        Returns:
            释放的锁数量
        """
        return self.db.release_expired_locks(timeout_seconds)

    def update_message_status(self, msg_id: int, new_status: str) -> bool:
        return self.db.update_message_status(msg_id, new_status)

    def get_message_collapsed(self, msg_id: int) -> bool:
        """
        获取消息的折叠状态

        Args:
            msg_id: 消息 ID

        Returns:
            是否折叠
        """
        msg = self.db.get_message(msg_id)
        if not msg:
            return False
        return bool(msg.get("collapsed", 0))

    def set_message_collapsed(self, msg_id: int, collapsed: bool) -> bool:
        """
        设置消息的折叠状态

        Args:
            msg_id: 消息 ID
            collapsed: 是否折叠

        Returns:
            是否成功
        """
        try:
            collapsed_int = 1 if collapsed else 0
            result = self.db.update_message(msg_id, collapsed=collapsed_int)
            logger.debug(f"消息 ID={msg_id} 折叠状态: {collapsed}")
            return result
        except Exception as e:
            logger.error(f"设置消息折叠状态失败: {e}\n{traceback.format_exc()}")
            return False

    async def pause_message(self, msg_id: int, reason: str = "Message processing paused") -> bool:
        """
        暂停消息及其关联的事务

        Args:
            msg_id: 消息 ID
            reason: 暂停原因

        Returns:
            是否成功
        """
        # 获取消息信息
        msg = self.db.get_message(msg_id)
        if not msg:
            logger.warning(f"消息 ID={msg_id} 不存在，无法暂停")
            return False

        current_status = msg.get("status")
        # 终态无法暂停
        if current_status in ("done", "failed", "cancelled", "archived"):
            logger.error(f"消息 ID={msg_id} 状态为 {current_status}（终态），无法暂停")
            return False

        # 已暂停的消息，幂等性返回成功
        if current_status == "paused":
            logger.info(f"消息 ID={msg_id} 已处于暂停状态")
            return True

        # 暂停关联的事务
        if self._task_manager:
            tasks = self._task_manager.list_tasks(message_id=msg_id)
            for task in tasks:
                task_id = task.get("id")
                task_status = task.get("status")
                if task_status in ("running", "accepting"):
                    await self._task_manager.pause_task(task_id, reason=f"消息#{msg_id} 暂停")
                    logger.debug(f"暂停事务 ID={task_id}")

        # 更新消息状态为paused
        success = self.update_message_status(msg_id, "paused")
        if success:
            logger.info(f"消息 ID={msg_id} 已暂停")

        return success

    async def cancel_message(self, msg_id: int) -> bool:
        """
        恢复暂停的消息及其关联的事务

        Args:
            msg_id: 消息 ID

        Returns:
            是否成功
        """
        # 获取消息信息
        msg = self.db.get_message(msg_id)
        if not msg:
            logger.warning(f"消息 ID={msg_id} 不存在，无法恢复")
            return False

        current_status = msg.get("status")
        # 终态无法取消
        if current_status in ("done", "failed", "archived"):
            logger.error(f"消息 ID={msg_id} 状态为 {current_status}（终态），无法取消")
            return False

        # 已取消的消息，幂等性返回成功
        if current_status == "cancelled":
            logger.info(f"消息 ID={msg_id} 已处于取消状态")
            return True

        # 恢复关联的事务
        tasks = self._task_manager.list_tasks(message_id=msg_id)
        for task in tasks:
            task_id = task.get("id")
            task_status = task.get("status")
            if task_status in ("running", "accepting"):
                await self._task_manager.cancel_task(task_id, reason=f"消息#{msg_id} 取消")
                logger.debug(f"恢复事务 ID={task_id}")

        # 更新消息状态为processing
        success = self.update_message_status(msg_id, "processing")
        if success:
            logger.info(f"消息 ID={msg_id} 已恢复")

        return success

    async def resume_message(self, msg_id: int) -> bool:
        """
        恢复暂停的消息及其关联的事务

        Args:
            msg_id: 消息 ID

        Returns:
            是否成功
        """
        # 获取消息信息
        msg = self.db.get_message(msg_id)
        if not msg:
            logger.warning(f"消息 ID={msg_id} 不存在，无法恢复")
            return False

        current_status = msg.get("status")
        # 已运行的消息，幂等性返回成功
        if current_status in ("processing", "accepting"):
            logger.info(f"消息 ID={msg_id} 已在运行中")
            return True

        success = False

        # 恢复关联的事务（包括终态事务，保留 context）
        tasks = self._task_manager.list_tasks(message_id=msg_id)
        for task in tasks:
            task_id = task.get("id")
            task_status = task.get("status")
            # 恢复所有状态的事务（保留 context）
            # 更新消息状态为 processing
            success = self.update_message_status(msg_id, "processing")
            await self._task_manager.resume_task(
                task_id, keep_context=True, reason=f"消息#{msg_id} 恢复"
            )
            logger.debug(f"恢复事务 ID={task_id}（状态：{task_status}）")

        if success:
            logger.info(f"消息 ID={msg_id} 已恢复")

        return success

    async def restart_message(self, msg_id: int) -> bool:
        """
        重启消息及其关联的事务

        重启前执行清理：
        1. 递归删除所有回复消息（含子消息）及其关联事务树
        2. 递归删除根事务的所有子事务

        Args:
            msg_id: 消息 ID

        Returns:
            是否成功
        """
        # 获取消息信息
        msg = self.db.get_message(msg_id)
        if not msg:
            logger.warning(f"消息 ID={msg_id} 不存在，无法重启")
            return False

        current_status = msg.get("status")
        # 已运行的消息，幂等性返回成功
        if current_status in ("processing", "accepting"):
            logger.info(f"消息 ID={msg_id} 已在运行中")
            return True

        # task_manager必须存在才能重启消息
        if not self._task_manager:
            logger.error(f"task_manager 不存在，无法重启消息 ID={msg_id}")
            return False

        # 步骤1：获取根事务ID
        root_tasks = self._task_manager.list_tasks(message_id=msg_id, root_only=True)
        if not root_tasks:
            logger.warning(f"消息 ID={msg_id} 没有关联的根事务，无法重启")
            return False

        root_task_id = root_tasks[0].get("id")

        # 步骤2：递归删除根事务的所有子事务
        deleted_sub_tasks = 0
        sub_tasks = self._task_manager.db.get_children(root_task_id)
        for sub_task in sub_tasks:
            deleted_sub_tasks += self._task_manager.db.delete_task_recursive(
                sub_task["id"], force=True
            )
        if deleted_sub_tasks > 0:
            logger.info(f"删除根事务 {root_task_id} 的 {deleted_sub_tasks} 个子事务")

        # 步骤3：递归删除所有回复消息（含子消息）及其关联事务树
        deleted_reply_messages = 0
        replies = self.db.get_messages_by_reply_to(msg_id)
        for reply in replies:
            reply_id = reply["id"]
            # 先删除回复消息关联的事务树
            reply_tasks = self._task_manager.get_tasks_by_message(reply_id)
            deleted_reply_tasks = 0
            for reply_task in reply_tasks:
                deleted_reply_tasks += self._task_manager.db.delete_task_recursive(
                    reply_task["id"], force=True
                )
            if deleted_reply_tasks > 0:
                logger.info(f"删除回复消息 {reply_id} 关联的 {deleted_reply_tasks} 个事务")
            # 再递归删除回复消息（包括其子消息）
            deleted_reply_messages += self.db.delete_message_recursive(reply_id)
        if deleted_reply_messages > 0:
            logger.info(f"删除消息 {msg_id} 的 {deleted_reply_messages} 条回复消息")

        # 步骤4：重启根事务（保留费用统计）
        from peng.llm import MessageHistory

        _old_ctx = self._task_manager.db.get_task(root_task_id)
        _old_context = _old_ctx.get("context") if _old_ctx else ""
        _new_context = ""
        if _old_context:
            _history = MessageHistory()
            if _history.from_str(_old_context):
                _new_context = _history.reset_messages_keep_stats()
        self._task_manager.db.update_task(
            root_task_id, progress=0, info="", context=_new_context, checklist="", report=""
        )
        success = await self._task_manager.restart_task(root_task_id, reason=f"消息#{msg_id} 重启")
        if success:
            self.update_message_status(msg_id, "processing")
            logger.info(
                f"消息 ID={msg_id} 已重启（清理了 {deleted_sub_tasks} 个子事务, {deleted_reply_messages} 条回复消息）"
            )

        return success

    def mark_replied(self, msg_id: int) -> bool:
        """
        标记消息为已回复

        用于简单消息（无需事务处理）回复后的状态更新。

        Args:
            msg_id: 消息 ID

        Returns:
            是否成功
        """
        # 获取当前状态
        msg = self.db.get_message(msg_id)
        if not msg:
            return False

        old_status = msg.get("status", "")
        # 只能从 processing 或 interrupted 状态转换
        if old_status not in ("processing", "interrupted"):
            return False

        return self.update_message_status(msg_id, old_status, "replied")

    def mark_failed(self, msg_id: int, reason: str | None = None) -> bool:
        """
        标记消息为失败

        用于事务执行失败后的状态更新。

        Args:
            msg_id: 消息 ID
            reason: 失败原因

        Returns:
            是否成功
        """
        # 获取当前状态
        msg = self.db.get_message(msg_id)
        if not msg:
            return False

        success = self.update_message_status(msg_id, "failed")

        return success

    def mark_interrupted(self, msg_id: int, reason: str = "Request interrupted by user") -> bool:
        """
        标记消息为中断状态

        Args:
            msg_id: 消息 ID
            reason: 中断原因

        Returns:
            是否成功
        """
        # 获取当前状态
        msg = self.db.get_message(msg_id)
        if not msg:
            return False

        success = self.update_message_status(msg_id, "interrupted")

        # 保存中断原因并设置取消标记
        if success:
            try:
                from datetime import datetime

                # 修复原SQL错误：archived_at字段名错误，应该用正确的字段
                # 使用update_message方法更新字段
                self.db.update_message(
                    msg_id, is_cancelled=1, archived_at=datetime.now().isoformat()
                )
            except Exception as e:
                logger.warning(f"保存中断信息失败: {e}")

        return success

    def mark_paused(self, msg_id: int, reason: str = "Message processing paused") -> bool:
        """
        标记消息为暂停状态

        Args:
            msg_id: 消息 ID
            reason: 暂停原因

        Returns:
            是否成功
        """
        # 获取当前状态
        msg = self.db.get_message(msg_id)
        if not msg:
            return False

        success = self.update_message_status(msg_id, "paused")

        # 保存暂停原因并设置暂停标记
        if success:
            try:
                self.db.update_message(msg_id, is_paused=1)
            except Exception as e:
                logger.warning(f"保存暂停信息失败: {e}")

        return success

    def mark_cancelled(self, msg_id: int, reason: str = "Message processing cancelled") -> bool:
        """
        标记消息为取消状态

        Args:
            msg_id: 消息 ID
            reason: 取消原因

        Returns:
            是否成功
        """
        # 获取当前状态
        msg = self.db.get_message(msg_id)
        if not msg:
            return False

        success = self.update_message_status(msg_id, "cancelled")

        # 设置取消标记
        if success:
            try:
                self.db.update_message(msg_id, is_cancelled=1)
            except Exception as e:
                logger.warning(f"设置取消标记失败: {e}")

        return success

    def check_and_mark_stale_processing_messages(self) -> list[int]:
        """
        检查并标记失效的处理中消息

        Returns:
            被标记为中断的消息 ID 列表
        """
        return self.db.check_and_mark_stale_processing_messages()

    def check_and_mark_paused_messages(self) -> list[int]:
        """
        检查被暂停的消息

        Returns:
            被暂停的消息 ID 列表
        """
        return self.db.check_and_mark_paused_messages()

    # ==================== 统计和查询 ====================

    def count_by_status(self) -> dict[str, int]:
        """统计各状态消息数量"""
        return self.db.count_by_status()

    def get_recent_messages(self, limit: int = 20) -> list[dict[str, Any]]:
        """
        获取最近消息

        Args:
            limit: 返回数量限制

        Returns:
            消息列表
        """
        return self.db.get_all()[:limit]

    def get_chain_ids(self, chain_head_id: int) -> list[int]:
        """
        获取消息链中的所有消息ID

        用于多轮对话优化：筛选相关的新消息。

        Args:
            chain_head_id: 消息链首ID

        Returns:
            消息ID列表
        """
        chain = self.get_message_tree(chain_head_id)
        if chain is None:
            return []

        # 收集所有消息ID
        all_messages = chain.all_messages
        return [msg.id for msg in all_messages]

    def move_message(
        self, msg_id: int, target_id: int | None = None, only_message: bool = False
    ) -> dict[str, Any]:
        """
        移动消息到新的父节点（委托给 MessageDatabase）

        Args:
            msg_id: 要移动的消息ID
            target_id: 目标父消息ID，None表示拖拽到空隙变成树根
            only_message: 是否仅移动消息不同步移动关联事务（内部使用）

        Returns:
            {"success": bool, "message": str, "msg_id": int}
        """
        return self.db.move_message(msg_id, target_id=target_id, only_message=only_message)
