"""
事务管理工具（统一调用bin/task命令行接口）

提供事务查询、控制、子事务管理等功能，内部统一调用bin/task命令行工具。
所有功能通过命令行接口实现，确保一致性和可维护性。

这是一个单一工具类，通过action参数区分不同功能。

大模型必用功能（直接提供说明）：
- tree: 获取事务树
- msg: 获取事务关联的消息链
- pause/cancel/resume/restart: 控制事务
- new: 创建子事务

其他功能：使用--help参数探索详细信息
"""

import logging
import os
import re
import shlex
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

from peng.common import jsonp as json
from peng.common.colors import ANSI_COLORS as _ANSI
from peng.common.paths import get_temp_directory, chown_to_project_owner, ensure_dir_with_owner
from peng.config import PathsConfig, get_agent_name, get_master_name, get_project_root
from peng.tools.base import DualOutput, Tool, ToolError
from peng.tools.file import replace_content, trim_diff, normalize_line_endings
from peng.common.utils import generate_colored_diff

logger = logging.getLogger(__name__)
AGENT_NAME = get_agent_name()
MASTER_NAME = get_master_name()
PROJECT_ROOT = get_project_root()


def _format_markdown_for_display(content: str, title: str = "") -> str:
    """将 Markdown 文本格式化为终端友好的高亮显示。

    使用 ANSI 颜色码对 Markdown 元素进行语法高亮：
    - 标题（#/##/###）：bright_cyan + bold
    - 已完成项（✅ / - [x]）：bright_green
    - 待办项（⌛ / - [ ]）：bright_yellow
    - 失败/警告（❌ / ⚠️）：bright_red / yellow
    - 加粗文本（**text**）：bold
    - 代码块（```）：dim
    - 列表项（- / *）：bright_white
    - 分隔线（---）：dim

    Args:
        content: Markdown 格式文本
        title: 可选的标题行（会以 bright_cyan+bold 高亮显示在内容前）
    Returns:
        带 ANSI 高亮的终端显示文本
    """
    if not content:
        return content

    lines = content.split("\n")
    result: list[str] = []

    if title:
        result.append(f"{_ANSI['bright_cyan']}{_ANSI['bold']}{'─' * 60}{_ANSI['reset']}")
        result.append(f"{_ANSI['bright_cyan']}{_ANSI['bold']}{title}{_ANSI['reset']}")
        result.append(f"{_ANSI['bright_cyan']}{_ANSI['bold']}{'─' * 60}{_ANSI['reset']}")

    in_code_block = False
    for line in lines:
        stripped = line.strip()

        # 代码块开关
        if stripped.startswith("```"):
            in_code_block = not in_code_block
            result.append(f"{_ANSI['dim']}{line}{_ANSI['reset']}")
            continue
        if in_code_block:
            result.append(f"{_ANSI['dim']}{line}{_ANSI['reset']}")
            continue

        # 分隔线
        if re.match(r"^-{3,}$", stripped) or re.match(r"^={3,}$", stripped):
            result.append(f"{_ANSI['dim']}{line}{_ANSI['reset']}")
            continue

        # 标题
        if stripped.startswith("###"):
            result.append(f"{_ANSI['cyan']}{_ANSI['bold']}{line}{_ANSI['reset']}")
        elif stripped.startswith("##"):
            result.append(f"{_ANSI['bright_cyan']}{_ANSI['bold']}{line}{_ANSI['reset']}")
        elif stripped.startswith("#"):
            result.append(f"{_ANSI['bright_cyan']}{_ANSI['bold']}{line}{_ANSI['reset']}")
        # 状态符号着色
        elif "✅" in stripped:
            result.append(f"{_ANSI['bright_green']}{_render_inline_bold(line)}{_ANSI['reset']}")
        elif "⌛" in stripped:
            result.append(f"{_ANSI['bright_yellow']}{_render_inline_bold(line)}{_ANSI['reset']}")
        elif "❌" in stripped:
            result.append(f"{_ANSI['bright_red']}{_render_inline_bold(line)}{_ANSI['reset']}")
        elif "⚠️" in stripped:
            result.append(f"{_ANSI['yellow']}{_render_inline_bold(line)}{_ANSI['reset']}")
        # checkbox
        elif re.match(r"^\s*[-*]\s+\[x\]", stripped, re.IGNORECASE):
            result.append(f"{_ANSI['bright_green']}{_render_inline_bold(line)}{_ANSI['reset']}")
        elif re.match(r"^\s*[-*]\s+\[.\]", stripped):
            result.append(f"{_ANSI['bright_yellow']}{_render_inline_bold(line)}{_ANSI['reset']}")
        # 列表项
        elif stripped.startswith("- ") or stripped.startswith("* "):
            result.append(f"{_ANSI['bright_white']}{_render_inline_bold(line)}{_ANSI['reset']}")
        else:
            result.append(_render_inline_bold(line))

    return "\n".join(result)


