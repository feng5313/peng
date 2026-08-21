"""
钩子系统 - 钩子执行器

负责匹配和执行钩子。
支持同步和异步钩子。
所有执行方法都是异步的，避免事件循环冲突。
"""

import asyncio
import logging
import re
from typing import Any

from peng.hooks.types import BaseHook, HookActionType

logger = logging.getLogger(__name__)


class HookExecutor:
    """钩子执行器

    负责匹配和执行钩子。

    新设计特点：
    - 支持 before/after 两大类挂载点
    - 对 tools/hooks/skills/flow 四大类进行独立匹配
    - 按优先级排序执行（priority 数值越大越优先）
    - 正确处理 block 返回值（不抛出异常）
    - 支持异步钩子（hook_action_async）
    - 所有执行方法都是异步的，避免事件循环冲突
    """

    def __init__(self, hooks: list[BaseHook]):
        """初始化钩子执行器

        Args:
            hooks: 钩子列表
        """
        self.hooks = hooks

    def get_matched_hooks(self, position: str, category: str, name: str) -> list[BaseHook]:
        """获取匹配的钩子

        根据挂载点位置、分类和名称匹配钩子。

        Args:
            position: 挂载点位置（before/after）
            category: 分类（tools / hooks / skills / flow）
            name: 具体名称（工具名/钩子名/技能名/流程名）

        Returns:
            匹配的钩子列表（按优先级排序）
        """
        matched_hooks = []

        for hook in self.hooks:
            patterns = hook.hook_patterns

            # 获取指定位置的匹配模式
            if position == "before":
                pattern_list = patterns.before
            elif position == "after":
                pattern_list = patterns.after
            else:
                continue

            # 检查是否匹配
            for pattern in pattern_list:
                if pattern.category == category:
                    if re.match(pattern.pattern, name):
                        matched_hooks.append(hook)
                        break

        # 按优先级排序（数值越大越优先）
        matched_hooks.sort(key=lambda h: getattr(h, "priority", 100), reverse=True)

        return matched_hooks

    async def execute_before_hooks(
        self,
        message_id: int,
        task_id: int,
        category: str,
        name: str,
        metadata: dict[str, Any],
    ) -> tuple[str, str | None]:
        """执行 before 钩子（异步）

        Args:
            message_id: 消息ID
            task_id: 事务ID
            category: 分类（tools/hooks/skills/flow）
            name: 具体名称（工具名/钩子名/技能名/流程名）
            metadata: 元数据字典

        Returns:
            list of HookActionType: [("block", reason) 或 ("continue", None)]
        """
        # 1. 获取所有匹配的 before 钩子
        matched_hooks = self.get_matched_hooks("before", category, name)

        if not matched_hooks:
            return []

        logger.info(
            f"[HookExecutor] 执行 {len(matched_hooks)} 个 before 钩子 "
            f"(category={category}, name={name})"
        )

        # 2. 依次执行（已按优先级排序）
        results = []
        for hook in matched_hooks:
            try:
                # 检查是否是异步钩子
                if hook.is_async():
                    # 异步钩子：直接 await
                    action, reason = await hook.hook_action_async(message_id, task_id, category, name, metadata)
                    results.append((action, reason))
                else:
                    # 同步钩子：直接调用
                    action, reason = hook.hook_action(message_id, task_id, category, name, metadata)
                    results.append((action, reason))
                logger.info(
                    f"[HookExecutor] 钩子 {hook.hook_name} 执行结果: "
                    f"action={action}, reason={reason}"
                )

            except Exception as e:
                logger.error(
                    f"[HookExecutor] 钩子 {hook.hook_name} 执行失败: {e}",
                    exc_info=True,
                )
                # 不抛出异常，继续执行下一个钩子
        return results

    async def execute_after_hooks(
        self,
        message_id: int,
        task_id: int,
        category: str,
        name: str,
        metadata: dict[str, Any],
    ) -> tuple[str, str | None]:
        """执行 after 钩子（异步）

        Args:
            message_id: 消息ID
            task_id: 事务ID
            category: 分类（tools/hooks/skills/flow）
            name: 具体名称
            metadata: 元数据字典

        Returns:
            HookActionType: ("block", reason) 或 ("continue", None)
        """
        # 1. 获取所有匹配的 after 钩子
        matched_hooks = self.get_matched_hooks("after", category, name)

        if not matched_hooks:
            return []

        logger.info(
            f"[HookExecutor] 执行 {len(matched_hooks)} 个 after 钩子 "
            f"(category={category}, name={name})"
        )

        # 2. 依次执行（已按优先级排序）
        results = []
        for hook in matched_hooks:
            try:
                # 检查是否是异步钩子
                if hook.is_async():
                    # 异步钩子：直接 await
                    action, reason = await hook.hook_action_async(message_id, task_id, category, name, metadata)
                    results.append((action, reason))
                else:
                    # 同步钩子
                    action, reason = hook.hook_action(message_id, task_id, category, name, metadata)
                    results.append((action, reason))

                logger.info(
                    f"[HookExecutor] 钩子 {hook.hook_name} after 执行结果: "
                    f"action={action}, reason={reason}"
                )
            except Exception as e:
                logger.error(
                    f"[HookExecutor] 钩子 {hook.hook_name} 执行失败: {e}",
                    exc_info=True,
                )
                # 不抛出异常，继续执行下一个钩子

        return results
