"""
事务#2089：message 和 info 工具发送者/接收者流程测试

覆盖：
1. names 辅助：is_agent_side / build_task_sender_tag
2. message 工具选项辅助：_pop_option_pairs / _extract_leading_msg_id
3. MessageTool reply 流程：发送者自动标识（鹏🐣·事务#N）、--from剥离、--nest/--workdir剥离、
   reply缺ID回退本事务链首；事务#2502禁止根消息：send一律拒绝、
   缺ID且本事务无链首时拒绝
4. InfoTool：--sender 事务#N 自动注入
5. info 解析：sender 字段 round-trip
"""

from __future__ import annotations

import pytest

from peng.config import get_agent_name, get_master_name
from peng.message.names import build_task_sender_tag, is_agent_side
from peng.task.info import parse_info_to_list_with_status, serialize_info_list
from peng.tools.message import (
    MessageTool,
    _extract_leading_msg_id,
    _pop_option_pairs,
)
from peng.tools.task import InfoTool

AGENT_NAME = get_agent_name()
MASTER_NAME = get_master_name()


# ==================== names 辅助 ====================


class TestNames:
    def test_is_agent_side_variants(self):
        assert is_agent_side(AGENT_NAME)
        assert is_agent_side(f"{AGENT_NAME}·事务#2089")
        assert is_agent_side("peng")
        assert not is_agent_side(MASTER_NAME)
        assert not is_agent_side("master")
        assert not is_agent_side(None)
        assert not is_agent_side("")

    def test_build_task_sender_tag(self):
        assert build_task_sender_tag(2089, AGENT_NAME) == f"{AGENT_NAME}·事务#2089"
        assert build_task_sender_tag(None, AGENT_NAME) == AGENT_NAME


# ==================== message 工具选项辅助 ====================


class TestOptionHelpers:
    def test_pop_option_pairs_independent(self):
        kept, removed = _pop_option_pairs(["--from", "风主", "--timeout", "300"], ("--from",))
        assert kept == ["--timeout", "300"]
        assert removed == ["--from", "风主"]

    def test_pop_option_pairs_attached(self):
        kept, removed = _pop_option_pairs(["--to=事务#2064"], ("--to",))
        assert kept == []
        assert removed == ["--to=事务#2064"]

    def test_pop_option_pairs_multi(self):
        kept, removed = _pop_option_pairs(
            ["--nest", "epyc", "--workdir", "/x", "-n", "w11"],
            ("--to", "--nest", "-n", "--workdir", "-w"),
        )
        assert kept == []
        assert removed == ["--nest", "epyc", "--workdir", "/x", "-n", "w11"]

    def test_pop_option_pairs_no_value(self):
        # flag后紧跟另一个flag：不吞并
        kept, removed = _pop_option_pairs(["--to", "--timeout", "5"], ("--to",))
        assert kept == ["--timeout", "5"]
        assert removed == ["--to"]

    def test_extract_leading_msg_id(self):
        assert _extract_leading_msg_id("12 已完成") == ("12", "已完成")
        assert _extract_leading_msg_id("#12 已完成") == ("12", "已完成")
        assert _extract_leading_msg_id("消息#12 x") == ("12", "x")
        assert _extract_leading_msg_id("已完成") == (None, "已完成")
        assert _extract_leading_msg_id("") == (None, "")


# ==================== MessageTool 流程 ====================


class _FakeTaskManager:
    """task 工具上下文的假 TaskManager（get_task 查询）"""

    def __init__(self, tasks: dict[int, dict]):
        self._tasks = tasks

    def get_task(self, task_id):
        return self._tasks.get(task_id)


class _CapturedRun:
    def __init__(self):
        self.cmds: list[list[str]] = []

    def __call__(self, cmd, **kwargs):
        self.cmds.append(list(cmd))
        return type("R", (), {"returncode": 0, "stdout": "ok", "stderr": ""})()


