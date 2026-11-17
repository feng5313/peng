"""事务#2751测试：sleep 工具（等待 + 心跳保活 + 中断提前结束）

需求：在 src/peng/tools/task.py 添加 sleep 工具，参数单位秒，最多5分钟，
sleep 期间保持心跳；追加指示（消息#13493）：必须 reason 参数说明等待理由。

验证点：
1. _parse_seconds / _normalize_reason 纯函数容错
2. 拒绝路径：seconds<=0/非法/>300（5分钟上限）、reason 缺失/空白/长度不符
3. 正常等待：按时长完成；起点立刻打心跳；短心跳间隔下按节流多次刷新
4. 中断（真实共享监控 peng.task.interrupt_monitor，check_all=True）：
   事务状态变更 / 未读 info → 立刻结束等待并给出原因
5. 无事务上下文（adhoc）：纯等待不报错、不打心跳
6. 上层 cancel：CancelledError 向上抛且监控协程被收掉（无残留任务）
7. registry 注册 sleep，参数 required=[seconds, reason]
8. bash._interrupt_monitor 委托共享模块（原有签名/返回契约不变）
"""

import asyncio
import math
import time
from unittest.mock import MagicMock

import pytest

import peng.task.interrupt_monitor as im
from peng.tools.bash import BashTool
from peng.tools.registry import ToolRegistry
from peng.tools.task import SleepTool, _normalize_reason, _parse_seconds


class TestParseSeconds:
    def test_int_and_float(self):
        assert _parse_seconds(30) == 30.0
        assert _parse_seconds(5.5) == 5.5
        assert _parse_seconds(0) == 0.0

    def test_string_with_unit(self):
        assert _parse_seconds("30") == 30.0
        assert _parse_seconds("30s") == 30.0
        assert _parse_seconds("30 sec") == 30.0
        assert _parse_seconds("30 秒") == 30.0
        assert _parse_seconds(" 2.5Seconds ") == 2.5

    def test_invalid(self):
        assert _parse_seconds(None) is None
        assert _parse_seconds(True) is None
        assert _parse_seconds(False) is None
        assert _parse_seconds("abc") is None
        assert _parse_seconds("") is None
        assert _parse_seconds(math.nan) is None
        assert _parse_seconds(math.inf) is None


class TestNormalizeReason:
    def test_folds_escapes_and_whitespace(self):
        assert _normalize_reason(" 等待daemon编译\\n完成 ") == "等待daemon编译 完成"

    def test_non_string_is_empty(self):
        assert _normalize_reason(None) == ""
        assert _normalize_reason(30) == ""
        assert _normalize_reason("") == ""


def _make_sleep_tool(task_row: dict | None = None) -> tuple[SleepTool, MagicMock]:
    """注入 mock 事务上下文的 SleepTool（返回工具与 task_manager mock）"""
    tool = SleepTool()
    tool.set_context(task_id=42, message_id=100)
    tm = MagicMock()
    row = task_row or {
        "id": 42,
        "status": "running",
        "info": "[]",
        "heartbeat_timeout": 300,
    }
    # 中断监控走 db.get_task（与 bash 监控同路径），两者都要返回同一行数据
    tm.get_task.return_value = row
    tm.db.get_task.return_value = row
    tm.update_heartbeat.return_value = True
    tm.STATUS_CHINESE = {"paused": "已暂停", "cancelled": "已取消"}
    tool.set_context(task_manager=tm)
    return tool, tm


def _patch_msg_db(monkeypatch, reply_count: int = 0) -> None:
    """隔离真实DB：共享监控 check_all=True 会查根消息回复数"""
    import peng.message.database as msg_db_mod

    class FakeMsgDB:
        def get_reply_count(self, msg_id):
            return reply_count

        def get_processing_replies(self, msg_id, receiver=None):
            return []

    monkeypatch.setattr(msg_db_mod, "MessageDatabase", FakeMsgDB)


@pytest.mark.asyncio
async def test_reject_invalid_seconds():
    tool, tm = _make_sleep_tool()
    for bad in (None, 0, -3, "abc", True):
        result = await tool.execute(seconds=bad, reason="等待编译产物")
        assert result.summary.startswith("❌"), f"seconds={bad!r} 应被拒绝"
    assert tm.update_heartbeat.call_count == 0


@pytest.mark.asyncio
async def test_reject_over_5_minutes():
    """上限300秒（5分钟）：超时必须拒绝，且不真的等待"""
    tool, tm = _make_sleep_tool()
    started = time.monotonic()
    result = await tool.execute(seconds=301, reason="等待远端大模型推理完成")
    assert "5分钟" in result.summary and result.summary.startswith("❌")
    assert time.monotonic() - started < 1.0
    assert tm.update_heartbeat.call_count == 0


@pytest.mark.asyncio
async def test_reject_missing_or_bad_reason():
    """追加指示（消息#13493）：reason 必填，缺失/空白/长度不符一律拒绝"""
    tool, tm = _make_sleep_tool()
    for bad_reason in (None, "", "   ", "短", "x" * 201):
        result = await tool.execute(seconds=1, reason=bad_reason)
        assert result.summary.startswith("❌"), f"reason={bad_reason!r} 应被拒绝"
        assert "reason" in result.summary
    assert tm.update_heartbeat.call_count == 0


