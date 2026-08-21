"""
事务管理模块 - 类型定义

定义事务管理相关的类型和数据结构，包括：
- TaskStatus: 事务状态枚举
- TaskPriority: 事务优先级枚举
- TaskType: 事务类型枚举
- TaskRecord: 事务记录数据类
- TaskStep: 事务步骤数据类

需求6整改：添加当前步骤字段和暂停状态
"""

from __future__ import annotations

import logging
import traceback
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import Enum, IntEnum
from typing import Any, TypeAlias

logger = logging.getLogger(__name__)


class TaskStatus(Enum):
    """事务状态枚举

    状态流转：
    - pending -> running ->
      (accepting -> accepted | paused | interrupted | cancelled | failed) -> completed
    - running 可直接转为 completed（无需验收的情况）
    """

    PENDING = "pending"  # 等待执行
    RUNNING = "running"  # 执行中
    ACCEPTING = "accepting"  # 验收中
    ACCEPTED = "accepted"  # 已验收
    PAUSED = "paused"  # 已暂停
    INTERRUPTED = "interrupted"  # 已中断
    COMPLETED = "completed"  # 已完成
    FAILED = "failed"  # 失败
    CANCELLED = "cancelled"  # 已取消

    @classmethod
    def terminal_states(cls) -> list[TaskStatus]:
        """获取终止状态列表"""
        return [cls.COMPLETED, cls.FAILED, cls.CANCELLED]

    @classmethod
    def active_states(cls) -> list[TaskStatus]:
        """获取活跃状态列表（需要处理的）"""
        return [cls.RUNNING, cls.ACCEPTING]

    @classmethod
    def paused_states(cls) -> list[TaskStatus]:
        """获取可暂停状态列表"""
        return [cls.RUNNING, cls.ACCEPTING]

    def is_terminal(self) -> bool:
        """是否是终止状态"""
        return self in self.terminal_states()

    def is_active(self) -> bool:
        """是否是活跃状态"""
        return self in self.active_states()


class StepStatus(Enum):
    """步骤状态枚举"""

    PENDING = "pending"  # 等待执行
    RUNNING = "running"  # 执行中
    COMPLETED = "completed"  # 已完成
    FAILED = "failed"  # 失败
    SKIPPED = "skipped"  # 跳过


class TaskPriority(IntEnum):
    """事务优先级枚举（数值越小优先级越高）"""

    CRITICAL = 0  # 关键事务
    HIGH = 1  # 高优先级
    NORMAL = 2  # 普通优先级
    LOW = 3  # 低优先级

    def __lt__(self, other: object) -> bool:
        if isinstance(other, TaskPriority):
            return self.value < other.value
        return NotImplemented

    def __le__(self, other: object) -> bool:
        if isinstance(other, TaskPriority):
            return self.value <= other.value
        return NotImplemented


class TaskType(Enum):
    """事务类型枚举"""

    MESSAGE = "message"  # 消息指示
    CLI = "cli"  # 命令行指示
    TODO = "todo"  # TODOs.md 待办
    PROJECT = "project"  # 项目开发进度
    HISTORY = "history"  # 历史消息
    COMMUNICATION = "communication"  # 沟通记录
    AUTO = "auto"  # 自动事务
    EVOLUTION = "evolution"  # 进化探索


