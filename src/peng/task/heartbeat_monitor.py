"""
消息和事务心跳检测

定期检查处理中的消息和事务心跳状态，自动处理异常情况。

需求 §3.2 心跳监测功能：
- §3.2.1 更新调用链上级的心跳
- §3.2.2 超时检测
- §3.2.3 事务进程失效检测
- §3.2.4 中断处理
- §3.2.5 同步事务状态到消息
- §3.2.6 事务完成后更新消息状态
- §3.2.7 暂停处理
- §3.2.8 归档过期消息/事务
"""

import asyncio
import contextlib
import logging
import os
import re
import traceback
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

import psycopg2

from peng.common.colors import ANSI_COLORS

logger = logging.getLogger(__name__)


@dataclass
class HeartbeatStatus:
    """心跳状态"""

    id: int
    type: str  # 'message' 或 'task'
    process_id: int | None
    locked_at: datetime | None
    heartbeat_at: datetime | None
    is_expired: bool
    seconds_since_heartbeat: float
    status: str = ""

    def to_dict(self) -> dict:
        """转换为字典"""
        return {
            "id": self.id,
            "type": self.type,
            "process_id": self.process_id,
            "is_expired": self.is_expired,
            "seconds_since_heartbeat": self.seconds_since_heartbeat,
            "status": self.status,
        }


@dataclass
class HeartbeatCheckResult:
    """心跳检查结果"""

    expired_messages: list[int] = field(default_factory=list)
    interrupted_messages: list[int] = field(default_factory=list)
    interrupted_tasks: list[int] = field(default_factory=list)
    synced_messages: list[int] = field(default_factory=list)  # 事务状态同步到消息
    deleted_messages: list[int] = field(default_factory=list)  # 待删除消息已处理
    started_tasks: list[int] = field(default_factory=list)  # 自动启动的待定事务


