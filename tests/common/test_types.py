"""
types.py 的单元测试

测试 ToolCall 类和 MetaSingleton 元类
"""

import pytest
from peng.common.types import ToolCall, MetaSingleton


class TestMetaSingleton:
    """测试 MetaSingleton 元类"""

    def test_singleton_instance(self):
        """测试单例模式 - 同一类只创建一个实例"""
        
        class SingletonClass(metaclass=MetaSingleton):
            def __init__(self, value=None):
                self.value = value
        
        # 创建两个实例
        instance1 = SingletonClass(value=1)
        instance2 = SingletonClass(value=2)
        
        # 应该是同一个实例
        assert instance1 is instance2
        # 第二次创建不会改变已有实例的属性
        assert instance1.value == 1

    def test_multiple_classes_independent(self):
        """测试不同类的单例是独立的"""
        
        class ClassA(metaclass=MetaSingleton):
            pass
        
        class ClassB(metaclass=MetaSingleton):
            pass
        
        instance_a = ClassA()
        instance_b = ClassB()
        
        assert instance_a is not instance_b
        assert isinstance(instance_a, ClassA)
        assert isinstance(instance_b, ClassB)

    def test_singleton_with_args(self):
        """测试带参数的单例创建"""
        
        class ConfigSingleton(metaclass=MetaSingleton):
            def __init__(self, config=None):
                self.config = config or {}
        
        instance1 = ConfigSingleton(config={"key": "value1"})
        instance2 = ConfigSingleton(config={"key": "value2"})
        
        # 应该是同一个实例，使用第一次的参数
        assert instance1 is instance2
        assert instance1.config == {"key": "value1"}


class TestToolCall:
    """测试 ToolCall 数据类"""

    def test_toolcall_init(self):
        """测试 ToolCall 构造函数"""
        tool_call = ToolCall(
            id="call_123",
            name="test_function",
            arguments={"param1": "value1", "param2": 42}
        )
        
        assert tool_call.id == "call_123"
        assert tool_call.name == "test_function"
        assert tool_call.arguments == {"param1": "value1", "param2": 42}

    def test_toolcall_from_api_format_basic(self):
        """测试从 API 格式解析 - 基本场景"""
        api_data = {
            "id": "call_456",
            "function": {
                "name": "search_function",
                "arguments": '{"query": "test", "limit": 10}'
            }
        }
        
        tool_call = ToolCall.from_api_format(api_data)
        
        assert tool_call.id == "call_456"
        assert tool_call.name == "search_function"
        assert tool_call.arguments == {"query": "test", "limit": 10}

    def test_toolcall_from_api_format_with_dict_args(self):
        """测试从 API 格式解析 - 参数已经是字典的情况"""
        api_data = {
            "id": "call_789",
            "function": {
                "name": "analyze_function",
                "arguments": {"mode": "deep", "threshold": 0.5}
            }
        }
        
        tool_call = ToolCall.from_api_format(api_data)
        
        assert tool_call.id == "call_789"
        assert tool_call.name == "analyze_function"
        assert tool_call.arguments == {"mode": "deep", "threshold": 0.5}

    def test_toolcall_from_api_format_empty_data(self):
        """测试从 API 格式解析 - 空数据"""
        api_data = {}
        
        tool_call = ToolCall.from_api_format(api_data)
        
        assert tool_call.id == ""
        assert tool_call.name == ""
        assert tool_call.arguments == {}

    def test_toolcall_from_api_format_missing_function(self):
        """测试从 API 格式解析 - 缺少 function 字段"""
        api_data = {
            "id": "call_abc"
        }
        
        tool_call = ToolCall.from_api_format(api_data)
        
        assert tool_call.id == "call_abc"
        assert tool_call.name == ""
        assert tool_call.arguments == {}

    def test_toolcall_from_api_format_missing_arguments(self):
        """测试从 API 格式解析 - 缺少 arguments 字段"""
        api_data = {
            "id": "call_def",
            "function": {
                "name": "simple_function"
            }
        }
        
        tool_call = ToolCall.from_api_format(api_data)
        
        assert tool_call.id == "call_def"
        assert tool_call.name == "simple_function"
        assert tool_call.arguments == {}

    def test_toolcall_from_api_format_invalid_json(self):
        """测试从 API 格式解析 - 无效的 JSON 字符串"""
        api_data = {
            "id": "call_ghi",
            "function": {
                "name": "bad_json_function",
                "arguments": '{invalid json}'
            }
        }
        
        with pytest.raises(Exception):
            ToolCall.from_api_format(api_data)

    def test_toolcall_to_api_format(self):
        """测试转换为 API 格式"""
        tool_call = ToolCall(
            id="call_xyz",
            name="export_function",
            arguments={"format": "csv", "include_header": True}
        )
        
        api_format = tool_call.to_api_format()
        
        assert api_format["id"] == "call_xyz"
        assert api_format["type"] == "function"
        assert api_format["function"]["name"] == "export_function"
        # arguments 应该是 JSON 字符串
        import json
        assert api_format["function"]["arguments"] == '{"format": "csv", "include_header": true}'
        # 验证可以反序列化
        parsed_args = json.loads(api_format["function"]["arguments"])
        assert parsed_args == {"format": "csv", "include_header": True}

    def test_toolcall_to_api_format_unicode(self):
        """测试转换为 API 格式 - 包含 Unicode 字符"""
        tool_call = ToolCall(
            id="call_unicode",
            name="chinese_function",
            arguments={"message": "你好，世界！", "emoji": "🐣"}
        )
        
        api_format = tool_call.to_api_format()
        
        assert api_format["id"] == "call_unicode"
        assert api_format["function"]["name"] == "chinese_function"
        # 确保 Unicode 字符被保留
        assert "你好" in api_format["function"]["arguments"]
        assert "🐣" in api_format["function"]["arguments"]

    def test_toolcall_roundtrip(self):
        """测试往返转换 - from_api_format -> to_api_format -> from_api_format"""
        original_api_data = {
            "id": "call_roundtrip",
            "function": {
                "name": "roundtrip_function",
                "arguments": '{"data": [1, 2, 3], "nested": {"key": "value"}}'
            }
        }
        
        # 第一次转换
        tool_call = ToolCall.from_api_format(original_api_data)
        
        # 转换为 API 格式
        api_format = tool_call.to_api_format()
        
        # 再次转换回来
        tool_call2 = ToolCall.from_api_format(api_format)
        
        # 验证数据一致性
        assert tool_call.id == tool_call2.id
        assert tool_call.name == tool_call2.name
        assert tool_call.arguments == tool_call2.arguments

    def test_toolcall_with_complex_arguments(self):
        """测试复杂参数"""
        tool_call = ToolCall(
            id="call_complex",
            name="complex_function",
            arguments={
                "list": [1, 2, 3],
                "dict": {"nested": {"deep": "value"}},
                "null": None,
                "boolean": False,
                "number": 3.14159
            }
        )
        
        api_format = tool_call.to_api_format()
        
        import json
        parsed = json.loads(api_format["function"]["arguments"])
        assert parsed["list"] == [1, 2, 3]
        assert parsed["dict"] == {"nested": {"deep": "value"}}
        assert parsed["null"] is None
        assert parsed["boolean"] is False
        assert abs(parsed["number"] - 3.14159) < 0.00001
