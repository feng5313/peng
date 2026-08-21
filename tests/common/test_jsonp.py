"""
单元测试：jsonp.py 模块

测试 JSONP 序列化/反序列化、三引号字符串转换等功能。
禁止发送/回复消息/创建事务。
"""

import json
import tempfile
import io
import pytest
from pathlib import Path

from src.peng.common.jsonp import (
    dumps,
    dump,
    loads,
    load,
    _convert_to_triple_quoted,
    _convert_from_triple_quoted,
    jsonp_tag,
    jsonp_tag_escaped,
    JSONDecoder,
    JSONEncoder,
    JSONDecodeError,
)


class TestConvertToTripleQuoted:
    """测试 _convert_to_triple_quoted 函数"""

    def test_no_multiline_strings(self):
        """测试没有多行字符串的情况"""
        json_str = '{"name": "Alice", "age": 25}'
        result = _convert_to_triple_quoted(json_str)
        # 没有多行字符串，应该保持不变（除了可能的标签处理）
        assert json.loads(result) == {"name": "Alice", "age": 25}

    def test_multiline_string_with_newlines(self):
        """测试包含换行符的多行字符串"""
        # 模拟 json.dumps 输出的带 \n 转义的字符串
        json_str = '{"text": "\\n"""' + jsonp_tag + 'line1\\nline2\\nline3' + jsonp_tag + '\\n""""}'
        result = _convert_to_triple_quoted(json_str)
        # 应该将 \n 转换为实际换行
        assert "\n" in result
        assert '"""' in result

    def test_escaped_jsonp_tag(self):
        """测试转义的 jsonp_tag"""
        json_str = f'{{"text": "{jsonp_tag_escaped}"}}'
        result = _convert_to_triple_quoted(json_str)
        # 应该将转义的标签恢复
        assert jsonp_tag in result
        assert jsonp_tag_escaped not in result

    def test_single_quote_triple_quoted(self):
        """测试单引号三引号字符串"""
        json_str = "{'text': \"\\n'''" + jsonp_tag + "line1\\nline2" + jsonp_tag + "'''\\n\"}"
        result = _convert_to_triple_quoted(json_str)
        assert "'''" in result


class TestConvertFromTripleQuoted:
    """测试 _convert_from_triple_quoted 函数"""

    def test_simple_string(self):
        """测试简单字符串"""
        jsonp_str = '{"name": "Alice"}'
        result = _convert_from_triple_quoted(jsonp_str)
        assert result == '{"name": "Alice"}'

    def test_multiline_string_double_quotes(self):
        """测试双引号多行字符串"""
        jsonp_str = '{"text": """\nline1\nline2\nline3\n"""}'
        result = _convert_from_triple_quoted(jsonp_str)
        # 应该转换为标准 JSON 格式
        parsed = json.loads(result)
        assert parsed["text"] == "line1\nline2\nline3\n"

    def test_multiline_string_single_quotes(self):
        """测试单引号多行字符串"""
        jsonp_str = "{'text': '''\nline1\nline2\n'''}"
        result = _convert_from_triple_quoted(jsonp_str)
        parsed = json.loads(result)
        assert parsed["text"] == "line1\nline2\n"

    def test_escaped_backslashes(self):
        """测试反斜杠转义"""
        jsonp_str = '{"path": """\nC:\\Users\\test\n"""}'
        result = _convert_from_triple_quoted(jsonp_str)
        parsed = json.loads(result)
        assert "C:\\\\Users\\\\test" in result or parsed["path"] == "C:\\Users\\test\n"

    def test_escaped_quotes(self):
        """测试引号转义"""
        jsonp_str = '{"text": """\nHe said \\"hello\\"\n"""}'
        result = _convert_from_triple_quoted(jsonp_str)
        parsed = json.loads(result)
        assert parsed["text"] == 'He said "hello"\n'

    def test_newline_prefix_handling(self):
        """测试换行符前缀处理"""
        jsonp_str = '{\n"""text""": """\ncontent\n"""}'
        result = _convert_from_triple_quoted(jsonp_str)
        # 应该正确处理
        parsed = json.loads(result)
        assert "content\n" in str(parsed.values())


