#!/usr/bin/env python3
"""
事务定时器模块

提供事务的定时重启功能：
- 扫描timer>0的事务
- 检查是否达到重启时间
- 自动重启过期事务
"""

from __future__ import annotations

import asyncio
import logging
import time
import traceback
from datetime import datetime
from threading import Thread
from typing import Any
import re

from peng.common import MetaSingleton
from peng.task.manager import TaskManager
from peng.task.task_types import TaskStatus

logger = logging.getLogger(__name__)


def parse_timer(content: str | None):
    if content and len(content) > 6:
        timer_match = re.search(r"@timer=(\d+)", content)
        if timer_match:
            timer = int(timer_match.group(1))
            if timer and timer > 0:
                return timer
    return None


class TaskTimer(metaclass=MetaSingleton):
    """
    事务定时器

    负责定期扫描并重启设置了定时器的事务。
    """

    # 默认扫描间隔（秒）
    DEFAULT_SCAN_INTERVAL = 10

    def __init__(self):
        """初始化定时器"""
        self.task_manager = TaskManager()
        self.running = False
        self.thread: Thread | None = None
        self.scan_interval = self.DEFAULT_SCAN_INTERVAL

    def start(self, scan_interval: int = DEFAULT_SCAN_INTERVAL):
        """
        启动定时器

        Args:
            scan_interval: 扫描间隔（秒），默认10秒
        """
        if self.running:
            logger.debug("[TaskTimer] 定时器已经在运行")
            return

        self.scan_interval = scan_interval
        self.running = True
        self.thread = Thread(target=self._scan_loop, daemon=True)
        self.thread.start()
        logger.info(f"[TaskTimer] 定时器已启动，扫描间隔: {scan_interval}秒")

    def stop(self):
        """停止定时器"""
        if not self.running:
            return

        self.running = False
        if self.thread:
            self.thread.join(timeout=5)
        logger.info("[TaskTimer] 定时器已停止")

    def _scan_loop(self):
        """定时扫描循环"""
        while self.running:
            try:
                self._scan_and_restart_tasks()
            except Exception as e:
                logger.error(f"[TaskTimer] 扫描异常: {e}\n{traceback.format_exc()}")

            # 等待下一次扫描
            time.sleep(self.scan_interval)

    def _scan_and_restart_tasks(self):
        """
        扫描并重启定时任务

        逻辑：
        1. 查询所有timer>0的事务
        2. 检查是否达到重启时间：
           - 如果started_at为空，使用completed_at
           - 如果completed_at为空，使用created_at
        3. 如果当前时间 >= (重启时间 + timer)，重启事务
        """
        # 查询所有timer>0的事务
        tasks = self._get_tasks_with_timer()

        if not tasks:
            return

        logger.debug(f"[TaskTimer] 发现 {len(tasks)} 个定时任务")

        for task in tasks:
            try:
                self._check_and_restart_task(task)
            except Exception as e:
                logger.error(
                    f"[TaskTimer] 处理任务 #{task.get('id')} 异常: {e}\n{traceback.format_exc()}"
                )

    def _get_tasks_with_timer(self) -> list[dict[str, Any]]:
        """
        获取所有设置了定时器的事务

        Returns:
            事务列表
        """
        try:
            # 查询timer>0且非终态的事务
            # 注意：只处理非终态的事务（pending, running, accepting, paused等）
            # 已完成的事务如果设置了timer，会在完成后才检查是否需要重启
            all_tasks = self.task_manager.db.list_tasks(include_archived=False)

            # 过滤出设置了定时器的事务（content中@timer=优先，timer字段次之）
            tasks_with_timer = [
                t for t in all_tasks if parse_timer(t.get("content")) or t.get("timer", 0) > 0
            ]

            return tasks_with_timer
        except Exception as e:
            logger.error(f"[TaskTimer] 获取定时任务失败: {e}\n{traceback.format_exc()}")
            return []

    def _check_and_restart_task(self, task: dict[str, Any]):
        """
        检查并重启单个任务

        Args:
            task: 任务字典
        """
        task_id = task.get("id")
        content = task.get("content")
        timer = parse_timer(content)
        if not timer:
            timer = task.get("timer", 0)
        if not timer:
            return
        started_at = task.get("started_at")
        completed_at = task.get("completed_at")
        status = task.get("status")
        created_at = task.get("created_at")

        # 如果任务当前是活跃状态（running, accepting等），不处理
        if status in [
            "running",
            "accepting",
            "cancelled",
        ]:
            return
        if status == "paused" and not "!@timer=" in content:
            return

        # 确定基准时间
        # 对于已完成的事务（completed/failed/interrupted），使用 completed_at 作为基准
        # 这样定时器从完成时刻开始计时，避免任务刚完成就立即被重启
        # 对于其他状态，使用 started_at（最近启动时间）或 created_at（创建时间）
        finished_statuses = ["completed", "failed", "interrupted"]
        if status in finished_statuses and completed_at:
            base_time = completed_at
        else:
            base_time = started_at or created_at

        if not base_time:
            return

        try:
            base_datetime = datetime.fromisoformat(base_time)
        except Exception as e:
            logger.error(
                f"[TaskTimer] 解析任务 #{task_id} 时间戳失败: {e}\n{traceback.format_exc()}"
            )
            return

        # 计算应该重启的时间
        restart_datetime = base_datetime.timestamp() + timer
        current_datetime = datetime.now().timestamp()

        # 检查是否需要重启
        if current_datetime >= restart_datetime:
            # 二次检查：从DB重新读取最新状态，防止竞态条件
            # （例如任务在初次检查后、重启前刚刚变为 running）
            latest_task = self.task_manager.db.get_task(task_id)
            if latest_task:
                latest_status = latest_task.get("status")
                if latest_status in ["running", "accepting", "cancelled"]:
                    logger.info(
                        f"[TaskTimer] 事务 #{task_id} 当前状态为 {latest_status}，跳过重启"
                        f"（timer={timer}秒，基准时间={base_time}）"
                    )
                    return
                # 关键防护：paused 状态的任务如果心跳仍在活跃（30秒内），
                # 说明是 heartbeat_monitor 刚暂停的临时状态（在其 restart 的 5s 窗口内），
                # 跳过重启避免与 heartbeat_monitor 的 force restart 竞态
                if latest_status == "paused":
                    heartbeat_at = latest_task.get("heartbeat_at")
                    if heartbeat_at:
                        try:
                            hb_time = datetime.fromisoformat(heartbeat_at)
                            hb_age = (datetime.now() - hb_time).total_seconds()
                            if hb_age < 30:
                                logger.info(
                                    f"[TaskTimer] 事务 #{task_id} 心跳仍活跃（{hb_age:.0f}秒前），"
                                    f"可能是 heartbeat_monitor 临时暂停，跳过重启"
                                )
                                return
                        except (ValueError, TypeError):
                            pass

            keep_context = status in [
                "paused",
                "interrupted",
                "failed",
            ]
            self._restart_task(
                int(task_id),
                keep_context=keep_context,
                timer=timer,
                base_time=base_time,
                status=status,
            )

    def _restart_task(
        self,
        task_id: int,
        keep_context=False,
        timer: int = 0,
        base_time: str = "",
        status: str = "",
    ):
        """
        重启任务

        Args:
            task_id: 任务ID
            keep_context: 是否保留上下文
            timer: 定时器秒数（用于日志）
            base_time: 基准时间（用于日志）
            status: 任务状态（用于日志）
        """
        try:
            # 调用TaskManager的重启方法（async）
            # force=False: 禁止定时器强制杀死运行中的进程
            success = asyncio.run(
                self.task_manager.restart_task(
                    task_id,
                    keep_context=keep_context,
                    reason="定时器自动重启",
                    force=False,
                )
            )

            if success:
                # 更新started_at时间戳
                now = datetime.now().isoformat()
                self.task_manager.db.update_task(task_id, started_at=now)

                from peng.task.output_manager import get_output_manager

                get_output_manager().write_output(
                    task_id=task_id,
                    source="system",
                    content=f"\n🕑 [{now[:19]}] 吉时已到...\n",
                )

                logger.info(
                    f"[TaskTimer] 事务 #{task_id} 已重启，started_at={now}"
                    f"（timer={timer}秒，基准时间={base_time}，状态={status}）"
                )
            else:
                logger.error(
                    f"[TaskTimer] 事务 #{task_id} 重启失败"
                    f"（timer={timer}秒，基准时间={base_time}，状态={status}）"
                )
        except Exception as e:
            logger.error(f"[TaskTimer] 重启任务 #{task_id} 异常: {e}\n{traceback.format_exc()}")


# 全局定时器实例
_timer_instance: TaskTimer | None = None


def start_timer(scan_interval: int = TaskTimer.DEFAULT_SCAN_INTERVAL):
    """
    启动全局定时器

    Args:
        scan_interval: 扫描间隔（秒），默认10秒
    """
    global _timer_instance

    if _timer_instance is None:
        _timer_instance = TaskTimer()

    _timer_instance.start(scan_interval)
    return _timer_instance


def stop_timer():
    """停止全局定时器"""
    global _timer_instance

    if _timer_instance:
        _timer_instance.stop()


def get_timer() -> TaskTimer | None:
    """获取全局定时器实例"""
    return _timer_instance


# 命令行入口
if __name__ == "__main__":
    # 初始化日志系统
    from peng.common.logging_config import setup_logging_from_config

    setup_logging_from_config()

    # 启动定时器
    timer = start_timer(scan_interval=10)

    print("事务定时器已启动（扫描间隔: 10秒）")
    print("按 Ctrl+C 停止")

    try:
        # 保持运行
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        print("\n停止定时器...")
        stop_timer()
        print("定时器已停止")
