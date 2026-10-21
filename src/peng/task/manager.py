"""
事务管理模块 - 事务管理器

提供事务的高级管理接口，包括：
- 事务创建和执行
- 事务状态管理
- 步骤管理
- 依赖管理
- 心跳机制
- 验收管理

架构设计：
- 实现三层架构：MessageManager → TaskManager
- 使用 platform 进行进程控制（杀进程、检查进程存在等）
"""

from __future__ import annotations

import logging
import sys
import os
import asyncio
import subprocess

import traceback
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from peng.common.paths import get_working_directory
from peng.common import MetaSingleton
from peng.config.loader import get_project_root, load_config

from .database import TaskDatabase
from .executor import run_task_process
from .task_types import StepStatus, TaskPriority, TaskStatus, TaskType

logger = logging.getLogger(__name__)

# 类型别名
HeartbeatCallback = Callable[[int], bool]


class TaskManager(metaclass=MetaSingleton):
    """
    事务管理器

    提供事务的高级管理接口，封装 TaskDatabase 的底层操作。
    支持与消息模块的集成。
    支持 worktree 并发控制（相同 worktree 禁止并行执行）。

    架构：
    - 被 MessageManager 调用，处理复杂事务
    - 使用 platform 进行进程控制
    """

    # 事务状态中文映射
    STATUS_CHINESE = {
        "pending": "等待执行",
        "running": "执行中",
        "accepting": "验收中",
        "accepted": "已验收",
        "paused": "已暂停",
        "interrupted": "已中断",
        "completed": "已完成",
        "failed": "失败",
        "cancelled": "已取消",
    }

    # 事务状态图标映射
    STATUS_ICON = {
        "pending": "⏳",
        "running": "⚙️",
        "accepting": "🔍",
        "accepted": "✅",
        "paused": "⏸️",
        "interrupted": "⚠️",
        "completed": "✅",
        "failed": "❗",
        "cancelled": "❌",
    }

    def __init__(
        self,
        repo_root: Path | None = None,
    ):
        # 懒加载：延迟初始化 message_manager 以避免循环依赖
        if not hasattr(self, "message_manager"):
            self.message_manager = None
        if not hasattr(self, "db"):
            self.db = TaskDatabase()

    def get_message_manager(self):
        """获取消息管理器（懒加载）"""
        if self.message_manager is None:
            from peng.message.manager import MessageManager

            self.message_manager = MessageManager()
        return self.message_manager

    def create_task(self, **kwargs: Any) -> int:
        """
        创建事务（通用字典参数形式，透传给 database.create_task）

        常用字段：message_id, content, description, priority, task_type,
        parent_task_id, caller, caller_session_id, timeout, deadline,
        metadata, creator, prompt, workdir, status, is_complex, timer, id
        """
        logger.info(f"[Task] 创建事务：priority={kwargs.get('priority', 'NORMAL')}")

        # 如果 workdir 未设置且有 message_id，从消息继承 workdir
        if not kwargs.get("workdir") and kwargs.get("message_id"):
            try:
                from peng.message.database import MessageDatabase

                msg_db = MessageDatabase()
                message = msg_db.get_message(kwargs["message_id"])
                if message and message.get("workdir"):
                    kwargs["workdir"] = message.get("workdir")
                    logger.debug(
                        f"[Task] 从消息#{kwargs['message_id']}继承工作目录：{kwargs['workdir']}"
                    )
            except Exception as e:
                logger.warning(f"[Task] 从消息#{kwargs['message_id']}继承工作目录失败：{e}")

        return self.db.create_task(**kwargs)

    def get_message_id(self, task_id):
        task = self.db.get_task(task_id)
        return task.get("message_id")

    def _execute_task_process(self, task_id: int) -> int:
        """
        为事务创建进程执行（使用 subprocess 避免 BrokenPipeError）
        创建独立的子进程来执行事务，使用 platform 控制进程生命周期。
        """
        logger.info(f"[TaskManager._execute_task_process] task_id={task_id}")

        # 获取事务信息
        task = self.db.get_task(task_id)
        logger.info("[TaskManager] 创建子进程执行事务...")

        try:
            # 构建子进程环境：系统环境 + shell 配置文件环境（确保 PATH 包含 npm 等工具路径）
            from peng.common.utils import get_merged_env

            child_env = get_merged_env()
            logger.debug(f"[TaskManager] 子进程 PATH={child_env.get('PATH', '')}")

            process = subprocess.Popen(
                [sys.executable, Path(__file__).parent / f"runner.py", "--task_id", f"{task_id}"],
                # 🔧 事务#2105：runner子进程CWD确定化——显式指定项目根目录。
                # 原cwd=None继承服务进程CWD，而WebUI工作区切换（set_workdir→
                # os.chdir）会改变服务进程CWD，导致runner携带任意工作目录
                # 启动（如huizi），回退_load_agent_md()的相对路径读取会注入
                # 其他项目的AGENTS.md（含@provider!锁定标记），跨工作目录污染
                # 事务系统提示词。runner.py入口处亦有同源兜底chdir。
                cwd=str(get_project_root()),
                env=child_env,
            )
            pid = process.pid
            logger.info(f"[TaskManager] 事务{task_id}的子进程已启动: PID={pid}")
            # 更新事务process_id
            self.db.update_task(task_id, process_id=pid)
            return pid

        except Exception as e:
            # 启动失败，记录错误并抛出异常
            logger.error(f"[TaskManager] 子进程启动失败: {e}\n{traceback.format_exc()}")
            raise

    def get_task(self, task_id: int) -> dict[str, Any] | None:
        """
        获取事务

        Args:
            task_id: 事务 ID

        Returns:
            事务字典
        """
        return self.db.get_task(task_id)

    def get_tasks_by_ids(self, task_ids: list[int]) -> list[dict[str, Any]]:
        """
        批量获取多个事务（并发优化：减少N次查询为1次）

        Args:
            task_ids: 事务ID列表

        Returns:
            事务字典列表
        """
        return self.db.get_tasks_by_ids(task_ids)

    def list_tasks(
        self,
        status: TaskStatus | str | None = None,
        priority: TaskPriority | int | None = None,
        caller: str | None = None,
        message_id: int | None = None,
        limit: int | None = None,
        order: str = "ASC",
        root_only: bool = False,
        include_archived: bool = True,
        exclude_fields: list[str] | None = None,
    ) -> list[dict[str, Any]]:
        """列出事务

        Args:
            root_only: 是否只返回根事务
            include_archived: 是否包含已归档事务
            exclude_fields: 需要从 SELECT 中排除的字段列表
        """
        return self.db.list_tasks(
            status=status,
            priority=priority,
            caller=caller,
            message_id=message_id,
            limit=limit,
            order=order,
            root_only=root_only,
            include_archived=include_archived,
            exclude_fields=exclude_fields,
        )

    def get_pending_tasks(self) -> list[dict[str, Any]]:
        """获取待执行事务"""
        return self.db.get_by_status(TaskStatus.PENDING)

    def get_subtree(self, root_id: int) -> list[dict[str, Any]]:
        """
        获取整个事务树（某个根节点下的所有事务）

        Args:
            root_id: 事务树根节点 ID

        Returns:
            事务树中所有事务列表，按深度和路径排序
        """
        return self.db.get_subtree(root_id)

    # ==================== 事务执行 ====================

    def start_task(
        self,
        task_id: int,
    ) -> bool:
        success = self.db.start_task(task_id)
        if success:
            # start 时取消归档事务自身及其根消息
            self._unarchive_task_and_root_message(task_id)
        return success

    def complete_task(
        self,
        task_id: int,
        result: str | None = None,
        force: bool = False,
    ) -> bool:
        """
        完成事务

        Args:
            task_id: 事务 ID
            result: 结果
            force: 是否强制完成（忽略验收）

        Returns:
            是否成功
        """
        task = self.db.get_task(task_id)
        if task is None:
            return False

        # 需求：事务完成前flush所有输出缓冲区
        from peng.task.output_manager import get_output_manager

        output_manager = get_output_manager()
        output_manager.flush_all_buffers(task_id)

        # 费用校验（优化4）：检查context中累计费用是否合理
        self._validate_task_cost(task_id, task)

        success = self.db.complete_task(task_id, result)

        # 需求 §3.2.5/§3.2.6: 同步状态到关联的消息
        if success and task.get("message_id"):
            self._sync_task_completion_to_message(task_id, task.get("message_id"), result)

        # 事务完成，清理关联nest端的agent-browser浏览器进程
        if success:
            self._cleanup_nest_browser(task_id)

        return success

    def fail_task(self, task_id: int, error: str) -> bool:
        """标记事务失败"""
        task = self.db.get_task(task_id)
        success = self.db.fail_task(task_id, error)

        # 需求 §3.2.5: 同步状态到关联的消息
        if success and task and task.get("message_id"):
            self._sync_task_status_to_message(task_id, task.get("message_id"), "failed", error)

        # 事务失败，清理关联nest端的agent-browser浏览器进程
        if success and task:
            self._cleanup_nest_browser(task_id)

        return success

    def update_task_status(self, task_id: int, old_status: str, new_status: str) -> bool:
        """
        统一的事务状态变更接口

        通过OutputManager记录状态变化信息。

        Args:
            task_id: 事务 ID
            old_status: 旧状态
            new_status: 新状态

        Returns:
            是否成功
        """
        # 获取事务信息
        task = self.db.get_task(task_id)
        if not task:
            logger.warning(f"事务 ID={task_id} 不存在，无法更新状态")
            return False

        # 更新状态
        success = self.db.update_tasks_status([task_id], new_status) > 0
        if success:
            # 通过OutputManager记录状态变化
            old_chinese = f"{self.STATUS_CHINESE.get(old_status, old_status)} {self.STATUS_ICON.get(old_status, '')}"
            new_chinese = f"{self.STATUS_CHINESE.get(new_status, new_status)} {self.STATUS_ICON.get(new_status, '')}"
            logger.debug(f"事务 ID={task_id} 状态: {old_chinese} → {new_chinese}")

        return success

    def get_task_collapsed(self, task_id: int) -> bool:
        """
        获取事务的折叠状态

        Args:
            task_id: 事务 ID

        Returns:
            是否折叠
        """
        task = self.db.get_task(task_id)
        if not task:
            return False
        return bool(task.get("collapsed", 0))

    def set_task_collapsed(self, task_id: int, collapsed: bool) -> bool:
        """
        设置事务的折叠状态

        Args:
            task_id: 事务 ID
            collapsed: 是否折叠

        Returns:
            是否成功
        """
        try:
            collapsed_int = 1 if collapsed else 0
            result = self.db.update_task(task_id, collapsed=collapsed_int)
            logger.debug(f"事务 ID={task_id} 折叠状态: {collapsed}")
            return result
        except Exception as e:
            import traceback

            logger.error(f"设置事务折叠状态失败: {e}\n{traceback.format_exc()}")
            return False

    def stop_task(
        self,
        task_id: int,
        recursive: bool = False,
        keep_context: bool = False,
        reason="",
        kill_process: bool = False,
    ) -> bool:
        """
        停止事务

        需求 §5.11: 递归停止事务树，杀死活跃进程

        Args:
            task_id: 事务 ID
            recursive: 是否递归停止所有子事务（默认 True）

        Returns:
            是否成功
        """
        task = self.db.get_task(task_id)
        if not task:
            return False

        current_status = task.get("status")
        if not reason:
            reason = traceback.format_stack()
        # 终态无法停止（已经是终态了）
        if current_status in TaskStatus.terminal_states():
            logger.info(f"{reason}, 事务 {task_id} 状态为 {current_status}（终态），无需停止")
            return True  # 幂等性返回成功

        to_stop_task_ids = []
        # 递归停止所有子事务
        if recursive:
            to_stop_tasks = self.db.get_descendants(task_id)
            for t in to_stop_tasks:
                if t.get("status") in [TaskStatus.RUNNING.value, TaskStatus.ACCEPTING.value]:
                    to_stop_task_ids.append(t.get("id"))
        # 🔧 关键修复：提取事务ID列表（而不是传入字典列表）
        to_stop_task_ids.append(task_id)
        # 🔧 修复：先更新状态，再杀进程！避免心跳监控在状态仍为running时检测到进程不存在而误重启
        if keep_context:
            count = self.db.update_tasks(to_stop_task_ids, status=TaskStatus.PAUSED.value)
        else:
            # 取消事务：先批量更新状态，再逐个保留费用统计并清空对话消息
            count = self.db.update_tasks(to_stop_task_ids, status=TaskStatus.CANCELLED.value)
            from peng.llm import MessageHistory

            for tid in to_stop_task_ids:
                t = self.db.get_task(tid)
                old_context = t.get("context") if t else ""
                if old_context:
                    history = MessageHistory()
                    if history.from_str(old_context):
                        new_context = history.reset_messages_keep_stats()
                        self.db.update_task(tid, context=new_context)
        if kill_process:
            self._kill_tasks_processes(
                to_stop_task_ids, caller="stop_task", reason=f"事务{task_id}停止"
            )
        # 事务被取消（keep_context==False）时，清理关联nest端的agent-browser浏览器进程
        # 事务被暂停（keep_context==True）时不清理，恢复后可能继续使用浏览器
        if not keep_context:
            for tid in to_stop_task_ids:
                self._cleanup_nest_browser(tid)
        # 🔧 Task#382：同步事务状态到关联的消息（暂停/取消时消息状态也需更新）
        sync_status = "paused" if keep_context else "cancelled"
        if task.get("message_id"):
            self._sync_task_status_to_message(task_id, task.get("message_id"), sync_status, reason)
        logger.info(f" 事务#{task_id}, {reason} 递归停止了 {count} 个子事务")
        return True

    # ==================== 暂停/恢复/重启（需求 6） ====================
    async def cancel_task(
        self, task_id: int, recursive: bool = True, kill_process: bool = True, reason: str = ""
    ) -> bool:
        return self.stop_task(
            task_id,
            recursive=recursive,
            keep_context=False,
            reason=reason,
            kill_process=kill_process,
        )

    async def pause_task(
        self, task_id: int, recursive: bool = False, kill_process: bool = False, reason: str = ""
    ) -> bool:
        return self.stop_task(
            task_id,
            recursive=recursive,
            keep_context=True,
            reason=reason,
            kill_process=kill_process,
        )

    async def resume_task(self, task_id: int, keep_context=True, reason="", force=False) -> bool:
        return await self.restart_task(
            task_id=task_id, keep_context=keep_context, reason=reason, force=force
        )

    async def restart_task(self, task_id: int, keep_context=False, reason="", force=True) -> bool:
        """
        彻底重新开始事务
        Args:
            task_id: 事务 ID
        Returns:
            是否成功
        """
        task = self.db.get_task(task_id)
        if task is None:
            return False

        current_status = task.get("status")
        non_restartable_statuses = [TaskStatus.RUNNING.value, TaskStatus.ACCEPTING.value]
        descendants = self.db.get_descendants(task_id)
        all_descendant_ids = [d["id"] for d in descendants]

        if current_status == TaskStatus.CANCELLED.value:
            keep_context = False

        if current_status == TaskStatus.PENDING.value and keep_context:
            logger.warning(f"事务#{task_id} 状态为 {current_status}，需用start/restart启动")
            return False

        if current_status in non_restartable_statuses:
            if force:
                if keep_context:
                    await self.pause_task(task_id, kill_process=True, reason=reason)
                else:
                    await self.cancel_task(task_id, reason=reason)
                # 等待未成功杀死的进程自行退出
                await asyncio.sleep(5)
            else:
                logger.warning(f"事务#{task_id} 状态为 {current_status}，需取消/暂停才能重启")
                return False

        context = task.get("context")
        if keep_context:
            # 保留原有 context，不更新此字段
            update_kwargs = {
                "status": TaskStatus.RUNNING.value,
                "progress": 0,
                "error": "",
                "result": "",
                "heartbeat_at": datetime.now().isoformat(),
                "started_at": datetime.now().isoformat(),
            }
        else:
            # 清空 context 中的对话消息，但保留累计统计（费用、token用量）
            # 🔧 Task#1326：restart 不再删除/归档 output 文件，保留原样
            # 新进程的输出会继续追加到同一文件，历史输出不会丢失
            # 保留累计统计（费用、token用量），仅清空对话消息
            new_context = ""
            if context:
                from peng.llm import MessageHistory

                history = MessageHistory()
                if history.from_str(context):
                    new_context = history.reset_messages_keep_stats()
                    logger.info(
                        f"事务#{task_id} 重启：保留累计统计 "
                        f"(费用={history.get_total_cost():.4f}元, "
                        f"总消耗={history.get_total_tokens_consumed()}tokens)，清空对话历史"
                    )
            update_kwargs = {
                "status": TaskStatus.RUNNING.value,
                "progress": 0,
                "error": "",
                "result": "",
                "context": new_context,
                "report": "",
                "heartbeat_at": datetime.now().isoformat(),
                "started_at": datetime.now().isoformat(),
                # 🔧 事务#2307（消息#10863）：全新重启清除粘性provider记录，让新进程
                # 按"同优先级空闲优先+LRU轮换"重新分配（restart/start优先选比较空闲
                # 的provider）；content显式@provider的用户意图保留。resume（保留上下文）
                # 走上面分支不清除——继续运行的事务默认不换provider，除非不可用。
                "provider": self.db._parse_provider_from_content(task.get("content") or ""),
            }

        success = self.db.update_task(task_id, **update_kwargs)
        if success:
            # restart/resume 时取消归档事务自身及其根消息
            self._unarchive_task_and_root_message(task_id)
            # 重新获取事务信息（包含更新后的状态）
            task = self.db.get_task(task_id)
            # 同步状态到关联的消息（设为 processing）
            if task.get("message_id"):
                self._sync_task_status_to_message(
                    task_id, task.get("message_id"), "restarted", "事务已重启"
                )
            # 🔧 Task#1326：仅在清空上下文的重启时通知前端清除输出缓存
            # （keep_context=True 的 resume 不改 output，无需通知）
            # output 文件保留不删除，前端清除 xterm 缓存后从文件末尾继续读取新输出
            if not keep_context:
                try:
                    from peng.webui.websocket import broadcast_task_update, manager as ws_manager

                    loop = ws_manager._loop
                    if loop and ws_manager.active_connections:
                        asyncio.run_coroutine_threadsafe(
                            broadcast_task_update(task_id, "restarted", {"task_id": task_id}),
                            loop,
                        )
                    else:
                        self.db._notify_webui_via_http(
                            "restarted", task_id, {"task_id": task_id}
                        )
                except Exception as e:
                    logger.debug(f"[restart_task] 广播 restarted 事件失败: {e}")
            info = ""
            old_pid = ""
            if context and keep_context:
                from peng.llm import MessageHistory

                history = MessageHistory()
                history.from_str(context)
                tokens = history.get_token_count()
                old_pid = task.get("process_id") if "pid" not in reason else None
                pid_info = f"，进程#{old_pid}" if old_pid else ""
                info = f"▶️ [{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] 事务#{task_id} {reason}{pid_info}，上下文（{tokens // 1000}K tokens），继续处理..."
            if info:
                self.db.add_task_info(task_id=task_id, info_content=info, dedup=True)
            pid = self._execute_task_process(task_id)
            if context and not keep_context:
                from peng.task.output_manager import get_output_manager
                from peng.common.colors import ANSI_COLORS

                info = f"🔄  [{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] 事务#{task_id} {reason}，历史信息已清除，重新开始处理，进程#{pid}..."
                get_output_manager().write_output(
                    task_id=task_id,
                    source="system",
                    content=f"{ANSI_COLORS.get('bright_yellow')}{info}{ANSI_COLORS.get('reset')}\n",
                )
        return success

    def _mark_all_info_read(self, task, read=True) -> None:
        """
        标记事务的所有info为未读

        Args:
            task_id: 事务ID
        """
        try:
            from peng.task.info import (
                parse_info_to_list_with_status,
                serialize_info_list,
            )

            task_id = task.get("id")
            current_info = task.get("info", "[]") or "[]"
            info_list_with_status = parse_info_to_list_with_status(current_info)
            # 将所有info标记为已读
            for item in info_list_with_status:
                item["read"] = read
            new_value = serialize_info_list(info_list_with_status)
            self.db.update_task(task_id, info=new_value)
            logger.info(f"[TaskManager] 已标记事务 {task_id} info为{'已读' if read else '未读'}。")

        except Exception as e:
            logger.warning(
                f"[TaskManager] 标记事务 {task_id} info为{'已读' if read else '未读'}失败: {e}"
            )

    def _get_all_descendant_ids(self, task_id: int) -> list[int]:
        """
        递归获取事务的所有子孙事务ID

        Args:
            task_id: 事务ID

        Returns:
            所有子孙事务ID列表
        """
        descendants = []
        children = self.db.get_child_task_ids(task_id)

        for child_id in children:
            descendants.append(child_id)
            descendants.extend(self._get_all_descendant_ids(child_id))

        return descendants

    def _is_task_timeout(self, task: dict[str, Any]) -> bool:
        """
        检查事务是否超时

        Args:
            task: 事务信息

        Returns:
            是否超时
        """
        timeout = task.get("timeout", 0)
        if timeout <= 0:
            return False

        started_at = task.get("started_at")
        if not started_at:
            return False

        try:
            from datetime import datetime

            start_time = datetime.fromisoformat(started_at)
            elapsed = (datetime.now() - start_time).total_seconds()
            return elapsed > timeout
        except Exception as e:
            logger.warning(f"[TaskManager] 检查事务超时时出错: {e}")
            return False

    def _mark_task_tree_interrupted(self, task_id: int) -> None:
        """
        标记事务树为中断状态

        Args:
            task_id: 事务ID
        """
        try:
            # 递归标记所有子孙事务
            all_task_ids = [task_id] + self._get_all_descendant_ids(task_id)

            for tid in all_task_ids:
                task = self.db.get_task(tid)
                if task and task.get("status") != TaskStatus.INTERRUPTED.value:
                    self.db.update_task(
                        tid,
                        status=TaskStatus.INTERRUPTED.value,
                        error="事务树被标记为中断",
                    )
                    logger.info(f"[TaskManager] 事务 {tid} 已标记为中断")

        except Exception as e:
            logger.error(
                f"[TaskManager] 标记事务树 {task_id} 为中断时出错: {e}\n{traceback.format_exc()}"
            )

    # ==================== 进程控制辅助方法 ====================

    def _kill_process(
        self,
        process_id: int,
        force: bool = True,
        caller: str = "TaskManager",
        reason: str = "unknown",
    ) -> bool:
        """
        杀死单个进程（包括所有子进程）

        Args:
            process_id: 进程 ID (os_pid)
            force: 是否强制杀死（使用 SIGKILL，默认 True）
            caller: 调用者
            reason: 杀死原因

        Returns:
            是否成功
        """
        if process_id <= 0:
            logger.warning(
                f"[{caller}] 进程ID无效 ({process_id}), reason={reason}, 跳过杀死操作\n"
                f"调用堆栈:\n{traceback.format_stack()[-5:]}"
            )
            return False

        from peng.common.platform import kill_process

        # 默认使用强制杀死 + 杀死整个进程树
        # 这确保 AI agent 创建的所有子进程都被正确终止
        success = kill_process(
            process_id, force=force, kill_tree=True, caller=caller, reason=reason
        )
        if success:
            logger.info(
                f"[{caller}] 成功杀死进程 {process_id} 及其子进程 - reason={reason}, force={force}"
            )
        else:
            logger.error(
                f"[{caller}] 杀死进程 {process_id} 失败 - "
                f"reason={reason}, force={force}\n调用堆栈:\n{traceback.format_stack()[-5:]}"
            )
        return success

    def _kill_tasks_processes(self, task_ids: list[int], caller, reason) -> None:
        """
        批量杀死事务列表中的活跃进程

        Args:
            task_ids: 事务 ID 列表
        """
        if not task_ids:
            return

        for task_id in task_ids:
            process_id = None
            try:
                task = self.db.get_task(task_id)
                if task:
                    process_id = task.get("process_id")
            except Exception as e:
                logger.error(f"从数据库获取事务 {task_id} 失败: {e}\n{traceback.format_exc()}")

            if process_id and process_id > 0:
                logger.info(f"正在杀死事务 {task_id} 的进程 {process_id}")
                self._kill_process(process_id, caller=caller, reason=reason)
        self.db.update_tasks(task_ids, process_id=0)

    def _validate_task_cost(self, task_id: int, task: dict) -> None:
        """费用校验（优化4）：事务完成时检查累计费用合理性

        检查规则：
        1. 累计费用为负 → 异常告警
        2. breakdown各provider费用之和与_total_cost偏差过大 → 告警
        3. 无任何LLM调用记录但有费用 → 告警

        Args:
            task_id: 事务ID
            task: 事务信息
        """
        try:
            context = task.get("context")
            if not context:
                return

            from peng.llm import MessageHistory

            history = MessageHistory()
            if not history.from_str(context):
                return

            total_cost = history.get_total_cost()
            breakdown = history.get_cost_breakdown()
            last_usage = history.get_last_usage()

            # 检查1：费用为负
            if total_cost < 0:
                logger.warning(
                    f"⚠️ [费用校验] 事务#{task_id} 累计费用为负: {total_cost:.6f}元"
                )

            # 检查2：breakdown费用之和 vs total_cost
            if breakdown:
                bd_sum = sum(v.get("cost", 0) for v in breakdown.values())
                if abs(bd_sum - total_cost) > 0.001:
                    logger.warning(
                        f"⚠️ [费用校验] 事务#{task_id} 费用不一致: "
                        f"breakdown合计={bd_sum:.6f}元 vs 总计={total_cost:.6f}元"
                    )

            # 检查3：有费用但无LLM调用记录
            if total_cost > 0 and not last_usage:
                logger.warning(
                    f"⚠️ [费用校验] 事务#{task_id} 有累计费用({total_cost:.6f}元)但无LLM调用记录"
                )

            # 正常情况记录info级别摘要
            if breakdown:
                parts = []
                for p_name, bd in breakdown.items():
                    parts.append(
                        f"{p_name}: {bd['cost']:.4f}元({bd['calls']}次调用, "
                        f"缓存命中{bd['input_hit']}/{bd['input_hit'] + bd['input_miss']})"
                    )
                logger.info(
                    f"💰 [费用校验] 事务#{task_id} 总费用={total_cost:.4f}元, "
                    f"明细: {'; '.join(parts)}"
                )
            elif total_cost > 0:
                logger.info(
                    f"💰 [费用校验] 事务#{task_id} 总费用={total_cost:.4f}元（无breakdown明细，可能为旧格式context）"
                )
        except Exception as e:
            logger.debug(f"[费用校验] 事务#{task_id} 校验失败: {e}")

    def _cleanup_nest_browser(self, task_id: int) -> None:
        """清理事务关联nest端的agent-browser浏览器进程

        仅在事务进入终态（completed/cancelled/failed）时调用。
        使用 agent-browser close --all 优雅关闭，不强杀。
        异步后台执行，不阻塞主流程；失败仅记录日志。

        Args:
            task_id: 事务 ID
        """
        try:
            task = self.db.get_task(task_id)
            if not task:
                return
            nest_name = task.get("nest")
            if not nest_name:
                return

            # 在后台线程中异步执行，避免阻塞事务处理
            import threading

            def _do_cleanup(nest: str):
                try:
                    import asyncio
                    from peng.nest.client import bash

                    async def _async_cleanup():
                        try:
                            resp = await bash(
                                "agent-browser close --all",
                                timeout=10000,
                                nest_name=nest,
                            )
                            if resp.error:
                                logger.debug(f"清理 nest '{nest}' agent-browser 失败: {resp.error}")
                            else:
                                logger.info(f"已清理 nest '{nest}' 的 agent-browser 会话")
                        except Exception as e:
                            logger.debug(f"清理 nest '{nest}' agent-browser 异常: {e}")

                    try:
                        loop = asyncio.get_running_loop()
                        asyncio.ensure_future(_async_cleanup())
                    except RuntimeError:
                        asyncio.run(_async_cleanup())
                except Exception as e:
                    logger.debug(f"清理 nest '{nest}' agent-browser 后台任务失败: {e}")

            thread = threading.Thread(
                target=_do_cleanup,
                args=(nest_name,),
                daemon=True,
                name=f"cleanup-browser-{nest_name}",
            )
            thread.start()
            logger.info(
                f"已启动 nest '{nest_name}' agent-browser 清理后台任务（事务#{task_id}终态）"
            )

        except Exception as e:
            logger.debug(f"启动事务#{task_id}的nest浏览器清理失败: {e}")

    # ==================== 事务 - 消息状态同步（需求 §3.2.5/§3.2.6） ====================

    def _sync_task_status_to_message(
        self,
        task_id: int,
        message_id: int,
        status: str,
        result: str | None = None,
    ) -> None:
        """
        同步事务状态到关联的消息（需求 §3.2.5）

        Args:
            task_id: 事务 ID
            message_id: 消息 ID
            status: 事务状态 (running/paused/failed/cancelled)
            result: 结果或错误信息
        """
        try:
            # 获取事务关联的消息数据库路径
            task = self.db.get_task(task_id)
            if not task:
                logger.error(f"事务 #{task_id} 不存在")
                return

            message_id = task.get("message_id")
            if not message_id:
                return

            # 子事务的状态变化不应直接影响消息状态，消息状态由根事务控制
            if task.get("parent_task_id"):
                return

            # 使用相同的数据库（PostgreSQL 使用连接池）
            mm = self.get_message_manager()
            message = mm.get_message(message_id)

            if not message:
                logger.error(f"消息 #{message_id} 不存在，无法同步事务 #{task_id} 状态")
                return

            if status == "failed":
                pass  # 事务失败
            elif status == "cancelled":
                pass  # 事务已取消
            elif status == "paused":
                pass  # 事务已暂停
            elif status == "resumed":
                pass  # 事务已恢复
            elif status == "restarted":
                pass  # 事务已重启
            else:
                pass  # 状态更新完成

            # 同步消息状态
            if status == "failed":
                mm.mark_failed(message_id, result)
            elif status == "paused":
                mm.mark_paused(message_id, "事务已暂停")
            elif status == "cancelled":
                mm.mark_cancelled(message_id, "事务已取消")
            elif status in ("resumed", "restarted"):
                # 恢复或重启后，消息状态变为 processing（强制转换）
                mm.db.mark_processing(message_id, auto_reply=False, force=True)
                # 重启时自动取消归档
                if status == "restarted":
                    mm.unarchive_message(message_id)

            logger.info(f"已同步事务 #{task_id} 状态 ({status}) 到消息 #{message_id}")

        except Exception as e:
            logger.error(
                f"同步事务 #{task_id} 状态到消息 #{message_id} 失败：{e}\n{traceback.format_exc()}"
            )

    def _sync_task_completion_to_message(
        self,
        task_id: int,
        message_id: int,
        result: str | None = None,
    ) -> None:
        """
        事务完成后同步状态到消息并发送验收报告（需求 §3.2.6）

        Args:
            task_id: 事务 ID
            message_id: 消息 ID
            result: 事务执行结果
        """
        try:
            task = self.db.get_task(task_id)
            if not task:
                logger.error(f"事务 #{task_id} 不存在，无法同步状态")
                return

            message_id = task.get("message_id")
            if not message_id:
                return

            # 子事务的状态变化不应直接影响消息状态，消息状态由根事务控制
            if task.get("parent_task_id"):
                return

            # 使用相同的数据库（PostgreSQL 使用连接池）
            mm = self.get_message_manager()
            message = mm.get_message(message_id)

            if not message:
                logger.error(f"消息 #{message_id} 不存在，无法同步事务 #{task_id} 完成状态")
                return
            mm.db.mark_done(message_id, force=True, auto_reply=False)

            logger.info(f"已同步事务 #{task_id} 完成状态到消息 #{message_id}")

        except Exception as e:
            logger.error(
                f"同步事务 #{task_id} 完成状态到消息 #{message_id} 失败：{e}\n{traceback.format_exc()}"
            )

    def update_progress(self, task_id: int, progress: int) -> bool:
        """更新进度"""
        if progress < 0:
            progress = 0
        elif progress > 100:
            progress = 100
        return self.db.update_task(task_id, progress=progress)

    # ==================== 步骤管理 ====================

    def add_step(
        self,
        task_id: int,
        name: str,
        order: int = 0,
        description: str | None = None,
    ) -> int:
        """添加步骤"""
        return self.db.add_step(
            task_id=task_id,
            name=name,
            order=order,
            description=description,
        )

    def get_steps(self, task_id: int) -> list[dict[str, Any]]:
        """获取步骤列表"""
        return self.db.get_steps(task_id)

    def start_step(self, step_id: int) -> bool:
        """开始步骤"""
        return self.db.update_step_status(step_id, StepStatus.RUNNING)

    def complete_step(self, step_id: int, result: str | None = None) -> bool:
        """完成步骤"""
        success = self.db.update_step_status(step_id, StepStatus.COMPLETED)
        if success and result:
            # 更新步骤结果
            pass  # 可以扩展数据库方法
        return success

    def fail_step(self, step_id: int, error: str) -> bool:
        """标记步骤失败"""
        return self.db.update_step_status(step_id, StepStatus.FAILED, error)

    # ==================== 依赖管理 ====================

    def add_dependency(self, task_id: int, depends_on_id: int) -> bool:
        """添加依赖关系"""
        return self.db.add_dependency(task_id, depends_on_id)

    def get_dependencies(self, task_id: int) -> list[int]:
        """获取依赖的事务 ID 列表"""
        return self.db.get_dependencies(task_id)

    def get_dependents(self, task_id: int) -> list[int]:
        """获取依赖此事务的事务 ID 列表"""
        return self.db.get_dependents(task_id)

    # ==================== 心跳机制 ====================
    def update_heartbeat(self, task_id: int | None) -> bool:
        """
        更新心跳

        同时同步更新关联消息的心跳（需求 §3.2.1）
        """
        success = self.db.update_heartbeat(task_id)

        # 同步更新关联消息的心跳
        if success:
            task = self.db.get_task(task_id)
            if task and task.get("message_id"):
                try:
                    from peng.message.database import MessageDatabase

                    msg_db = MessageDatabase()
                    now = datetime.now().isoformat()
                    # 使用MessageDatabase的update_message方法更新消息心跳
                    msg_db.update_message(task["message_id"], heartbeat_at=now)
                except Exception as e:
                    logger.error(f"同步消息心跳失败: {e}\n{traceback.format_exc()}")

        return success

    def get_timed_out_tasks(self, timeout_seconds: int = 300) -> list[dict[str, Any]]:
        """获取超时事务"""
        return self.db.get_timed_out_tasks(timeout_seconds)

    # ==================== 统计和查询 ====================

    def count_by_status(self) -> dict[str, int]:
        """统计各状态事务数量"""
        counts = {}
        for status in TaskStatus:
            tasks = self.db.get_by_status(status)
            counts[status.value] = len(tasks)
        return counts

    def get_tasks_by_message(self, message_id: int) -> list[dict[str, Any]]:
        """按消息 ID 获取事务"""
        return self.db.get_by_message(message_id)

    # ==================== 子事务管理 ====================

    def create_subtask(
        self,
        parent_task_id: int,
        content: str = "",
        **kwargs: Any,
    ) -> int:
        """
        创建子事务（通用字典参数形式）

        Args:
            parent_task_id: 父事务 ID（必填）
            content: 事务内容
            **kwargs: 其他字段（description, priority, task_type, timeout, deadline,
                metadata, prompt, workdir, small_model, info, creator, is_complex, timer）

        Returns:
            子事务 ID
        """
        # 获取父事务信息
        parent_task = self.db.get_task(parent_task_id)
        if not parent_task:
            raise ValueError(f"父事务 {parent_task_id} 不存在")

        # 如果 workdir 未设置，从父事务继承 workdir
        if not kwargs.get("workdir") and parent_task.get("workdir"):
            kwargs["workdir"] = parent_task.get("workdir")
            logger.debug(f"[Task] 从父事务#{parent_task_id}继承工作目录：{kwargs['workdir']}")

        # 构建创建参数
        create_kwargs = {
            "title": "",
            "content": content,
            "parent_task_id": parent_task_id,
            "root_id": parent_task.get("root_id") or parent_task_id,
            "nest": parent_task.get("nest") or None,
        }
        create_kwargs.update(kwargs)

        # 创建子事务
        child_id = self.db.create_task(**create_kwargs)

        logger.info(f"创建子事务 #{child_id}，父事务：#{parent_task_id}")
        return child_id

    def get_parent(self, task_id: int) -> list[dict[str, Any]]:
        parent_id = self.get_parent_id(task_id)
        return self.db.get_task(parent_id) if parent_id > 0 else []

    def get_parent_id(self, task_id: int) -> int:
        task = self.db.get_task(task_id)
        return 0 if not task else task.get("parent_task_id")

    def get_children(self, task_id: int) -> list[dict[str, Any]]:
        """
        获取某个事务的所有子事务

        Args:
            task_id: 事务 ID

        Returns:
            子事务列表（包含完整信息）
        """
        return self.db.get_children(task_id)

    def get_pending_children(self, parent_id: int) -> list[dict[str, Any]]:
        """
        获取某个事务的待执行子事务

        Args:
            parent_id: 父事务 ID

        Returns:
            子事务列表
        """
        return self.db.get_pending_children(parent_id)

    def get_running_children(self, parent_id: int) -> list[dict[str, Any]]:
        """
        获取某个事务的运行中子事务

        Args:
            parent_id: 父事务 ID

        Returns:
            子事务列表
        """
        return self.db.get_running_children(parent_id)

    def get_completed_children(self, parent_id: int) -> list[dict[str, Any]]:
        """
        获取某个事务的已完成子事务

        Args:
            parent_id: 父事务 ID

        Returns:
            子事务列表
        """
        return self.db.get_completed_children(parent_id)

    def get_failed_children(self, parent_id: int) -> list[dict[str, Any]]:
        """
        获取某个事务的失败子事务

        Args:
            parent_id: 父事务 ID

        Returns:
            子事务列表
        """
        return self.db.get_failed_children(parent_id)

    def children_all_completed(self, parent_id: int) -> bool:
        """
        检查某个事务的所有子事务是否都已完成

        Args:
            parent_id: 父事务 ID

        Returns:
            True if all children are completed, False otherwise
        """
        return self.db.children_all_completed(parent_id)

    def add_child_dependency(
        self, parent_task_id: int, child_task_id: int, depends_on_child_id: int
    ) -> bool:
        """
        添加子事务之间的依赖关系

        只有同一父事务的子事务才能建立依赖关系。

        Args:
            parent_task_id: 父事务 ID
            child_task_id: 子事务 ID
            depends_on_child_id: 依赖的子事务 ID

        Returns:
            True if dependency added successfully, False otherwise
        """
        # 获取两个子事务的信息
        child1 = self.db.get_task(child_task_id)
        child2 = self.db.get_task(depends_on_child_id)

        if not child1 or not child2:
            logger.warning(f"子事务不存在：{child_task_id} 或 {depends_on_child_id}")
            return False

        # 验证两个子事务属于同一个父事务
        child1_parent = child1.get("parent_task_id")
        child2_parent = child2.get("parent_task_id")

        if child1_parent != parent_task_id or child2_parent != parent_task_id:
            logger.warning(f"子事务 {child_task_id} 和 {depends_on_child_id} 不属于同一父事务")
            return False

        # 添加依赖关系
        return self.db.add_dependency(child_task_id, depends_on_child_id)

    def get_waiting_task_for_chain(self, chain_head_id: int) -> dict[str, Any] | None:
        """
        获取消息链的等待回复事务

        用于多轮对话优化：检测消息链是否有等待中的事务。

        Args:
            chain_head_id: 消息链链首ID

        Returns:
            等待中的事务字典，如果没有则返回None
        """
        # 查找与消息链链首关联的事务
        tasks = self.list_tasks(message_id=chain_head_id, status="running")

        if tasks:
            # 返回第一个running的事务
            return tasks[0]
        return None

    def archive_task(self, task_id: int, recursive: bool = True) -> bool:
        """
        归档事务

        Args:
            task_id: 事务ID
            recursive: 是否递归归档子事务（默认True）

        Returns:
            是否成功
        """
        try:
            if recursive:
                archived_count = self.db.archive_task_tree(task_id)
                return archived_count > 0
            else:
                return self.db.mark_archived(task_id)
        except Exception as e:
            logger.error(f"归档事务失败: {e}")
            return False

    def unarchive_task(self, task_id: int, recursive: bool = True) -> bool:
        """
        取消归档事务

        Args:
            task_id: 事务ID
            recursive: 是否递归取消归档子事务（默认True）

        Returns:
            是否成功
        """
        try:
            if recursive:
                unarchived_count = self.db.unarchive_task_tree(task_id)
                return unarchived_count > 0
            else:
                return self.db.unarchive_task(task_id)
        except Exception as e:
            logger.error(f"取消归档事务失败: {e}")
            return False

    def _unarchive_task_and_root_message(self, task_id: int) -> None:
        """
        取消归档事务自身及其关联的根消息（链首消息）

        用于 start/resume/restart 操作时自动取消归档。

        Args:
            task_id: 事务ID
        """
        try:
            # 取消归档事务自身（递归子事务）
            self.unarchive_task(task_id)

            # 取消归档根消息
            task = self.db.get_task(task_id)
            if not task or not task.get("message_id"):
                return

            message_id = task.get("message_id")
            mm = self.get_message_manager()
            message = mm.get_message(message_id)
            if not message:
                return

            # 找到根消息：chain_head_id 指向链首消息（根消息）
            root_message_id = message.get("chain_head_id") or message_id
            mm.unarchive_message(root_message_id)

            logger.info(f"已取消归档事务 #{task_id} 及其根消息 #{root_message_id}")
        except Exception as e:
            logger.error(f"取消归档事务 #{task_id} 及其根消息失败: {e}\n{traceback.format_exc()}")