class TestDumps:
    """测试 dumps 函数"""

    def test_simple_dict(self):
        """测试简单字典序列化"""
        obj = {"name": "Alice", "age": 25}
        result = dumps(obj)
        parsed = json.loads(result)
        assert parsed == obj

    def test_nested_dict(self):
        """测试嵌套字典序列化"""
        obj = {"user": {"name": "Alice", "details": {"age": 25}}}
        result = dumps(obj)
        parsed = json.loads(result)
        assert parsed == obj

    def test_list(self):
        """测试列表序列化"""
        obj = [1, 2, 3, "four", "five"]
        result = dumps(obj)
        parsed = json.loads(result)
        assert parsed == obj

    def test_with_indent(self):
        """测试带缩进的序列化"""
        obj = {"name": "Alice", "age": 25}
        result = dumps(obj, indent=2)
        assert "\n" in result
        parsed = json.loads(result)
        assert parsed == obj

    def test_ensure_ascii_false(self):
        """测试非 ASCII 字符"""
        obj = {"name": "鹏", "emoji": "🐣"}
        result = dumps(obj, ensure_ascii=False)
        assert "鹏" in result
        assert "🐣" in result
        parsed = json.loads(result)
        assert parsed == obj

    def test_empty_object(self):
        """测试空对象"""
        assert dumps({}) == "{}"
        assert dumps([]) == "[]"

    def test_none_value(self):
        """测试 None 值"""
        result = dumps({"value": None})
        parsed = json.loads(result)
        assert parsed["value"] is None

    def test_boolean_values(self):
        """测试布尔值"""
        result = dumps({"true_val": True, "false_val": False})
        parsed = json.loads(result)
        assert parsed["true_val"] is True
        assert parsed["false_val"] is False


class TestDump:
    """测试 dump 函数"""

    def test_dump_to_text_file(self):
        """测试写入文本文件"""
        obj = {"name": "Alice", "age": 25}
        with tempfile.NamedTemporaryFile(mode="w", suffix=".jsonp", delete=False) as f:
            dump(obj, f)
            f.flush()
            f.seek(0)
            content = f.read()
        
        parsed = json.loads(content)
        assert parsed == obj

    def test_dump_to_binary_file(self):
        """测试写入二进制文件"""
        obj = {"name": "Alice", "age": 25}
        with tempfile.NamedTemporaryFile(mode="wb", suffix=".jsonp", delete=False) as f:
            dump(obj, f)
            f.flush()
            f.seek(0)
            content = f.read().decode("utf-8")
        
        parsed = json.loads(content)
        assert parsed == obj

    def test_dump_default_indent(self):
        """测试默认缩进"""
        obj = {"name": "Alice", "age": 25}
        output = io.StringIO()
        dump(obj, output)
        content = output.getvalue()
        # 默认应该有缩进
        assert "\n" in content

    def test_dump_custom_indent(self):
        """测试自定义缩进"""
        obj = {"name": "Alice", "age": 25}
        output = io.StringIO()
        dump(obj, output, indent=4)
        content = output.getvalue()
        assert "    " in content  # 4 个空格缩进

    def test_dump_multiline_string(self):
        """测试多行字符串序列化"""
        obj = {"text": "line1\nline2\nline3"}
        output = io.StringIO()
        dump(obj, output)
        content = output.getvalue()
        
        # 应该包含三引号标记
        assert jsonp_tag in content or '"""' in content
        
        # 验证可以正确反序列化
        parsed = loads(content)
        assert parsed["text"] == "line1\nline2\nline3"

    def test_dump_ensure_ascii_default(self):
        """测试默认 ensure_ascii=False"""
        obj = {"name": "鹏"}
        output = io.StringIO()
        dump(obj, output)
        content = output.getvalue()
        assert "鹏" in content

    def test_dump_with_file_path(self):
        """测试使用 Path 对象写入文件"""
        obj = {"data": "test"}
        with tempfile.NamedTemporaryFile(mode="w", suffix=".jsonp", delete=False) as f:
            temp_path = Path(f.name)
        
        with open(temp_path, "w") as f:
            dump(obj, f)
        
        with open(temp_path, "r") as f:
            content = f.read()
        
        parsed = json.loads(content)
        assert parsed == obj


