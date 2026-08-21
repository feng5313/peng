"""
钩子模板

演示如何使用新的钩子系统定义钩子。
复制此文件作为模板，重命名为你的钩子文件名。
"""

import logging

from peng.hooks.types import BaseHook, HookPattern, HookPatterns

logger = logging.getLogger(__name__)


class YourHookName(BaseHook):
    """你的钩子描述

    钩子类必须：
    1. 继承 BaseHook
    2. 定义 hook_name 属性（唯一标识）
    3. 定义 hook_patterns 属性（匹配模式）
    4. 实现 hook_action 方法

    可选：
    - 定义 priority 属性（优先级，默认 100）
    """

    # 钩子名称（必须唯一）
    hook_name = "your_hook_name"

    # 优先级（可选，默认 100，数值越大越优先）
    priority = 100

    # 钩子匹配模式
    hook_patterns = HookPatterns(
        before=[
            # Before 挂载点：在操作之前执行
            # 示例：在 bash 工具调用之前执行
            # HookPattern(category="tools", pattern=r"^bash$"),
        ],
        after=[
            # After 挂载点：在操作之后执行
            # 示例：在所有工具调用之后执行
            # HookPattern(category="tools", pattern=r".*"),
        ],
    )

    def hook_action(self, message_id: int, task_id: int, category: str, name: str, metadata: dict):
        """钩子执行逻辑

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

        Returns:
            HookActionType: ("block", reason) 或 ("continue", None)
        """
        # 示例1：检查元数据类型
        if "tool" in metadata:
            from peng.hooks.types import ToolMetadata

            tool = metadata["tool"]
            tool_name = tool.name
            tool_args = tool.args

            # 打印工具调用信息
            print(f"[{self.hook_name}] 工具调用: {tool_name}")
            print(f"[{self.hook_name}] 参数: {tool_args}")

            # 示例2：阻止某些操作
            # if "rm -rf" in str(tool_args):
            #     return "block", "禁止执行危险命令"

        # 示例3：修改工具参数
        # if "tool" in metadata:
        #     tool = metadata["tool"]
        #     tool_name = tool.name
        #     if tool_name == "bash":
        #         command = tool.args.get("command", "")
        #         # 在命令前添加 sudo
        #         tool.args["command"] = f"sudo {command}"

        # 示例4：修改流程参数
        # if "flow" in metadata:
        #     flow = metadata["flow"]
        #     if flow.name == "prompt":
        #         # 修改 prompt
        #         flow.args = f"【系统提示】请谨慎回答。\n\n{flow.args}"

        # 继续执行
        return "continue", None

    async def hook_action_async(self, message_id: int, task_id: int, category: str, name: str, metadata: dict):
        """钩子执行逻辑（异步版本）

        用于需要异步操作的钩子（如等待、网络请求等）。

        Args:
            message_id: 消息ID
            task_id: 事务ID
            category: 分类（tools/hooks/skills/flow）
            name: 具体名称（工具名/钩子名/技能名/流程名）
            metadata: 元数据字典

        Returns:
            HookActionType: ("block", reason) 或 ("continue", None)

        Examples:
            >>> # 异步等待检查某些条件
            >>> import asyncio
            >>> await asyncio.sleep(1)
            >>> return "continue", None

            >>> # 阻止操作
            >>> return "block", "操作不符合要求"
        """
        import asyncio

        logger = logging.getLogger(__name__)

        # 示例：异步等待1秒
        logger.info(f"[{self.hook_name}] 异步钩子执行中...")
        await asyncio.sleep(1)

        logger.info(f"[{self.hook_name}] 异步钩子执行完成")
        return "continue", None
