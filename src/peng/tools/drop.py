"""
drop 工具：清理上一轮迭代中无用的超长工具调用返回结果（事务#2114）

大模型上一轮迭代中，超过阈值字节数（provider级 drop_hint_threshold，
默认1024）的工具返回结果末尾带有提示：
" 如果这些返回结果是毫无用处的垃圾信息，请用drop工具清理，id xxxx，尽量节省上下文空间！"
其中 xxxx 为该结果的唯一4位数字ID。

模型确认某个带此提示的返回结果对后续工作毫无用处时，调用本工具按id清理：
- 对应 tool 消息的 content 整体替换为占位文案，工具调用记录和参数保持不变
- 只能清理上一轮迭代的结果，禁止跨迭代清理（保护上下文前缀缓存）
"""

import logging
import re
from typing import Any

from peng.tools.base import Tool

logger = logging.getLogger(__name__)


class DropTool(Tool):
    """清理上一轮迭代中无用的超长工具调用返回结果，节省上下文空间。"""

    @property
    def name(self) -> str:
        return "drop"

    @property
    def description(self) -> str:
        return """清理上一轮迭代中无用的超长工具调用返回结果，节省上下文空间。

上一轮迭代中，超过阈值字节数的工具返回结果末尾带有提示：
" 如果这些返回结果是毫无用处的垃圾信息，请用drop工具清理，id xxxx，尽量节省上下文空间！"
其中 xxxx 为该结果的唯一4位数字ID。

当且仅当你确认某个带此提示的返回结果对后续工作毫无用处时，才调用本工具清理对应结果：
- 该结果将被替换为"垃圾信息，已被drop工具清理。"，工具调用记录和参数保持不变
- 只能清理上一轮迭代的结果，不能跨迭代清理
- 对后续工作有用的结果（如稍后需要引用的代码/命令输出内容）不要清理

参数：
- id: 4位数字ID（必填），来自工具结果末尾提示中的 "id xxxx"
"""

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "id": {
                    "type": "string",
                    "description": "要清理的工具结果的4位数字ID（来自结果末尾提示中的 id xxxx）",
                }
            },
            "required": ["id"],
        }

    async def execute(self, id: str) -> str:  # noqa: A002 - 工具参数名与API约定一致
        """
        按id清理上一轮迭代的工具返回结果

        Args:
            id: 4位数字ID（来自工具结果末尾drop提示中的 "id xxxx"）

        Returns:
            清理结果说明（成功或错误信息）
        """
        history = self.get_context("history")
        if history is None or not hasattr(history, "drop_tool_result"):
            return "错误：drop工具不可用（未获取到消息历史上下文）。"

        drop_id = str(id).strip()
        if not re.fullmatch(r"\d{4}", drop_id):
            return f"错误：id必须为4位数字（收到 '{drop_id}'）。"

        try:
            success, message = history.drop_tool_result(drop_id)
        except Exception as e:
            logger.error(f"[DropTool] 清理失败: {e}")
            return f"错误：清理失败: {e}"

        if success:
            logger.info(f"[DropTool] {message}")
        else:
            logger.warning(f"[DropTool] {message}")
        return message


# 导出工具实例
DROP_TOOL = DropTool()