def _render_inline_bold(text: str) -> str:
    """将 Markdown 加粗标记 **text** 转换为 ANSI bold 高亮。"""
    return re.sub(r"\*\*(.+?)\*\*", rf"{_ANSI['bold']}\1{_ANSI['reset']}", text)


def _write_temp_file(content: str) -> str:
    """将内容写入临时文件，返回文件路径"""
    temp_dir = get_temp_directory()
    ensure_dir_with_owner(temp_dir)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    path = temp_dir / f"peng-task-{timestamp}.txt"
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)
    chown_to_project_owner(path)
    return str(path)


class ChecklistTool(Tool):
    """
    事务检查清单工具（ChecklistTool）

    读取和写入事务自身的checklist字段，用于跟踪信息收集、工作和测试验收事项进度。
    支持通过id参数查看其他事务的checklist（只读）。
    """

    def __init__(self):
        """初始化工具"""
        super().__init__()

    @property
    def name(self) -> str:
        return "checklist"

    @property
    def description(self) -> str:
        return """事务事项检查清单工具（ChecklistTool）。

使用Markdown格式文本，读取和写入事务的checklist字段。
用于跟踪信息收集、工作步骤、测试验收、进一步优化方向（如有）、git diff审核、提交等事项和进度。
**事务启动时必须调用此工具检查checklist，并随工作进展更新checklist。**
常用功能：
1. 读取：action=read（获取当前事务的checklist内容）
2. 重写：action=write checklist_content progress=<0-100>（重写checklist内容，同时更新事务总体进度，支持\\n换行符）
3. 编辑：action=edit oldString newString [replaceAll] progress=<0-100>（编辑/替换checklist中部分内容，同时更新进度）
4. 查看其他事务：action=read id=<其他事务ID>（只读，不能修改其他事务的checklist）
重要：write和edit操作时必须同时提供progress参数（0-100整数），表示事务总体完成进度。report工具要求progress=100时才允许汇报！
注意：
checklist 在任务初期必须严格按照需求原样填写，禁止改变描述（因为此时你还没有全面了解情况，改变描述很可能歪曲需求！）
checklist 应该随着任务进展更新补充为详细完整的任务功能列表、进展（⌛表示未完成， ✅表示已完成）、后继工作相关详细信息、相关代码位置和问题跟踪表，更新时禁止简化checklist避免丢失已有工作记录！
"""

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["read", "write", "edit"],
                    "description": "操作类型：read=读取检查清单，write=更新检查清单，edit=编辑检查清单（功能类似文件edit工具）",
                },
                "checklist_content": {
                    "type": "string",
                    "description": "检查清单内容（write操作时必填，支持\\n换行符）",
                },
                "progress": {
                    "type": "string",
                    "description": "事务总体进度，0-100的整数（write/edit操作时可选，不提供则保留当前进度）。根据checklist完成情况估算：全部完成=100，完成一半=50，刚开始=10等。report工具要求progress=100才允许汇报！",
                },
                "id": {
                    "type": "integer",
                    "description": "事务ID（可选，不提供则操作当前事务；其他事务只读）",
                },
                "oldString": {
                    "type": "string",
                    "description": "要替换的文本（edit操作时必填）",
                },
                "newString": {
                    "type": "string",
                    "description": "替换后的文本（edit操作时必填，必须与oldString不同）",
                },
                "replaceAll": {
                    "type": "boolean",
                    "description": "是否替换所有匹配的oldString（edit操作时可选，默认false）",
                },
            },
            "required": ["action"],
        }

    async def execute(
        self,
        action: str,
        checklist_content: str | None = None,
        progress: str | None = None,
        id: int | None = None,
        oldString: str | None = None,
        newString: str | None = None,
        replaceAll: bool = False,
        **kwargs,
    ) -> DualOutput:
        """
        执行ChecklistTool操作

        Args:
            action: 操作类型（read/write/edit）
            checklist_content: 检查清单内容（write时必填）
            progress: 事务总体进度 0-100（write/edit时可选）
            id: 事务ID（可选，不提供则操作当前事务；其他事务只读）
            oldString: 要替换的文本（edit时必填）
            newString: 替换后的文本（edit时必填）
            replaceAll: 是否替换所有匹配（edit时可选，默认False）
        Returns:
            DualOutput: 执行结果
        """
        try:
            my_task_id = self.get_context("task_id")
            task_manager = self.get_context("task_manager")

            if not my_task_id or not task_manager:
                return DualOutput(
                    summary="❌ 无法获取事务上下文",
                    display="❌ 无法获取事务上下文",
                )

            # 确定目标事务ID
            target_id = id if id is not None else my_task_id
            is_other_task = target_id != my_task_id

            task = task_manager.get_task(target_id)
            if not task:
                return DualOutput(
                    summary=f"❌ 事务#{target_id}不存在",
                    display=f"❌ 事务#{target_id}不存在",
                )

            if action == "read":
                # 读取checklist
                value = task.get("checklist", "") or ""
                progress_val = task.get("progress", 0) or 0
                if value:
                    summary = f"✅ 事务#{target_id}的检查清单（进度: {progress_val}%）：\n{value}"
                    display = _format_markdown_for_display(
                        value, title=f"📋 事务#{target_id} 检查清单（进度: {progress_val}%）"
                    )
                else:
                    summary = f"事务#{target_id}的检查清单：(未设置)"
                    display = summary

                if checklist_content:
                    summary += "\n checklist_content参数不为空，你是否正确设置了action参数？"
                    display += "\n ⚠️ checklist_content参数不为空，你是否正确设置了action参数？"
                return DualOutput(summary=summary, display=display)

            elif action == "write":
                # 非自身事务禁止写入
                if is_other_task:
                    return DualOutput(
                        summary=f"❌ 无权修改事务#{target_id}的检查清单（只能修改自身事务）",
                        display=f"❌ 无权修改事务#{target_id}的检查清单（只能修改自身事务）",
                    )
                if not checklist_content:
                    return DualOutput(
                        summary="❌ write操作需要提供checklist_content参数",
                        display="❌ write操作需要提供checklist_content参数",
                    )
                progress = progress.replace("%", "")
                if progress.isnumeric():
                    progress = int(progress)
                else:
                    return DualOutput(
                        summary="❌ write操作需要提供progress参数（0-100整数，表示事务总体进度）",
                        display="❌ write操作需要提供progress参数（0-100整数，表示事务总体进度）",
                    )
                # 校验progress范围
                progress = max(0, min(100, int(progress)))
                # 处理转义换行符
                checklist_content = checklist_content.replace("\\n", "\n")
                success = task_manager.db.update_task(
                    target_id, checklist=checklist_content, progress=progress
                )
                if success:
                    summary = f"✅ 事务#{target_id}的检查清单已更新（进度: {progress}%）"
                    display = _format_markdown_for_display(
                        checklist_content,
                        title=f"📋 事务#{target_id} 检查清单已更新（进度: {progress}%）",
                    )
                else:
                    summary = f"❌ 事务#{target_id}的检查清单更新失败"
                    display = summary
                return DualOutput(summary=summary, display=display)

            elif action == "edit":
                # 非自身事务禁止编辑
                if is_other_task:
                    return DualOutput(
                        summary=f"❌ 无权修改事务#{target_id}的检查清单（只能修改自身事务）",
                        display=f"❌ 无权修改事务#{target_id}的检查清单（只能修改自身事务）",
                    )
                if not oldString:
                    return DualOutput(
                        summary="❌ edit操作需要提供oldString参数",
                        display="❌ edit操作需要提供oldString参数",
                    )
                if not newString:
                    return DualOutput(
                        summary="❌ edit操作需要提供newString参数",
                        display="❌ edit操作需要提供newString参数",
                    )
                if oldString == newString:
                    return DualOutput(
                        summary="无需更改：oldString和newString相同",
                        display="⚠️ 无需更改：oldString和newString相同",
                    )
                # 读取当前checklist
                current_content = task.get("checklist", "") or ""
                if not current_content:
                    return DualOutput(
                        summary=f"❌ 事务#{target_id}的检查清单为空，无法编辑",
                        display=f"❌ 事务#{target_id}的检查清单为空，无法编辑",
                    )
                # 复用file.py的replace_content多策略替换（直接处理字符串，无需临时文件）
                try:
                    new_content = replace_content(current_content, oldString, newString, replaceAll)
                except ToolError as e:
                    return DualOutput(
                        summary=f"❌ 编辑checklist失败: {e}",
                        display=f"❌ 编辑checklist失败: {e}",
                    )
                # 写回checklist
                progress = progress.replace("%", "")
                if progress.isnumeric():
                    progress = int(progress)
                else:
                    progress = None
                final_progress = progress if progress is not None else task.get("progress", 0)
                final_progress = max(0, min(100, int(final_progress)))
                success = task_manager.db.update_task(
                    target_id, checklist=new_content, progress=final_progress
                )
                if success:
                    summary = f"✅ 事务#{target_id}的检查清单已编辑更新（进度: {final_progress}%）"
                    # 生成 diff 显示（参考 file.py EditTool 的 display 逻辑）
                    display_parts = []
                    diff = trim_diff(
                        generate_colored_diff(
                            normalize_line_endings(oldString),
                            normalize_line_endings(newString),
                            f"checklist#{target_id}",
                        )
                    )
                    if diff:
                        display_parts.append(f"\n{diff}\n")
                    old_lines = oldString.split("\n")
                    new_lines = newString.split("\n")
                    additions = max(0, len(new_lines) - len(old_lines))
                    deletions = max(0, len(old_lines) - len(new_lines))
                    display_parts.append(f"\n✅ {summary} (+{additions}/-{deletions} 行)\n")
                    display = "".join(display_parts)
                else:
                    summary = f"❌ 事务#{target_id}的检查清单编辑更新失败"
                    display = summary
                return DualOutput(summary=summary, display=display)

            else:
                return DualOutput(
                    summary=f"❌ 不支持的操作: {action}",
                    display=f"❌ 不支持的操作: {action}",
                )

        except Exception as e:
            output = f"❌ checklist工具执行失败: {e}"
            return DualOutput(summary=output, display=output)