def _make_tool(monkeypatch, task_id=2089, message_id=9479, task_manager=None):
    tool = MessageTool()
    tool.set_context(
        task_id=task_id,
        message_id=message_id,
        nest="local",
        workdir="/mnt/p/feng/work/peng",
        task_manager=task_manager,
    )
    captured = _CapturedRun()
    monkeypatch.setattr("peng.tools.message.subprocess.run", captured)
    return tool, captured


async def _run_tool(tool, args: str):
    return await tool.execute(args=args)


class TestMessageToolReply:
    async def test_reply_forces_sender_receiver_and_strips_llm_options(self, monkeypatch):
        """reply：--from/--to/--nest 被剥离，发送者=鹏🐣·事务#N，接收者=风主"""
        tool, captured = _make_tool(monkeypatch)
        result = await _run_tool(
            tool, "reply 12 已完成 --from 风主🍃 --to 鹏 --nest epyc --workdir D:/x"
        )
        cmd = captured.cmds[0]
        assert cmd[2] == "reply", cmd
        # ID 在命令中
        assert "12" in cmd
        # 强制发送者/接收者
        assert f"{AGENT_NAME}·事务#2089" in cmd
        i_from = cmd.index("--from")
        assert cmd[i_from + 1] == f"{AGENT_NAME}·事务#2089"
        i_to = cmd.index("--to")
        assert cmd[i_to + 1] == MASTER_NAME
        # LLM 的冒充/环境选项已剥离
        assert "风主" not in cmd[i_from + 1 : i_from + 2]
        assert "--nest" not in cmd and "--workdir" not in cmd
        assert "✅" in result.summary

    async def test_reply_without_id_falls_back_to_own_chain_message(self, monkeypatch):
        """reply 无ID：自动回复本事务链首消息"""
        tool, captured = _make_tool(monkeypatch, message_id=9479)
        await _run_tool(tool, "reply 已完成")
        cmd = captured.cmds[0]
        assert "9479" in cmd
        i_from = cmd.index("--from")
        assert cmd[i_from + 1] == f"{AGENT_NAME}·事务#2089"

    async def test_reply_hash_id_form(self, monkeypatch):
        """reply #12 形态的消息ID"""
        tool, captured = _make_tool(monkeypatch)
        await _run_tool(tool, "reply #12 已完成")
        cmd = captured.cmds[0]
        assert "12" in cmd


