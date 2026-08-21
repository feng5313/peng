"""
钩子系统 v3.0 - 核心数据类型

新设计特点：
- 基于 before/after 挂载点
- 支持 tools/hooks/skills/flow 四大类匹配
- 类定义方式（必须实现 hook_name, hook_patterns, hook_action）
- 简化的返回值：("block", reason) / ("continue", None)
"""

import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)


# ==================== 钩子模式 ====================


@dataclass
class HookPattern:
    """钩子匹配模式

    用于定义钩子的触发条件，支持通过正则表达式匹配具体事件。

    Attributes:
        category: 分类（tools / hooks / skills / flow）
        pattern: 正则表达式
    """

    category: str  # 分类：tools / hooks / skills / flow
    pattern: str  # 正则表达式


@dataclass
class HookPatterns:
    """钩子模式集合

    包含 before 和 after 两个挂载点的匹配模式。

    Attributes:
        before: before 挂载点的匹配模式列表
        after: after 挂载点的匹配模式列表
    """

    before: list[HookPattern] = field(default_factory=list)
    after: list[HookPattern] = field(default_factory=list)


# ==================== 元数据类型 ====================


@dataclass
class FlowMetadata:
    """流程元数据

    用于 flow 类型的钩子，包含流程节点名称和参数。

    Attributes:
        name: 流程节点名称（如 "prompt", "complete"）
                before 时可修改，后续 hook 调用会使用修改后的 name
        args: 流程参数（before 时可修改，后续会使用修改后的参数）
    """

    name: str
    args: Any


@dataclass
class ToolMetadata:
    """工具元数据

    用于 tools 类型的钩子，包含工具名称、参数和执行结果。

    Attributes:
        name: 工具名称（before 时可修改）
        args: 工具参数（before 时可修改，执行时会使用修改后的参数）
        result: 工具执行结果（after 时有值，before 时为 None）
    """

    name: str
    args: dict
    result: Any = None


@dataclass
class SkillMetadata:
    """技能元数据

    用于 skills 类型的钩子，包含技能名称和加载器。

    Attributes:
        name: 技能名称（before 时可修改）
        loader: SkillLoader 实例（before 时可修改）
    """

    name: str
    loader: Any


@dataclass
class HookMetadata:
    """钩子元数据

    用于 hooks 类型的钩子（钩子监控），包含钩子信息。

    Attributes:
        name: 钩子名称（before 时可修改）
        hooks: 钩子列表（用于监控，before 时可修改）
        action: 钩子 action 函数（before 时可修改）
    """

    name: str
    hooks: list[Any] | None = None
    action: Any = None


# ==================== 钩子动作类型 ====================


HookActionType = tuple[str, str | None]
"""钩子动作类型

Returns:
    ("block", reason) - 阻止当前操作执行，reason 会加入 history
    ("continue", None) - 继续执行当前操作
"""


# ==================== 钩子基类 ====================


class BaseHook(ABC):
    """钩子基类

    所有钩子必须继承此类并实现 required attributes 和方法。

    必需属性：
        hook_name: 钩子名称（唯一标识）
        hook_patterns: HookPatterns - 匹配模式

    必需方法（二选一）：
        hook_action(): 同步钩子执行逻辑
        hook_action_async(): 异步钩子执行逻辑

    可选属性：
        priority: 优先级（数值越大越优先，默认 100）
    """

    hook_name: str  # 钩子名称（唯一标识）
    hook_patterns: HookPatterns  # 匹配模式

    def hook_action(
        self,
        message_id: int,
        task_id: int,
        category: str,
        name: str,
        metadata: dict[str, Any],
    ) -> HookActionType:
        """钩子入口函数（同步版本）

        如果钩子需要异步操作，请实现 hook_action_async。

        Args:
            message_id: 消息ID
            task_id: 事务ID
            category: 分类（tools/hooks/skills/flow）
            name: 具体名称（工具名/钩子名/技能名/流程名）
            metadata: 元数据字典，根据挂载点不同包含不同字段：
                - "history": History（对话历史，可读取和修改）
                - "flow": FlowMetadata（before 时可修改所有字段）
                - "tool": ToolMetadata（before 时可修改 name 和 args）
                - "skill": SkillMetadata（before 时可修改 name 和 loader）
                - "hook": HookMetadata（before 时可修改所有字段）

                钩子可以直接修改这些对象，后续步骤会使用修改后的值。

        Returns:
            HookActionType: ("block", reason) 或 ("continue", None)

        Examples:
            >>> # 阻止操作
            >>> return "block", "禁止操作 python 脚本"
            >>> # 继续执行
            >>> return "continue", None
        """
        pass

    async def hook_action_async(
        self,
        message_id: int,
        task_id: int,
        category: str,
        name: str,
        metadata: dict[str, Any],
    ) -> HookActionType:
        """钩子入口函数（异步版本）

        默认调用同步版本。如果需要异步操作，请覆盖此方法。

        Args:
            message_id: 消息ID
            task_id: 事务ID
            category: 分类（tools/hooks/skills/flow）
            name: 具体名称（工具名/钩子名/技能名/流程名）
            metadata: 元数据字典

        Returns:
            HookActionType: ("block", reason) 或 ("continue", None)
        """
        # 默认调用同步版本
        return self.hook_action(message_id, task_id, metadata)

    def is_async(self) -> bool:
        """检查钩子是否是异步的"""
        # 如果子类覆盖了 hook_action_async，则认为是异步的
        return self.__class__.hook_action_async is not BaseHook.hook_action_async