class HeartbeatMonitor:
    """
    心跳监控器

    定期检查处理中的消息和事务心跳状态，自动处理异常情况。

    需求 §3.2:
    - 归档超过7天（根据心跳算）的暂停/中断/失败消息和事务
    - 归档是独立标记，不是状态的一部分
    """

    DEFAULT_TIMEOUT = 300  # 5分钟超时
    DEFAULT_CHECK_INTERVAL = 7  # 每7秒检查一次（需求 §3.2）
    DEFAULT_ARCHIVE_DAYS = 7  # 默认归档天数

    def __init__(
        self,
        db_path: Path | None = None,
        tasks_db_path: Path | None = None,
        timeout_seconds: int = DEFAULT_TIMEOUT,
        check_interval: int = DEFAULT_CHECK_INTERVAL,
        archive_days: int = DEFAULT_ARCHIVE_DAYS,
        auto_start_pending_tasks: bool = True,
    ):
        """
        初始化心跳监控器

        注意：PostgreSQL 不需要数据库路径，使用连接池。

        Args:
            db_path: 消息数据库路径（已废弃，PostgreSQL 不需要）
            tasks_db_path: 事务数据库路径（已废弃，PostgreSQL 不需要）
            timeout_seconds: 超时时间（秒）
            check_interval: 检查间隔（秒）
            archive_days: 归档天数（超过此天数的暂停/中断/失败消息自动归档）
            auto_start_pending_tasks: 是否自动启动待定事务（默认True）
        """
        self.timeout_seconds = timeout_seconds
        self.check_interval = check_interval
        self.archive_days = archive_days
        self.auto_start_pending_tasks = auto_start_pending_tasks
        self._running = False
        self._task: asyncio.Task | None = None

        # 使用 MessageDatabase 和 TaskDatabase 代替直接数据库访问
        from peng.message.database import MessageDatabase
        from peng.task.database import TaskDatabase

        self.msg_db = MessageDatabase()
        self.task_db = TaskDatabase()

    def _get_messages_connection(self):
        """获取消息数据库连接"""
        return self.msg_db._get_connection()

    def _get_tasks_connection(self):
        """获取事务数据库连接"""
        return self.task_db._get_connection()

    def _get_default_heartbeat_timeout(self) -> int:
        """从配置获取事务默认心跳超时时间"""
        from peng.config.loader import get_default_heartbeat_timeout

        return get_default_heartbeat_timeout()

    # ==================== 进程检查 ====================

    def _get_service_start_time(self) -> datetime | None:
        """获取当前服务进程的启动时间

        使用 psutil 获取当前进程的创建时间（create_time）。
        如果 psutil 不可用或获取失败，返回 None。

        Returns:
            服务进程启动时间，或 None
        """
        try:
            import psutil

            proc = psutil.Process(os.getpid())
            return datetime.fromtimestamp(proc.create_time())
        except Exception:
            return None

    def _get_service_restart_hint(self, threshold_minutes: int = 5) -> str:
        """检查服务进程是否刚重启不久，返回补充提示信息

        如果服务进程启动时间不早于 threshold_minutes 分钟前，
        说明服务刚重启，可能是服务重启导致的进程失效而非真正的进程崩溃。

        Args:
            threshold_minutes: 阈值分钟数（默认5分钟）

        Returns:
            补充提示字符串，如果服务启动时间较早则返回空字符串
        """
        start_time = self._get_service_start_time()
        if start_time is None:
            return ""
        elapsed = datetime.now() - start_time
        if elapsed < timedelta(minutes=threshold_minutes):
            return f"{ANSI_COLORS.get('bright_yellow', '')}（服务于 {start_time.strftime('%H:%M:%S')} 重启，距今仅 {int(elapsed.total_seconds())} 秒）{ANSI_COLORS.get('reset', '')}"
        return ""

    def _check_os_process_exists(self, os_pid: int | None) -> bool:
        """
        技术说明
        os.kill(pid, 0) 是检查进程是否存在的标准方法：
        - 信号 0 是特殊信号，不会实际发送任何信号给进程
        - 如果进程存在，什么都不会发生，返回 True
        - 如果进程不存在，会抛出 ProcessLookupError 或 OSError，返回 False

        Args:
            os_pid: 操作系统进程ID， None 或 0 表示没有关联子进程

        Returns:
            操作系统进程是否存在
        """
        if os_pid is None or os_pid == 0:
            return True  # 没有子进程，不需要检查
        # 使用跨平台封装检查进程是否存在
        from peng.common.platform import process_exists

        return process_exists(os_pid)

    # ==================== §3.2.2-3.2.3: 消息心跳检查 ====================

    def check_message_heartbeats(self) -> list[int]:
        """
        检查处理中消息的状态（需求 §3.2.2-3.2.3）

        包括：
        1. 进程已失效的消息 -> 设置为 interrupted
        2. 关联事务已中断的消息 -> 设置为 interrupted

        注意：心跳超时检测由事务级别 check_task_heartbeats 负责，消息级别不再检测心跳超时。

        Returns:
            interrupted_ids
        """
        # 使用MessageDatabase获取所有 processing 状态的消息
        from peng.message.database import MessageDatabase

        msg_db = MessageDatabase()
        processing_messages = msg_db.get_processing_messages_with_heartbeat()

        if not processing_messages:
            return []

        interrupted_ids = []

        for msg in processing_messages:
            msg_id = msg["id"]
            process_id = msg["process_id"]  # 这是 os_pid

            # 1. 首先检查关联事务状态：如果事务中断，消息也应该中断
            tasks = msg_db.get_tasks_by_message_status(msg_id)
            for task in tasks:
                if task["status"] == "interrupted":
                    interrupted_ids.append(msg_id)
                    logger.warning(f"⚠️ 消息 #{msg_id} 的关联事务 #{task['id']} 已中断")
                    break  # 一个事务中断就够了，无需继续检查

            if msg_id in interrupted_ids:
                continue  # 已标记为中断，跳过其他检查

            # 2. 没有 process_id 的消息（主线程处理）- 跳过其他检查
            if process_id is None or process_id == 0:
                continue

            # 3. 检查进程是否存在
            if not self._check_os_process_exists(process_id):
                interrupted_ids.append(msg_id)
                logger.warning(f"⚠️ 消息 #{msg_id} 进程 {process_id} 已失效")

        # 更新进程失效的消息状态为 interrupted
        if interrupted_ids:
            msg_db.update_messages_status_batch(
                interrupted_ids,
                "interrupted",
                {
                    "locked_by": None,
                    "locked_at": None,
                    "heartbeat_at": None,
                },
            )
            logger.info(f"🔄 已将 {len(interrupted_ids)} 条消息状态设为 interrupted")

        return interrupted_ids

    # ==================== §3.2.3: 事务心跳检查 ====================

    def _task_has_output_or_reply(self, message_id: int, task_id: int) -> bool:
        """
        检查事务是否已有输出或回复（用于判断简单事务是否已完成）

        Args:
            message_id: 消息ID
            task_id: 事务ID

        Returns:
            是否已有输出或回复
        """
        try:
            # 1. 检查消息是否有回复（通过MessageDatabase）
            reply_count = self.msg_db.count_reply_by_message_id(message_id)
            if reply_count > 0:
                logger.debug(
                    f"消息 #{message_id} 已有 {reply_count} 条回复，事务 #{task_id} 可能已完成"
                )
                return True

            # 2. 检查是否有输出文件
            from pathlib import Path

            from peng.config.loader import load_config

            try:
                config = load_config()
                output_dir = (
                    config.paths.output_dir if hasattr(config, "paths") else Path("data/output")
                )
                if output_dir.exists():
                    # 检查是否有该事务的输出文件
                    output_file = output_dir / f"task-{task_id}.out"
                    if output_file.exists() and output_file.stat().st_size > 0:
                        logger.debug(f"事务 #{task_id} 已有输出文件: {output_file.name}")
                        return True
            except Exception as e:
                logger.debug(f"检查输出文件失败: {e}")

            return False

        except Exception as e:
            logger.warning(f"检查事务 #{task_id} 输出/回复失败: {e}\n{traceback.format_exc()}")
            return False

    def _try_mark_task_completed(self, task_id: int) -> bool:
        """
        尝试标记事务为已完成（用于简单事务完成但状态更新延迟的情况）

        Args:
            task_id: 事务ID

        Returns:
            是否成功更新
        """
        try:
            # 通过TaskDatabase标记事务为完成
            success = self.task_db.mark_task_completed_if_running(task_id)
            if success:
                logger.info(f"✅ 事务 #{task_id} 已自动标记为完成（简单事务完成但状态更新延迟）")
                return True
        except Exception as e:
            logger.warning(f"标记事务 #{task_id} 为完成失败: {e}\n{traceback.format_exc()}")
        return False

    # ==================== §3.2.5: 同步事务状态到消息 ====================
    def sync_task_status_to_messages(self) -> list[int]:
        """
        同步事务状态到关联的消息（需求 §3.2.5）

        同步规则（任何不一致都同步）：
        - pending -> pending
        - running -> processing
        - completed -> done
        - paused -> paused
        - interrupted -> interrupted
        - failed -> failed
        - cancelled -> cancelled

        Returns:
            被同步的消息ID列表
        """
        synced_ids = []

        # 事务状态到消息状态的映射
        status_map = {
            "pending": "read",
            "running": "processing",
            "accepting": "accepting",  # Bug #2修复
            "accepted": "done",  # Bug #1修复
            "paused": "paused",
            "interrupted": "interrupted",
            "completed": "done",
            "failed": "failed",
            "cancelled": "cancelled",
        }

        # 事务状态优先级（值越大优先级越高）
        # 当同一消息关联多个事务时，选择最高优先级的状态
        # Bug #3修复：completed/accepted应该是最高优先级（终态）
        TASK_STATUS_PRIORITY = {
            "accepted": 10,  # Bug #3修复：验收完成，最高优先级
            "completed": 10,  # Bug #3修复：事务完成，最高优先级
            "done": 10,  # 完成状态（同completed）
            "running": 9,  # Bug #3修复：降低优先级
            "processing": 9,  # 处理中（同running）
            "accepting": 8,  # 验收中
            "pending": 7,  # 等待中
            "paused": 6,  # 暂停
            "interrupted": 5,  # 中断
            "cancelled": 4,  # 取消
            "failed": 3,  # 失败
        }

        # 1. 从 tasks.db 获取所有根事务的 message_id 和 status（通过TaskDatabase）
        # 🔧 Task#382修复：只使用根事务状态，子事务不应影响消息状态
        # （子事务可能继承父事务的message_id，但其状态不应覆盖根事务的状态）
        # 使用优先级选择：同一消息关联多个根事务时，选择最高优先级的状态
        task_status_map = {}  # message_id -> (task_id, task_status, priority)
        all_tasks = self.task_db.get_all_tasks_with_message_id(root_only=True)

        for row in all_tasks:
            msg_id = row["message_id"]
            task_id = row["id"]
            task_status = row["status"]
            priority = TASK_STATUS_PRIORITY.get(task_status, 0)

            # 如果消息还没有记录，或者当前事务优先级更高，则更新
            if msg_id not in task_status_map:
                task_status_map[msg_id] = (task_id, task_status, priority)
            else:
                _, _, existing_priority = task_status_map[msg_id]
                if priority > existing_priority:
                    task_status_map[msg_id] = (task_id, task_status, priority)

        if not task_status_map:
            return []

        # 2. 从 messages.db 获取所有消息（通过MessageDatabase）
        all_messages = self.msg_db.get_all_messages()

        if not all_messages:
            return []

        for row in all_messages:
            msg_id = row["id"]
            msg_status = row["status"]
            if msg_id not in task_status_map:
                continue

            task_id, task_status, _ = task_status_map[msg_id]
            expected_status = status_map.get(task_status)

            # 状态不一致就同步
            if expected_status and msg_status != expected_status:
                # 通过MessageDatabase批量更新消息状态
                self.msg_db.update_messages_status_by_ids(
                    [msg_id], expected_status, datetime.now().isoformat()
                )
                synced_ids.append(msg_id)
                logger.info(
                    f"🔄 已同步事务状态 ({task_status}) 到消息 #{msg_id} ({msg_status} -> {expected_status})"
                )

        return synced_ids

    # ==================== §3.2.6: 事务完成后更新消息状态 ====================

    def update_completed_task_messages(self) -> list[int]:
        """
        事务完成后更新消息状态（需求 §3.2.6）

        如果事务树状态为已验收（completed），消息状态为 processing，
        则更新消息状态为 done。
        Returns:
            被更新的消息ID列表
        """
        completed_ids = []

        # 1. 从 tasks.db 获取已验收完成的事务的 message_id（通过TaskDatabase）
        completed_message_ids = set(self.task_db.get_distinct_message_ids_by_status("completed"))

        if not completed_message_ids:
            return []

        # 2. 从 messages.db 获取 processing 状态且在完成列表中的消息（通过MessageDatabase）
        messages_to_update = self.msg_db.get_messages_by_status_and_ids(
            "processing", list(completed_message_ids)
        )

        for row in messages_to_update:
            msg_id = row["id"]
            # 通过MessageDatabase更新消息状态
            self.msg_db.update_messages_status_by_ids([msg_id], "done", datetime.now().isoformat())
            completed_ids.append(msg_id)
            logger.info(f"✅ 事务完成，已更新消息 #{msg_id} 状态为 done")

        return completed_ids

    # ==================== §3.2.7: 暂停处理 ====================

    def handle_paused_messages(self) -> list[int]:
        """
        处理暂停的消息（需求 §3.2.7）

        如果消息状态为 paused 而对应事务未暂停，更新事务状态为 paused。
        Returns:
            被处理的事务ID列表
        """
        paused_task_ids = []

        # 1. 从 messages.db 获取暂停的消息（通过MessageDatabase）
        paused_msg_ids = self.msg_db.get_paused_message_ids()

        if not paused_msg_ids:
            return []

        # 2. 从 tasks.db 获取关联的事务（通过TaskDatabase）
        tasks = self.task_db.get_tasks_by_message_ids_list(paused_msg_ids)

        # 3. 更新需要暂停的事务
        tasks_to_pause = []  # [task_id, ...]
        for row in tasks:
            task_status = row["status"]
            task_id = row["id"]
            # 如果事务未暂停且非终态，需要暂停
            if task_status not in ("paused", "completed", "cancelled", "failed"):
                tasks_to_pause.append(task_id)

        if tasks_to_pause:
            # 批量更新事务状态为暂停（通过TaskDatabase）
            for task_id in tasks_to_pause:
                self.task_db.update_task(task_id, status="paused", error="关联消息已暂停")
                paused_task_ids.append(task_id)
                logger.info(f"⏸️ 已同步消息暂停状态到事务 #{task_id}")

        return paused_task_ids

    # ==================== §3.2.8: 归档过期消息/事务 ====================

    def archive_old_messages(self) -> list[int]:
        """
        归档超过指定天数的暂停/中断/失败消息（需求 §3.2.8）

        归档条件：
        - 状态为 paused/interrupted/failed/cancelled
        - 归档时间（archived_at）超过 archive_days 天，或从未设置过归档时间
        - 尚未被归档（is_archived = 0）
        Returns:
            被归档的消息ID列表
        """
        from peng.message.database import MessageDatabase

        msg_db = MessageDatabase()

        # 查询符合条件的消息ID
        to_archive = msg_db.get_messages_by_status_with_archived(
            ["paused", "interrupted", "failed", "cancelled"],
            archive_threshold=(datetime.now() - timedelta(days=self.archive_days)).isoformat(),
            is_archived=0,
        )

        if not to_archive:
            return []

        # 禁止归档 消息#1
        if 1 in to_archive:
            to_archive.remove(1)

        # 批量归档
        msg_db.update_messages_archived(to_archive)
        logger.info(f"📦 已归档 {len(to_archive)} 条过期消息")
        return to_archive

    # ==================== §3.2.10: 处理 interrupted 消息 ====================

    async def process_interrupted_messages(self) -> int:
        """
        处理 interrupted 状态的消息（新需求）

        规则：
        - 只有在没有任何 processing 状态的消息时才处理
        - 每次只处理一条 interrupted 消息
        - 跳过发送者是 AGENT_NAME 自己的消息

        Returns:
            已处理的消息数量（0 或 1）
        """
        from peng.config import get_agent_name

        AGENT_NAME = get_agent_name()
        processed_count = 0

        try:
            from peng.message.manager import MessageManager

            # 1. 检查是否有 processing 状态的消息（通过MessageDatabase）
            processing_count = self.msg_db.count_processing_messages()

            # 如果有 processing 状态的消息，跳过
            if processing_count > 1:
                return 0

            # 2. 获取 interrupted 状态且发送者不是 AGENT_NAME 的消息（通过MessageDatabase）
            row = self.msg_db.get_message_by_status_and_sender("interrupted", AGENT_NAME, 1)

            if not row:
                return 0

            interrupted_msg_id = row["id"]

            # 检查是否是回复消息，如果是则跳过
            interrupted_msg = self.msg_db.get_message(interrupted_msg_id)
            if interrupted_msg and interrupted_msg.get("reply_to") is not None:
                logger.debug(f"跳过处理 interrupted 消息 #{interrupted_msg_id}：这是回复消息")
                return 0

            # 3. 处理这条 interrupted 消息（使用 resume 而不是 restart，以保留 context）
            msg_manager = MessageManager()
            success = await msg_manager.resume_message(interrupted_msg_id)

            if success:
                logger.info(f"✅ 已恢复 interrupted 消息 #{interrupted_msg_id}")
                processed_count = 1
            else:
                logger.warning(f"⚠️ 恢复 interrupted 消息 #{interrupted_msg_id} 失败")

        except Exception as e:
            logger.error(f"处理 interrupted 消息失败: {e}\n{traceback.format_exc()}")

        return processed_count

    # ==================== 自动启动 wait 条件满足的事务 ====================

    async def auto_start_waiting_tasks(self) -> list[int]:
        """
        自动启动 wait 条件已满足的 pending 事务
        Returns:
            已插入 start 控制的事务 ID 列表
        """
        started_ids: list[int] = []
        try:
            completed_tasks = self.task_db.list_tasks(status="completed")
            completed_task_ids = [task.get("id") for task in completed_tasks]
            pending_tasks = self.task_db.list_tasks(status="pending", include_archived=False)
            pending_task_ids = [task.get("id") for task in pending_tasks]
            for task_id in pending_task_ids:
                dependencies = self.task_db.get_dependencies(task_id)
                all_ready = True
                for id in dependencies:
                    if id not in completed_task_ids:
                        all_ready = False
                if all_ready:
                    from peng.task.manager import TaskManager

                    task_manager = TaskManager()
                    await task_manager.restart_task(task_id, reason="依赖事务已完成，自动启动")
                    started_ids.append(task_id)
                    logger.info(f"[Heartbeat] wait条件已满足，自动启动事务#{task_id} ")
        except Exception as e:
            logger.error(
                f"[Heartbeat] auto_start_waiting_tasks 异常: {e}\n{traceback.format_exc()}"
            )
        return started_ids

    # ==================== 同步检查阶段（在线程池中执行） ====================

    def _sync_check_phase(self) -> dict:
        """
        同步检查阶段：所有纯同步的数据库操作集中执行。

        此方法会被 asyncio.to_thread() 放到线程池中运行，
        避免同步 psycopg2 操作阻塞 asyncio 事件循环。

        Returns:
            dict 包含同步阶段的检查结果和需要后续异步处理的数据
        """
        result = {
            "expired_messages": [],
            "interrupted_messages": [],
            "heartbeats_expired_tasks": [],
            "synced_messages": [],
            "deleted_messages": [],
            "task_resume_ids": [],
            "task_resume_reasons": {},
            "interrupted_msg_id": None,
            "message_controls": [],
            "task_controls": [],
            "waiting_task_ids": [],
        }

        try:
            # 1. 检查消息进程状态
            result["interrupted_messages"] = self.check_message_heartbeats()

            # 2. 同步事务状态到消息
            result["synced_messages"] = self.sync_task_status_to_messages()

            # 3. 归档过期消息/事务
            self.archive_old_messages()

            # 4. 处理待删除消息
            result["deleted_messages"] = self.handle_pending_delete_messages()

            # 5. 检查事务心跳（同步部分：获取数据、判断超时）
            result["task_resume_ids"], result["task_resume_reasons"] = (
                self._check_task_heartbeats_sync()
            )

            # 6. 查找需要恢复的 interrupted 消息
            interrupted_msg = self._find_interrupted_message_to_resume()
            result["interrupted_msg_id"] = interrupted_msg

            # 7. 获取未处理的消息控制项
            result["message_controls"] = self.msg_db.get_unprocessed_message_controls()

            # 8. 获取未处理的事务控制项
            result["task_controls"] = self.task_db.get_unprocessed_task_controls()

            # 9. 查找 wait 条件满足的 pending 事务
            result["waiting_task_ids"] = self._find_waiting_tasks_to_start()

        except Exception as e:
            logger.error(f"同步检查阶段异常：{e}\n{traceback.format_exc()}")

        return result

    def _check_task_heartbeats_sync(self) -> tuple[list[int], dict[int, str]]:
        """
        事务心跳检查的同步部分：获取数据并判断哪些需要恢复。

        Returns:
            (resume_ids, task_reasons)
        """
        running_tasks = self.task_db.get_running_tasks_with_heartbeat_and_details()

        if not running_tasks:
            return [], {}

        resume_ids: list[int] = []
        task_reasons: dict[int, str] = {}

        for row in running_tasks:
            task_id = row["id"]
            os_pid = row["process_id"]
            heartbeat_at = row["heartbeat_at"]
            heartbeat_timeout = (
                row["heartbeat_timeout"] or self._get_default_heartbeat_timeout()
            )  # 从配置获取默认值

            if heartbeat_at:
                try:
                    last_heartbeat = datetime.fromisoformat(heartbeat_at)
                    is_timeout = datetime.now() - last_heartbeat > timedelta(
                        seconds=heartbeat_timeout
                    )
                except (ValueError, TypeError):
                    is_timeout = False
            else:
                is_timeout = False

            if os_pid and os_pid > 0:
                if not self._check_os_process_exists(os_pid):
                    reason = f"操作系统进程pid {os_pid}已失效 "
                    reason += self._get_service_restart_hint()
                    resume_ids.append(task_id)
                    task_reasons[task_id] = reason

            if is_timeout:
                reason = f"心跳超时 {heartbeat_timeout} 秒，可能原因[工具调用或bash命令卡死(应该后台运行并监控)/大模型单步输出过长（应分步输出减少单步时长，例如单次写入小于500行）等]"
                resume_ids.append(task_id)
                task_reasons[task_id] = reason

        return resume_ids, task_reasons

    def _find_interrupted_message_to_resume(self) -> int | None:
        """
        查找需要恢复的 interrupted 消息（同步部分）。

        Returns:
            消息ID，或 None
        """
        try:
            from peng.config import get_agent_name

            AGENT_NAME = get_agent_name()

            processing_count = self.msg_db.count_processing_messages()
            if processing_count > 1:
                return None

            row = self.msg_db.get_message_by_status_and_sender("interrupted", AGENT_NAME, 1)
            if not row:
                return None

            msg_id = row["id"]

            # 检查是否是回复消息
            interrupted_msg = self.msg_db.get_message(msg_id)
            if interrupted_msg and interrupted_msg.get("reply_to") is not None:
                return None

            return msg_id
        except Exception as e:
            logger.debug(f"查找 interrupted 消息失败: {e}")
            return None

    def _find_waiting_tasks_to_start(self) -> list[int]:
        """
        查找 wait 条件已满足的 pending 事务（同步部分）。

        Returns:
            需要启动的事务ID列表
        """
        started_ids: list[int] = []
        try:
            completed_tasks = self.task_db.list_tasks(status="completed")
            completed_task_ids = [task.get("id") for task in completed_tasks]
            pending_tasks = self.task_db.list_tasks(status="pending", include_archived=False)
            pending_task_ids = [task.get("id") for task in pending_tasks]
            for task_id in pending_task_ids:
                if task_id is None:
                    continue
                dependencies = self.task_db.get_dependencies(task_id)
                # 没有设置任何依赖的 pending 事务不应被自动启动
                # 只有明确设置了依赖且依赖都已满足的事务才自动启动
                if not dependencies:
                    continue
                all_ready = all(dep_id in completed_task_ids for dep_id in dependencies)
                if all_ready:
                    started_ids.append(task_id)
        except Exception as e:
            logger.error(f"查找等待事务失败: {e}\n{traceback.format_exc()}")
        return started_ids

    # ==================== 主检查入口 ====================

    async def check_all(self) -> dict[str, list[int]]:
        """
        执行所有心跳检查（供后台服务调用）

        包括：
        1. §3.2.2-3.2.3: 检查消息心跳和进程状态
        2. §3.2.3: 检查事务心跳和进程状态
        3. §3.2.5: 同步事务状态到消息
        4. §3.2.6: 事务完成后更新消息状态
        5. §3.2.7: 处理暂停的消息
        6. §3.2.8: 归档过期消息/事务
        7. §3.2.9: 处理 pending 消息
        8. §3.2.10: 处理 interrupted 消息
        9. 处理 pending 事务（自动启动）
        10. 自动启动 wait 条件已满足的 pending 事务

        Returns:
            包含各类处理结果ID的字典
        """
        result = {
            "expired_messages": [],
            "interrupted_messages": [],
            "heartbeats_expired_tasks": [],
            "synced_messages": [],
            "processed_message_controls": [],
            "processed_task_controls": [],
            "processed_pending_messages": [],
            "processed_interrupted_messages": [],
            "started_tasks": [],
        }

        try:
            # ========== 同步阶段：所有同步DB操作在线程池中执行 ==========
            sync_result = await asyncio.to_thread(self._sync_check_phase)

            # 收集同步阶段结果
            result["expired_messages"] = sync_result["expired_messages"]
            result["interrupted_messages"] = sync_result["interrupted_messages"]
            result["synced_messages"] = sync_result["synced_messages"]
            result["heartbeats_expired_tasks"] = sync_result["task_resume_ids"]

            # ========== 异步阶段：需要 await 的操作在事件循环中执行 ==========

            # 1. 强制恢复超时/失效的事务（async: resume_task）
            if sync_result["task_resume_ids"]:
                from peng.task import TaskManager

                tm = TaskManager()
                for task_id in sync_result["task_resume_ids"][:2]:
                    reason = sync_result["task_resume_reasons"].get(task_id, "")
                    await tm.resume_task(task_id, reason=reason, force=True)
                    logger.info(
                        f"🔄 {ANSI_COLORS.get('bright_blue', '')}事务#{task_id} {reason}，已强制恢复！{ANSI_COLORS.get('reset', '')} "
                    )

            # 2. 恢复 interrupted 消息（async: resume_message）
            if sync_result["interrupted_msg_id"]:
                try:
                    from peng.message.manager import MessageManager

                    msg_manager = MessageManager()
                    success = await msg_manager.resume_message(sync_result["interrupted_msg_id"])
                    if success:
                        result["processed_interrupted_messages"] = [
                            sync_result["interrupted_msg_id"]
                        ]
                        logger.info(
                            f"✅ 已恢复 interrupted 消息 #{sync_result['interrupted_msg_id']}"
                        )
                    else:
                        logger.warning(
                            f"⚠️ 恢复 interrupted 消息 #{sync_result['interrupted_msg_id']} 失败"
                        )
                except Exception as e:
                    logger.error(f"恢复 interrupted 消息失败: {e}\n{traceback.format_exc()}")

            # 3. 处理消息控制表（async: pause/cancel/resume/restart）
            if sync_result["message_controls"]:
                processed = await self._process_message_controls_async(
                    sync_result["message_controls"]
                )
                result["processed_message_controls"] = processed

            # 4. 处理事务控制表（async: pause/cancel/resume/restart）
            if sync_result["task_controls"]:
                processed = await self._process_task_controls_async(sync_result["task_controls"])
                result["processed_task_controls"] = processed

            # 5. 自动启动 wait 条件满足的 pending 事务（async: restart_task）
            if sync_result["waiting_task_ids"]:
                from peng.task.manager import TaskManager

                task_manager = TaskManager()
                for task_id in sync_result["waiting_task_ids"]:
                    await task_manager.restart_task(task_id, reason="依赖事务已完成，自动启动")
                    result["started_tasks"].append(task_id)
                    logger.info(f"[Heartbeat] wait条件已满足，自动启动事务#{task_id} ")

        except Exception as e:
            logger.error(f"心跳检查异常： {e}\n{traceback.format_exc()}")
        return result

    async def _process_message_controls_async(self, controls: list[dict]) -> list[int]:
        """
        异步处理消息控制项（从同步阶段获取的未处理控制列表）

        Args:
            controls: 未处理的消息控制项列表

        Returns:
            已处理的控制ID列表
        """
        from peng.message.manager import MessageManager

        processed_ids = []

        try:
            msg_manager = MessageManager()

            for control in controls:
                control_id = control["id"]
                msg_id = control["message_id"]
                action = control["action"]

                try:
                    if action == "pause":
                        await msg_manager.pause_message(msg_id)
                    elif action == "cancel":
                        await msg_manager.cancel_message(msg_id)
                    elif action == "resume":
                        await msg_manager.resume_message(msg_id)
                    elif action == "restart" or action == "start":
                        await msg_manager.restart_message(msg_id)
                    else:
                        logger.warning(f"未知的控制操作: {action}")
                        continue

                    # 标记为已处理
                    self.msg_db.mark_message_control_processed(control_id)
                    processed_ids.append(control_id)
                    logger.info(f"✅ 已处理消息控制 #{control_id}: {action} -> 消息 #{msg_id}")

                except Exception as e:
                    logger.error(
                        f"❌ 处理消息控制 #{control_id} 失败: {e}\n{traceback.format_exc()}"
                    )

        except Exception as e:
            logger.error(f"处理消息控制表失败: {e}\n{traceback.format_exc()}")

        return processed_ids

    async def _process_task_controls_async(self, controls: list[dict]) -> list[int]:
        """
        异步处理事务控制项（从同步阶段获取的未处理控制列表）

        Args:
            controls: 未处理的事务控制项列表

        Returns:
            已处理的控制ID列表
        """
        from peng.task.manager import TaskManager

        processed_ids = []

        try:
            task_manager = TaskManager()

            for control in controls:
                control_id = control["id"]
                task_id = control["task_id"]
                action = control["action"]

                try:
                    if action == "pause":
                        await task_manager.pause_task(
                            task_id, reason=f"收到控制命令：pause (control_id={control_id})"
                        )
                    elif action == "cancel":
                        await task_manager.cancel_task(
                            task_id, reason=f"收到控制命令：cancel (control_id={control_id})"
                        )
                    elif action == "resume":
                        await task_manager.resume_task(
                            task_id, reason=f"收到控制命令：resume (control_id={control_id})"
                        )
                    elif action == "restart" or action == "start":
                        await task_manager.restart_task(
                            task_id,
                            reason=f"收到控制命令：{action} (control_id={control_id})",
                        )
                    elif action == "up":
                        task = task_manager.get_task(task_id)
                        if task:
                            current = task.get("priority", 2)
                            new_priority = max(0, current - 1)
                            task_manager.db.update_task(task_id, priority=new_priority)
                    elif action == "down":
                        task = task_manager.get_task(task_id)
                        if task:
                            current = task.get("priority", 2)
                            new_priority = min(3, current + 1)
                            task_manager.db.update_task(task_id, priority=new_priority)
                    else:
                        logger.warning(f"未知的控制操作: {action}")
                        continue

                    # 标记为已处理
                    self.task_db.mark_task_control_processed(control_id)
                    processed_ids.append(control_id)
                    logger.info(f"✅ 已处理事务控制 #{control_id}: {action} -> 事务 #{task_id}")

                except Exception as e:
                    logger.error(
                        f"❌ 处理事务控制 #{control_id} 失败: {e}\n{traceback.format_exc()}"
                    )

        except Exception as e:
            logger.error(f"处理事务控制表失败: {e}\n{traceback.format_exc()}")

        return processed_ids

    def handle_pending_delete_messages(self) -> list[int]:
        """
        处理待删除标记的消息

        处理流程：
        1. 获取所有待删除消息 ID
        2. 对每个消息，先递归杀掉事务树所有进程
        3. 递归删掉事务树
        4. 然后删除消息本身

        Returns:
            已删除的消息 ID 列表
        """
        from peng.message.database import MessageDatabase
        from peng.task.manager import TaskManager

        deleted_ids = []

        try:
            msg_db = MessageDatabase()
            task_manager = TaskManager()

            # 获取所有待删除消息
            pending_ids = msg_db.get_pending_delete_messages()
            if not pending_ids:
                return []

            for msg_id in pending_ids:
                try:
                    # 步骤 1：获取所有关联事务（含子事务）
                    tasks = task_manager.get_tasks_by_message(msg_id)
                    all_task_ids = []
                    for task in tasks:
                        all_task_ids.append(task["id"])
                        # 收集所有子事务 ID
                        descendants = task_manager.db.get_descendants(task["id"])
                        all_task_ids.extend([d["id"] for d in descendants])

                    # 步骤 3：递归删掉事务树
                    deleted_tasks = 0
                    for task in tasks:
                        deleted_tasks += task_manager.db.delete_task_recursive(task["id"])
                    if deleted_tasks > 0:
                        logger.info(f"消息 {msg_id}: 已删除 {deleted_tasks} 个事务（含子事务）")

                    # 步骤 4：删除消息本身
                    if msg_db.delete_message(msg_id):
                        deleted_ids.append(msg_id)
                        logger.info(f"已删除消息 #{msg_id}")

                except Exception as e:
                    logger.error(f"删除消息 {msg_id} 失败：{e}\n{traceback.format_exc()}")
                    # 删除失败时清除待删除标记，避免死循环
                    msg_db.clear_pending_delete(msg_id)

        except Exception as e:
            logger.error(f"处理待删除消息失败：{e}\n{traceback.format_exc()}")

        return deleted_ids

    # ==================== 控制表处理 ====================

    async def process_message_controls(self) -> list[int]:
        """
        处理消息控制表（事务要求 §7）

        处理流程：
        1. 从message_controls表获取未处理的控制项
        2. 按顺序执行控制操作
        3. 执行后删除记录

        Returns:
            已处理的控制ID列表
        """
        from peng.message.manager import MessageManager

        processed_ids = []

        try:
            # 获取未处理的控制项（按created_at排序）（通过MessageDatabase）
            controls = self.msg_db.get_unprocessed_message_controls()

            if not controls:
                return []

            msg_manager = MessageManager()

            for control in controls:
                control_id = control["id"]
                msg_id = control["message_id"]
                action = control["action"]

                try:
                    # 执行控制操作
                    if action == "pause":
                        await msg_manager.pause_message(msg_id)
                    elif action == "cancel":
                        await msg_manager.cancel_message(msg_id)
                    elif action == "resume":
                        await msg_manager.resume_message(msg_id)
                    elif action == "restart" or action == "start":
                        await msg_manager.restart_message(msg_id)
                    else:
                        logger.warning(f"未知的控制操作: {action}")
                        continue

                    # 标记为已处理（通过MessageDatabase）
                    self.msg_db.mark_message_control_processed(control_id)
                    processed_ids.append(control_id)
                    logger.info(f"✅ 已处理消息控制 #{control_id}: {action} -> 消息 #{msg_id}")

                except Exception as e:
                    logger.error(
                        f"❌ 处理消息控制 #{control_id} 失败: {e}\n{traceback.format_exc()}"
                    )

        except Exception as e:
            logger.error(f"处理消息控制表失败: {e}\n{traceback.format_exc()}")

        return processed_ids

    async def process_task_controls(self) -> list[int]:
        """
        处理事务控制表（事务要求 §7）

        处理流程：
        1. 从task_controls表获取未处理的控制项
        2. 按顺序执行控制操作
        3. 执行后删除记录

        Returns:
            已处理的控制ID列表
        """
        from peng.task.manager import TaskManager

        processed_ids = []

        try:
            # 获取未处理的控制项（按created_at排序）（通过TaskDatabase）
            controls = self.task_db.get_unprocessed_task_controls()

            if not controls:
                return []

            task_manager = TaskManager()

            for control in controls:
                control_id = control["id"]
                task_id = control["task_id"]
                action = control["action"]

                try:
                    # 执行控制操作
                    if action == "pause":
                        await task_manager.pause_task(
                            task_id, reason=f"收到控制命令：pause (control_id={control_id})"
                        )
                    elif action == "cancel":
                        await task_manager.cancel_task(
                            task_id, reason=f"收到控制命令：cancel (control_id={control_id})"
                        )
                    elif action == "resume":
                        await task_manager.resume_task(
                            task_id, reason=f"收到控制命令：resume (control_id={control_id})"
                        )
                    elif action == "restart" or action == "start":
                        await task_manager.restart_task(
                            task_id, reason=f"收到控制命令：{action} (control_id={control_id})"
                        )
                    elif action == "up":
                        # 提高优先级
                        task = task_manager.get_task(task_id)
                        if task:
                            current = task.get("priority", 2)
                            new_priority = max(0, current - 1)
                            task_manager.db.update_task(task_id, priority=new_priority)
                    elif action == "down":
                        # 降低优先级
                        task = task_manager.get_task(task_id)
                        if task:
                            current = task.get("priority", 2)
                            new_priority = min(3, current + 1)
                            task_manager.db.update_task(task_id, priority=new_priority)
                    else:
                        logger.warning(f"未知的控制操作: {action}")
                        continue

                    # 标记为已处理（通过TaskDatabase）
                    self.task_db.mark_task_control_processed(control_id)
                    processed_ids.append(control_id)
                    logger.info(f"✅ 已处理事务控制 #{control_id}: {action} -> 事务 #{task_id}")

                except Exception as e:
                    logger.error(
                        f"❌ 处理事务控制 #{control_id} 失败: {e}\n{traceback.format_exc()}"
                    )

        except Exception as e:
            logger.error(f"处理事务控制表失败: {e}\n{traceback.format_exc()}")

        return processed_ids

    # ==================== 异步监控 ====================
    async def start_monitoring(self) -> None:
        """启动后台监控"""
        if self._running:
            return
        self._running = True

        async def monitor_loop():
            while self._running:
                try:
                    result = await self.check_all()
                    total = sum(len(v) for v in result.values())
                    # 只在有实际操作时输出日志
                    if total > 0 and any(
                        [
                            result.get("expired_messages"),
                            result.get("interrupted_messages"),
                            result.get("heartbeats_expired_tasks"),
                            result.get("synced_messages"),
                            result.get("processed_message_controls"),
                            result.get("processed_task_controls"),
                            result.get("processed_pending_messages"),
                            result.get("started_tasks"),
                        ]
                    ):
                        logger.info(f"💓 心跳检查完成，处理了 {total} 项")
                except Exception as e:
                    logger.error(f"❌ 心跳监控错误: {e}\n{traceback.format_exc()}")
                await asyncio.sleep(self.check_interval)

        self._task = asyncio.create_task(monitor_loop())

    async def stop_monitoring(self) -> None:
        """停止后台监控"""
        self._running = False
        if self._task:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None


def get_heartbeat_monitor(db_path: Path | None = None) -> HeartbeatMonitor:
    """获取心跳监控器实例（PostgreSQL 不需要 db_path）"""
    return HeartbeatMonitor(db_path=None)
