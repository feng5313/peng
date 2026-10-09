"""
事务#2089：message 和 info 工具发送者/接收者流程测试

覆盖：
1. names 辅助：is_agent_side / build_task_sender_tag
2. message 工具选项辅助：_pop_option_pairs / _extract_leading_msg_id
3. MessageTool reply/send 流程：发送者自动标识（鹏🐣·事务#N）、--from剥离、
   接收者路由（agent/事务#N/#N/风主拒绝）、reply缺ID回退本事务链首
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


class TestMessageToolSend:
    async def test_send_default_receiver_is_agent(self, monkeypatch):
        """send 默认：接收者=agent（发布新任务），不再发全局消息给风主（#9460根因）"""
        tool, captured = _make_tool(monkeypatch)
        await _run_tool(tool, "send 帮我截屏")
        cmd = captured.cmds[0]
        i_from = cmd.index("--from")
        i_to = cmd.index("--to")
        assert cmd[i_from + 1] == f"{AGENT_NAME}·事务#2089"
        assert cmd[i_to + 1] == AGENT_NAME
        # 新任务注入上下文环境
        assert "--nest" in cmd and "--workdir" in cmd

    async def test_send_to_agent_alias(self, monkeypatch):
        tool, captured = _make_tool(monkeypatch)
        await _run_tool(tool, "send 帮我截屏 --to peng")
        cmd = captured.cmds[0]
        assert cmd[cmd.index("--to") + 1] == AGENT_NAME

    async def test_send_to_master_rejected(self, monkeypatch):
        """send --to 风主：拒绝，提示用 reply（杜绝无意义全局消息）"""
        tool, captured = _make_tool(monkeypatch)
        result = await _run_tool(tool, "send 通知你一下 --to 风主🍃")
        assert captured.cmds == []  # 未执行命令
        assert "❌" in result.summary
        assert "reply" in result.summary

    async def test_send_to_task_routes_to_chain(self, monkeypatch):
        """send --to 事务#N：路由到该事务根消息链（receiver=agent，--reply）"""
        fake_tm = _FakeTaskManager({2064: {"id": 2064, "message_id": 9302, "root_id": None}})
        monkeypatch.setattr(
            "peng.message.database.MessageDatabase",
            type(
                "FakeDB",
                (),
                {"get_message": staticmethod(lambda mid: {"id": mid} if mid == 9302 else None)},
            ),
        )
        tool, captured = _make_tool(monkeypatch, task_manager=fake_tm)
        await _run_tool(tool, "send 进度同步：xxx完成 --to 事务#2064")
        cmd = captured.cmds[0]
        i_reply = cmd.index("--reply")
        assert cmd[i_reply + 1] == "9302"
        i_to = cmd.index("--to")
        assert cmd[i_to + 1] == AGENT_NAME
        i_from = cmd.index("--from")
        assert cmd[i_from + 1] == f"{AGENT_NAME}·事务#2089"
        # 链内路由不注入环境（由链首继承）
        assert "--nest" not in cmd and "--workdir" not in cmd

    async def test_send_to_subtask_notes_root_chain(self, monkeypatch):
        """send --to 子事务：路由根消息链并给出提示"""
        fake_tm = _FakeTaskManager({2065: {"id": 2065, "message_id": 9302, "root_id": 2064}})
        monkeypatch.setattr(
            "peng.message.database.MessageDatabase",
            type(
                "FakeDB",
                (),
                {"get_message": staticmethod(lambda mid: {"id": mid})},
            ),
        )
        tool, captured = _make_tool(monkeypatch, task_manager=fake_tm)
        result = await _run_tool(tool, "send hi --to 事务#2065")
        cmd = captured.cmds[0]
        assert cmd[cmd.index("--reply") + 1] == "9302"
        assert "子事务" in result.summary

    async def test_send_to_task_not_exists(self, monkeypatch):
        fake_tm = _FakeTaskManager({})
        tool, captured = _make_tool(monkeypatch, task_manager=fake_tm)
        result = await _run_tool(tool, "send hi --to 事务#9999")
        assert captured.cmds == []
        assert "❌" in result.summary and "不存在" in result.summary

    async def test_send_to_msg_id_routes_to_chain(self, monkeypatch):
        """send --to #9302：回复进该消息链"""
        monkeypatch.setattr(
            "peng.message.database.MessageDatabase",
            type(
                "FakeDB",
                (),
                {"get_message": staticmethod(lambda mid: {"id": mid})},
            ),
        )
        tool, captured = _make_tool(monkeypatch)
        await _run_tool(tool, "send hi --to #9302")
        cmd = captured.cmds[0]
        assert cmd[cmd.index("--reply") + 1] == "9302"
        assert cmd[cmd.index("--to") + 1] == AGENT_NAME

    async def test_send_to_invalid_rejected(self, monkeypatch):
        tool, captured = _make_tool(monkeypatch)
        result = await _run_tool(tool, "send hi --to 张三")
        assert captured.cmds == []
        assert "❌" in result.summary and "非法接收者" in result.summary

    async def test_send_strips_from(self, monkeypatch):
        """send：--from 一律剥离（禁止冒充）"""
        tool, captured = _make_tool(monkeypatch)
        result = await _run_tool(tool, "send hi --from 风主🍃")
        cmd = captured.cmds[0]
        assert cmd[cmd.index("--from") + 1] == f"{AGENT_NAME}·事务#2089"
        assert "发送者已自动标识" in result.summary

    async def test_send_attached_to_form(self, monkeypatch):
        """send --to=事务#N 粘连形式"""
        fake_tm = _FakeTaskManager({2064: {"id": 2064, "message_id": 9302, "root_id": None}})
        monkeypatch.setattr(
            "peng.message.database.MessageDatabase",
            type(
                "FakeDB",
                (),
                {"get_message": staticmethod(lambda mid: {"id": mid})},
            ),
        )
        tool, captured = _make_tool(monkeypatch, task_manager=fake_tm)
        await _run_tool(tool, "send hi --to=事务#2064")
        cmd = captured.cmds[0]
        assert cmd[cmd.index("--reply") + 1] == "9302"


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