class ReportTool(Tool):
    """
    工作汇报工具

    将工作汇报写入事务自身的report字段，或查看/修改其他事务的report字段。
    非自身事务只能查看，不能修改。
    """

    def __init__(self):
        super().__init__()

    @property
    def name(self) -> str:
        return "report"

    @property
    def description(self) -> str:
        return """工作汇报工具：复杂事务需求要全部达成且验证后，用本工具作汇报总结，以此结束事务处理。禁止在工作完成前的事务处理中途使用本工具！ 汇报内容要包含需求概述，解决方案，所做变更(包括文件路径)，测试验收结果，注意事项等信息。
支持通过id参数查看其他事务的report字段（只读）。
常用功能：
1. 汇报：summary=<汇报内容>（写入当前事务的report字段）
2. 查看：action=read（读取当前事务的report字段）
3. 查看其他事务：action=read id=<其他事务ID>（只读，不能修改其他事务的report）
"""

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "summary": {
                    "type": "string",
                    "description": "汇报总结具体内容，需简明扼要。（写入当前事务的report字段）",
                },
                "progress": {
                    "type": "string",
                    "description": "事务总体进度，0-100的整数（write操作时必填）。根据checklist完成情况估算：全部完成=100，完成一半=50，刚开始=10等。report工具要求progress=100才允许汇报！",
                },
                "action": {
                    "type": "string",
                    "enum": ["read"],
                    "description": "操作类型：read=读取report字段。不提供action时默认为写入模式（需提供summary参数）",
                },
                "id": {
                    "type": "integer",
                    "description": "事务ID（可选，不提供则操作当前事务；查看其他事务时为只读）",
                },
            },
            "required": [],
        }

    async def execute(
        self,
        **kwargs,
    ) -> DualOutput:
        """
        执行report操作

        - 有 action=read：读取report字段（支持id参数查看其他事务）
        - 有 summary（无action或action非read）：写入report字段（仅限自身事务）
        """
        try:
            action: str | None = kwargs.get("action")
            report_content: str = kwargs.get("summary", "")
            progress: str = kwargs.get("progress") or "0"
            progress = progress.replace("%", "")
            if progress.isnumeric():
                progress = int(progress)
            else:
                progress = 0
            target_id_param: int | None = kwargs.get("id")
            my_task_id = self.get_context("task_id")
            task_manager = self.get_context("task_manager")

            if not my_task_id or not task_manager:
                return DualOutput(
                    summary="❌ 无法获取事务上下文",
                    display="❌ 无法获取事务上下文",
                )

            # 确定目标事务ID
            target_id = target_id_param if target_id_param is not None else my_task_id
            is_other_task = target_id != my_task_id

            task = task_manager.get_task(target_id)
            if not task:
                return DualOutput(
                    summary=f"❌ 事务#{target_id}不存在",
                    display=f"❌ 事务#{target_id}不存在",
                )

            # 读取模式
            if action == "read":
                value = task.get("report", "") or ""
                progress_val = task.get("progress", 0) or 0
                if value:
                    summary = f"✅ 事务#{target_id}的report（进度: {progress_val}%）：\n{value}"
                    display = _format_markdown_for_display(
                        value, title=f"📝 事务#{target_id} 工作汇报（进度: {progress_val}%）"
                    )
                else:
                    summary = f"事务#{target_id}的report：(未设置)"
                    display = summary
                if report_content:
                    summary += "\n summary参数不为空，你是否正确设置了action参数？"
                    display += "\n ⚠️ summary参数不为空，你是否正确设置了action参数？"
                return DualOutput(summary=summary, display=display)

            # 写入模式（仅限自身事务）
            if is_other_task:
                return DualOutput(
                    summary=f"❌ 无权修改事务#{target_id}的report（只能修改自身事务）",
                    display=f"❌ 无权修改事务#{target_id}的report（只能修改自身事务）",
                )

            if not report_content:
                return DualOutput(
                    summary="❌ write操作需要提供summary参数",
                    display="❌ write操作需要提供summary参数",
                )
            if not progress:
                progress = task.get("progress") or 0
            if progress < 100:
                output = f"❌ 事务#{target_id}进度为 {progress}，尚未完成！report工具仅允许在事务全部完成（progress=100）后使用。请继续完成未完成的工作，更新 checklist 以推进进度。"
                return DualOutput(summary=output, display=output)

            # 将汇报写入事务的report字段
            success = task_manager.db.update_task(target_id, report=report_content, progress=100)
            if not success:
                output = "❌  report工具调用失败：写入report字段失败"
                return DualOutput(summary=output, display=output)

            summary = "✅  report工具调用成功。"
            display = _format_markdown_for_display(
                report_content, title=f"📝 事务#{target_id} 工作汇报已提交"
            )
            return DualOutput(summary=summary, display=display)

        except Exception as e:
            output = f"❌  report工具调用失败： {e}"
            return DualOutput(summary=output, display=output)