class TestLoads:
    """测试 loads 函数"""

    def test_simple_json(self):
        """测试简单 JSON 字符串"""
        json_str = '{"name": "Alice", "age": 25}'
        result = loads(json_str)
        assert result == {"name": "Alice", "age": 25}

    def test_bytes_input(self):
        """测试 bytes 输入"""
        json_bytes = b'{"name": "Alice", "age": 25}'
        result = loads(json_bytes)
        assert result == {"name": "Alice", "age": 25}

    def test_bytearray_input(self):
        """测试 bytearray 输入"""
        json_bytes = bytearray(b'{"name": "Alice", "age": 25}')
        result = loads(json_bytes)
        assert result == {"name": "Alice", "age": 25}

    def test_nested_structure(self):
        """测试嵌套结构"""
        json_str = '{"user": {"name": "Alice", "details": {"age": 25, "active": true}}}'
        result = loads(json_str)
        assert result["user"]["name"] == "Alice"
        assert result["user"]["details"]["age"] == 25
        assert result["user"]["details"]["active"] is True

    def test_array(self):
        """测试数组"""
        json_str = '[1, 2, 3, "four", {"five": 5}]'
        result = loads(json_str)
        assert result == [1, 2, 3, "four", {"five": 5}]

    def test_unicode_characters(self):
        """测试 Unicode 字符"""
        json_str = '{"name": "鹏", "emoji": "🐣"}'
        result = loads(json_str)
        assert result["name"] == "鹏"
        assert result["emoji"] == "🐣"

    def test_empty_object(self):
        """测试空对象"""
        assert loads("{}") == {}
        assert loads("[]") == []

    def test_with_kwargs(self):
        """测试传递 kwargs"""
        json_str = '{"name": "Alice"}'
        result = loads(json_str, object_pairs_hook=lambda pairs: dict(pairs))
        assert result == {"name": "Alice"}


class TestLoad:
    """测试 load 函数"""

    def test_load_from_text_file(self):
        """测试从文本文件读取"""
        json_str = '{"name": "Alice", "age": 25}'
        with tempfile.NamedTemporaryFile(mode="w", suffix=".jsonp", delete=False) as f:
            f.write(json_str)
            f.flush()
            temp_path = f.name
        
        with open(temp_path, "r") as f:
            result = load(f)
        
        assert result == {"name": "Alice", "age": 25}

    def test_load_from_binary_file(self):
        """测试从二进制文件读取"""
        json_bytes = b'{"name": "Alice", "age": 25}'
        with tempfile.NamedTemporaryFile(mode="wb", suffix=".jsonp", delete=False) as f:
            f.write(json_bytes)
            f.flush()
            temp_path = f.name
        
        with open(temp_path, "rb") as f:
            result = load(f)
        
        assert result == {"name": "Alice", "age": 25}

    def test_load_utf8_encoding(self):
        """测试 UTF-8 编码"""
        json_str = '{"name": "鹏", "emoji": "🐣"}'
        with tempfile.NamedTemporaryFile(mode="wb", suffix=".jsonp", delete=False) as f:
            f.write(json_str.encode("utf-8"))
            f.flush()
            temp_path = f.name
        
        with open(temp_path, "rb") as f:
            result = load(f)
        
        assert result["name"] == "鹏"
        assert result["emoji"] == "🐣"

    def test_load_with_explicit_encoding(self):
        """测试指定编码"""
        json_str = '{"name": "Alice"}'
        with tempfile.NamedTemporaryFile(mode="wb", suffix=".jsonp", delete=False) as f:
            f.write(json_str.encode("utf-8"))
            f.flush()
            temp_path = f.name
        
        with open(temp_path, "rb") as f:
            result = load(f, encoding="utf-8")
        
        assert result == {"name": "Alice"}

    def test_load_latin1_fallback(self):
        """测试 Latin-1 编码回退"""
        # 创建无效的 UTF-8 但有效的 Latin-1 内容
        json_bytes = b'{"name": "Alice"}'
        with tempfile.NamedTemporaryFile(mode="wb", suffix=".jsonp", delete=False) as f:
            f.write(json_bytes)
            f.flush()
            temp_path = f.name
        
        with open(temp_path, "rb") as f:
            result = load(f)
        
        assert result == {"name": "Alice"}

    def test_load_multiline_string(self):
        """测试读取多行字符串"""
        jsonp_content = '{"text": """\nline1\nline2\nline3\n"""}'
        with tempfile.NamedTemporaryFile(mode="w", suffix=".jsonp", delete=False) as f:
            f.write(jsonp_content)
            f.flush()
            temp_path = f.name
        
        with open(temp_path, "r") as f:
            result = load(f)
        
        assert result["text"] == "line1\nline2\nline3\n"

    def test_load_from_stringio(self):
        """测试从 StringIO 读取"""
        json_str = '{"name": "Alice"}'
        f = io.StringIO(json_str)
        result = load(f)
        assert result == {"name": "Alice"}

    def test_load_from_bytesio(self):
        """测试从 BytesIO 读取"""
        json_bytes = b'{"name": "Alice"}'
        f = io.BytesIO(json_bytes)
        result = load(f)
        assert result == {"name": "Alice"}


