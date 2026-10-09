"""
消息管理工具（统一调用bin/msg命令行接口）

提供消息发布、回复、读取、控制等功能，内部统一调用bin/msg命令行工具。
所有功能通过命令行接口实现，确保一致性和可维护性。

这是一个单一工具类，通过action参数区分不同功能。

大模型必用功能（直接提供说明）：
- reply: 回复消息（回复给风主🍃，挂在目标消息链内）
- tree: 获取消息树
- control: 控制消息事务（pause/cancel/resume/restart）

其他功能：使用--help参数探索详细信息

事务#2089 发送者/接收者流程规范：
- 发送者：由工具自动标识为「鹏🐣·事务#N」（N=调用方事务ID），LLM传入的
  --from 一律剥离（禁止冒充风主），消息可追踪来源事务。
- 接收者：
  - reply <消息#ID> → 固定回复给风主🍃（进入风主发件箱，挂在目标消息链内）
  - send → 发布新任务消息给鹏（接收者=agent），支持 --to 显式指定：
    agent名（默认）/ 事务#N（路由到该事务根消息链，注入运行中事务）/ #N|消息#N（回复进该消息链）
    禁止 --to 风主（全局消息请用 reply 挂到已有消息链，杜绝无意义全局消息）
- 回复/链内路由消息的 nest/workdir 由数据库层从链首消息继承（保证消息链上下文一致）。
"""

import logging
import os
import re
import shlex
import subprocess
import sys
from typing import Any

from peng.common.paths import get_temp_directory, chown_to_project_owner, ensure_dir_with_owner
from peng.config import get_agent_name, get_master_name, get_project_root
from peng.common.option_split import find_unparsed_option_flags, split_trailing_options
from peng.message.names import build_task_sender_tag
from peng.tools.base import (
    DualOutput,
    Tool,
    ToolError,
)

logger = logging.getLogger(__name__)
AGENT_NAME = get_agent_name()
MASTER_NAME = get_master_name()
PROJECT_ROOT = get_project_root()

# --to 接收者的合法别名（事务#2089）
_AGENT_ALIASES = {"peng", "鹏", "鹏🐣", "agent", "ai"}
_MASTER_ALIASES = {"master", "风主", "风主🍃"}
_TASK_ID_RE = re.compile(r"^(?:事务#?|task[:#]?)(\d+)$", re.IGNORECASE)
_MSG_ID_RE = re.compile(r"^(?:消息#?|#)(\d+)$", re.IGNORECASE)


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


def _pop_option_pairs(tokens: list[str], flags: tuple[str, ...]) -> tuple[list[str], list[str]]:
    """从选项token列表中移除指定flag及其取值（--flag value / --flag=value / -f value）。

    Args:
        tokens: 选项token列表（flag与取值按原顺序）
        flags: 要移除的flag集合

    Returns:
        (剩余tokens, 被移除的原始token列表)
    """
    flag_set = set(flags)
    kept: list[str] = []
    removed: list[str] = []
    i = 0
    while i < len(tokens):
        tok = tokens[i]
        bare = tok.split("=", 1)[0]
        if bare in flag_set:
            removed.append(tok)
            if "=" not in tok and i + 1 < len(tokens) and not tokens[i + 1].startswith("-"):
                # 独立取值形式：--flag value
                removed.append(tokens[i + 1])
                i += 2
                continue
            i += 1
            continue
        kept.append(tok)
        i += 1
    return kept, removed


def _extract_leading_msg_id(content: str) -> tuple[str | None, str]:
    """从内容头部提取消息ID token（支持 12 / #12 / 消息#12 形态）。

    Args:
        content: 子命令参数内容（已去除子命令名）

    Returns:
        (消息ID字符串或None, 剩余内容)
    """
    if not content:
        return None, ""
    first, _, rest = content.partition(" ")
    candidates = (first, first.lstrip("#") if first.startswith("#") else None)
    for cand in candidates:
        if cand and cand.isdigit():
            return cand, rest.strip()
    m = _MSG_ID_RE.match(first)
    if m:
        return m.group(1), rest.strip()
    return None, content


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
1. 回复消息（回复给风主🍃，挂在目标消息链内）：reply <id> <回复内容> [--timeout TIMEOUT]
      示例：reply 12 已完成。
      示例：reply 已完成（无ID时自动回复本事务的链首消息）
