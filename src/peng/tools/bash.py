"""
Bash/Cmd工具

执行shell命令（跨平台支持）。

工具名称根据事务关联的 nest 平台动态变化：
- Windows 平台：工具名为 "cmd"，引导大模型使用 cmd.exe 语法（dir, type, cd 等）
- Unix/Linux/macOS 平台：工具名为 "bash"，使用 bash 语法（ls, cat, pwd 等）
- 注册时固定以 "bash" 为主名称，"cmd" 作为别名注册到 ToolRegistry

限制：
- 输出最大 2000 行或 50KB
- 超过限制时保存到文件并提供访问提示
- 禁止修改项目自身核心代码（bin, src, tests, docs, config, pyproject.toml）
- 修改工作目录外文件时自动备份

跨平台支持：
- Windows: 使用 cmd.exe 语法（dir, type, cd 等）
- Unix/Linux/macOS: 使用 bash 语法（ls, cat, pwd 等）
"""

import asyncio
import logging
import os
import re
import shlex
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from peng.common.colors import get_bash_colors
from peng.common.paths import *
from peng.nest.protocol import NestError
from peng.task.info import is_urgent_content
from peng.tools.base import DualOutput, Tool, ToolError
from peng.common.truncation import truncate_output

logger = logging.getLogger(__name__)

# 从统一颜色配置获取颜色常量
_COLORS = get_bash_colors()
RED = _COLORS.get("ERROR", "")
GREEN = _COLORS.get("SUCCESS", "")
RESET = _COLORS.get("RESET", "")

# 平台检测
IS_WINDOWS = sys.platform == "win32"
PLATFORM_NAME = "Windows (cmd.exe)" if IS_WINDOWS else "Unix/Linux/macOS (bash)"

# 项目保护说明：
# AI 不能通过 tool_call 对自身项目进行任何文件操作（读、写、编辑）。
# 如需修改自身项目，请使用 git worktree 在临时目录克隆分身后操作。


