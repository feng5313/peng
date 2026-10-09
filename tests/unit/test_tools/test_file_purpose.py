"""
文件工具 purpose 必填参数测试（事务#1777）

参考 bash 工具 purpose 校验：write/edit/line_edit 三个文件工具
必须通过 purpose 参数（4-200字符）说明操作目的，否则拒绝执行；
🔧 工具调用 显示信息中应包含 📌 目的 提示（fly watch CLI 与
webui xterm 共用该显示链路）。
"""

import asyncio

import pytest

from peng.tools.base import ToolError
from peng.tools.file import EditTool, LineEditTool, WriteTool


def _run(coro):
    return asyncio.run(coro)


# ============================================================
# parameters 定义：purpose 必填
# ============================================================


class TestPurposeParameterDefinition:
    """parameters 定义：purpose 在 properties 且 required"""

    @pytest.mark.parametrize("tool_cls", [WriteTool, EditTool, LineEditTool])
    def test_purpose_in_properties_and_required(self, tool_cls):
        tool = tool_cls()
        params = tool.parameters
        assert "purpose" in params["properties"]
        assert "purpose" in params["required"]

    @pytest.mark.parametrize("tool_cls", [WriteTool, EditTool, LineEditTool])
    def test_description_mentions_purpose(self, tool_cls):
        tool = tool_cls()
        assert "purpose" in tool.description


# ============================================================
# execute 拒绝路径：purpose 缺失/过短/过长
# ============================================================


class TestPurposeValidation:
    """purpose 校验在 nest 连接之前执行，缺失/非法时抛 ToolError"""

    @pytest.mark.parametrize(
        "purpose",
        [None, "", "   ", "abc", "x" * 201],
    )
    def test_write_rejected(self, purpose):
        tool = WriteTool()
        kwargs = {"filePath": "/tmp/x.py", "content": "print(1)"}
        if purpose is not None:
            kwargs["purpose"] = purpose
        with pytest.raises(ToolError, match="purpose"):
            _run(tool.execute(**kwargs))

    @pytest.mark.parametrize(
        "purpose",
        [None, "", "   ", "abc", "x" * 201],
    )
    def test_edit_rejected(self, purpose):
        tool = EditTool()
        kwargs = {"filePath": "/tmp/x.py", "oldString": "a", "newString": "b"}
        if purpose is not None:
            kwargs["purpose"] = purpose
        with pytest.raises(ToolError, match="purpose"):
            _run(tool.execute(**kwargs))

    @pytest.mark.parametrize(
        "purpose",
        [None, "", "   ", "abc", "x" * 201],
    )
    def test_line_edit_rejected(self, purpose):
        tool = LineEditTool()
        kwargs = {
            "filePath": "/tmp/x.py",
            "operation": "delete",
            "startLine": 1,
            "endLine": 2,
        }
        if purpose is not None:
            kwargs["purpose"] = purpose
        with pytest.raises(ToolError, match="purpose"):
            _run(tool.execute(**kwargs))


# ============================================================
# 🔧 工具调用 显示：write/edit/line_edit 显示 📌 目的
# ============================================================


class TestFormatToolCallInfoPurpose:
    """tool_engine._format_tool_call_info 显示 purpose（fly watch/webui xterm 共用）"""

    @staticmethod
    def _engine():
        # _format_tool_call_info 仅依赖类属性，跳过 __init__ 以避免环境依赖
        from peng.llm.tool_engine import ToolExecutionEngine

        return ToolExecutionEngine.__new__(ToolExecutionEngine)

    def test_write_purpose_displayed(self):
        info = self._engine()._format_tool_call_info(
            "write",
            {"filePath": "/tmp/a.py", "content": "print(1)", "purpose": "初始化示例脚本"},
        )
        assert "📌 目的: 初始化示例脚本" in info

    def test_edit_purpose_displayed(self):
        info = self._engine()._format_tool_call_info(
            "edit",
            {
                "filePath": "/tmp/a.py",
                "oldString": "print(1)",
                "newString": "print(2)",
                "purpose": "修改变量输出值",
            },
        )
        assert "📌 目的: 修改变量输出值" in info

    def test_line_edit_purpose_displayed(self):
        info = self._engine()._format_tool_call_info(
            "line_edit",
            {
                "filePath": "/tmp/a.py",
                "operation": "delete",
                "startLine": 1,
                "endLine": 2,
                "purpose": "删除冗余日志代码",
            },
        )
        assert "📌 目的: 删除冗余日志代码" in info

    def test_missing_purpose_tolerated(self):
        """purpose 缺失时显示容错（不出现 📌 目的 段，回退原显示）"""
        info = self._engine()._format_tool_call_info(
            "write", {"filePath": "/tmp/a.py", "content": "x = 1"}
        )
        assert "📌 目的:" not in info
        assert "/tmp/a.py" in info

    def test_non_string_purpose_tolerated(self):
        """purpose 为非字符串（异常参数）时显示容错"""
        info = self._engine()._format_tool_call_info(
            "write", {"filePath": "/tmp/a.py", "content": "x = 1", "purpose": 123}
        )
        assert "📌 目的:" not in info
