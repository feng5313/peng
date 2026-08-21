"""
事务管理模块

提供事务管理功能，包括：
- 事务创建和调度
- 事务状态管理
- 事务步骤管理
- 事务依赖管理
- Git Worktree 管理
- 智能决策
- 验收流程
- 事务恢复
- 超时配置
"""

from .database import TaskDatabase
from .manager import TaskManager
from .task_types import (StepStatus, TaskDependency, TaskPriority, TaskRecord,
                         TaskStatus, TaskStep, TaskType)
from .heartbeat_monitor import HeartbeatMonitor, HeartbeatStatus, get_heartbeat_monitor

__all__ = [
    # 类型
    "TaskStatus",
    "StepStatus",
    "TaskPriority",
    "TaskType",
    "TaskRecord",
    "TaskStep",
    "TaskDependency",
    "WorktreeStatus",
    "WorktreeRecord",
    "SubtaskStrategy",
    "WorktreeDecision",
    "TaskDecision",
    "AcceptanceStatus",
    "CriteriaType",
    "AcceptanceCriterion",
    "AcceptanceReport",
    "TaskReport",
    "RecoveryStrategy",
    "CheckpointType",
    "Checkpoint",
    "RecoveryPlan",
    "TimeoutMode",
    "TaskComplexity",
    "TimeoutConfig",
    # 数据库
    "TaskDatabase",
    # 管理器
    "TaskManager",
]