@pytest.mark.asyncio
async def test_sleep_waits_and_keeps_heartbeat(monkeypatch):
    """按时长完成等待；起点立刻打心跳，等待期间按心跳间隔持续刷新"""
    _patch_msg_db(monkeypatch)
    tool, tm = _make_sleep_tool()

    async def fast_interval(task_manager, task_id):
        return 0.5

    tool._resolve_heartbeat_interval = fast_interval
    started = time.monotonic()
    result = await tool.execute(seconds=2.0, reason="等待daemon编译完成")
    elapsed = time.monotonic() - started
    assert result.summary.startswith("✅"), result.summary
    assert elapsed >= 2.0, f"等待不足：{elapsed}"
    assert elapsed < 2.6, f"等待超发：{elapsed}"
    # 起点1次 + 0.5s间隔在2秒内至少2次（静默等待期心跳不停摆）
    assert tm.update_heartbeat.call_count >= 3, tm.update_heartbeat.call_count
    tm.update_heartbeat.assert_called_with(42)


@pytest.mark.asyncio
async def test_sleep_default_interval_respects_task_timeout():
    """心跳间隔依据事务 heartbeat_timeout 动态收紧（至少保证4次刷新）"""
    tool, _tm = _make_sleep_tool({"status": "running", "info": "[]", "heartbeat_timeout": 20})
    assert await tool._resolve_heartbeat_interval(_tm, 42) == 5.0

    tool2, tm2 = _make_sleep_tool({"status": "running", "info": "[]"})
    tm2.get_task.side_effect = Exception("db down")
    interval = await tool2._resolve_heartbeat_interval(tm2, 42)
    assert 1.0 <= interval <= SleepTool.HEARTBEAT_INTERVAL


@pytest.mark.asyncio
async def test_sleep_interrupts_on_status_change(monkeypatch):
    """事务被暂停 → 共享监控立刻结束等待（真实 monitor_interrupts）"""
    _patch_msg_db(monkeypatch)
    tool, tm = _make_sleep_tool({"status": "paused", "info": "[]", "heartbeat_timeout": 300})
    started = time.monotonic()
    result = await tool.execute(seconds=20, reason="等待测试跑完")
    elapsed = time.monotonic() - started
    assert elapsed < 5, f"未被及时中断：{elapsed}"
    assert result.summary.startswith("⚠️"), result.summary
    assert "已暂停" in result.summary


@pytest.mark.asyncio
async def test_sleep_interrupts_on_unread_info(monkeypatch):
    """未读 info → check_all=True 立刻结束等待（风主指示不被等待吞掉）"""
    _patch_msg_db(monkeypatch)
    tool, _tm = _make_sleep_tool(
        {
            "status": "running",
            "info": '[{"content": "立刻停止等待", "read": false}]',
            "heartbeat_timeout": 300,
        }
    )
    started = time.monotonic()
    result = await tool.execute(seconds=20, reason="等待远端服务就绪")
    assert time.monotonic() - started < 5
    assert result.summary.startswith("⚠️"), result.summary
    assert "立刻停止等待" in result.summary


@pytest.mark.asyncio
async def test_sleep_without_task_context_is_plain_wait():
    """无事务上下文（adhoc）：退化为纯等待，不打心跳也不报错"""
    tool = SleepTool()
    started = time.monotonic()
    result = await tool.execute(seconds=0.3, reason="临时等待外部端口")
    assert result.summary.startswith("✅"), result.summary
    assert time.monotonic() - started >= 0.3


@pytest.mark.asyncio
async def test_sleep_cancel_propagates_and_reaps_monitor(monkeypatch):
    """上层cancel（控制信号/紧急中断）：CancelledError向上抛，监控协程无残留"""
    _patch_msg_db(monkeypatch)
    tool, tm = _make_sleep_tool()
    task = asyncio.create_task(tool.execute(seconds=10, reason="等待快照完成"))
    await asyncio.sleep(0.4)
    assert tm.update_heartbeat.call_count >= 1
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    leftovers = [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]
    assert not leftovers, f"残留监控任务：{leftovers}"


@pytest.mark.asyncio
async def test_bash_monitor_delegates_to_shared_module(monkeypatch):
    """bash._interrupt_monitor 委托 peng.task.interrupt_monitor（回归原有契约）"""

    async def fake(task_manager, task_id, message_id, check_all=False, label="执行"):
        return f"captured:{label}:{check_all}"

    monkeypatch.setattr(im, "monitor_interrupts", fake)
    tm = MagicMock()
    reason = await BashTool()._interrupt_monitor(tm, 42, 100, check_all=True)
    assert reason == "captured:命令:True"


def test_registry_registers_sleep():
    registry = ToolRegistry()
    registry.load_builtin_tools()
    tool = registry.get("sleep")
    assert isinstance(tool, SleepTool)
    definition = tool.to_openai_format()
    assert definition["function"]["name"] == "sleep"
    assert sorted(definition["function"]["parameters"]["required"]) == ["reason", "seconds"]
    desc = definition["function"]["description"]
    assert "5分钟" in desc and "reason" in desc