class InfoTool(Tool):
    """
    事务上下文注入工具（InfoTool）

    向指定id的事务注入上下文信息，用于父事务控制子事务或向事务发送提醒。
    info字段必须是普通字符串格式。
    """

    def __init__(self):
        """初始化工具"""
        super().__init__()

    @property
    def name(self) -> str:
        return "info_tool"

    @property
    def description(self) -> str:
        return """事务信息提示工具（InfoTool）。

向指定id的事务注入上下文信息，用于父事务向运行中的子事务大模型发送紧急信息指示。
info字段必须是普通字符串格式，支持\\n换行符。
常用功能：
1. 向事务注入信息：info <task_id> <info_string>
     示例：info 1 "请暂停当前事务"
     示例：info 1 info1\\ninfo2\\ninfo3（多行信息）
2. 查看事务的info信息：info <task_id>
     示例：info 1（获取事务#1的注入信息）
3. 删除事务的info信息：info <task_id> --delete
     示例：info 1 --delete（清空事务#1的注入信息）
    """

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "task_id": {
                    "type": "integer",
                    "description": "事务ID",
                },
                "info_string": {
                    "type": "string",
                    "description": '信息字符串（如果不提供则显示事务已有info，空字符串""则删除已有info，有内容则添加新info）',
                },
            },
            "required": ["task_id"],
        }

    async def execute(
        self,
        task_id: int,
        info_string: str | None = None,
        **kwargs,
    ) -> DualOutput:
        """
        执行InfoTool操作

        Args:
            task_id: 事务ID
            info_string: 信息字符串（可选）
                - 不提供：显示 info
                - 空字符串 ""：删除 info
                - 有内容：添加 info
        Returns:
            DualOutput: 执行结果
        """
        try:
            # 调用bin/task命令 - Windows 上需要通过 python 解释器执行无扩展名的脚本
            task_script = str(PROJECT_ROOT / "bin" / "task")

            def _build_task_cmd(*extra_args: str) -> list[str]:
                """构建跨平台的 task 命令列表
                --local 强制本地模式：工具在本地事务上下文中运行，
                临时文件只在本地创建，远程模式无法读取本地临时文件。
                注意：--workdir 仅在 subtask 子命令时注入（其他子命令不支持），
                --nest 永远不注入（bin/task 不支持此参数）。
                """
                if os.name == "nt":
                    base = [sys.executable, task_script, "--local"]
                else:
                    base = [task_script, "--local"]
                cmd = base + list(extra_args)
                # 从工具上下文获取 workdir
                # 仅在 subtask 子命令时注入（其他子命令不支持 --workdir）
                subcmd = extra_args[0] if extra_args else ""
                if subcmd == "subtask":
                    args_str = " ".join(extra_args)
                    ctx_workdir = self.get_context("workdir")
                    if ctx_workdir and "--workdir" not in args_str and "-w" not in args_str:
                        cmd.extend(["--workdir", str(ctx_workdir)])
                return cmd

            # 根据info_string参数构建命令
            temp_file = None
            if info_string is None:
                # 显示 info
                cmd = _build_task_cmd("info", str(task_id))
            elif info_string == "":
                # 删除 info
                cmd = _build_task_cmd("info", str(task_id), "")
            else:
                # 添加 info - 将 info_string 写入临时文件，通过 --file 参数传递
                temp_file = _write_temp_file(info_string)
                cmd = _build_task_cmd("info", str(task_id), "--file", temp_file)

            try:
                result = subprocess.run(
                    cmd,
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    check=False,
                    timeout=300,  # info操作允许5分钟
                )
            except Exception as e:
                raise ToolError(f"info操作执行失败: {e}")
            finally:
                # 清理临时文件
                if temp_file and os.path.exists(temp_file):
                    try:
                        os.remove(temp_file)
                    except OSError:
                        logger.warning(f"删除临时文件失败: {temp_file}")

            # 防御性检查：Windows下GBK解码失败可能导致stdout/stderr为None
            stdout = result.stdout or ""
            stderr = result.stderr or ""

            if result.returncode != 0:
                raise ToolError(f"info操作失败: {stderr or stdout}")

            return DualOutput(
                summary=f"事务#{task_id}的info操作完成",
                display=stdout,
            )

        except Exception as e:
            return DualOutput(
                summary=f"事务#{task_id}的info操作失败",
                display=f"事务#{task_id}的info操作失败",
            )


