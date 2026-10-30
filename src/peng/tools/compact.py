"""
compact 工具：清理/精简上一轮迭代中无用的超长工具返回结果（事务#2114）

大模型上一轮迭代中，超过阈值字节数（provider级 compact_hint_threshold，
默认1024字节）的工具返回结果末尾带有提示：
" 返回结果较长（NNNN字符），如果无用或可以用总结大幅精简，请用compact工具清理或精简，id xxxx，尽量节省上下文空间！"
其中 xxxx 为该结果的唯一4位数字ID，NNNN为该结果字符数。

模型确认某段带此提示的结果对后续工作无用（或可大幅精简）时，调用本工具按id精简：
- tool 消息的 content 整体替换为指定精简文字，工具调用记录和参数保持不变
- 只能精简上一轮迭代的内容，禁止跨迭代精简（保护上下文前缀缓存）

注意（事务#2521）：ID只在结果返回入历史后才存在，不能预判同批次并行调用的
read等工具即将返回结果的ID；错误消息中id以"id#xxxx"格式书写，避免构成
marker子串被后续同id精简误命中。
"""

import logging
import re
from typing import Any

from peng.tools.base import Tool

logger = logging.getLogger(__name__)


class CompactTool(Tool):
    """清理/精简上一轮迭代中无用的超长工具结果，节省上下文空间。"""

    @property
    def name(self) -> str:
        return "compact"

    @property
    def description(self) -> str:
        return """如果上一轮工具调用的返回结果中有较长无用或可被大幅精简的信息，用此工具清理或精简之，节省上下文空间。
- 只能清理或精简上一轮迭代的工具结果，不能跨迭代清理
- 对后续工作有用的结果（如稍后需要引用的代码/命令输出内容）不要清理
"""

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "id": {
                    "type": "string",
                    "description": "要精简工具结果的4位数字ID（来自工具结果末尾提示中的 id xxxx）",
                },
                "replace": {
                    "type": "string",
                    "description": "替换文字。例1：“无用信息”；例2：“内容精简：<精简内容>”",
                },
            },
            "required": ["id", "replace"],
        }

    async def execute(self, id: str, replace: str) -> str:  # noqa: A002 - 工具参数名与API约定一致
        """
        按id精简上一轮迭代的工具返回结果

        Args:
            id: 4位数字ID（来自工具结果末尾compact提示中的 "id xxxx"）

        Returns:
            精简结果说明（成功或错误信息）
        """
        history = self.get_context("history")
        if history is None or not hasattr(history, "compact_by_id"):
            return "错误：compact工具不可用（未获取到消息历史上下文）。"

        compact_id = str(id).strip()
        if not re.fullmatch(r"\d{4}", compact_id):
            return f"错误：id必须为4位数字（收到 '{compact_id}'）。"

        try:
            success, message = history.compact_by_id(compact_id, replace)
        except Exception as e:
            logger.error(f"[CompactTool] 精简失败: {e}")
            return f"错误：精简失败: {e}"

        if success:
            logger.info(f"[CompactTool] {message}")
        else:
            logger.warning(f"[CompactTool] {message}")
        return message


# 导出工具实例
COMPACT_TOOL = CompactTool()