@dataclass
class TaskRecord:
    """
    事务记录

    表示一个事务的完整信息，与数据库字段对应。
    需求6整改：添加当前步骤字段和上下文
    """

    # 基本信息
    id: int = 0
    title: str = ""
    content: str = ""
    description: str | None = None

    # 状态和进度
    status: TaskStatus = TaskStatus.PENDING
    progress: int = 0  # 进度百分比 0-100

    # 当前步骤信息（需求6新增）
    current_step_index: int = 0  # 当前步骤索引
    current_step_name: str | None = None  # 当前步骤名称

    # 优先级和类型
    priority: TaskPriority = TaskPriority.NORMAL
    task_type: TaskType = TaskType.AUTO

    # 时间
    created_at: str = ""
    started_at: str | None = None
    completed_at: str | None = None
    deadline: str | None = None

    # 定时器（定时重启）
    timer: int = 0  # 定时重启间隔（秒），0表示不重启
    last_restart_at: str | None = None  # 最后一次重启时间

    # 关联
    message_id: int | None = None
    parent_task_id: int | None = None
    root_id: int | None = None  # 事务树根节点 ID（架构整改新增）
    depth: int = 0  # 事务深度（架构整改新增）
    path: str = ""  # 事务路径，如"/1/5/12"（架构整改新增）
    caller: str | None = None
    caller_session_id: str | None = None

    # Git Worktree（架构整改新增）
    worktree_path: str | None = None  # 关联的 worktree 路径
    worktree_locked: bool = False  # worktree 是否被锁定

    # 执行信息
    process_id: int | None = None
    error: str | None = None
    result: str | None = None

    # 超时
    timeout: int = 0  # 秒
    timeout_started_at: str | None = None

    # 心跳
    heartbeat_at: str | None = None
    heartbeat_timeout: int = 0  # 秒（0表示使用配置默认值）

    # 执行上下文（需求6新增）
    context: dict[str, Any] = field(default_factory=dict)

    # 事务提示词（事务二补充需求新增）
    prompt: str = ""

    # 是否使用小模型（事务二补充需求新增）
    small_model: bool = False

    # 相关信息（事务二补充需求新增）
    info: str = ""

    # 是否为复杂事务（事务三新增）
    is_complex: bool = False

    # 事务创建者
    creator: str = ""

    # LLM Provider（当前使用的provider名称）
    provider: str = ""

    # 元数据
    metadata: dict[str, Any] = field(default_factory=dict)

    # 验收清单和工作汇报（Markdown 文本）
    checklist: str = ""
    report: str = ""

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> TaskRecord:
        """从字典创建事务记录"""
        status = data.get("status", "pending")
        if isinstance(status, str):
            status = TaskStatus(status)

        priority = data.get("priority", 2)
        if isinstance(priority, int):
            priority = TaskPriority(priority)

        task_type = data.get("task_type", "auto")
        if isinstance(task_type, str):
            task_type = TaskType(task_type)

        metadata = data.get("metadata", "{}")
        if isinstance(metadata, str):
            try:
                import json

                metadata = json.loads(metadata)
            except Exception as e:
                import traceback

                logger.error(f"❌ from_dict() - types.py:195: 异常 - {e}\n{traceback.format_exc()}")
                metadata = {}

        # 解析 context 字段（需求6新增）
        context = data.get("context", "{}")
        if isinstance(context, str):
            try:
                import json

                context = json.loads(context)
            except Exception as e:
                import traceback

                logger.error(f"❌ from_dict() - types.py:206: 异常 - {e}\n{traceback.format_exc()}")
                context = {}

        return cls(
            id=data.get("id", 0),
            title=data.get("title", ""),
            content=data.get("content", ""),
            description=data.get("description"),
            status=status,
            progress=data.get("progress", 0) or 0,
            current_step_index=data.get("current_step_index", 0) or 0,
            current_step_name=data.get("current_step_name"),
            priority=priority,
            task_type=task_type,
            created_at=data.get("created_at", ""),
            started_at=data.get("started_at"),
            completed_at=data.get("completed_at"),
            deadline=data.get("deadline"),
            timer=data.get("timer", 0) or 0,
            last_restart_at=data.get("last_restart_at"),
            message_id=data.get("message_id"),
            parent_task_id=data.get("parent_task_id"),
            root_id=data.get("root_id"),
            depth=data.get("depth", 0) or 0,
            path=data.get("path", "") or "",
            caller=data.get("caller"),
            caller_session_id=data.get("caller_session_id"),
            process_id=data.get("process_id"),
            error=data.get("error"),
            result=data.get("result"),
            timeout=data.get("timeout", 0) or 0,
            timeout_started_at=data.get("timeout_started_at"),
            heartbeat_at=data.get("heartbeat_at"),
            heartbeat_timeout=data.get("heartbeat_timeout", 0) or 0,
            context=context,
            prompt=data.get("prompt", "") or "",
            small_model=bool(data.get("small_model", False) or False),
            info=data.get("info", "") or "",
            is_complex=bool(data.get("is_complex", False) or False),
            creator=data.get("creator", "") or "",
            provider=data.get("provider", "") or "",
            metadata=metadata,
            worktree_path=data.get("worktree_path"),
            worktree_locked=bool(data.get("worktree_locked", False) or False),
            checklist=data.get("checklist", "") or "",
            report=data.get("report", "") or "",
        )


