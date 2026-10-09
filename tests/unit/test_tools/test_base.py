"""
测试工具基类
"""
import pytest
from pathlib import Path

from peng.tools.base import Tool, ToolError, DualOutput


# 创建一个用于测试的简单工具实现
class MockTool(Tool):
    """测试用的简单工具实现"""

    @property
    def name(self) -> str:
        return "test_tool"

    @property
    def description(self) -> str:
        return "A test tool for unit testing"

    @property
    def parameters(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "param1": {"type": "string", "description": "First parameter"},
                "param2": {"type": "integer", "description": "Second parameter"},
            },
            "required": ["param1"],
        }

    async def execute(self, **kwargs) -> str:
        param1 = kwargs.get("param1", "")
        param2 = kwargs.get("param2", 0)
        return f"executed with {param1} and {param2}"


class TestDualOutput:
    """测试DualOutput类"""

    def test_dual_output_creation(self):
        """测试DualOutput创建"""
        output = DualOutput(summary="Test summary")
        assert output.summary == "Test summary"
        assert output.display is None
        assert output.attachments == []

    def test_dual_output_with_display(self):
        """测试DualOutput带display参数"""
        output = DualOutput(
            summary="Test summary",
            display="Full display content"
        )
        assert output.summary == "Test summary"
        assert output.display == "Full display content"

    def test_dual_output_with_attachments(self):
        """测试DualOutput带attachments参数"""
        attachments = [
            {"type": "file", "mime": "image/png", "url": "data:image/png;base64,..."}
        ]
        output = DualOutput(
            summary="Test summary",
            attachments=attachments
        )
        assert len(output.attachments) == 1
        assert output.attachments[0]["type"] == "file"

    def test_dual_output_str(self):
        """测试DualOutput的字符串表示"""
        output = DualOutput(summary="Test summary")
        assert str(output) == "Test summary"


class TestTool:
    """测试Tool基类"""

    def test_tool_initialization(self):
        """测试工具初始化"""
        tool = MockTool()
        assert tool._llm_client is None
        assert tool._context == {}

    def test_set_context(self):
        """测试设置上下文"""
        tool = MockTool()
        tool.set_context(message_id=123, task_id=456)
        assert tool.get_context("message_id") == 123
        assert tool.get_context("task_id") == 456

    def test_set_context_update_existing(self):
        """测试更新已存在的上下文"""
        tool = MockTool()
        tool.set_context(message_id=123)
        tool.set_context(message_id=456, task_id=789)
        assert tool.get_context("message_id") == 456
        assert tool.get_context("task_id") == 789

    def test_get_context_nonexistent(self):
        """测试获取不存在的上下文"""
        tool = MockTool()
        assert tool.get_context("nonexistent_key") is None

    def test_set_llm_client(self):
        """测试设置LLM客户端"""
        tool = MockTool()
        mock_client = "mock_llm_client"
        tool.set_llm_client(mock_client)
        assert tool.llm_client == mock_client

    def test_llm_client_property(self):
        """测试LLM客户端属性"""
        tool = MockTool()
        assert tool.llm_client is None

        tool.set_llm_client("client")
        assert tool.llm_client == "client"

    def test_tool_name_property(self):
        """测试工具名称属性"""
        tool = MockTool()
        assert tool.name == "test_tool"

    def test_tool_description_property(self):
        """测试工具描述属性"""
        tool = MockTool()
        assert tool.description == "A test tool for unit testing"

    def test_tool_parameters_property(self):
        """测试工具参数属性"""
        tool = MockTool()
        params = tool.parameters
        assert params["type"] == "object"
        assert "param1" in params["properties"]
        assert "param2" in params["properties"]
        assert "param1" in params["required"]

    @pytest.mark.asyncio
    async def test_tool_execute(self):
        """测试工具执行"""
        tool = MockTool()
        result = await tool.execute(param1="test", param2=42)
        assert result == "executed with test and 42"

    @pytest.mark.asyncio
    async def test_tool_execute_with_defaults(self):
        """测试工具执行使用默认值"""
        tool = MockTool()
        result = await tool.execute(param1="test")
        assert result == "executed with test and 0"

    def test_tool_to_openai_format(self):
        """测试转换为OpenAI格式"""
        tool = MockTool()
        openai_format = tool.to_openai_format()
        assert openai_format["type"] == "function"
        assert openai_format["function"]["name"] == "test_tool"
        assert openai_format["function"]["description"] == "A test tool for unit testing"
        assert "param1" in openai_format["function"]["parameters"]["properties"]


class TestToolError:
    """测试ToolError异常"""

    def test_tool_error_creation(self):
        """测试ToolError创建"""
        error = ToolError("Test error message")
        assert str(error) == "Test error message"

    def test_tool_error_raise(self):
        """测试ToolError抛出"""
        with pytest.raises(ToolError) as exc_info:
            raise ToolError("Test error")
        assert str(exc_info.value) == "Test error"
