"""
钩子系统 - 钩子管理器

统一管理钩子系统，负责加载和执行钩子。
"""

import logging
from typing import Any

from peng.hooks.executor import HookExecutor
from peng.hooks.loader import HookLoader
from peng.hooks.types import HookActionType

logger = logging.getLogger(__name__)

from peng.common import MetaSingleton


class HookManager(metaclass=MetaSingleton):
    """钩子管理器

    负责加载和管理钩子系统，提供统一的接口供其他模块使用。

    功能：
    - 加载 src/peng/hooks/ 目录下的钩子
    - 提供 before/after 钩子执行接口（异步）
    - 管理 tools/hooks/skills/flow 四类钩子
    """

    def __init__(self, hooks_dir: str = "src/peng/hooks"):
        """初始化钩子管理器

        Args:
            hooks_dir: 钩子目录路径（默认 "src/peng/hooks/"）
        """
        # 加载钩子
        loader = HookLoader(hooks_dir)
        hooks = loader.load_hooks()

        # 创建执行器
        self.executor = HookExecutor(hooks)

        # 保存钩子列表（用于调试）
        self.hooks = hooks

        # 加载统计
        self.hooks_count = len(hooks)
        logger.info(f"[HookManager] 加载了 {self.hooks_count} 个钩子")

    async def execute_before_hooks(
        self,
        message_id: int,
        task_id: int,
        category: str,
        name: str,
        metadata: dict[str, Any],
    ) -> tuple[str, str | None]:
        """执行 before 钩子

        Args:
            message_id: 消息ID
            task_id: 事务ID
            category: 分类（tools/hooks/skills/flow）
            name: 具体名称
            metadata: 元数据字典

        Returns:
            HookActionType: ("block", reason) 或 ("continue", None)
        """
        return await self.executor.execute_before_hooks(
            message_id, task_id, category, name, metadata
        )

    async def execute_after_hooks(
        self,
        message_id: int,
        task_id: int,
        category: str,
        name: str,
        metadata: dict[str, Any],
    ) -> tuple[str, str | None]:
        """执行 after 钩子

        Args:
            message_id: 消息ID
            task_id: 事务ID
            category: 分类（tools/hooks/skills/flow）
            name: 具体名称
            metadata: 元数据字典

        Returns:
            HookActionType: ("block", reason) 或 ("continue", None)
        """
        return await self.executor.execute_after_hooks(
            message_id, task_id, category, name, metadata
        )


# 全局钩子管理器实例
_global_hook_manager: HookManager | None = None


def get_hook_manager() -> HookManager:
    """获取全局钩子管理器

    Returns:
        HookManager 实例
    """
    global _global_hook_manager

    if _global_hook_manager is None:
        logger.info("[HookManager] 初始化全局钩子管理器")
        _global_hook_manager = HookManager()

    return _global_hook_manager


def reset_hook_manager() -> None:
    """重置全局钩子管理器（用于测试）"""
    global _global_hook_manager
    _global_hook_manager = None