@dataclass
class TaskStep:
    """
    事务步骤

    表示事务的一个执行步骤。
    """

    # 基本信息
    id: int = 0
    task_id: int = 0
    name: str = ""
    description: str | None = None
    order: int = 0

    # 状态
    status: StepStatus = StepStatus.PENDING

    # 时间
    created_at: str = ""
    started_at: str | None = None
    completed_at: str | None = None

    # 执行信息
    error: str | None = None
    result: str | None = None

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> TaskStep:
        """从字典创建步骤"""
        status = data.get("status", "pending")
        if isinstance(status, str):
            status = StepStatus(status)

        return cls(
            id=data.get("id", 0),
            task_id=data.get("task_id", 0),
            name=data.get("name", ""),
            description=data.get("description"),
            order=data.get("order", 0),
            status=status,
            created_at=data.get("created_at", ""),
            started_at=data.get("started_at"),
            completed_at=data.get("completed_at"),
            error=data.get("error"),
            result=data.get("result"),
        )


@dataclass
class TaskDependency:
    """
    事务依赖关系

    表示事务之间的依赖关系。
    """

    id: int = 0
    task_id: int = 0  # 依赖者
    depends_on_id: int = 0  # 被依赖者
    created_at: str = ""

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> TaskDependency:
        """从字典创建依赖关系"""
        return cls(
            id=data.get("id", 0),
            task_id=data.get("task_id", 0),
            depends_on_id=data.get("depends_on_id", 0),
            created_at=data.get("created_at", ""),
        )


# ==================== 备份管理相关类型 ====================


class CheckpointType(Enum):
    """检查点类型枚举"""

    AUTO = "auto"  # 自动保存
    MANUAL = "manual"  # 手动保存
    BEFORE_TOOL_CALL = "before_tool_call"  # 工具调用前
    AFTER_TOOL_CALL = "after_tool_call"  # 工具调用后
    BEFORE_LLM_CALL = "before_llm_call"  # 大模型调用前
    AFTER_LLM_CALL = "after_llm_call"  # 大模型调用后


@dataclass
class Checkpoint:
    """
    事务检查点

    表示事务执行过程中的一个快照，用于恢复事务状态。
    """

    id: int = 0
    task_id: int = 0
    checkpoint_type: CheckpointType = CheckpointType.AUTO
    created_at: str = ""

    # 上下文数据
    history: list[dict[str, Any]] = field(default_factory=list)  # 对话历史
    step_index: int = 0  # 当前步骤索引
    iteration: int = 0  # 当前迭代次数
    tool_calls: list[dict[str, Any]] = field(default_factory=list)  # 工具调用记录
    metadata: dict[str, Any] = field(default_factory=dict)  # 元数据

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Checkpoint:
        """从字典创建检查点"""
        checkpoint_type = data.get("checkpoint_type", "auto")
        if isinstance(checkpoint_type, str):
            checkpoint_type = CheckpointType(checkpoint_type)

        history = data.get("history", "[]")
        if isinstance(history, str):
            import json

            history = json.loads(history)

        tool_calls = data.get("tool_calls", "[]")
        if isinstance(tool_calls, str):
            import json

            tool_calls = json.loads(tool_calls)

        metadata = data.get("metadata", "{}")
        if isinstance(metadata, str):
            import json

            metadata = json.loads(metadata)

        return cls(
            id=data.get("id", 0),
            task_id=data.get("task_id", 0),
            checkpoint_type=checkpoint_type,
            created_at=data.get("created_at", ""),
            history=history,
            step_index=data.get("step_index", 0),
            iteration=data.get("iteration", 0),
            tool_calls=tool_calls,
            metadata=metadata,
        )


class RecoveryStrategy(Enum):
    """恢复策略枚举"""

    LATEST_CHECKPOINT = "latest_checkpoint"  # 从最新检查点恢复
    SPECIFIC_CHECKPOINT = "specific_checkpoint"  # 从指定检查点恢复
    RESTART_FROM_BEGINNING = "restart_from_beginning"  # 从头重新开始
    RESUME_FROM_STEP = "resume_from_step"  # 从指定步骤恢复


class TimeoutMode(Enum):
    """超时模式枚举"""

    HARD = "hard"  # 硬超时，直接终止
    SOFT = "soft"  # 软超时，警告但不终止
    GRACEFUL = "graceful"  # 优雅超时，完成当前操作后终止


