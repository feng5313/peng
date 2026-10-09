"""
测试 ChecklistTool 的 progress 参数解析健壮性

背景（事务#1698）：
- 原代码 progress.replace("%", "") 在 progress=None/非字符串时崩溃
  → 'NoneType' object has no attribute 'replace'
- 触发场景：LLM 调用 checklist edit 时未传 progress（文档声明可选）
- 修复：新增 _parse_progress 统一解析（str/int/float/None 兼容容错）

验证点：
1. _parse_progress 纯函数各类型输入
2. ChecklistTool.execute edit 不传 progress → 不崩溃且保留当前进度（核心回归）
3. ChecklistTool.execute write 不传 progress → 明确参数错误而非crash
4. LLM传JSON数字 progress=85 正常工作
"""
import pytest
from unittest.mock import MagicMock

from peng.tools.task import ChecklistTool, _parse_progress


class TestParseProgress:
    """_parse_progress 辅助函数单元测试"""

    def test_none_returns_none(self):
        assert _parse_progress(None) is None

    def test_valid_string(self):
        assert _parse_progress("85") == 85

    def test_percent_suffix(self):
        assert _parse_progress("95%") == 95
        assert _parse_progress("95 %") == 95

    def test_int_input(self):
        assert _parse_progress(85) == 85

    def test_float_input(self):
        assert _parse_progress(85.0) == 85
        assert _parse_progress(85.7) == 85

    def test_bool_rejected(self):
        # bool是int子类，必须排除
        assert _parse_progress(True) is None
        assert _parse_progress(False) is None

    def test_invalid_string_returns_none(self):
        assert _parse_progress("abc") is None
        assert _parse_progress("") is None
        assert _parse_progress("八成") is None

    def test_range_clamp(self):
        assert _parse_progress(-5) == 0
        assert _parse_progress(150) == 100
        assert _parse_progress("-5") == 0
        assert _parse_progress("150") == 100


def _make_checklist_tool(task_row: dict) -> ChecklistTool:
    """构造注入了mock上下文的ChecklistTool。

    task_row: 模拟 db 中该事务的行数据（checklist/progress字段）
    """
    tool = ChecklistTool()
    tool.set_context(task_id=42)

    task_manager = MagicMock()
    task_manager.get_task.return_value = task_row
    task_manager.db.update_task.return_value = True
    task_manager.db.get_task.return_value = task_row
    tool.set_context(task_manager=task_manager)
    return tool


@pytest.mark.asyncio
async def test_edit_without_progress_keeps_current_progress():
    """核心回归：edit不传progress不再崩溃，保留当前进度"""
    row = {"id": 42, "checklist": "- [ ] 旧事项", "progress": 30}
    tool = _make_checklist_tool(row)
    result = await tool.execute(
        action="edit", oldString="- [ ] 旧事项", newString="- [x] 新事项"
    )
    assert "❌" not in result.summary, f"不应失败: {result.summary}"
    # update_task 应以保留的当前进度30调用
    call_kwargs = tool.get_context("task_manager").db.update_task.call_args.kwargs
    assert call_kwargs["checklist"] == "- [x] 新事项"
    assert call_kwargs["progress"] == 30


@pytest.mark.asyncio
async def test_edit_with_int_progress():
    """LLM可能传JSON数字progress=85"""
    row = {"id": 42, "checklist": "- [ ] 旧事项", "progress": 30}
    tool = _make_checklist_tool(row)
    result = await tool.execute(
        action="edit",
        oldString="- [ ] 旧事项",
        newString="- [x] 新事项",
        progress=85,
    )
    assert "❌" not in result.summary, f"不应失败: {result.summary}"
    call_kwargs = tool.get_context("task_manager").db.update_task.call_args.kwargs
    assert call_kwargs["progress"] == 85


@pytest.mark.asyncio
async def test_edit_with_string_progress():
    row = {"id": 42, "checklist": "- [ ] 旧事项", "progress": 30}
    tool = _make_checklist_tool(row)
    result = await tool.execute(
        action="edit",
        oldString="- [ ] 旧事项",
        newString="- [x] 新事项",
        progress="85%",
    )
    assert "❌" not in result.summary, f"不应失败: {result.summary}"
    call_kwargs = tool.get_context("task_manager").db.update_task.call_args.kwargs
    assert call_kwargs["progress"] == 85


@pytest.mark.asyncio
async def test_edit_with_invalid_progress_keeps_current():
    """非法字符串progress时保留当前进度而非crash"""
    row = {"id": 42, "checklist": "- [ ] 旧事项", "progress": 45}
    tool = _make_checklist_tool(row)
    result = await tool.execute(
        action="edit",
        oldString="- [ ] 旧事项",
        newString="- [x] 新事项",
        progress="abc",
    )
    assert "❌" not in result.summary, f"不应失败: {result.summary}"
    call_kwargs = tool.get_context("task_manager").db.update_task.call_args.kwargs
    assert call_kwargs["progress"] == 45


@pytest.mark.asyncio
async def test_write_without_progress_returns_param_error_not_crash():
    """write不传progress：明确参数错误提示，绝不出现AttributeError崩溃文案"""
    row = {"id": 42, "checklist": "", "progress": 10}
    tool = _make_checklist_tool(row)
    result = await tool.execute(action="write", checklist_content="- [ ] 任务")
    assert "write操作需要提供progress参数" in result.summary
    assert "'NoneType' object has no attribute" not in result.summary


@pytest.mark.asyncio
async def test_write_with_int_progress():
    row = {"id": 42, "checklist": "", "progress": 0}
    tool = _make_checklist_tool(row)
    result = await tool.execute(
        action="write", checklist_content="- [ ] 任务", progress=50
    )
    assert "❌" not in result.summary, f"不应失败: {result.summary}"
    call_kwargs = tool.get_context("task_manager").db.update_task.call_args.kwargs
    assert call_kwargs["checklist"] == "- [ ] 任务"
    assert call_kwargs["progress"] == 50


# ---------- ReportTool 第三处同类隐患回归（事务#1698 全局审计发现） ----------
from peng.tools.task import ReportTool


def _make_report_tool(task_row: dict) -> ReportTool:
    tool = ReportTool()
    tool.set_context(task_id=42)
    task_manager = MagicMock()
    task_manager.get_task.return_value = task_row
    task_manager.db.update_task.return_value = True
    tool.set_context(task_manager=task_manager)
    return tool


@pytest.mark.asyncio
async def test_report_with_int_progress_no_crash():
    """LLM传JSON数字progress=100时report不应AttributeError崩溃"""
    row = {"id": 42, "report": "", "progress": 99}
    tool = _make_report_tool(row)
    result = await tool.execute(summary="工作完成汇报", progress=100)
    assert "'NoneType' object has no attribute" not in result.summary
    assert "'int' object has no attribute" not in result.summary
    assert "✅" in result.summary


@pytest.mark.asyncio
async def test_report_without_progress_uses_zero_and_rejects():
    """不传progress维持原语义：按0处理并拒绝未满100的提交"""
    row = {"id": 42, "report": "", "progress": 50}
    tool = _make_report_tool(row)
    result = await tool.execute(summary="工作汇报")
    assert "尚未完成" in result.summary or "progress=100" in result.summary


@pytest.mark.asyncio
async def test_report_read_with_int_id_and_defaults():
    """read模式带各种缺省参数不崩溃"""
    row = {"id": 42, "report": "已有汇报", "progress": 100}
    tool = _make_report_tool(row)
    result = await tool.execute(action="read")
    assert "已有汇报" in result.summary
