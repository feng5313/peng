"""
事务执行器

完全负责事务执行的所有事情（重整版）：
- 单次LLM调用
- 所有git操作（通过bash工具，无硬编码）
- 所有工具调用
- 流式输出
- 消息管理
- 子事务创建
- 事务状态管理

极简架构：一次LLM调用，大模型自主控制一切。
"""

from __future__ import annotations

import asyncio
import logging
import sys
import time
import traceback
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from peng.common.colors import ANSI_COLORS
from peng.common import jsonp
from peng.config.loader import load_config
from peng.message.manager import MessageManager
from peng.task.output_manager import get_output_manager
from peng.tools.registry import ToolRegistry
from peng.common.logging_config import setup_logging_from_config

logger = logging.getLogger(__name__)


@dataclass
class ExecutorContext:
    """执行器上下文"""

    task_id: int
    description: str
    work_dir: Path
    message_context: str = ""

    # 事务管理器
    task_manager: Any = None

    # 消息链追踪
    message_id: int | None = None
    parent_task_id: int | None = None

    # 输出管理
    output_manager: Any = None

    # 事务信息（用于选择提示词）
    is_complex: bool = False
    task_depth: int = 0

    # 等待状态已废弃


@dataclass
class ExecutorResult:
    """执行结果"""

    success: bool
    error: str | None = None


class Executor:
    """事务执行器"""

    def __init__(
        self,
        task_manager: Any = None,
        message_manager: Any = None,
    ):
        self.task_manager = task_manager
        self.message_manager = message_manager
        self.output_manager = get_output_manager()

    async def execute(self, message_id, task_id) -> ExecutorResult:
        """
        执行事务的主入口

        重整流程：单次LLM调用完成所有工作
        - 大模型自主决定一切操作
        - 无任何git硬编码（通过bash工具）
        - 完全负责所有事情
        - 支持子事务失败自动重试（最多重试3次）
        - 支持多轮对话优化（等待回复状态）
        """
        try:
            # 阶段9：检查是否暂停（事务开始时）
            try:
                from peng.webui.websocket import get_pause_signal

                pause_signal = get_pause_signal(task_id)
                if pause_signal == "paused":
                    logger.info(f"[Executor] 检测到暂停信号，事务 #{task_id} 已暂停")

                    # 更新事务状态为暂停
                    if self.task_manager:
                        self.task_manager.db.update_task(
                            task_id,
                            status="paused",
                            error="事务已暂停",
                        )

                    return ExecutorResult(success=False, error="事务已暂停")

            except ImportError:
                pass  # webui模块不可用（命令行模式）
            result = await self._execute_llm(message_id, task_id)
            return result
        except Exception as e:
            logger.error(f"事务执行失败: {e}\n{traceback.format_exc()}")
            return ExecutorResult(success=False, error=f"{str(e)} 事务执行失败")

    async def _execute_llm(self, message_id, task_id) -> ExecutorResult:
        """执行LLM调用完成所有工作"""
        try:
            logger.info(f"[Executor] _execute_llm 开始: task_id={task_id}")

            from peng.llm.agent import PengAgent

            # 创建 Agent
            agent = PengAgent(
                message_manager=self.message_manager,
            )
            logger.info("[Executor] Agent创建成功，开始流式输出")

            # 执行事务（流式输出）
            chunk_count = 0
            last_heartbeat = 0
            async for chunk in agent.run_stream(message_id, task_id):
                chunk_count += 1
                if chunk_count == 1:
                    logger.info(f"[Executor] 开始收到第一个chunk: chunk_type={type(chunk)}")
                self.output_manager.write_output(
                    content=chunk.content,
                    source=chunk.source,
                    task_id=task_id,
                )
                # 大模型思考/输出期间定期更新心跳，防止心跳超时被误杀
                # 默认 heartbeat_timeout=180s，每 60 秒更新一次心跳确保安全
                now = time.time()
                if now - last_heartbeat > 60:
                    try:
                        self.task_manager.update_heartbeat(task_id)
                        last_heartbeat = now
                    except Exception:
                        pass  # 心跳更新失败不影响主流程

            logger.info(f"[Executor] 流式输出结束，共收到 {chunk_count} 个chunk")
            return ExecutorResult(success=True)

        except Exception as e:
            logger.error(f"LLM调用失败: {e}\n{traceback.format_exc()}")
            # 其他异常返回错误消息
            return ExecutorResult(success=False, error=str(e))