@dataclass
class RecoveryPlan:
    """
    事务恢复计划

    包含恢复事务所需的完整信息。
    """

    strategy: RecoveryStrategy = RecoveryStrategy.LATEST_CHECKPOINT
    checkpoint_id: int | None = None  # 指定检查点 ID
    resume_step: int | None = None  # 恢复到的步骤
    restore_history: bool = True  # 是否恢复对话历史
    restore_metadata: bool = True  # 是否恢复元数据

    def to_dict(self) -> dict[str, Any]:
        """转换为字典"""
        return {
            "strategy": self.strategy.value,
            "checkpoint_id": self.checkpoint_id,
            "resume_step": self.resume_step,
            "restore_history": self.restore_history,
            "restore_metadata": self.restore_metadata,
        }


# ==================== 其他缺失类型 ====================


class WorktreeStatus(Enum):
    """Worktree 状态枚举"""

    IDLE = "idle"  # 空闲
    BUSY = "busy"  # 忙碌
    LOCKED = "locked"  # 锁定
    ERROR = "error"  # 错误


@dataclass
class WorktreeRecord:
    """Worktree 记录"""

    id: int = 0
    path: str = ""
    branch_name: str = ""
    status: WorktreeStatus = WorktreeStatus.IDLE
    locked_by_task_id: int | None = None
    created_at: str = ""
    last_used_at: str | None = None


class SubtaskStrategy(Enum):
    """子事务策略枚举"""

    SEQUENTIAL = "sequential"  # 顺序执行
    PARALLEL = "parallel"  # 并行执行
    MIXED = "mixed"  # 混合模式


@dataclass
class WorktreeDecision:
    """Worktree 决策"""

    action: str  # create, reuse, wait
    worktree_path: str | None = None
    reason: str = ""


@dataclass
class TaskDecision:
    """事务决策"""

    action: str  # execute, wait, delegate
    reason: str = ""
    subtasks: list[str] = field(default_factory=list)


class AcceptanceStatus(Enum):
    """验收状态枚举"""

    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    PASSED = "passed"
    FAILED = "failed"


class CriteriaType(Enum):
    """验收标准类型枚举"""

    FUNCTIONAL = "functional"
    PERFORMANCE = "performance"
    SECURITY = "security"
    CODE_QUALITY = "code_quality"
    DOCUMENTATION = "documentation"


@dataclass
class AcceptanceCriterion:
    """验收标准"""

    id: int = 0
    task_id: int = 0
    name: str = ""
    description: str = ""
    criteria_type: CriteriaType = CriteriaType.FUNCTIONAL
    expected_result: str = ""
    actual_result: str | None = None
    status: AcceptanceStatus = AcceptanceStatus.PENDING
    verified_at: str | None = None

    def to_dict(self) -> dict[str, Any]:
        """转换为字典"""
        return {
            "id": self.id,
            "task_id": self.task_id,
            "name": self.name,
            "description": self.description,
            "criteria_type": self.criteria_type.value,
            "expected_result": self.expected_result,
            "actual_result": self.actual_result,
            "status": self.status.value,
            "verified_at": self.verified_at,
        }


@dataclass
class AcceptanceReport:
    """验收报告"""

    task_id: int = 0
    overall_status: AcceptanceStatus = AcceptanceStatus.PENDING
    criteria: list[AcceptanceCriterion] = field(default_factory=list)
    summary: str = ""
    verified_at: str = ""

    def to_dict(self) -> dict[str, Any]:
        """转换为字典"""
        return {
            "task_id": self.task_id,
            "overall_status": self.overall_status.value,
            "criteria": [c.to_dict() for c in self.criteria],
            "summary": self.summary,
            "verified_at": self.verified_at,
        }


@dataclass
class TaskReport:
    """事务报告"""

    task_id: int = 0
    title: str = ""
    status: TaskStatus = TaskStatus.PENDING
    progress: int = 0
    acceptance_report: AcceptanceReport | None = None
    checkpoints: list[Checkpoint] = field(default_factory=list)
    created_at: str = ""
    completed_at: str | None = None


class TaskComplexity(Enum):
    """事务复杂度枚举"""

    SIMPLE = "simple"  # 简单事务，无需子事务
    COMPLEX = "complex"  # 复杂事务，需要子事务
    VERY_COMPLEX = "very_complex"  # 非常复杂，需要多层子事务


@dataclass
class TimeoutConfig:
    """超时配置"""

    mode: TimeoutMode = TimeoutMode.HARD
    default_timeout: int = 300  # 默认 5 分钟
    max_timeout: int = 3600  # 最大 1 小时
    graceful_period: int = 30  # 优雅期 30 秒


# 类型别名
TaskCallback: TypeAlias = Callable[..., Any]
HeartbeatCallback: TypeAlias = Callable[..., Any]
