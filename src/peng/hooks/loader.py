"""
钩子系统 v3.0 - 钩子加载器

负责加载和管理自定义钩子。
新特点：
- 扫描 hooks/ 目录
- 动态加载 BaseHook 子类
- 验证钩子必需属性
- 允许钩子名称重复（多个同名钩子都会加载）
"""

import importlib.util
import inspect
import logging
import os
from pathlib import Path
from typing import Any

from peng.hooks.types import BaseHook

logger = logging.getLogger(__name__)


class HookLoader:
    """钩子加载器 v3.0

    负责发现、加载和验证自定义钩子。

    新设计特点：
    - 扫描 hooks/ 目录下的 .py 文件
    - 动态加载 BaseHook 子类
    - 验证钩子必需属性（hook_name, hook_patterns, hook_action）
    - 允许钩子名称重复（多个同名钩子都会加载）
    """

    def __init__(self, hooks_dir: str):
        """初始化钩子加载器

        Args:
            hooks_dir: 钩子目录路径（如 "hooks/"）
        """
        self.hooks_dir = hooks_dir
        self.hooks: list[BaseHook] = []

    def load_hooks(self) -> list[BaseHook]:
        """加载所有钩子

        扫描 hooks/ 目录，加载所有 .py 文件中的 BaseHook 子类。

        Returns:
            加载的钩子列表
        """
        hooks = []

        # 1. 检查 hooks/ 目录是否存在
        if not os.path.exists(self.hooks_dir):
            logger.warning(f"[HookLoader] 钩子目录不存在: {self.hooks_dir}")
            return hooks

        # 2. 扫描目录
        for filename in os.listdir(self.hooks_dir):
            # 3. 只加载 .py 文件
            if not filename.endswith(".py"):
                continue

            # 4. 跳过模板文件
            if filename == "template.py":
                continue

            filepath = os.path.join(self.hooks_dir, filename)

            # 5. 动态加载模块
            try:
                module = self._load_module(filepath)

                # 6. 查找所有 BaseHook 子类
                for attr_name in dir(module):
                    attr = getattr(module, attr_name)

                    # 检查是否是 BaseHook 子类且不是 BaseHook 本身
                    if (
                        inspect.isclass(attr)
                        and issubclass(attr, BaseHook)
                        and attr is not BaseHook
                    ):
                        # 7. 验证钩子
                        hook = self._validate_hook(attr)
                        if hook:
                            hooks.append(hook)

            except Exception as e:
                logger.error(f"❌[HookLoader] 加载钩子失败 {filename}: {e}")

        logger.info(f"[HookLoader] 成功加载 {len(hooks)} 个钩子")
        return hooks

    def _load_module(self, filepath: str):
        """动态加载模块

        Args:
            filepath: Python 文件路径

        Returns:
            加载的模块对象
        """
        spec = importlib.util.spec_from_file_location("hook_module", filepath)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def _validate_hook(self, hook_class: type) -> BaseHook | None:
        """验证钩子类

        检查钩子类是否具有必需的属性和方法。

        Args:
            hook_class: 钩子类

        Returns:
            钩子实例，验证失败返回 None
        """
        try:
            # 1. 检查必需属性
            if not hasattr(hook_class, "hook_name"):
                logger.error(f"[HookLoader] 钩子缺少 hook_name: {hook_class}")
                return None

            if not hasattr(hook_class, "hook_patterns"):
                logger.error(f"[HookLoader] 钩子缺少 hook_patterns: {hook_class}")
                return None

            if not hasattr(hook_class, "hook_action") and not hasattr(
                hook_class, "hook_action_async"
            ):
                logger.error(f"[HookLoader] 钩子缺少 hook_action或hook_action_async: {hook_class}")
                return None

            # 2. 实例化钩子
            hook = hook_class()

            logger.info(f"[HookLoader] 验证通过: {hook.hook_name} (来源: {hook_class})")

            # 3. 钩子名称允许重复，多个同名钩子都会被加载并执行
            return hook

        except Exception as e:
            logger.error(f"[HookLoader] 验证钩子失败: {e}")
            return None