class BashTool(Tool):
    """执行shell命令工具

    工具名称根据 nest 平台动态变化：
    - Windows: "cmd"
    - Unix/Linux/macOS: "bash"
    注册时以 "bash" 为主名称，"cmd" 作为别名。
    """

    # 工具别名：在注册表中自动注册为 "bash" 的别名
    # 当 Windows nest 的事务调用 "cmd" 时，注册表通过别名找到此工具
    ALIASES = ("cmd",)

    # 禁止的危险命令模式
    BLOCKED_PATTERNS = [
        "dd if=/dev/zero",
        ":(){:|:&};:",  # fork bomb
        "chmod -R 777 /",
        "> /dev/sda",
        "mkfs",
    ]

    if not IS_WINDOWS:
        # 预编译正则表达式（用于文件操作检测）
        _RM_PATTERN = re.compile(r"\brm\s+(-[rf]+\s+)?([^\s;&|]+)")
        _DEL_PATTERN = re.compile(r"\bdel\s+([^\s;&|]+)")
        _MKDIR_PATTERN = re.compile(r"\bmkdir\s+(-[p]+\s+)?([^\s;&|]+)")
        # 写入重定向模式（检测 > 或 >> 操作）
        _WRITE_REDIRECT_PATTERN = re.compile(r"[>|>]\s*([^\s;&|]+)")
        # 复制/移动模式
        _CP_PATTERN = re.compile(r"\bcp\s+(-[rf]+\s+)?([^\s;&|]+)\s+([^\s;&|]+)")
        _MV_PATTERN = re.compile(r"\bmv\s+(-[rf]+\s+)?([^\s;&|]+)\s+([^\s;&|]+)")
        # 文件修改模式
        _SED_PATTERN = re.compile(r"\bsed\s+.*(-i\s+[^\s;]+|-[iI]\s*[^\s;]*)\s+([^\s;&|]+)")
        _AWK_PATTERN = re.compile(r"\bawk\s+.*[>|>]\s*([^\s;&|]+)")
        _TEE_PATTERN = re.compile(r"\btee\s+(-a\s+)?([^\s;&|]+)")
        _ECHO_PATTERN = re.compile(r"\becho\s+.*[>|>]\s*([^\s;&|]+)")

    else:
        # Windows版本
        _RM_PATTERN = re.compile(r"\b(rmdir|rd|del)\s+((?:/[a-zA-Z]+\s+)*)?([^\s;&|]+)")
        _DEL_PATTERN = re.compile(r"\b(del|erase)\s+([^\s;&|]+)")
        _MKDIR_PATTERN = re.compile(r"\b(mkdir|md)\s+([^\s;&|]+)")
        # 注意：原正则 [>|>] 有误，实际上是匹配 > 或 >，这里修正为匹配 > 或 >>
        _WRITE_REDIRECT_PATTERN = re.compile(r"[>]{1,2}\s*([^\s;&|]+)")
        # 5. Linux cp -> Windows copy 或 xcopy 或 robocopy
        _CP_PATTERN = re.compile(
            r"\b(copy|xcopy|robocopy)\s+((?:/[a-zA-Z]+\s+)*)?([^\s;&|]+)\s+([^\s;&|]+)"
        )
        _MV_PATTERN = re.compile(r"\bmove\s+((?:/[a-zA-Z]+\s+)*)?([^\s;&|]+)\s+([^\s;&|]+)")

        # 7. 文件修改模式
        _SED_PATTERN = re.compile(
            r"\b(powershell.*(?:Set-Content|Add-Content|sc|ac).*-Path\s+)([^\s;&|]+)"
        )
        _AWK_PATTERN = re.compile(r"\bawk\s+.*[>|>]\s*([^\s;&|]+)")
        _TEE_PATTERN = re.compile(r"\b(Tee-Object|tee)\s+(?:-FilePath\s+)?([^\s;&|]+)")
        _ECHO_PATTERN = re.compile(r"\becho\s+.*[>]{1,2}\s*([^\s;&|]+)")

    def _get_nest_platform(self) -> tuple[bool, str]:
        """动态获取当前事务关联 nest 的实际平台

        通过工具上下文中的 nest 名称，查询该 nest 上报的 platform 信息。
        使用公共函数 get_nest_platform()（两级查询）：
        1. NestBridgeManager（主服务进程场景）
        2. webui HTTP API（事务子进程场景，NestBridgeManager 无连接）

        Returns:
            (is_windows, platform_name) 元组:
            - is_windows: 是否为 Windows 平台
            - platform_name: 平台显示名称（如 "Windows (cmd.exe)" 或 "Unix/Linux/macOS (bash)"）
        """
        # 默认回退到本机平台
        fallback = (IS_WINDOWS, PLATFORM_NAME)

        nest_name = self.get_context("nest")
        if not nest_name:
            return fallback

        try:
            from peng.tools._nest_helper import get_nest_platform

            platform_str = get_nest_platform(nest_name).lower()
            if "windows" in platform_str:
                return (True, "Windows (cmd.exe)")
            elif "linux" in platform_str or "darwin" in platform_str:
                return (False, "Unix/Linux/macOS (bash)")
        except Exception as e:
            logger.debug(f"获取 nest '{nest_name}' 平台信息失败: {e}")

        return fallback

    @property
    def name(self) -> str:
        """工具名称：根据 nest 平台动态返回 'cmd' 或 'bash'"""
        is_win, _ = self._get_nest_platform()
        return "cmd" if is_win else "bash"

    @property
    def primary_name(self) -> str:
        """注册主名称（固定不变，用于注册表 key）"""
        return "bash"

    @property
    def description(self) -> str:
        is_win, platform_name = self._get_nest_platform()
        win_cmds = "dir, type, cd, del, copy, move, mkdir, cls"
        unix_cmds = "ls, cat, pwd, rm, cp, mv, mkdir, clear"
        return f"""执行shell命令。
当前操作系统为: {platform_name}
请使用正确的命令语法： {win_cmds if is_win else unix_cmds} 等。
必须预估命令运行所需时间设置合适的timeout，命令运行时间超过timeout则强制超时结束。
调用的命令必须有持续stdout/stderr输出，如果长时间（3分钟）无输出会被认定为卡死，导致心跳超时强制结束！
如果无法保证命令进程有持续stdout/stderr输出，也应该定时检测命令进程CPU占用，确认运行正常，以免心跳超时强制结束！
⚠️ 运行命令必须说明目的（purpose参数），要简明扼要（4-200个字符），否则将被拒绝执行！
内置命令 stop：停止daemon工具启动的后台进程。用法： stop <daemon_id>（可多个id）或 stop all 清理全部。
  长时间运行的命令建议改用daemon工具（非阻塞后台运行）。
"""

    @property
    def parameters(self) -> dict[str, Any]:
        _, platform_name = self._get_nest_platform()
        return {
            "type": "object",
            "properties": {
                "command": {
                    "type": "string",
                    "description": f"要执行的shell命令（当前平台: {platform_name}）。stop <daemon_id> 为停止daemon的内置命令",
                },
                "purpose": {
                    "type": "string",
                    "description": "运行该命令的目的说明（必填，4-200个字符）。大模型调用bash工具时必须说明执行该命令的目的，否则将被拒绝。",
                },
                "timeout": {
                    "type": "integer",
                    "description": "超时时间（毫秒，默认120000）",
                    "default": 120000,
                },
            },
            "required": ["command", "purpose"],
        }

    # sleep 命令允许的最大时长（秒）
    MAX_SLEEP_SECONDS = 160

    def _contains_sleep(self, command: str) -> bool:
        """检测命令是否包含sleep命令（需要启用中断监控）"""
        return bool(re.search(r"(?<!\w)\bsleep\b(?!\w)", command.lower()))

    async def _kill_proc_safely(self, proc):
        """安全终止子进程：kill + 关闭transport + wait

        关键：必须关闭 transport 才能让 pipe 的 readline() 返回，
        否则 await proc.wait() 会永远阻塞。
        """
        if proc is None or proc.returncode is not None:
            return
        try:
            proc.kill()
        except Exception:
            pass
        # 关闭 transport（这会关闭底层的 pipe fd，让 readline() 立即返回 EOF）
        try:
            if proc._transport is not None:
                proc._transport.close()
        except Exception:
            pass
        # 现在 wait() 不会阻塞了
        try:
            await asyncio.wait_for(proc.wait(), timeout=3)
        except Exception:
            pass

    async def _interrupt_monitor(
        self, task_manager, task_id, message_id, check_all: bool = False
    ):
        """中断监控协程：定期检查事务状态/info/根消息回复/Ctrl+C

        Args:
            task_manager: 事务管理器
            task_id: 当前事务ID
            message_id: 当前根消息ID
            check_all: 是否检查全部中断条件（含sleep的命令为True，保持原有行为：
                任何未读info/任何新回复都打断）；其他命令为False，仅检查
                事务状态变更 + 感叹号开头的紧急未读info/新回复（事务#1860），
                普通消息/info不打断命令（与思考流打断策略一致）

        Returns:
            中断原因字符串，None表示未中断
        """
        _last_known_reply_count = -1
        check_interval = 2  # 每2秒检查一次

        while True:
            await asyncio.sleep(check_interval)

            # 条件1: 事务状态不再是 running/accepting（所有启用监控的命令都检查：
            # cancel/pause等系统级控制操作必须立即响应，覆盖所有场景）
            task = None
            if task_manager and task_id:
                try:
                    task = task_manager.db.get_task(task_id)
                    if task:
                        status = task.get("status")
                        if status not in ("running", "accepting"):
                            status_cn = task_manager.STATUS_CHINESE.get(status, status)
                            return f"事务已被{status_cn}（状态: {status}），命令已终止。请立刻处理事务状态变更。"
                except Exception as e:
                    logger.debug(f"[interrupt_monitor] 检查事务状态失败: {e}")

                # 条件2: info字段检查
                # check_all（含sleep命令，原有行为）：任何未读info都打断
                # 否则（事务#1860）：仅感叹号开头的紧急未读info打断
                try:
                    if task is None:
                        info_field = "[]"
                    else:
                        info_field = task.get("info", "[]") or "[]"
                    from peng.task.info import parse_info_to_list_with_status, get_unread_info_items

                    info_list = parse_info_to_list_with_status(info_field)
                    unread_items = get_unread_info_items(info_list)
                    if check_all:
                        if unread_items:
                            # 收集未读info内容
                            info_contents = [item["content"] for item in unread_items]
                            info_text = "\n".join(f"  - {c}" for c in info_contents)
                            return f"收到info注入信息，命令已终止。请立刻处理：\n{info_text}"
                    else:
                        for item in unread_items:
                            if is_urgent_content(item.get("content")):
                                content = (item.get("content") or "").replace("\\n", "\n")
                                return (
                                    "收到感叹号开头的紧急info，命令已终止。请立刻处理：\n"
                                    f"  - {content.strip()[:200]}"
                                )
                except Exception as e:
                    logger.debug(f"[interrupt_monitor] 检查info字段失败: {e}")

            # 条件3: 根消息有新回复
            if message_id:
                if check_all:
                    # 原有行为：回复计数增加即打断
                    try:
                        from peng.message.database import MessageDatabase

                        msg_db = MessageDatabase()
                        reply_count = msg_db.get_reply_count(message_id)
                        if _last_known_reply_count >= 0 and reply_count > _last_known_reply_count:
                            return f"根消息#{message_id}收到新回复（当前{reply_count}条），命令已终止。请立刻用message工具查看并处理。"
                        _last_known_reply_count = reply_count
                    except Exception as e:
                        logger.debug(f"[interrupt_monitor] 检查消息回复失败: {e}")
                else:
                    # 事务#1860：仅感叹号开头的紧急processing回复打断
                    # （processing=未处理待注入，与 tool_engine._get_processing_messages
                    #  消费语义一致，处理后标记done不会重复触发）
                    try:
                        from peng.config import get_agent_name
                        from peng.message.database import MessageDatabase

                        msg_db = MessageDatabase()
                        replies = msg_db.get_processing_replies(
                            message_id, receiver=get_agent_name()
                        )
                        for reply in replies:
                            if is_urgent_content(reply.get("content")):
                                content = (reply.get("content") or "").replace("\\n", "\n")
                                return (
                                    f"根消息#{message_id}收到感叹号开头的紧急消息"
                                    f"#{reply.get('id')} [{reply.get('sender')}]，"
                                    f"命令已终止。请立刻处理：{content.strip()[:200]}"
                                )
                    except Exception as e:
                        logger.debug(f"[interrupt_monitor] 检查紧急消息回复失败: {e}")

    async def _handle_stop_builtin(self, command: str):
        """stop内置命令：停止daemon工具启动的后台进程（bash/daemon工具通用）

        拦截规则：命令首词为 stop，且其余部分为空、纯daemon id列表（或all）。
        避免误拦截真实shell命令：如 "stop; rm xx"、"stop && xx" 等不拦截。

        Args:
            command: 原始命令

        Returns:
            拦截返回DualOutput结果，未拦截返回None（继续正常执行）
        """
        m = re.match(r"^\s*stop(?:\s+(.+?))?\s*$", command)
        if not m:
            return None
        rest = (m.group(1) or "").strip()

        from peng.common.daemon_registry import stop_daemon_ids, stop_all_daemons

        if not rest:
            usage = "用法: stop <daemon_id> [daemon_id2 ...] 或 stop all 清理全部daemon"
            return DualOutput(summary=f"\n{usage}", display=usage)
        if rest == "all":
            output = await stop_all_daemons()
            return DualOutput(summary=f"\n{output}", display=output)
        ids = rest.split()
        # 全部是纯id形式才拦截（防止误拦截真实shell命令）
        if not all(re.fullmatch(r"[A-Za-z0-9_.\-]+", i) for i in ids):
            return None
        output = await stop_daemon_ids(ids)
        return DualOutput(summary=f"\n{output}", display=output)

    async def execute(self, **kwargs: Any) -> str:
        """执行shell命令（通过 nest 远程执行）"""
        command: str = kwargs.get("command", "")
        timeout: int = kwargs.get("timeout", 120000)
        purpose: str = kwargs.get("purpose", "").strip()

        # 目的说明检查：大模型必须说明运行命令的目的
        if not purpose or len(purpose) < 4 or len(purpose) > 200:
            raise ToolError(
                "命令被拒绝：必须通过 purpose 参数简明扼要地说明运行该命令的目的（4-200字符）。"
                "请说明你为什么要执行这个命令，例如 purpose='查看当前目录下的文件列表'。"
            )

        # 安全检查
        if not self._is_command_safe(command):
            raise ToolError("命令被拒绝：包含危险模式")

        # 🔧 stop内置命令：停止daemon后台进程
        stop_result = await self._handle_stop_builtin(command)
        if stop_result is not None:
            return stop_result

        # 检查 sleep 时长
        sleep_error = self._check_sleep_duration(command)
        if sleep_error:
            raise ToolError(sleep_error)

        # 获取 nest 客户端
        client = self._get_nest_client()
        nest_name = self.get_context("nest") or "local"  # 当前事务关联的 nest 名称
        is_local_nest = nest_name == "local"  # 只有 local nest 才能 fallback 到本地执行

        if client:
            if client.connected:
                try:
                    return await self._execute_via_nest(client, command, timeout, purpose)
                except NestError as e:
                    # 🔧 只有 local nest 连接不可用时才能 fallback 到本地执行
                    # 远程 nest（如 epyc/win11）不可用时不能在本地执行
                    _conn_err_patterns = ("关闭", "正在关闭", "未连接", "WebSocket已关闭")
                    err_msg = str(e)
                    if is_local_nest and any(p in err_msg for p in _conn_err_patterns):
                        logger.info(f"local nest 不可用({e})，fallback 到本地执行")
                        return await self._execute_local(command, timeout, purpose)
                    raise  # 远程 nest 或非连接类错误，正常抛出
            else:
                # client 存在但未连接：仍然尝试通过 send_request（它有子进程转发 fallback）
                try:
                    return await self._execute_via_nest(client, command, timeout, purpose)
                except NestError as e:
                    if is_local_nest:
                        logger.info(f"local nest 不可用({e})，fallback 到本地执行")
                        return await self._execute_local(command, timeout, purpose)
                    raise
        else:
            # 没有 client，只有 local nest 能走本地执行
            if is_local_nest:
                return await self._execute_local(command, timeout, purpose)
            raise NestError(f"Nest '{nest_name}' 未连接，无法执行远程命令")
    
    def _get_nest_client(self):
        """获取 nest 客户端（从工具上下文获取 nest 名称和 task_id）"""
        try:
            from peng.nest.client import get_nest_client
            client = get_nest_client()
            # 通过 get_context('nest') 获取事务关联的 nest
            nest_name = self.get_context("nest")
            if nest_name:
                client.set_nest_name(nest_name)
            # 设置 task_id 用于 nest 不可用时持续重试保活
            client.set_task_id(self.get_context("task_id"))
            return client
        except Exception:
            return None
    
    async def _execute_via_nest(self, client, command: str, timeout: int, purpose: str) -> str:
        """通过 nest 远程执行命令（带中断监控）"""
        from peng.common.colors import get_bash_colors

        _COLORS = get_bash_colors()
        RED = _COLORS.get("ERROR", "")
        GREEN = _COLORS.get("SUCCESS", "")
        RESET = _COLORS.get("RESET", "")

        # 动态获取工作目录
        workdir = self.get_context("workdir")

        # 获取上下文用于中断监控
        task_manager = self.get_context("task_manager")
        task_id = self.get_context("task_id")
        message_id = self.get_context("message_id")

        # 判断是否需要中断监控（事务#1860：所有命令都启用监控——
        # 感叹号开头的紧急新回复消息/未读info需打断正在运行的bash进程；
        # 含sleep的命令传check_all=True保持原有行为：任何未读info/新回复都打断）
        enable_monitor = task_manager and task_id

        try:
            # 创建远程执行任务
            bash_task = asyncio.ensure_future(
                client.bash(command=command, cwd=workdir, timeout=timeout)
            )

            if enable_monitor:
                # 带中断监控的执行模式：远程命令 + 中断监控，任一先完成即返回
                monitor_task = asyncio.ensure_future(
                    self._interrupt_monitor(
                        task_manager, task_id, message_id,
                        check_all=self._contains_sleep(command),
                    )
                )

                try:
                    done, pending = await asyncio.wait(
                        [bash_task, monitor_task],
                        timeout=timeout / 1000,
                        return_when=asyncio.FIRST_COMPLETED,
                    )

                    # 取消未完成的任务
                    for t in pending:
                        t.cancel()

                    # 检查是否超时
                    if not done:
                        for t in pending:
                            try:
                                await t
                            except (asyncio.CancelledError, Exception):
                                pass
                        raise ToolError(f"命令执行超时（{timeout}ms）") from None

                    # 检查中断监控是否触发
                    interrupt_reason = None
                    if monitor_task in done:
                        try:
                            interrupt_reason = monitor_task.result()
                        except (asyncio.CancelledError, Exception):
                            pass

                    # 中断监控触发：发送kill命令到远程nest
                    if interrupt_reason:
                        # 尝试远程终止进程
                        try:
                            await client.bash(
                                command="kill -INT -- -$PPID 2>/dev/null; kill $PPID 2>/dev/null",
                                cwd=workdir,
                                timeout=3000,
                            )
                        except Exception:
                            pass
                        for t in pending:
                            try:
                                await t
                            except (asyncio.CancelledError, Exception):
                                pass
                        return DualOutput(
                            summary=f"\n⚠️ {interrupt_reason}", display=interrupt_reason
                        )

                    # bash_task 正常完成
                    for t in pending:
                        try:
                            await t
                        except (asyncio.CancelledError, Exception):
                            pass
                    result = bash_task.result()

                except (asyncio.CancelledError, ToolError):
                    raise
                except Exception as e:
                    output = f"{RED}命令执行失败: {command}\n{e}{RESET}\n"
                    return DualOutput(summary=output, display=output)
            else:
                # 无监控模式：直接等待结果
                result = await bash_task

            # 构建结果
            result_parts: list[str] = []
            stdout_text = result.get("stdout", "")
            stderr_text = result.get("stderr", "")
            exit_code = result.get("exit_code", -1)

            if stdout_text:
                result_parts.append(stdout_text)
            if stderr_text:
                result_parts.append(f"[stderr]\n{stderr_text}")
            if exit_code != 0:
                result_parts.append(f"[exit code: {exit_code}]")

            # 🔧 Task#72：通过远程nest注册后台进程PID
            if task_id and exit_code == 0 and self._is_daemon_command(command):
                daemon_info = await self._register_remote_daemon_pids(
                    client, command, task_id
                )
                if daemon_info:
                    result_parts.append(daemon_info)

            full_output = "\n".join(result_parts) or "[无输出]"

            # 截断输出
            truncation_result = truncate_output(full_output)
            output = "\n" + truncation_result.output
            display = f"\n命令 {command} 执行成功。"

            from peng.config.loader import load_config
            config = load_config()
            if truncation_result.truncated and truncation_result.full_path:
                max_lines = config.truncation.max_lines
                output = f"\n命令执行成功，输出过长已截断 {truncation_result.removed_lines} 行，"
                output += f"完整输出已保存到文件: {truncation_result.full_path}\n"
                output += f"可使用read工具读取更多输出。前 {max_lines} 行输出如下：{truncation_result.output}\n"
            return DualOutput(summary=output, display=display)

        except NestError as e:
            output = f"{RED}命令执行失败: {command}\n{e}{RESET}\n"
            return DualOutput(summary=output, display=output)
        except Exception as e:
            if isinstance(e, ToolError):
                raise
            output = f"{RED}命令执行失败: {command}\n{e}{RESET}\n"
            return DualOutput(summary=output, display=output)
    
    async def _execute_local(self, command: str, timeout: int, purpose: str) -> str:
        """本地执行命令（无 nest 时的兼容模式）"""

        # 检查是否修改受保护的项目路径
        self._check_protected_paths(command)

        # 检测文件操作命令并生成输出信息
        file_op_info = self._detect_file_operations(command)

        # 备份工作目录外的文件
        backup_info = self._backup_files_if_needed(command)

        proc = None
        try:
            # 动态获取工作目录（从config.webui.workdir读取）
            workdir = self.get_context("workdir")
            workdir = Path(workdir) if workdir else get_working_directory()

            # 非Windows系统使用bash，支持here-string等bash特性
            if not IS_WINDOWS:
                # 使用/bin/bash -c来执行命令，支持bash特性如<<<
                shell_cmd = f"/bin/bash -c {shlex.quote(command)}"
            else:
                shell_cmd = command

            proc = await asyncio.create_subprocess_shell(
                shell_cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=workdir,
                env=os.environ.copy(),
            )

            # 收集输出的缓冲区
            stdout_lines: list[str] = []
            stderr_lines: list[str] = []

            # 获取上下文
            task_manager = self.get_context("task_manager")
            task_id = self.get_context("task_id")
            message_id = self.get_context("message_id")
            _last_heartbeat_time = 0.0  # 上次心跳时间戳

            # 读取stdout和stderr（实时写入output文件）
            async def read_stream(stream, is_stdout):
                """读取流并实时写入output文件

                Args:
                    stream: 流对象（stdout 或 stderr）
                    is_stdout: 是否为 stdout（True=stdout, False=stderr）
                """
                nonlocal _last_heartbeat_time
                while True:
                    try:
                        line = await stream.readline()
                        if not line:
                            break

                        # 解码行
                        if IS_WINDOWS:
                            line_text = self._decode_windows_output(line)
                        else:
                            line_text = line.decode("utf-8", errors="replace")

                        # 添加到缓冲区（传给大模型）
                        if is_stdout:
                            stdout_lines.append(line_text)
                        else:
                            stderr_lines.append(line_text)

                        # 进程有输出说明在活跃工作，更新心跳（每5秒最多一次，避免频繁写入DB）
                        if task_manager and task_id:
                            now = time.time()
                            if now - _last_heartbeat_time >= 5:
                                _last_heartbeat_time = now
                                try:
                                    task_manager.update_heartbeat(task_id)
                                except Exception:
                                    pass  # 心跳更新失败不应影响命令执行
                    except Exception as e:
                        logger.info(f"读取流失败: cmd={shell_cmd} {e}")
                        stderr_lines.append(f"[读取流异常] {e}\n")
                        break

            # 构建 stream 读取任务
            stream_task = asyncio.ensure_future(
                asyncio.gather(
                    read_stream(proc.stdout, True),  # stdout
                    read_stream(proc.stderr, False),  # stderr
                )
            )

            # 判断是否需要中断监控（事务#1860：所有命令都启用监控，同_execute_via_nest——
            # 感叹号开头的紧急新回复消息/未读info需打断正在运行的bash进程；
            # 含sleep的命令传check_all=True保持原有行为：任何未读info/新回复都打断）
            enable_monitor = task_manager and task_id

            if enable_monitor:
                # 带监控的执行模式：命令执行 + 中断监控，任一先完成即返回
                monitor_task = asyncio.ensure_future(
                    self._interrupt_monitor(
                        task_manager, task_id, message_id,
                        check_all=self._contains_sleep(command),
                    )
                )
                # SIGINT handler: 收到Ctrl+C立即取消所有任务
                interrupted_by_signal = False
                loop = asyncio.get_running_loop()

                def _sigint_handler():
                    nonlocal interrupted_by_signal
                    interrupted_by_signal = True
                    if not stream_task.done():
                        stream_task.cancel()
                    if not monitor_task.done():
                        monitor_task.cancel()

                try:
                    loop.add_signal_handler(signal.SIGINT, _sigint_handler)
                except (NotImplementedError, OSError):
                    pass  # Windows 或不支持

                try:
                    # FIRST_COMPLETED: 任一完成即返回
                    done, pending = await asyncio.wait(
                        [stream_task, monitor_task],
                        timeout=timeout / 1000,
                        return_when=asyncio.FIRST_COMPLETED,
                    )

                    # 取消未完成的任务
                    for t in pending:
                        t.cancel()

                    # 检查是否超时
                    if not done:
                        await self._kill_proc_safely(proc)
                        # 等待 stream_task 被 cancel 完成
                        for t in pending:
                            try:
                                await t
                            except (asyncio.CancelledError, Exception):
                                pass
                        raise ToolError(f"命令执行超时（{timeout}ms）") from None

                    # 检查哪个先完成
                    interrupt_reason = None
                    if monitor_task in done:
                        try:
                            interrupt_reason = monitor_task.result()
                        except (asyncio.CancelledError, Exception):
                            pass

                    # Ctrl+C 信号中断
                    if interrupted_by_signal:
                        await self._kill_proc_safely(proc)
                        # 等待 pending 任务完成 cancel
                        for t in pending:
                            try:
                                await t
                            except (asyncio.CancelledError, Exception):
                                pass
                        interrupt_msg = "收到Ctrl+C信号，命令已终止。请立刻处理。"
                        return DualOutput(summary=f"\n⚠️ {interrupt_msg}", display=interrupt_msg)

                    # 中断监控触发
                    if interrupt_reason:
                        await self._kill_proc_safely(proc)
                        # 等待 stream_task 被 cancel 完成
                        for t in pending:
                            try:
                                await t
                            except (asyncio.CancelledError, Exception):
                                pass
                        return DualOutput(
                            summary=f"\n⚠️ {interrupt_reason}", display=interrupt_reason
                        )

                    # stream_task 正常完成（命令执行完毕）
                    # cancel monitor_task 并等待
                    for t in pending:
                        try:
                            await t
                        except (asyncio.CancelledError, Exception):
                            pass
                    # await stream_task 传播可能的异常
                    try:
                        stream_task.result()
                    except Exception as e:
                        raise ToolError(f"命令执行异常: {e}") from e

                finally:
                    try:
                        loop.remove_signal_handler(signal.SIGINT)
                    except (NotImplementedError, OSError):
                        pass
            else:
                # 无监控模式：原有逻辑不变
                try:
                    await asyncio.wait_for(
                        stream_task,
                        timeout=timeout / 1000,
                    )
                except TimeoutError:
                    await self._kill_proc_safely(proc)
                    raise ToolError(f"命令执行超时（{timeout}ms）") from None

            # 构建结果
            result_parts: list[str] = []

            # 添加备份信息（如果有）
            if backup_info:
                result_parts.append(backup_info)

            # 添加文件操作信息（如果有）
            if file_op_info:
                result_parts.append(file_op_info)

            # 合并stdout和stderr
            stdout_text = "".join(stdout_lines)
            stderr_text = "".join(stderr_lines)

            if stdout_text:
                result_parts.append(stdout_text)
            if stderr_text:
                result_parts.append(f"[stderr]\n{stderr_text}")

            # 添加退出码（如果非0）
            if proc.returncode != 0:
                result_parts.append(f"[exit code: {proc.returncode}]")

            # 检测nohup后台进程并注册PID
            daemon_info = await self._register_daemon_pids(command, proc, task_id)
            if daemon_info:
                result_parts.append(daemon_info)

            full_output = "\n".join(result_parts) or "[无输出]"

            # 截断输出（如果过长）
            truncation_result = truncate_output(full_output)
            output = "\n" + truncation_result.output
            display = f"\n命令 {command} 执行成功。"
            from peng.config.loader import load_config

            config = load_config()
            if truncation_result.truncated and truncation_result.full_path:
                max_lines = config.truncation.max_lines
                output = f"\n命令执行成功，输出过长已截断 {truncation_result.removed_lines} 行，"
                output += f"完整输出已保存到文件: {truncation_result.full_path}\n"
                output += f"可使用read工具读取更多输出。前 {max_lines} 行输出如下：{truncation_result.output}\n"
            return DualOutput(summary=output, display=display)
        except Exception as e:
            output = f"{RED}命令执行失败: {command}\n{e}{RESET}\n"
            return DualOutput(summary=output, display=output)
        finally:
            # 确保所有情况下子进程都被正确清理
            await self._kill_proc_safely(proc)

    def _is_daemon_command(self, command: str) -> bool:
        """检测命令是否会产生不跟随父进程结束的后台进程

        检测模式：
        - nohup ... & (nohup + 后台运行)
        - setsid ... & (setsid + 后台运行)
        - disown (显式脱离控制)
        - ( ... ) & (子shell后台运行)
        - daemon模式启动的服务

        Args:
            command: shell命令

        Returns:
            是否为守护进程命令
        """
        cmd_lower = command.strip().lower()

        # nohup 命令（最常见）
        if re.search(r'\bnohup\b', cmd_lower):
            return True

        # setsid 命令
        if re.search(r'\bsetsid\b', cmd_lower):
            return True

        # disown 命令（脱离shell控制）
        if re.search(r'\bdisown\b', cmd_lower):
            return True

        # 后台运行的子shell: (cmd) &
        if re.search(r'\(.*\)\s*&', cmd_lower):
            return True

        return False

    def _extract_daemon_process_pattern(self, command: str) -> str | None:
        """从 nohup命令中提取要查找的进程模式

        策略：优先提取 Python 脚本名/可执行文件名，这样 pgrep 匹配更精确。
        例如：nohup uv run python3 train_pipeline.py → 提取 train_pipeline.py
              nohup python3 train.py → 提取 train.py
              nohup ./run.sh → 提取 run.sh

        Args:
            command: shell 命令

        Returns:
            用于 pgrep 匹配的进程名模式，无法提取时返回 None
        """
        # 移除 nohup 前缀
        cmd = re.sub(r'\bnohup\b\s*', '', command.strip())

        # 移除 setsid 前缀
        cmd = re.sub(r'\bsetsid\b\s*', '', cmd)

        # 移除 cd ... && 前缀（处理 cd /path && nohup ... 的情况）
        cmd = re.sub(r'^cd\s+[^\s&]+\s*&&\s*', '', cmd)

        # 移除环境变量赋值
        cmd = re.sub(r'[A-Za-z_][A-Za-z0-9_]*=\S+\s+', '', cmd)

        # 移除重定向和输出文件
        cmd = re.sub(r'>\s*>>?\s*\S+', '', cmd)
        cmd = re.sub(r'2>\s*>?\s*/dev/null', '', cmd)
        cmd = re.sub(r'1>\s*>>?\s*/dev/null', '', cmd)
        cmd = re.sub(r'&\s*$', '', cmd)

        # 移除引号
        cmd = cmd.strip().strip("'\"").strip()

        # 分割命令为多个部分
        parts = cmd.split()
        if not parts:
            return None

        # 策略 1: 查找.py 或.sh 等脚本文件作为匹配模式（最精确）
        for part in parts:
            if part.endswith(('.py', '.sh', '.pl', '.rb', '.js', '.ts')):
                # 返回脚本文件名（不带路径）
                return os.path.basename(part)

        # 策略 2: 使用完整命令行作为匹配模式
        # （比仅取第一个词精确得多，如 "sleep 105" 不会误匹配所有sleep进程）
        return " ".join(parts) if parts else None

    async def _register_daemon_pids(
        self, command: str, proc, task_id
    ) -> str | None:
        """检测并注册nohup等后台命令产生的daemon进程

        在命令执行成功后，如果检测到是nohup等守护进程命令，
        查找产生的后台进程PID并注册到daemon.json（daemon_registry，
        每个PID一个daemon条目，可用 stop <id> 停止）。

        Args:
            command: 原始shell命令
            proc: 已完成的子进程对象
            task_id: 当前事务ID（可能为None）

        Returns:
            注册信息字符串（有注册时），None（无注册时）
        """
        if not self._is_daemon_command(command):
            return None

        if not task_id:
            return None

        # 命令失败时不注册
        if proc.returncode != 0:
            return None

        try:
            import peng.common.daemon_registry as dr

            # 使用pgrep查找该命令产生的子进程
            pattern = self._extract_daemon_process_pattern(command)
            if not pattern:
                logger.debug(f"无法从命令提取进程模式: {command}")
                return None

            # 查找后台进程PID（排除pgrep自身）
            # 注意：需要短暂等待，因为nohup后台进程可能还没启动
            try:
                await asyncio.sleep(1)
                result = subprocess.run(
                    ["pgrep", "-f", pattern],
                    capture_output=True,
                    text=True,
                    timeout=5,
                )
                if result.returncode == 0 and result.stdout.strip():
                    pids = [int(p) for p in result.stdout.strip().split('\n') if p.strip()]
                    if pids:
                        nest_name = dr.normalize_nest(self.get_context("nest"))
                        ids = []
                        for pid in pids:
                            entry = dr.register_daemon(
                                task_id=task_id,
                                command=command,
                                pid=pid,
                                nest=nest_name,
                                platform=sys.platform if nest_name is None else None,
                            )
                            ids.append(entry["id"])
                        id_list = ", ".join(ids)
                        return (
                            f"{GREEN}📋 已注册后台进程daemon: [{id_list}] "
                            f"(PID {pids}, task#{task_id})，"
                            f"可用 stop {ids[0]} 停止，task kill {task_id} 清理{RESET}"
                        )
            except (subprocess.TimeoutExpired, ValueError, OSError) as e:
                logger.debug(f"pgrep查找后台进程失败: {e}")

        except Exception as e:
            logger.debug(f"注册后台进程daemon失败: {e}")

        return None

    async def _register_remote_daemon_pids(
        self, client, command: str, task_id
    ) -> str | None:
        """通过远程nest检测并注册后台进程daemon

        在远程nest上执行pgrep查找后台进程，将远程PID和nest来源一起注册到daemon.json。

        Args:
            client: nest客户端
            command: 原始shell命令
            task_id: 当前事务ID

        Returns:
            注册信息字符串（有注册时），None（无注册时）
        """
        from peng.common.colors import get_bash_colors
        _COLORS = get_bash_colors()
        GREEN = _COLORS.get("SUCCESS", "")
        RESET = _COLORS.get("RESET", "")

        try:
            import peng.common.daemon_registry as dr

            pattern = self._extract_daemon_process_pattern(command)
            if not pattern:
                logger.debug(f"无法从命令提取进程模式: {command}")
                return None

            # 在远程nest上执行pgrep查找后台进程
            # 注意：需要短暂等待，因为nohup后台进程可能还没启动
            try:
                await asyncio.sleep(1)
                result = await client.bash(
                    command=f"pgrep -f {shlex.quote(pattern)}",
                    timeout=10,
                )
                stdout = result.get("stdout", "").strip()
                exit_code = result.get("exit_code", -1)
                if exit_code == 0 and stdout:
                    pids = [int(p) for p in stdout.split('\n') if p.strip()]
                    if pids:
                        nest_name = self.get_context("nest") or getattr(client, '_nest_name', None)
                        ids = []
                        for pid in pids:
                            entry = dr.register_daemon(
                                task_id=task_id,
                                command=command,
                                pid=pid,
                                nest=nest_name,
                            )
                            ids.append(entry["id"])
                        id_list = ", ".join(ids)
                        return (
                            f"{GREEN}📋 已注册远程后台进程daemon: [{id_list}] "
                            f"(PID {pids}, task#{task_id}, nest={nest_name})，"
                            f"可用 stop {ids[0]} 停止，task kill {task_id} 清理{RESET}"
                        )
            except (ValueError, OSError) as e:
                logger.debug(f"远程pgrep查找后台进程失败: {e}")

        except Exception as e:
            logger.debug(f"注册远程后台进程daemon失败: {e}")

        return None

    def _decode_windows_output(self, data: bytes) -> str:
        """
        解码 Windows 命令输出（尝试多种编码）

        Windows cmd.exe 默认使用 GBK/CP936 编码，
        但某些命令可能输出 UTF-8。

        Args:
            data: 原始字节数据

        Returns:
            解码后的字符串
        """
        if not data:
            return ""

        # 尝试的编码列表（按优先级）
        encodings = [
            "utf-8",  # 现代 Windows 工具可能使用 UTF-8
            "gbk",  # 简体中文 Windows 默认
            "cp936",  # GBK 的代码页名称
            "gb2312",  # 老式中文编码
            "cp1252",  # 西欧语言
            "latin-1",  # 兜底编码
        ]

        for encoding in encodings:
            try:
                return data.decode(encoding)
            except (UnicodeDecodeError, LookupError):
                continue

        # 所有编码都失败，使用 replace 模式
        return data.decode("utf-8", errors="replace")

    def _check_sleep_duration(self, command: str) -> str | None:
        """检查 sleep 命令时长是否超过限制

        Args:
            command: shell 命令

        Returns:
            None 表示通过检查，否则返回错误提示信息
        """
        command_lower = command.lower()
        sleep_match = re.search(r"\bsleep\s+(\d+(?:\.\d+)?)", command_lower)
        if sleep_match:
            try:
                duration = float(sleep_match.group(1))
                if duration > self.MAX_SLEEP_SECONDS:
                    return (
                        f"命令被拒绝：sleep 时长 {duration} 秒超过限制 "
                        f"（最大允许 {self.MAX_SLEEP_SECONDS} 秒），请缩短 sleep 时间"
                    )
            except ValueError:
                pass
        return None

    def _is_command_safe(self, command: str) -> bool:
        """检查命令是否安全"""
        command_lower = command.lower()
        for pattern in self.BLOCKED_PATTERNS:
            if pattern.lower() in command_lower or command_lower == "rm -rf /":
                return False
        return True

    def _check_protected_paths(self, command: str) -> None:
        """
        检查命令是否操作项目路径

        规则：禁止对自身项目进行任何文件操作（读、写、创建、删除）。
        如需修改自身项目，请使用 git worktree 在临时目录克隆分身后操作。

        例外：配置文件中 allowed_tool_call_dirs 指定的目录允许读写。

        Args:
            command: shell 命令

        Raises:
            ToolError: 如果命令尝试操作项目路径
        """
        import os

        from peng.config.loader import load_config

        config = load_config()
        paths = config.paths
        allowed_dirs = paths.allowed_tool_call_dirs_paths
        error_msg = "🚫  安全条例：不得bash操作自身项目文件。如有必要，请在临时目录克隆分身。\n"

        # 检测文件操作模式
        patterns = [
            (r">>\s*([^\s;&|]+)", "追加重定向"),
            (r">\s*([^\s;&|]+)", "覆盖重定向"),
            (r"\btee\s+([^\s;&|]+)", "tee命令"),
            (r"\bcp\s+.*\s+([^\s;&|]+)$", "cp命令"),
            (r"\bmv\s+.*\s+([^\s;&|]+)$", "mv命令"),
            (r"\btouch\s+([^\s;&|]+)", "touch命令"),
            (r"\brm\s+(-[rf]+\s+)?([^\s;&|]+)", "rm命令"),
            (r"\bmkdir\s+(-[p]+\s+)?([^\s;&|]+)", "mkdir命令"),
            # rsync: rsync [options] src dest (最后两个参数是路径)
            (r"\brsync\s+(?:.*\s+)?(\S+)\s+(\S+)$", "rsync命令"),
            # scp: scp src dest (最后两个参数是路径)
            (r"\bscp\s+(?:.*\s+)?(\S+)\s+(\S+)$", "scp命令"),
            # tar 解压: tar -xf archive -C dest
            (r"\btar\s+.*?-C\s+([^\s;&|]+)", "tar解压"),
            # unzip: unzip archive -d dest
            (r"\bunzip\s+.*?-d\s+([^\s;&|]+)", "unzip解压"),
            # install: install src dest
            (r"\binstall\s+.*?\s+([^\s;&|]+)\s+([^\s;&|]+)$", "install命令"),
        ]

        for pattern, _ in patterns:
            matches = re.findall(pattern, command)
            for match in matches:
                # 处理匹配结果
                targets = []
                if isinstance(match, str):
                    targets = [match]
                else:
                    # 元组匹配：取所有路径（如 rsync src dest）
                    targets = list(match)

                for target in targets:
                    if not target or target.startswith("-"):
                        continue
                    # 跳过远程路径（scp/rsync 的 user@host:path 格式）
                    if "@" in target or ":" in target:
                        continue
                    # 只检查看起来像文件路径的：以 /, ~ 开头，或包含 /
                    if not (target.startswith("/") or target.startswith("~") or "/" in target):
                        continue
                    # 检查是否是允许读写的目录
                    target_path = Path(target).expanduser().resolve()
                    is_allowed = False
                    for allowed_dir in allowed_dirs:
                        try:
                            target_path.relative_to(allowed_dir)
                            is_allowed = True
                            break
                        except ValueError:
                            pass
                    if is_allowed:
                        continue
                    if is_protected_project_path(target):
                        raise ToolError(error_msg)

    def _detect_file_operations(self, command: str) -> str:
        """
        检测命令中的文件操作并生成格式化输出

        Args:
            command: shell 命令

        Returns:
            格式化的文件操作信息（如果检测到）
        """
        info_parts = []

        # 检测删除文件命令: rm /path/to/file 或 rm -rf /path (Unix)
        rm_matches = self._RM_PATTERN.findall(command)
        for _, path in rm_matches:
            if path and not path.startswith("-"):
                info_parts.append(f"{RED}🗑️ 删除文件:{RESET} {path}")

        # 检测删除文件命令: del file (Windows)
        del_matches = self._DEL_PATTERN.findall(command)
        info_parts.extend(
            f"{RED}🗑️ 删除文件:{RESET} {path}"
            for path in del_matches
            if path and not path.startswith("-")
        )

        # 检测创建目录命令: mkdir -p /path/to/dir
        mkdir_matches = self._MKDIR_PATTERN.findall(command)
        for _, path in mkdir_matches:
            if path and not path.startswith("-"):
                info_parts.append(f"{GREEN}📁 创建目录:{RESET} {path}")

        return "\n".join(info_parts + ["执行了文件操作，注意测试验收！"]) if info_parts else ""

    def _detect_files_to_backup(self, command: str) -> list[Path]:
        """
        检测命令中将被修改的文件

        检测以下命令：
        - sed -i 文件（就地修改）
        - awk 输出到文件
        - echo 输出到文件
        - tee 输出到文件
        - 重定向 >, >> 输出到文件

        Args:
            command: shell 命令

        Returns:
            将被修改的文件路径列表
        """
        files_to_backup: list[Path] = []

        # 特殊设备文件，不应该备份
        special_devices = {
            "/dev/null",
            "/dev/zero",
            "/dev/full",
            "/dev/random",
            "/dev/urandom",
            "/proc/self/fd/0",  # stdin
            "/proc/self/fd/1",  # stdout
            "/proc/self/fd/2",  # stderr
        }

        def should_skip(path_str: str) -> bool:
            """检查是否应该跳过该路径"""
            if not path_str:
                return True

            # 检查原始路径是否为特殊设备
            if path_str in special_devices:
                return True

            # 转换为绝对路径后再检查
            try:
                abs_path_str = str(Path(path_str).expanduser().resolve())
                return abs_path_str in special_devices
            except Exception:
                return True

        # 检测 sed -i 命令
        sed_matches = self._SED_PATTERN.findall(command)
        for match in sed_matches:
            # match 是 (选项, 文件) 或 (选项, 文件)
            file_path = match[-1] if isinstance(match, tuple) and len(match) > 1 else match
            if file_path and not file_path.startswith("-") and not should_skip(file_path):
                path = Path(file_path).expanduser().resolve()
                if path.exists():
                    files_to_backup.append(path)

        # 检测 awk 输出到文件
        awk_matches = self._AWK_PATTERN.findall(command)
        for path in awk_matches:
            if path and not should_skip(path):
                abs_path = Path(path).expanduser().resolve()
                if abs_path.exists():
                    files_to_backup.append(abs_path)

        # 检测 echo 输出到文件
        echo_matches = self._ECHO_PATTERN.findall(command)
        for path in echo_matches:
            if path and not should_skip(path):
                abs_path = Path(path).expanduser().resolve()
                if abs_path.exists():
                    files_to_backup.append(abs_path)

        # 检测 tee 输出到文件
        tee_matches = self._TEE_PATTERN.findall(command)
        for match in tee_matches:
            file_path = match[-1] if isinstance(match, tuple) and len(match) > 1 else match
            if file_path and not should_skip(file_path):
                abs_path = Path(file_path).expanduser().resolve()
                if abs_path.exists():
                    files_to_backup.append(abs_path)

        # 检测重定向 >, >>
        redirect_matches = self._WRITE_REDIRECT_PATTERN.findall(command)
        for path in redirect_matches:
            if path and not should_skip(path):
                abs_path = Path(path).expanduser().resolve()
                if abs_path.exists():
                    files_to_backup.append(abs_path)

        # 检测 cp 覆盖目标文件
        cp_matches = self._CP_PATTERN.findall(command)
        for _, _, target in cp_matches:
            if target and not target.startswith("-") and not should_skip(target):
                abs_path = Path(target).expanduser().resolve()
                if abs_path.exists():
                    files_to_backup.append(abs_path)

        # 去重
        return list(dict.fromkeys(files_to_backup))

    def _backup_files_if_needed(self, command: str) -> str:
        """
        如果命令将修改工作目录外的文件，则备份

        Args:
            command: shell 命令

        Returns:
            备份信息字符串（如果有备份）
        """
        # 动态导入，避免循环依赖
        files_to_backup = self._detect_files_to_backup(command)
        backup_info_parts: list[str] = []

        for file_path in files_to_backup:
            if not is_in_working_directory(file_path):
                backup_path = backup_external_file(file_path)
                if backup_path:
                    backup_info_parts.append(
                        f"{GREEN}📦 备份文件:{RESET} {file_path} → {backup_path}"
                    )

        if backup_info_parts:
            backup_info_parts.append("执行了文件操作，注意测试验收！")
            return "\n".join(backup_info_parts)

        return ""