class TaskTool(Tool):
    """
    事务管理工具（统一调用bin/task命令行接口）
    单一工具类，通过参数区分不同功能。
    """

    def __init__(self):
        """初始化工具"""
        super().__init__()

    @property
    def name(self) -> str:
        return "task"

    @property
    def description(self) -> str:
        return """事务管理工具。

统一的事务管理接口，支持事务查询、控制、子事务管理等所有功能。
常用功能：
1. 显示事务树信息：tree <id>
     示例：tree 1（获取事务#1的完整树状结构信息）
2. 获取消息树：msg <id>
     示例：msg 1（获取事务#1关联的消息链）
3. 控制事务：pause|cancel|resume|restart|start <id>
     示例：pause 6（暂停事务#6）
     示例：start 7（启动待定事务#7）
     示例：restart 8（清除上下文重启事务#8，加--force参数无视当前状态强制重启）
4. 显示或设置事务状态：status <id> [pending|running|completed|failed]
     示例：status 1（显示事务#1的状态）
5. 创建子事务：subtask <parent_id> 事务需求描述 --prompt 提示词名称 --workdir 子事务工作目录 --creator 子事务创建者 [--small-model]
     示例：subtask 1 子事务需求描述 --prompt 提示词名称 --small-model （创建子事务，指定需求描述，指定提示词名称，使用小模型）
6. 删除子事务：delete <id>
     示例：delete 5（递归删除事务#5及其所有子事务，树根事务不可删除）
7. 列出所有未归档根事务：trees（无需参数）
8. 显示事务输出文件：output <id> [--begin 行号] [--end 行号] [--head 行数] [--tail 行数] [--line-count] [--verbose（带时间戳的详细输出）]
      含回滚超链接的行会在行尾追加 [R<序号>] task rollback <事务ID> R<序号>（含回滚ID+完整回滚命令行，橙色）
      示例：output 2 --tail 10 --verbose（显示事务#2输出文件的后10行，带时间戳）
      示例：output 7 --line-count（只显示事务#7输出文件的总行数，不显示文件内容）
9. 向运行中事务注入实时提示信息：info <id> 提示信息  发实时提示信息给事务，若事务不在处理则启动事务
      示例：info 3 编辑文件后要用git diff确认是否修改正确！ （提醒事务3核对文件编辑结果）
10. 修改事务需求描述：content <id> <detail>  (只允许在监控子事务时使用)
      示例：content 5 修改后的需求描述内容（修改事务#5的需求描述）
      示例：content 5 -d（清空事务#5的需求描述）
11. 定时器：timer <id> [seconds] [--delete]
     示例：timer 12 (察看事务#12的定时器设置，下一次启动时间)
     示例：timer 12 1800 (设置事务#12每半小时自动清除上下文重新启动)
12. 按回滚ID回滚事务到指定快照：rollback <id> <回滚ID> [--list] [-y]
      回滚ID即 output 显示中行尾 [R<序号>] 标记里的数字（支持 12 或 R12 写法）
     示例：rollback 5 --list（列出事务#5全部可用回滚快照ID）
     示例：rollback 5 R12（回滚事务#5到快照R12，需确认）
     示例：rollback 5 R12 -y（跳过确认直接回滚）

注意：
1. 参数中禁止管道操作
2. 支持换行转义符\\n，允许在参数中使用换行符
3. 避免使用英文单引号或双引号，建议使用中文引号或不使用引号
4. 使用--help参数获取详细帮助
    """

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "args": {
                    "type": "string",
                    "description": "子命令和参数，例如：'tree 1' 或 'pause 1'",
                }
            },
            "required": ["args"],
        }

    async def execute(
        self,
        args: str,
        **kwargs,
    ) -> DualOutput:
        """
        执行事务管理命令（调用bin/task）

        Args:
            args: 命令行参数字符串，包含action和其他参数，例如："tree 1"
            --help: 显示帮助信息
        Returns:
            DualOutput: 执行结果
        """
        # 构建命令 - Windows 上需要通过 python 解释器执行无扩展名的脚本
        task_script = str(PROJECT_ROOT / "bin" / "task")

        def _build_task_cmd(*extra_args: str) -> list[str]:
            """构建跨平台的 task 命令列表
            --local 强制本地模式：工具在本地事务上下文中运行，
            临时文件只在本地创建，远程模式无法读取本地临时文件。
            注意：--workdir 仅在 subtask 子命令时注入（其他子命令不支持），
            --nest 永远不注入（bin/task 不支持此参数）。
            """
            if os.name == "nt":
                base = [sys.executable, task_script, "--local"]
            else:
                base = [task_script, "--local"]
            cmd = base + list(extra_args)
            # 从工具上下文获取 workdir
            # 仅在 subtask 子命令时注入（其他子命令不支持 --workdir）
            subcmd = extra_args[0] if extra_args else ""
            if subcmd == "subtask":
                args_str = " ".join(extra_args)
                ctx_workdir = self.get_context("workdir")
                if ctx_workdir and "--workdir" not in args_str and "-w" not in args_str:
                    cmd.extend(["--workdir", str(ctx_workdir)])
            return cmd

        # 解析子命令和参数
        # 将内容写入临时文件，通过 --file 参数传递，避免转义符问题
        parts = args.split(maxsplit=1)
        subcmd = parts[0] if parts else ""
        subcmd_args = parts[1].strip() if len(parts) > 1 else ""

        # status子命令：设置状态为completed时，检查事务progress是否为100
        if subcmd == "status" and subcmd_args:
            status_parts = subcmd_args.split()
            # status <id> completed 的格式：至少2个参数，最后一个为completed
            if len(status_parts) >= 2 and status_parts[-1].lower() == "completed":
                target_id_str = status_parts[0]
                if target_id_str.isdigit():
                    target_id = int(target_id_str)
                    task_manager = self.get_context("task_manager")
                    if task_manager:
                        task = task_manager.get_task(target_id)
                        if task:
                            progress = task.get("progress", 0) or 0
                            if progress < 100:
                                output = f"❌ 事务#{target_id}进度为 {progress}%，尚未完成！禁止将状态设为completed。请继续完成未完成的工作，更新checklist以推进进度至100%。"
                                return DualOutput(summary=output, display=output)

        # 需要内容参数的子命令列表
        content_subcmds = {
            "info",
            "content",
            "title",
            "prompt",
            "description",
            "subtask",
            "result",
            "metadata",
            "workdir",
            "branch",
            "context",
            "checklist",
            "report",
        }

        temp_file = None
        if subcmd in content_subcmds and subcmd_args:
            cmd = _build_task_cmd(subcmd)
            # 如果子命令有ID参数（如 info 3 或 content 5），分离ID和内容
            arg_parts = subcmd_args.split(maxsplit=1)
            if arg_parts and arg_parts[0].isdigit():
                cmd.append(arg_parts[0])
                content = arg_parts[1] if len(arg_parts) > 1 else ""
            else:
                content = subcmd_args

            if content:
                temp_file = _write_temp_file(content)
                cmd.extend(["--file", temp_file])
        else:
            # 不需要内容参数的子命令，直接传递
            task_args = shlex.split(args)
            cmd = _build_task_cmd(*task_args)
        try:
            logger.info(f"执行命令: {cmd}")
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                check=False,
                timeout=300,  # 防止垃圾进程卡死项目
            )
        except Exception as e:
            output = f"task工具调用失败 {e}"
            return DualOutput(
                summary=output,
                display=output,
            )
        finally:
            # 清理临时文件
            if temp_file and os.path.exists(temp_file):
                try:
                    os.remove(temp_file)
                except OSError:
                    logger.warning(f"删除临时文件失败: {temp_file}")

        # 防御性检查：Windows下GBK解码失败可能导致stdout/stderr为None
        stdout = result.stdout or ""
        stderr = result.stderr or ""
        output = " ".join(cmd) + "\n" + stderr + "\n" + stdout
        if result.returncode != 0:
            output = f"❌  task工具调用失败： " + output
        else:
            output = f"✅  task工具调用成功： " + output
        return DualOutput(
            summary=output,
            display=output,
        )
