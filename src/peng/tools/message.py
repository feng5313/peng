"""
消息管理工具（统一调用bin/msg命令行接口）

提供消息发布、回复、读取、控制等功能，内部统一调用bin/msg命令行工具。
所有功能通过命令行接口实现，确保一致性和可维护性。

这是一个单一工具类，通过action参数区分不同功能。

大模型必用功能（直接提供说明）：
- reply: 回复消息
- tree: 获取消息树
- control: 控制消息事务（pause/cancel/resume/restart）

其他功能：使用--help参数探索详细信息
"""

import logging
import os
import shlex
import subprocess
import sys
from typing import Any

from peng.common.paths import get_temp_directory, chown_to_project_owner, ensure_dir_with_owner
from peng.config import get_agent_name, get_master_name, get_project_root
from peng.tools.base import DualOutput, Tool, ToolError

logger = logging.getLogger(__name__)
AGENT_NAME = get_agent_name()
MASTER_NAME = get_master_name()
PROJECT_ROOT = get_project_root()


def _write_temp_file(content: str) -> str:
    """将内容写入临时文件，返回文件路径"""
    temp_dir = get_temp_directory()
    ensure_dir_with_owner(temp_dir)
    from datetime import datetime

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    path = temp_dir / f"peng-msg-{timestamp}.txt"
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)
    chown_to_project_owner(path)
    return str(path)


class MessageTool(Tool):
    """
    消息管理工具（统一调用bin/msg命令行接口）
    单一工具类，通过参数区分不同功能。
    """

    def __init__(self):
        """初始化工具"""
        super().__init__()

    @property
    def name(self) -> str:
        return "message"

    @property
    def description(self) -> str:
        return """消息管理工具统一的消息管理接口，支持消息回复、读取、控制等所有功能。
常用功能：
1. 回复消息：reply <id> <回复内容> [--from FROM] [--to TO] [--timeout TIMEOUT]
     示例：reply 12 已完成。
     示例：reply 12 已完成 --timeout 300（设置300秒超时）
2. 发布新消息给鹏：send <消息内容> [--nest NEST] [--workdir WORKDIR]
     示例：send 截屏存到本目录 --nest win11 --workdir D:\\work\\temp
3. 显示所有未归档消息：trees
4. 显示完整上下文：context <id>
     示例：context 12（显示消息#12的完整上下文信息，包括关联的事务、消息链等）
5. 搜索消息：search <关键词>
     示例：search "错误" （搜索包含"错误"的消息
6. 显示消息分支树（从该消息开始）：tree <ID>
7. 显示消息完整树（从根消息开始）：full-tree <ID>

注意：
1. 参数中禁止管道操作
2. 支持换行转义符\\n，允许在参数中使用换行符
3. 避免使用英文单引号或双引号，建议使用中文引号或不使用引号
4. 复杂任务应该用report工具汇报总结工作，不要使用message工具除非被明确要求回复。
5. 回复消息后，禁止用message或report工具连续汇报类似内容！
6. 使用--help参数获取详细帮助
    """

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "args": {
                    "type": "string",
                    "description": "bin/msg命令行参数字符串，包含action和其他参数，例如：'reply 12 已完成。' 使用--help参数获取详细帮助",
                },
            },
            "required": ["args"],
        }

    async def execute(
        self,
        args: str,
        **kwargs,
    ) -> DualOutput:
        """
        执行消息管理命令（调用bin/msg）

        Args:
            args: 命令行参数字符串，包含action和其他参数，例如："reply 12 已完成。"
        Returns:
            DualOutput: 执行结果
        """
        try:
            parts = args.split(maxsplit=1)
            subcmd = parts[0] if parts else ""
            task_id = self.get_context("task_id")
            if task_id == 2:  # 事务#2为事务总管，禁止调用message工具
                output = f"❌  事务总管禁止调用message工具"
                return DualOutput(
                    summary=output,
                    display=output,
                )

            # 构建命令 - Windows 上需要通过 python 解释器执行无扩展名的脚本
            msg_script = str(PROJECT_ROOT / "bin" / "msg")
            if os.name == "nt":
                msg_script = sys.executable

            def _build_msg_cmd(*extra_args: str) -> list[str]:
                """构建跨平台的 msg 命令列表
                --local 强制本地模式：工具在本地事务上下文中运行，
                临时文件只在本地创建，远程模式无法读取本地临时文件。
                注意：--nest 和 --workdir 仅在 send/reply 子命令时注入，
                因为其他子命令不支持这些参数。
                """
                if os.name == "nt":
                    base = [msg_script, str(PROJECT_ROOT / "bin" / "msg"), "--local"]
                else:
                    base = [msg_script, "--local"]
                cmd = base + list(extra_args)
                # 从工具上下文获取 nest 和 workdir
                # 仅在 send/reply 子命令时注入（其他子命令不支持这些参数）
                subcmd = extra_args[0] if extra_args else ""
                if subcmd in ("send", "reply"):
                    args_str = " ".join(extra_args)
                    ctx_nest = self.get_context("nest")
                    ctx_workdir = self.get_context("workdir")
                    if ctx_nest and "--nest" not in args_str and "-n" not in args_str:
                        cmd.extend(["--nest", str(ctx_nest)])
                    if ctx_workdir and "--workdir" not in args_str and "-w" not in args_str:
                        cmd.extend(["--workdir", str(ctx_workdir)])
                return cmd

            # 解析子命令和参数
            # 将内容写入临时文件，通过 --file 参数传递，避免转义符问题
            subcmd_args = parts[1].strip() if len(parts) > 1 else ""

            # 需要内容参数的子命令列表
            content_subcmds = {"send", "reply", "content", "title", "search"}

            temp_file = None
            if subcmd in content_subcmds and subcmd_args:
                cmd = _build_msg_cmd(subcmd)
                # 如果子命令有ID参数（如 reply 12 或 content 5），分离ID和内容
                if subcmd in ("reply", "content", "title"):
                    arg_parts = subcmd_args.split(maxsplit=1)
                    if arg_parts and arg_parts[0].isdigit():
                        cmd.append(arg_parts[0])
                        content = arg_parts[1] if len(arg_parts) > 1 else ""
                    else:
                        content = subcmd_args
                else:
                    content = subcmd_args

                if content:
                    temp_file = _write_temp_file(content)
                    cmd.extend(["--file", temp_file])
            else:
                # 不需要内容参数的子命令，直接传递
                cmd = _build_msg_cmd(*shlex.split(args))

            try:
                logger.info(f"执行命令: {cmd}")
                result = subprocess.run(
                    cmd,
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    check=False,
                    timeout=20,  # 防止垃圾进程卡死项目
                )
            finally:
                # 清理临时文件
                if temp_file and os.path.exists(temp_file):
                    try:
                        os.remove(temp_file)
                    except OSError:
                        logger.warning(f"删除临时文件失败: {temp_file}")

            output = " ".join(cmd) + "\n" + (result.stderr or "") + "\n" + (result.stdout or "")
            if result.returncode != 0:
                output = f"❌  message工具调用失败： " + output
            else:
                output = f"✅  message工具调用成功： " + output
            return DualOutput(
                summary=output,
                display=output,
            )

        except Exception as e:
            output = f"❌  message工具调用失败： {e}"
            return DualOutput(
                summary=output,
                display=output,
            )