2. 发布新任务消息给鹏：send <消息内容> [--nest NEST] [--workdir WORKDIR] [--to <接收者>]
      示例：send 截屏存到本目录 --nest win11 --workdir D:\\work\\temp
      --to 可选（默认=发布新任务给鹏）：agent名 | 事务#ID（发给指定事务，注入其消息链） | #消息ID（发到指定消息链）
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
4. 选项（--nest/--workdir/--timeout 等）必须写在参数**末尾**（内容之后），
   否则会被当作内容的一部分而失效！例如：send 消息内容 --nest win11 --workdir D:\\work\\temp
5. 发送者由系统自动标识为「鹏🐣·事务#当前事务ID」，--from 参数会被忽略；
   禁止向风主发送全局新消息（send --to 风主无效），汇报请用 reply 回复已有消息链
6. 复杂任务应该用report工具汇报总结工作，不要使用message工具除非被明确要求回复。
7. 回复消息后，禁止用message或report工具连续汇报类似内容！
8. 使用--help参数获取详细帮助
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

    # ==================== 发送者/接收者解析（事务#2089） ====================

    def _sender_tag(self) -> str:
        """发送者标识：鹏🐣·事务#N（N=调用方事务ID），无事务上下文时为纯agent名"""
        return build_task_sender_tag(self.get_context("task_id"), AGENT_NAME)

    def _resolve_to_agent_target(self, to_value: str | None) -> tuple[str, int | None, str]:
        """解析 send 的 --to 接收者。

        Returns:
            (kind, target_id, note)：
            - kind="new_task"：发布新任务给鹏（默认），target_id=None
            - kind="chain"：路由到指定消息链（target_id=链内消息ID，回复注入该链）
            - kind="invalid"：非法目标，note=错误说明
        """
        if to_value is None or not to_value.strip():
            return "new_task", None, ""
        to = to_value.strip()

        # agent 名 → 新任务
        if to.lower() in _AGENT_ALIASES or to == AGENT_NAME:
            return "new_task", None, ""

        # 风主名 → 禁止（杜绝无意义全局消息，#9460 教训）
        if to in _MASTER_ALIASES or to == MASTER_NAME:
            return (
                "invalid",
                None,
                f"禁止用 send 向{MASTER_NAME}发送全局新消息（会产生无意义全局消息）。"
                f"向{MASTER_NAME}汇报请用 reply <消息#ID> 回复已有消息链。",
            )

        # 事务#N → 路由到该事务的根消息链
        m = _TASK_ID_RE.match(to)
        if m:
            task_id = int(m.group(1))
            task_manager = self.get_context("task_manager")
            task = task_manager.get_task(task_id) if task_manager else None
            if not task:
                return "invalid", None, f"事务#{task_id}不存在，无法路由消息。"
            msg_id = task.get("message_id")
            if not msg_id:
                return (
                    "invalid",
                    None,
                    f"事务#{task_id}没有关联消息链，无法路由消息；"
                    f"定向发送提示请改用 info_tool 注入。",
                )
            note = ""
            if task.get("root_id"):
                note = f"事务#{task_id}是子事务，消息已路由到其根消息链（由根事务处理）。"
            return "chain", int(msg_id), note

        # #N / 消息#N → 回复进该消息链
        m = _MSG_ID_RE.match(to)
        if m:
            msg_id = int(m.group(1))
            return "chain", msg_id, ""

        # 纯数字 → 消息ID
        if to.isdigit():
            return "chain", int(to), ""

        return (
            "invalid",
            None,
            f"非法接收者 {to!r}。合法格式：agent名（发布新任务）| 事务#ID（发给指定事务）"
            f" | #消息ID（发到指定消息链）。向{MASTER_NAME}汇报请用 reply。",
        )

    def _validate_chain_message(self, msg_id: int) -> str | None:
        """校验链内目标消息存在，返回错误信息（None=通过）"""
        try:
            from peng.message.database import MessageDatabase

            msg = MessageDatabase().get_message(msg_id)
            if not msg:
                return f"消息#{msg_id}不存在，无法路由消息。"
            return None
        except Exception as e:
            logger.warning(f"[message工具] 校验消息#{msg_id}失败: {e}")
            return None  # 数据库查询失败时交给 bin/msg 兜底校验

    # ==================== 主执行 ====================

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

            def _build_msg_cmd(*extra_args: str, inject_env: bool = False) -> list[str]:
                """构建跨平台的 msg 命令列表

                --local 强制本地模式：工具在本地事务上下文中运行，
                临时文件只在本地创建，远程模式无法读取本地临时文件。
                事务#2089：仅在 send 新任务（inject_env=True）时注入上下文
                nest/workdir（新任务需要执行环境）；reply/链内路由消息的
                nest/workdir 由数据库层从链首消息继承（保证消息链上下文一致）。
                """
                if os.name == "nt":
                    base = [msg_script, str(PROJECT_ROOT / "bin" / "msg"), "--local"]
                else:
                    base = [msg_script, "--local"]
                cmd = base + list(extra_args)
                if inject_env:
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

            # 内容型子命令的选项定义（事务#1998）：
            # LLM 传入的 args 是「内容 + 尾部选项」的纯文本，选项必须从内容中分离出来
            # 作为命令行参数传给 bin/msg，否则会被当作内容写进 --file 临时文件而失效
            # （如 send 内容 --nest win11 --workdir D:\work\temp 中的 --nest/--workdir）。
            value_flags: tuple[str, ...] = ("--file",)
            bool_flags: tuple[str, ...] = ("-d", "--delete")
            if subcmd in ("send", "reply"):
                value_flags = (
                    "--from",
                    "--to",
                    "--timeout",
                    "--file",
                    "--nest",
                    "-n",
                    "--workdir",
                    "-w",
                )
                bool_flags = ()

            temp_file = None
            hint = ""
            if subcmd in content_subcmds and subcmd_args:
                # 分离「内容」与「尾部选项」：内容经--file传递，选项原样交给CLI解析
                content, option_tokens = split_trailing_options(
                    subcmd_args, value_flags, bool_flags
                )
                if option_tokens:
                    logger.info(f"[message工具] 从内容中分离出尾部选项: {' '.join(option_tokens)}")

                # ===== 事务#2089：发送者/接收者流程清理（send/reply 专用） =====
                sender_note = ""
                if subcmd in ("send", "reply"):
                    # 剥离 LLM 传入的 --from（发送者由系统自动标识，禁止冒充）
                    option_tokens, removed = _pop_option_pairs(option_tokens, ("--from",))
                    if removed:
                        logger.warning(f"[message工具] 已剥离LLM传入的 --from: {' '.join(removed)}")
                        sender_note = f"\nℹ️ 发送者已自动标识为 {self._sender_tag()}（--from 参数被忽略）。"

                if subcmd == "reply":
                    # ===== reply 流程：接收者固定为风主🍃，挂在目标消息链内 =====
                    # 剥离 --to（回复方向固定：报告给风主）和 --nest/--workdir（由链首继承）
                    option_tokens, removed_to = _pop_option_pairs(
                        option_tokens, ("--to", "--nest", "-n", "--workdir", "-w")
                    )
                    if removed_to:
                        logger.warning(
                            f"[message工具] reply已剥离LLM传入的接收者/环境选项: {' '.join(removed_to)}"
                        )
                    # 提取目标消息ID（支持 12 / #12 / 消息#12；缺省时回退本事务链首消息）
                    msg_id_str, content = _extract_leading_msg_id(content)
                    if msg_id_str is None:
                        ctx_message_id = self.get_context("message_id")
                        if ctx_message_id:
                            msg_id_str = str(ctx_message_id)
                            hint += (
                                f"\nℹ️ 未指定回复目标消息ID，已自动回复本事务链首消息#{msg_id_str}。"
                            )
                    cmd = _build_msg_cmd(
                        "reply",
                        *( [msg_id_str] if msg_id_str else [] ),
                        *option_tokens,
                        "--from",
                        self._sender_tag(),
                        "--to",
                        MASTER_NAME,
                    )
                elif subcmd == "send":
                    # ===== send 流程：解析 --to 接收者（默认=发布新任务给鹏） =====
                    option_tokens, removed_to = _pop_option_pairs(option_tokens, ("--to",))
                    to_value = None
                    if removed_to:
                        # 支持 --to value 与 --to=value 两种形式
                        first_tok = removed_to[0]
                        if "=" in first_tok:
                            to_value = first_tok.split("=", 1)[1]
                        elif len(removed_to) > 1:
                            to_value = removed_to[1]
                        else:
                            to_value = ""
                        logger.info(f"[message工具] send解析到 --to: {to_value}")
                    kind, target_id, note = self._resolve_to_agent_target(to_value)
                    if kind == "invalid":
                        output = f"❌  message工具调用失败： {note}"
                        return DualOutput(summary=output, display=output)
                    if note:
                        hint += f"\nℹ️ {note}"
                    if kind == "new_task":
                        # 新任务消息：接收者=agent，注入上下文 nest/workdir 作为执行环境
                        cmd = _build_msg_cmd(
                            "send",
                            *option_tokens,
                            "--from",
                            self._sender_tag(),
                            "--to",
                            AGENT_NAME,
                            inject_env=True,
                        )
                    else:
                        # 链内路由：receiver=agent，回复注入目标链（运行中事务自动接收）
                        err = self._validate_chain_message(target_id)
                        if err:
                            output = f"❌  message工具调用失败： {err}"
                            return DualOutput(summary=output, display=output)
                        option_tokens, _removed_env = _pop_option_pairs(
                            option_tokens, ("--nest", "-n", "--workdir", "-w")
                        )
                        cmd = _build_msg_cmd(
                            "send",
                            *option_tokens,
                            "--reply",
                            str(target_id),
                            "--from",
                            self._sender_tag(),
                            "--to",
                            AGENT_NAME,
                        )
                else:
                    # content/title/search 等只读或字段操作子命令：原样传递
                    cmd = _build_msg_cmd(subcmd, *option_tokens)

                # 如果子命令有ID参数（如 content 5 或 title 5），分离ID和内容
                if subcmd in ("content", "title"):
                    arg_parts = content.split(maxsplit=1)
                    if arg_parts and arg_parts[0].isdigit():
                        cmd.append(arg_parts[0])
                        content = arg_parts[1] if len(arg_parts) > 1 else ""

                # 内容中残留疑似选项文本时给出显式提示，避免"选项被当作内容静默忽略"
                suspicious = find_unparsed_option_flags(content, value_flags + bool_flags)
                if suspicious:
                    hint += (
                        f"\n⚠️ 提示：内容中检测到疑似选项 {' '.join(suspicious)}，但未被解析为选项。"
                        f"若要传入选项，必须写在参数末尾（内容之后），例如："
                        f"{subcmd} 内容 --workdir /x；若只是正文提到这些字样可忽略本提示。"
                    )
                    logger.warning(f"[message工具] {subcmd} 内容疑似包含未解析选项: {suspicious}")

                if content:
                    temp_file = _write_temp_file(content)
                    cmd.extend(["--file", temp_file])
                if sender_note:
                    hint += sender_note
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

            output = (
                " ".join(cmd)
                + "\n"
                + (result.stderr or "")
                + "\n"
                + (result.stdout or "")
                + hint
            )
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
