"""
事务#2507回归测试：report工具存活daemon汇报门禁

背景：统计2860个daemon中4.8%在事务结束后无清理记录（rollback/异常中断/遗忘），
且存在模型声称"已清理"实际遗留的案例。report是事务完成最后关卡，新增门禁：
- 有存活daemon且summary无[保留daemon]标记 → 拒绝并给出stop指引
- 有存活daemon且含[保留daemon]标记 → 放行，结果注明保留清单（审计可见）
- 无存活daemon → 正常放行（原行为不变）
- daemon_registry异常 → 降级放行（可用性优先，绝不阻断report）
"""

from unittest.mock import MagicMock

import pytest

import peng.common.daemon_registry as dr
from peng.tools.task import ReportTool


def _make_report_tool(task_row: dict) -> ReportTool:
    tool = ReportTool()
    tool.set_context(task_id=42)
    task_manager = MagicMock()
    task_manager.get_task.return_value = task_row
    task_manager.db.update_task.return_value = True
    tool.set_context(task_manager=task_manager)
    return tool


def _patch_alive(monkeypatch, entries_or_exc):
    """monkeypatch daemon_registry.get_alive_task_daemons（report工具内部动态import）"""

    async def fake(task_id):
        if isinstance(entries_or_exc, Exception):
            raise entries_or_exc
        return entries_or_exc

    monkeypatch.setattr(dr, "get_alive_task_daemons", fake)


_ALIVE_ENTRY = {
    "id": "d1a2b",
    "pid": 12345,
    "nest": None,
    "command": "python -m http.server 18080",
    "alive_status": "alive",
}


@pytest.mark.asyncio
async def test_no_alive_daemons_passes():
    """无存活daemon → 正常放行（原行为）"""
    tool = _make_report_tool({"id": 42, "report": "", "progress": 100})
    import asyncio

    async def fake(task_id):
        return []

    tool._get_alive_daemons_safe = fake  # 直接替换避免依赖monkeypatch
    result = await tool.execute(summary="工作完成汇报", progress=100)
    assert "✅" in result.summary
    assert "汇报暂缓" not in result.summary


@pytest.mark.asyncio
async def test_alive_daemon_without_marker_rejected(monkeypatch):
    """有存活daemon且无保留标记 → 拒绝，给出stop/保留双路径指引，不写入report"""
    tool = _make_report_tool({"id": 42, "report": "", "progress": 100})
    _patch_alive(monkeypatch, [_ALIVE_ENTRY])
    result = await tool.execute(summary="工作完成汇报", progress=100)

    assert "汇报暂缓" in result.summary
    assert "d1a2b" in result.summary
    assert "stop d1a2b" in result.summary
    assert "[保留daemon]" in result.summary
    # 拒绝路径不得写入report字段
    tool.get_context("task_manager").db.update_task.assert_not_called()


@pytest.mark.asyncio
async def test_alive_daemon_with_marker_passes(monkeypatch):
    """含[保留daemon]标记 → 放行且结果注明保留清单（审计可见）"""
    tool = _make_report_tool({"id": 42, "report": "", "progress": 100})
    _patch_alive(monkeypatch, [_ALIVE_ENTRY])
    result = await tool.execute(
        summary="部署完成，http服务持续运行。[保留daemon] d1a2b 为用户部署的文件服务",
        progress=100,
    )

    assert "✅" in result.summary
    assert "已注明保留daemon: [d1a2b]" in result.summary
    tool.get_context("task_manager").db.update_task.assert_called_once()


@pytest.mark.asyncio
async def test_marker_with_spaces_also_matches(monkeypatch):
    """[保留 daemon]（带空格变体）同样放行"""
    tool = _make_report_tool({"id": 42, "report": "", "progress": 100})
    _patch_alive(monkeypatch, [_ALIVE_ENTRY])
    result = await tool.execute(summary="服务保留运行。[保留 daemon]", progress=100)
    assert "✅" in result.summary
    assert "汇报暂缓" not in result.summary


@pytest.mark.asyncio
async def test_registry_exception_degrades_gracefully(monkeypatch):
    """daemon_registry异常 → 降级放行，report工具可用性优先"""
    tool = _make_report_tool({"id": 42, "report": "", "progress": 100})
    _patch_alive(monkeypatch, RuntimeError("daemon.json corrupted"))
    result = await tool.execute(summary="工作完成汇报", progress=100)

    assert "✅" in result.summary
    assert "汇报暂缓" not in result.summary
    tool.get_context("task_manager").db.update_task.assert_called_once()


@pytest.mark.asyncio
async def test_many_daemons_list_truncated(monkeypatch):
    """存活daemon>10个 → 清单截断至10个并提示总数，stop指引给出前10个id"""
    entries = [
        {"id": f"d{i:033x}"[:5], "pid": 1000 + i, "nest": None, "command": "svc", "alive_status": "alive"}
        for i in range(15)
    ]
    tool = _make_report_tool({"id": 42, "report": "", "progress": 100})
    _patch_alive(monkeypatch, entries)
    result = await tool.execute(summary="工作完成汇报", progress=100)

    assert "共15个" in result.summary
    assert "仅列前10" in result.summary
    # 提示可用 task kill <task_id> 一次性清理
    assert "task kill 42" in result.summary
