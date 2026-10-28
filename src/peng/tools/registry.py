"""
工具注册表

管理所有可用工具，提供统一的注册、查询和调用接口。
支持双通道输出：display 打印到终端，summary 返回给 LLM。
"""

import logging
import traceback
from datetime import datetime
from pathlib import Path
from typing import Any

from peng.common.colors import colorize, get_colors
from peng.common.paths import get_temp_directory
from peng.tools.base import DualOutput, Tool, ToolError

logger = logging.getLogger(__name__)

# 获取颜色配置
_COLORS = get_colors()


class ToolNotFoundError(ToolError):
    """工具不存在"""


from peng.common import MetaSingleton


class ToolRegistry(metaclass=MetaSingleton):
    """工具注册表"""

    def __init__(self) -> None:
        """
        初始化工具注册表
        """
        self._tools: dict[str, Tool] = {}
        self._aliases: dict[str, str] = {}  # 别名 → 主名称 映射
        self._disabled: set[str] = set()
        self._llm_client = None
        self._message_manager = None
        self._context: dict[str, Any] = {}  # 工具上下文（message_id, task_id等）

    def set_llm_client(self, client) -> None:
        """
        设置LLM客户端，用于注入到工具

        Args:
            client: LLM客户端实例
        """
        self._llm_client = client

    def set_message_manager(self, manager) -> None:
        """
        设置消息管理器，用于注入到工具

        Args:
            manager: MessageManager 实例
        """
        self._message_manager = manager

    def set_context(self, **kwargs) -> None:
        """
        设置工具上下文

        Args:
            **kwargs: 上下文键值对（如message_id=123, task_id=456）
        """
        # 🔧 事务#2435：workdir/nest 统一清理首尾空白（context 唯一生产入口）。
        # 尾部空格会导致 daemon 包装 bat 的 cd /d "... " 失败、日志重定向路径无效
        workdir = kwargs.get("workdir")
        if isinstance(workdir, str):
            kwargs["workdir"] = workdir.strip()
        nest = kwargs.get("nest")
        if isinstance(nest, str):
            kwargs["nest"] = nest.strip()
        self._context.update(kwargs)

    def clear_context(self) -> None:
        """清空工具上下文"""
        self._context.clear()

    def set_show_tool_output(self, show: bool) -> None:
        """
        设置是否显示工具执行输出

        Args:
            show: 是否显示
        """
        self._show_tool_output = show

    def register(self, tool: Tool) -> None:
        """
        注册工具

        使用 tool.primary_name 作为注册主名称（固定不变），
        同时注册 tool.ALIASES 中的别名，使别名也指向同一工具实例。

        Args:
            tool: 工具实例

        Raises:
            ValueError: 工具名称已存在
        """
        primary = tool.primary_name
        if primary in self._tools:
            raise ValueError(f"工具 '{primary}' 已存在")
        self._tools[primary] = tool
        # 注册别名
        for alias in getattr(tool, "ALIASES", ()):
            if alias in self._tools:
                raise ValueError(f"别名 '{alias}' 与已有工具名冲突")
            if alias in self._aliases:
                raise ValueError(f"别名 '{alias}' 已被注册")
            self._aliases[alias] = primary

    def unregister(self, name: str) -> bool:
        """
        注销工具

        同时清除关联的别名。

        Args:
            name: 工具名称（主名称或别名）

        Returns:
            是否成功注销
        """
        # 解析别名
        primary = self._aliases.get(name, name)
        if primary in self._tools:
            del self._tools[primary]
            # 清除指向该工具的所有别名
            self._aliases = {a: p for a, p in self._aliases.items() if p != primary}
            return True
        return False

    def get(self, name: str) -> Tool | None:
        """
        获取工具

        支持通过主名称或别名查找。

        Args:
            name: 工具名称（主名称或别名）

        Returns:
            工具实例，不存在则返回None
        """
        primary = self._aliases.get(name, name)
        return self._tools.get(primary)

    def disable(self, names: list[str]):
        for name in names:
            # 同时禁用主名称和别名
            self._disabled.add(name)

    def enable(self, names: list[str]):
        for name in names:
            # 同时启用主名称和别名
            self._disabled.discard(name)

    def list_tools(self) -> list[str]:
        """
        列出所有工具名称（含别名）

        Returns:
            工具名称列表
        """
        return list(self._tools.keys()) + list(self._aliases.keys())

    def get_definitions(self, disable: list[str] = []) -> list[dict]:
        """
        获取所有工具的OpenAI格式定义

        在构建定义前，将当前上下文传播到所有工具实例，
        使工具的 description/parameters 能根据上下文（如 nest 平台）动态生成。

        Returns:
            工具定义列表（用于LLM API调用）
        """
        # 传播上下文到所有工具（使 description/parameters/name 能动态感知 nest 平台等）
        if hasattr(self, "_context") and self._context:
            for tool in self._tools.values():
                if hasattr(tool, "set_context"):
                    tool.set_context(**self._context)

        disabled_set = set(self._disabled) | set(disable)
        return [
            tool.to_openai_format()
            for tool in self._tools.values()
            # 同时检查动态名称和主名称是否被禁用
            if tool.name not in disabled_set
            and getattr(tool, "primary_name", tool.name) not in disabled_set
        ]

    async def execute(self, name: str, arguments: dict) -> Any:
        """
        执行指定工具

        支持通过主名称或别名查找工具。

        Args:
            name: 工具名称（主名称或别名）
            arguments: 工具参数

        Returns:
            执行结果

        Raises:
            ToolNotFoundError: 工具不存在
            ToolError: 执行失败
        """
        # 解析别名
        primary = self._aliases.get(name, name)
        if name in self._disabled or primary in self._disabled:
            raise ToolNotFoundError(f"工具 '{name}' 已禁用")
        tool = self._tools.get(primary)
        if not tool:
            raise ToolNotFoundError(f"工具 '{name}' 不存在")

        # 注入 LLM 客户端（如果工具支持）
        if self._llm_client and hasattr(tool, "set_llm_client"):
            tool.set_llm_client(self._llm_client)

        # 注入消息管理器（如果工具支持）
        if self._message_manager and hasattr(tool, "set_message_manager"):
            tool.set_message_manager(self._message_manager)

        # 注入上下文（如果工具支持）
        if hasattr(tool, "set_context") and hasattr(self, "_context"):
            tool.set_context(**self._context)

        try:
            result = await tool.execute(**arguments)

            # 处理双通道输出：返回 DualOutput 对象，打印由 agent 负责
            if isinstance(result, DualOutput):
                result = self._check_and_truncate_dual_output(name, result)
                return result

            # 普通输出：检查大小并截断
            result = self._check_and_truncate_result(name, result)
            return result
        except Exception as e:
            # 错误信息立即打印（工具层错误需要及时反馈）
            if self._show_tool_output:
                error_color = _COLORS.tools("tool_error")
                logger.error(
                    f"{colorize('❌ 工具执行错误:', error_color)} {e}",
                )
            # 返回错误消息给大模型，而不是抛出异常
            # 这样大模型可以知道工具执行失败并继续处理
            return f"工具执行失败: {e}"

    def _get_tool_result_max_bytes(self) -> int:
        """获取工具结果最大字节数（使用当前 provider 的 compact_threshold）

        优先从 _llm_client.compact_threshold 获取（即当前 provider 的压缩阈值），
        回退到 config.truncation.tool_result_max_bytes。
        """
        try:
            if self._llm_client and hasattr(self._llm_client, "compact_threshold"):
                return int(self._llm_client.compact_threshold * 0.5)
        except Exception:
            pass
        try:
            from peng.config import load_config

            config = load_config()
            return int(config.truncation.tool_result_max_bytes * 0.5)
        except Exception:
            return 8192  # 默认8KB

    def _truncate_text_by_bytes(
        self, text: str, max_bytes: int
    ) -> tuple[str, int, int]:
        """按字节数限制截断文本（按行对齐，不截断单行中间）

        Args:
            text: 原始文本
            max_bytes: 最大字节数

        Returns:
            (截断后的文本, 返回的行数, 总行数)
        """
        lines = text.split("\n")
        total_lines = len(lines)

        result_lines = []
        current_bytes = 0
        for line in lines:
            line_bytes = len((line + "\n").encode("utf-8"))
            if current_bytes + line_bytes > max_bytes:
                break
            result_lines.append(line)
            current_bytes += line_bytes

        end_line = len(result_lines)
        return "\n".join(result_lines), end_line, total_lines

    def _save_to_temp_file(self, tool_name: str, content: str) -> str:
        """将内容写入临时文件，返回文件路径

        Args:
            tool_name: 工具名称（用于文件名前缀）
            content: 要保存的内容

        Returns:
            临时文件的绝对路径
        """
        temp_dir = get_temp_directory()
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
        # 清理工具名中的特殊字符
        safe_name = "".join(c if c.isalnum() or c in "_-" else "_" for c in tool_name)
        filename = f"tool_{safe_name}_{timestamp}.txt"
        filepath = temp_dir / filename

        with open(filepath, "w", encoding="utf-8") as f:
            f.write(content)

        return str(filepath.resolve())

    def _check_and_truncate_result(self, tool_name: str, result: Any) -> Any:
        """检查普通结果大小，超过阈值则保存临时文件并返回按行截断的内容

        Args:
            tool_name: 工具名称
            result: 工具返回结果

        Returns:
            原始结果或截断后的内容（含行信息提示）
        """
        if result is None:
            return result

        # 将结果转为字符串
        if isinstance(result, str):
            text = result
        elif isinstance(result, (dict, list)):
            from peng.common import jsonp as json

            text = json.dumps(result, ensure_ascii=False, indent=2)
        else:
            text = str(result)

        # 检查字节数
        byte_size = len(text.encode("utf-8"))
        max_bytes = self._get_tool_result_max_bytes()

        if byte_size <= max_bytes:
            return result

        # 超过阈值：保存完整内容到临时文件
        temp_path = self._save_to_temp_file(tool_name, text)
        size_kb = byte_size / 1024

        # 按行截断返回内容
        truncated_text, end_line, total_lines = self._truncate_text_by_bytes(text, max_bytes)
        remaining_lines = total_lines - end_line

        logger.info(
            f"📦 [ToolRegistry] 工具 '{tool_name}' 返回结果过大 "
            f"({size_kb:.1f}KB > {max_bytes / 1024:.0f}KB)，已保存到: {temp_path}"
        )

        # 返回截断内容 + 行信息提示
        return (
            truncated_text
            + f"\n\n⚠️ [工具 '{tool_name}' 返回数据超出阈值 ({size_kb:.1f}KB > {max_bytes / 1024:.0f}KB)，已按行截断]\n"
            f"总行数: {total_lines}，当前返回: 第1-{end_line}行，剩余: {remaining_lines}行\n"
            f"完整内容已保存到临时文件: {temp_path}\n"
            + (f"💡 建议：将命令输出重定向到临时文件。\n" if tool_name in ("bash", "cmd") else f"可使用 read 工具读取该文件查看完整内容。")
        )

    def _check_and_truncate_dual_output(self, tool_name: str, result: DualOutput) -> DualOutput:
        """检查 DualOutput 的 summary 大小，超过阈值则按行截断

        - read 工具：不保存临时文件，直接按行截断返回 + 行信息提示
        - 其他工具：保存临时文件 + 按行截断返回 + 行信息提示

        DualOutput 的 summary 会发送给 LLM，所以需要截断。
        display 仅打印到终端，不进入 LLM 上下文，不需要截断。

        Args:
            tool_name: 工具名称
            result: DualOutput 对象

        Returns:
            原始 DualOutput 或 summary 被截断的 DualOutput
        """
        text = result.summary
        byte_size = len(text.encode("utf-8"))
        max_bytes = self._get_tool_result_max_bytes()

        if byte_size <= max_bytes:
            return result

        # 按行截断 summary 内容
        truncated_text, end_line, total_lines = self._truncate_text_by_bytes(text, max_bytes)
        remaining_lines = total_lines - end_line
        size_kb = byte_size / 1024

        if tool_name == "read":
            # read 工具：不保存临时文件，直接返回截断内容 + 行信息
            logger.info(
                f"📦 [ToolRegistry] read 工具返回结果过大 "
                f"({size_kb:.1f}KB > {max_bytes / 1024:.0f}KB)，已按行截断返回"
            )
            truncated_summary = (
                truncated_text
                + f"\n\n⚠️ [返回数据超出阈值 ({size_kb:.1f}KB > {max_bytes / 1024:.0f}KB)，已按行截断]\n"
                f"总行数: {total_lines}，当前返回: 第1-{end_line}行，剩余: {remaining_lines}行\n"
                f"可使用 read 工具的 offset 参数继续读取后续内容。"
            )
        else:
            # 其他工具：保存完整内容到临时文件 + 返回截断内容 + 行信息
            temp_path = self._save_to_temp_file(tool_name, text)
            logger.info(
                f"📦 [ToolRegistry] 工具 '{tool_name}' DualOutput.summary 过大 "
                f"({size_kb:.1f}KB > {max_bytes / 1024:.0f}KB)，已保存到: {temp_path}"
            )
            truncated_summary = (
                truncated_text
                + f"\n\n⚠️ [工具 '{tool_name}' 返回数据超出阈值 ({size_kb:.1f}KB > {max_bytes / 1024:.0f}KB)，已按行截断]\n"
                f"总行数: {total_lines}，当前返回: 第1-{end_line}行，剩余: {remaining_lines}行\n"
                + f"完整内容已保存到临时文件: {temp_path}\n"
                + (f"💡 建议：将命令输出重定向到临时文件。\n" if tool_name in ("bash", "cmd") else f"可使用 read 工具读取该文件查看完整内容。")
            )

        # 保留 display 不变（仅用于终端显示）
        return DualOutput(
            summary=truncated_summary,
            display=result.display or truncated_summary,
            attachments=result.attachments,
        )

    def _format_arguments(self, arguments: dict) -> str:
        """格式化参数预览"""
        if not arguments:
            return ""

        parts = []
        for key, value in list(arguments.items())[:3]:  # 最多显示3个参数
            if isinstance(value, str):
                preview = value[:30] + "..." if len(value) > 30 else value
                parts.append(f"{key}={repr(preview)}")
            elif isinstance(value, (int, float, bool)):
                parts.append(f"{key}={value}")
            else:
                parts.append(f"{key}=...")

        result = ", ".join(parts)
        if len(arguments) > 3:
            result += ", ..."
        return result

    def _format_result(self, result: Any) -> str:
        """格式化结果预览"""
        # 处理 DualOutput - 返回摘要
        if isinstance(result, DualOutput):
            summary = result.summary
            if len(summary) > 50:
                return summary[:50] + "..."
            return summary

        if result is None:
            return "None"

        if isinstance(result, str):
            if len(result) > 50:
                return result[:50] + "..."
            return result

        if isinstance(result, dict):
            keys = list(result.keys())[:5]
            return f"dict({', '.join(keys)}{'...' if len(result) > 5 else ''})"

        if isinstance(result, list):
            return f"list({len(result)} items)"

        return str(result)[:50]

    def load_builtin_tools(self) -> None:
        """
        加载所有内置工具

        包括: read, write, edit, bash, daemon, glob, grep, websearch, webfetch,
              lsp, goto_definition, find_references,
              message, task, skill, info
        """
        if self._tools:
            return
        from peng.tools.bash import BashTool, DaemonTool
        from peng.tools.compact import COMPACT_TOOL
        from peng.tools.file import EditTool, LineEditTool, ReadTool, WriteTool
        from peng.tools.glob import GlobTool
        from peng.tools.grep import GrepTool
        from peng.tools.lsp import LSPTool
        from peng.tools.message import MessageTool
        from peng.tools.nest import InputTool, NestTool, ShotTool, WindowTool
        from peng.tools.skill import SKILL_TOOL
        from peng.tools.task import ChecklistTool, InfoTool, ReportTool, TaskTool
        from peng.tools.web import WebFetchTool, WebSearchTool

        # 文件操作
        self.register(ReadTool())
        self.register(WriteTool())
        self.register(EditTool())
        self.register(LineEditTool())
        # 命令执行
        self.register(BashTool())
        self.register(DaemonTool())
        # 搜索
        self.register(GlobTool())
        self.register(GrepTool())
        # 网络
        self.register(WebSearchTool())
        self.register(WebFetchTool())
        # LSP
        self.register(LSPTool())
        # 消息管理工具（统一MessageTool）
        self.register(MessageTool())
        # 事务管理工具（统一TaskTool）
        self.register(TaskTool())
        # 事务检查清单工具
        self.register(ChecklistTool())
        # 工作汇报工具
        self.register(ReportTool())
        # Skill 加载工具（Progressive Disclosure）
        self.register(SKILL_TOOL)
        # 事务信息注入工具
        self.register(InfoTool())
        # 窗口查询和截屏工具
        self.register(WindowTool())
        self.register(ShotTool())
        # 远程输入工具
        self.register(InputTool())
        # Nest管理工具
        self.register(NestTool())
        # 超长工具结果/思考内容清理精简工具（事务#2114，事务#2270扩展思考内容）
        self.register(COMPACT_TOOL)

    def load_from_directory(self, path: str) -> None:
        """
        从目录加载工具

        Args:
            path: 工具目录路径
        """
        # TODO: 实现从目录加载