class DaemonTool(BashTool):
    """daemon工具：非阻塞后台运行shell命令

    参数同bash工具，区别：命令在后台daemon进程运行（不等待完成），
    立刻返回daemon id并提示退出方式（stop <id>）。

    - 本地nest: /bin/bash -c + start_new_session（会话首进程，pgid==pid，可killpg整组杀）
    - 远程Linux nest: setsid nohup ... & echo DAEMON_PID=$!
    - 远程macOS nest: nohup ... & echo DAEMON_PID=$!
    - Windows: PowerShell Start-Process（wmic在Win11 24H2+已移除）

    stdout/stderr 重定向到工作目录 .peng/daemon/daemon_<id>.log，
    进程元数据注册到 data/daemon.json（daemon_registry）。

    stop内置命令同bash工具： stop <id> / stop all
    """

    ALIASES = ()

    @property
    def name(self) -> str:
        return "daemon"

    @property
    def primary_name(self) -> str:
        return "daemon"

    @property
    def description(self) -> str:
        is_win, platform_name = self._get_nest_platform()
        win_cmds = "dir, type, cd, del, copy, move, mkdir, cls"
        unix_cmds = "ls, cat, pwd, rm, cp, mv, mkdir, clear"
        return f"""非阻塞后台daemon运行命令（命令在后台daemon进程运行，不等待完成，适合长时间运行的服务/训练/监控任务）。
当前操作系统为: {platform_name}
参数同bash工具，请使用正确的命令语法： {win_cmds if is_win else unix_cmds} 等。
立即返回 daemon id，stdout/stderr 写入日志文件（用read工具读取查看输出）。
停止方式： 用bash或daemon工具执行 stop <daemon_id>（或 stop all 清理全部）；也可用 task kill -d <daemon_id>。
启动多个daemon时逐个启动并记录id，任务完成后务必stop清理！
⚠️ 运行命令必须说明目的（purpose参数），要简明扼要（4-200个字符），否则将被拒绝执行！
"""

    @property
    def parameters(self) -> dict[str, Any]:
        _, platform_name = self._get_nest_platform()
        return {
            "type": "object",
            "properties": {
                "command": {
                    "type": "string",
                    "description": f"要后台运行的shell命令（当前平台: {platform_name}）。stop <daemon_id> 为停止daemon的内置命令",
                },
                "purpose": {
                    "type": "string",
                    "description": "运行该命令的目的说明（必填，4-200个字符）。大模型调用daemon工具时必须说明执行该命令的目的，否则将被拒绝。",
                },
                "timeout": {
                    "type": "integer",
                    "description": "启动命令的超时时间（毫秒，默认30000，daemon本身不受超时限制）",
                    "default": 30000,
                },
            },
            "required": ["command", "purpose"],
        }

    def _resolve_platform(self, is_local: bool, is_win: bool, nest_name: str) -> str:
        """获取daemon运行机器的平台标识（用于记录和stop时构建kill命令）"""
        if is_local:
            return sys.platform  # linux/darwin/win32
        try:
            from peng.tools._nest_helper import get_nest_platform

            return get_nest_platform(nest_name).lower()
        except Exception as e:
            logger.debug(f"获取nest '{nest_name}'平台失败: {e}")
            return "windows" if is_win else ""

    async def execute(self, **kwargs: Any) -> str:
        """非阻塞启动daemon进程，立刻返回daemon id"""
        command: str = kwargs.get("command", "")
        timeout: int = kwargs.get("timeout", 30000)
        purpose: str = kwargs.get("purpose", "").strip()

        # 目的说明检查（同bash工具）
        if not purpose or len(purpose) < 4 or len(purpose) > 200:
            raise ToolError(
                "命令被拒绝：必须通过 purpose 参数简明扼要地说明运行该命令的目的（4-200字符）。"
                "请说明你为什么要运行这个命令，例如 purpose='后台启动训练任务'。"
            )

        # 安全检查（同bash工具）
        if not self._is_command_safe(command):
            raise ToolError("命令被拒绝：包含危险模式")

        # 🔧 stop内置命令：停止daemon后台进程
        stop_result = await self._handle_stop_builtin(command)
        if stop_result is not None:
            return stop_result

        if not command.strip():
            raise ToolError("命令不能为空")

        nest_name = self.get_context("nest") or "local"
        is_local_nest = nest_name == "local"
        task_id = self.get_context("task_id")
        workdir = self.get_context("workdir")

        is_win, _ = self._get_nest_platform()
        platform = self._resolve_platform(is_local_nest, is_win, nest_name)

        import peng.common.daemon_registry as dr

        try:
            daemon_id = dr.generate_daemon_id()
            if is_local_nest:
                # 本地nest：直接在本地启动daemon进程（任务进程即在本机）
                self._check_protected_paths(command)
                backup_info = self._backup_files_if_needed(command)
                if backup_info:
                    logger.info(backup_info)
                pid, log_file = dr.start_local_daemon(command, workdir, daemon_id)
                entry = dr.register_daemon(
                    task_id=task_id,
                    command=command,
                    pid=pid,
                    nest=None,
                    log_file=log_file,
                    workdir=str(workdir) if workdir else None,
                    platform=platform,
                    daemon_id=daemon_id,
                )
            else:
                # 远程nest：通过nest在远程机器启动daemon
                client = self._get_nest_client()
                if not client:
                    raise NestError(f"Nest '{nest_name}' 客户端不可用，无法远程启动daemon")
                pid, log_file = await dr.start_remote_daemon(
                    client,
                    command=command,
                    workdir=workdir,
                    daemon_id=daemon_id,
                    is_windows=is_win,
                    platform=platform,
                    timeout=timeout,
                )
                entry = dr.register_daemon(
                    task_id=task_id,
                    command=command,
                    pid=pid,
                    nest=nest_name,
                    log_file=log_file,
                    workdir=str(workdir) if workdir else None,
                    platform=platform,
                    daemon_id=daemon_id,
                )
        except ToolError:
            raise
        except NestError:
            raise
        except Exception as e:
            raise ToolError(f"daemon启动失败: {e}") from e

        # 构建返回信息
        daemon_id = entry["id"]
        nest_hint = "" if entry.get("nest") is None else f", nest={entry['nest']}"
        output = (
            f"\n✅ daemon {daemon_id} 已启动 (PID {entry['pid']}{nest_hint})\n"
            f"命令: {command}\n"
            f"日志: {entry['log_file']}（用read工具读取查看输出）\n"
            f"停止: stop {daemon_id}（bash/daemon工具内置命令），或 task kill -d {daemon_id}"
        )
        display = f"\ndaemon {daemon_id} 已启动 (PID {entry['pid']})，停止方式: stop {daemon_id}"
        return DualOutput(summary=output, display=display)