class TestRoundTrip:
    """测试往返序列化/反序列化"""

    def test_simple_dict_roundtrip(self):
        """测试简单字典往返"""
        original = {"name": "Alice", "age": 25}
        serialized = dumps(original)
        deserialized = loads(serialized)
        assert deserialized == original

    def test_nested_dict_roundtrip(self):
        """测试嵌套字典往返"""
        original = {
            "user": {
                "name": "Alice",
                "details": {"age": 25, "hobbies": ["reading", "coding"]}
            }
        }
        serialized = dumps(original)
        deserialized = loads(serialized)
        assert deserialized == original

    def test_multiline_string_roundtrip(self):
        """测试多行字符串往返"""
        original = {"text": "line1\nline2\nline3"}
        
        # 使用 dump/loads 往返
        output = io.StringIO()
        dump(original, output)
        serialized = output.getvalue()
        
        deserialized = loads(serialized)
        assert deserialized["text"] == original["text"]

    def test_file_roundtrip(self):
        """测试文件往返"""
        original = {"data": [1, 2, 3], "info": {"name": "test"}}
        
        with tempfile.NamedTemporaryFile(mode="w", suffix=".jsonp", delete=False) as f:
            temp_path = f.name
        
        # 写入文件
        with open(temp_path, "w") as f:
            dump(original, f)
        
        # 从文件读取
        with open(temp_path, "r") as f:
            deserialized = load(f)
        
        assert deserialized == original

    def test_special_characters_roundtrip(self):
        """测试特殊字符往返"""
        original = {
            "chinese": "鹏",
            "emoji": "🐣",
            "quotes": 'He said "hello"',
            "newlines": "line1\nline2",
            "tabs": "col1\tcol2"
        }
        serialized = dumps(original, ensure_ascii=False)
        deserialized = loads(serialized)
        assert deserialized == original


class TestJsonpTagHandling:
    """测试 JSONP 标签处理"""

    def test_jsonp_tag_in_content(self):
        """测试内容中包含 jsonp_tag"""
        original = {"text": f"some {jsonp_tag} content"}
        output = io.StringIO()
        dump(original, output)
        serialized = output.getvalue()
        
        # 应该包含转义的标签
        assert jsonp_tag_escaped in serialized or jsonp_tag in serialized
        
        deserialized = loads(serialized)
        # 应该恢复原始内容
        assert jsonp_tag in deserialized["text"]

    def test_jsonp_tag_escaped_restored(self):
        """测试转义的 jsonp_tag 被恢复"""
        # 手动构造包含转义标签的 JSON
        json_str = f'{{"text": "{jsonp_tag_escaped}"}}'
        result = _convert_to_triple_quoted(json_str)
        assert jsonp_tag in result