class TestMessageToolNoRootMessage:
    """事务#2502：彻底禁止message工具发送根消息（send一律拒绝，reply必须挂已有链）"""

    @pytest.mark.parametrize(
        "args",
        [
            # 裸内容/选项开头裸内容：bin/msg默认转send（发送根消息），必须拦截
            "帮我截屏",
            "--nest win11 帮我截屏",
            "-n win11 内容",
            # 显式send各变体
            "send 帮我截屏",
            "send 帮我截屏 --to peng",
            "send 通知你一下 --to 风主🍃",
            "send hi --to 事务#2064",
            "send hi --to #9302",
            "send hi --to 张三",
            "send hi --from 风主🍃",
            "send hi --to=事务#2064",
        ],
    )
    async def test_send_always_rejected(self, monkeypatch, args):
        """send（根消息/新任务唯一入口）一律拒绝，且不得调用bin/msg"""
        tool, captured = _make_tool(monkeypatch)
        result = await _run_tool(tool, args)
        assert captured.cmds == []
        assert "❌" in result.summary
        assert "reply" in result.summary

    async def test_empty_args_rejected(self, monkeypatch):
        """空参数：拒绝（无子命令），不允许默认send发根消息"""
        tool, captured = _make_tool(monkeypatch)
        result = await _run_tool(tool, "")
        assert captured.cmds == []
        assert "❌" in result.summary and "根消息" in result.summary

    async def test_help_option_allowed(self, monkeypatch):
        """纯帮助选项放行（只读，不会创建消息）"""
        tool, captured = _make_tool(monkeypatch)
        await _run_tool(tool, "--help")
        assert len(captured.cmds) == 1

    async def test_readonly_subcommand_allowed(self, monkeypatch):
        """合法只读子命令正常透传"""
        tool, captured = _make_tool(monkeypatch)
        await _run_tool(tool, "trees")
        assert captured.cmds[0][2] == "trees"

    async def test_reply_without_id_falls_back_to_own_chain_message(self, monkeypatch):
        """reply 无ID：自动回复本事务链首消息（链内消息，非根消息）"""
        tool, captured = _make_tool(monkeypatch, message_id=9479)
        await _run_tool(tool, "reply 已完成")
        cmd = captured.cmds[0]
        assert "9479" in cmd
        # 链内回复不注入执行环境（由数据库层从链首继承）
        assert "--nest" not in cmd and "--workdir" not in cmd

    async def test_reply_without_id_and_context_rejected(self, monkeypatch):
        """reply 无ID且本事务无链首消息：拒绝，避免退化成根消息"""
        tool, captured = _make_tool(monkeypatch, message_id=None)
        result = await _run_tool(tool, "reply 已完成")
        assert captured.cmds == []
        assert "❌" in result.summary and "根消息" in result.summary

    async def test_reply_strips_env_options(self, monkeypatch):
        """reply 显式传入的 --nest/--workdir 被剥离（nest/workdir自动与根消息一致）"""
        tool, captured = _make_tool(monkeypatch)
        result = await _run_tool(tool, "reply 12 已完成 --nest epyc --workdir D:/x")
        cmd = captured.cmds[0]
        assert "12" in cmd
        assert "--nest" not in cmd and "--workdir" not in cmd
        assert "✅" in result.summary


# ==================== InfoTool ====================


class TestInfoTool:
    async def test_info_add_includes_sender(self, monkeypatch):
        tool = InfoTool()
        tool.set_context(task_id=2089)
        captured = _CapturedRun()
        monkeypatch.setattr("peng.tools.task.subprocess.run", captured)
        result = await _run_tool_args(tool, task_id=2064, info_string="请注意xxx")
        cmd = captured.cmds[0]
        assert cmd[2] == "info"
        assert "2064" in cmd
        assert "事务#2089" in cmd
        i_sender = cmd.index("--sender")
        assert cmd[i_sender + 1] == "事务#2089"

    async def test_info_view_no_sender(self, monkeypatch):
        """查看info不注入sender"""
        tool = InfoTool()
        tool.set_context(task_id=2089)
        captured = _CapturedRun()
        monkeypatch.setattr("peng.tools.task.subprocess.run", captured)
        await _run_tool_args(tool, task_id=2064)
        cmd = captured.cmds[0]
        assert "--sender" not in cmd

    async def test_info_invalid_task_id(self, monkeypatch):
        tool = InfoTool()
        tool.set_context(task_id=2089)
        captured = _CapturedRun()
        monkeypatch.setattr("peng.tools.task.subprocess.run", captured)
        result = await _run_tool_args(tool, task_id=0)
        assert captured.cmds == []
        assert "❌" in result.summary


async def _run_tool_args(tool, **kwargs):
    return await tool.execute(**kwargs)


# ==================== info 解析 round-trip ====================


class TestInfoParseSender:
    def test_roundtrip_with_sender(self):
        items = [{"content": "hi", "read": False, "id": 1, "sender": "事务#2089"}]
        parsed = parse_info_to_list_with_status(serialize_info_list(items))
        assert parsed == items

    def test_legacy_without_sender(self):
        parsed = parse_info_to_list_with_status('[{"content":"hi","read":true,"id":3}]')
        assert parsed == [{"content": "hi", "read": True, "id": 3}]
        assert "sender" not in parsed[0]

    def test_sender_ignored_when_empty(self):
        parsed = parse_info_to_list_with_status('[{"content":"hi","read":false,"sender":""}]')
        assert "sender" not in parsed[0]