# ==================== 进程入口函数 ====================
def run_task_process(task_id: int):
    """
    在子进程中运行重整后的事务流程

    使用 Executor 执行重整流程：
    1. 单次LLM调用（大模型自主控制所有操作）
    2. 更新事务和消息状态
    """

    async def _run():
        from peng.task.executor import Executor, ExecutorContext

        config = load_config()
        setup_logging_from_config(config)
        logger = logging.getLogger("peng.task")

        # 初始化 nest 客户端（子进程通过 _forward_via_server 连接 peng 后端）
        try:
            _nest_cfg = config.nest
            if _nest_cfg.enabled:
                from peng.nest.client import init_nest_clients, set_default_nest
                init_nest_clients()
                if _nest_cfg.default_nest:
                    set_default_nest(_nest_cfg.default_nest)
                logger.info(f"Nest 客户端已初始化（子进程转发模式）")
        except Exception as e:
            logger.warning(f"初始化 nest 客户端失败（非致命）: {e}")

        from peng.task.manager import TaskManager

        task_manager = TaskManager()
        try:
            from datetime import datetime

            task_manager.start_task(task_id)
            logger.info(f"[事务] 事务 #{task_id} 状态已更新为 running")

            # 创建 MessageManager
            message_manager = MessageManager()

            # 创建 Executor
            executor = Executor(
                task_manager=task_manager,
                message_manager=message_manager,
            )

            task = task_manager.db.get_task(task_id)
            message_id = task.get("message_id")
            # 执行单次调用流程
            result = await executor.execute(message_id, task_id)

            task_manager.db.update_task(task_id, process_id=0)
            if result.success:
                logger.info(f"[事务] 事务 #{task_id} 执行成功")
                # 立即更新事务状态为completed
                try:
                    task_manager.complete_task(task_id)
                    logger.info(f"[事务] 事务 #{task_id} 状态已更新为completed")
                except Exception as e:
                    logger.warning(f"[事务] 更新事务completed状态时出错: {e}")
            else:
                logger.error(f"[事务] 事务 #{task_id} 失败: {result.error}")
                try:
                    task_manager.fail_task(task_id, error=result.error or "执行失败")
                    logger.info(f"[事务] 事务 #{task_id} 状态已更新为失败")
                except Exception as e:
                    logger.warning(f"[事务] 更新事务失败状态时出错: {e}\n{traceback.format_exc()}")

        except Exception as e:
            tb = traceback.format_exc()
            logger.error(f"[事务] 事务 #{task_id} 异常: {e}\n{tb}")
            error_with_tb = f"{e}\n\nTraceback:\n{tb}"

            # 更新事务状态为失败
            try:
                if task_manager:
                    task_manager.fail_task(task_id, error=error_with_tb)
                    logger.info(f"[事务] 事务 #{task_id} 状态已更新为失败（异常）")
            except Exception as update_error:
                logger.warning(
                    f"[事务] 更新事务失败状态时出错: {update_error}\n{traceback.format_exc()}"
                )

    _run_async(_run())


# ==================== 辅助函数 ====================


def _setup_environment(work_dir: str):
    """设置环境（不改变工作目录，使用绝对路径）"""
    from peng.config import get_project_root

    project_root = get_project_root()
    src_path = str(project_root / "src")
    if src_path not in sys.path:
        sys.path.insert(0, src_path)


def _run_async(coro):
    """运行异步协程"""
    try:
        asyncio.run(coro)
    except Exception as e:
        tb = traceback.format_exc()
        logger.error(f"异步协程执行失败: {e}\n{tb}")
        error_with_tb = f"{e}\n\nTraceback:\n{tb}"