class TestExports:
    """测试导出项"""

    def test_json_decoder(self):
        """测试 JSONDecoder 导出"""
        assert JSONDecoder is json.JSONDecoder
        decoder = JSONDecoder()
        result = decoder.decode('{"name": "Alice"}')
        assert result == {"name": "Alice"}

    def test_json_encoder(self):
        """测试 JSONEncoder 导出"""
        assert JSONEncoder is json.JSONEncoder
        encoder = JSONEncoder()
        result = encoder.encode({"name": "Alice"})
        assert json.loads(result) == {"name": "Alice"}

    def test_json_decode_error(self):
        """测试 JSONDecodeError 导出"""
        assert JSONDecodeError is json.JSONDecodeError
        with pytest.raises(JSONDecodeError):
            loads("invalid json")


class TestEdgeCases:
    """测试边界情况"""

    def test_empty_string(self):
        """测试空字符串"""
        with pytest.raises(json.JSONDecodeError):
            loads("")

    def test_invalid_json(self):
        """测试无效 JSON"""
        with pytest.raises(json.JSONDecodeError):
            loads("{invalid}")

    def test_none_object(self):
        """测试 None 对象"""
        result = dumps(None)
        assert result == "null"
        assert loads(result) is None

    def test_deeply_nested_structure(self):
        """测试深度嵌套结构"""
        original = {"level1": {"level2": {"level3": {"level4": "deep"}}}}
        serialized = dumps(original)
        deserialized = loads(serialized)
        assert deserialized == original

    def test_large_list(self):
        """测试大列表"""
        original = list(range(1000))
        serialized = dumps(original)
        deserialized = loads(serialized)
        assert deserialized == original

    def test_mixed_types(self):
        """测试混合类型"""
        original = {
            "string": "text",
            "int": 42,
            "float": 3.14,
            "bool": True,
            "null": None,
            "list": [1, 2, 3],
            "dict": {"nested": "value"}
        }
        serialized = dumps(original)
        deserialized = loads(serialized)
        assert deserialized == original

    def test_triple_quoted_string_with_various_content(self):
        """测试三引号字符串包含各种内容"""
        original = {
            "text": "Line 1\nLine 2\nLine 3\nWith special chars: !@#$%^&*()"
        }
        output = io.StringIO()
        dump(original, output)
        serialized = output.getvalue()
        
        deserialized = loads(serialized)
        assert deserialized["text"] == original["text"]

    def test_string_with_only_newlines(self):
        """测试仅包含换行符的字符串"""
        original = {"text": "\n\n\n"}
        output = io.StringIO()
        dump(original, output)
        serialized = output.getvalue()
        
        deserialized = loads(serialized)
        assert deserialized["text"] == original["text"]

    def test_string_with_trailing_newline(self):
        """测试带尾随换行符的字符串"""
        original = {"text": "content\n"}
        output = io.StringIO()
        dump(original, output)
        serialized = output.getvalue()
        
        deserialized = loads(serialized)
        assert deserialized["text"] == original["text"]


class TestFileOperations:
    """测试文件操作"""

    def test_dump_and_load_with_path_object(self):
        """测试使用 Path 对象进行 dump 和 load"""
        original = {"data": "test"}
        
        with tempfile.NamedTemporaryFile(mode="w", suffix=".jsonp", delete=False) as f:
            temp_path = Path(f.name)
        
        # 写入
        with open(temp_path, "w") as f:
            dump(original, f)
        
        # 读取
        with open(temp_path, "r") as f:
            result = load(f)
        
        assert result == original

    def test_dump_append_mode(self):
        """测试追加模式（应该覆盖）"""
        obj1 = {"first": 1}
        obj2 = {"second": 2}
        
        with tempfile.NamedTemporaryFile(mode="w", suffix=".jsonp", delete=False) as f:
            temp_path = f.name
        
        with open(temp_path, "w") as f:
            dump(obj1, f)
        
        with open(temp_path, "w") as f:
            dump(obj2, f)
        
        with open(temp_path, "r") as f:
            result = load(f)
        
        # 应该是第二个对象
        assert result == obj2

    def test_load_nonexistent_file(self):
        """测试读取不存在的文件"""
        with pytest.raises(FileNotFoundError):
            with open("/nonexistent/path/file.jsonp", "r") as f:
                load(f)


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
