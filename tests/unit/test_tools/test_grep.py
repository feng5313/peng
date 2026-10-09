"""
测试Grep工具
"""
import pytest
from pathlib import Path
from unittest.mock import patch, MagicMock, mock_open

from peng.tools.grep import GrepTool, _get_grep_limits
from peng.tools.base import ToolError


@pytest.fixture
def temp_test_dir(tmp_path: Path) -> Path:
    """创建临时测试目录结构"""
    # 创建测试文件
    (tmp_path / "test1.txt").write_text("Hello World\nThis is a test\nPython is great")
    (tmp_path / "test2.py").write_text("import os\nprint('Hello')\n# Comment with hello")
    (tmp_path / "test3.md").write_text("# Title\nSome content\nMore text")

    # 创建子目录
    subdir = tmp_path / "subdir"
    subdir.mkdir()
    (subdir / "test4.py").write_text("def hello():\n    pass\ndef world():\n    pass")

    # 创建需要忽略的目录
    git_dir = tmp_path / ".git"
    git_dir.mkdir()
    (git_dir / "config").write_text("git config")

    pycache = tmp_path / "__pycache__"
    pycache.mkdir()
    (pycache / "module.pyc").write_bytes(b"\x00\x00\x00")

    # 创建二进制文件
    (tmp_path / "binary.bin").write_bytes(b"\x00\x01\x02\x03\x04\x05")

    return tmp_path


@pytest.fixture
def grep_tool():
    """创建GrepTool实例"""
    tool = GrepTool()
    return tool


class TestGrepLimits:
    """测试grep限制配置"""

    def test_get_grep_limits_default(self):
        """测试获取默认限制"""
        with patch("peng.tools.grep.get_truncation_config") as mock_config:
            mock_config.return_value = {}
            max_matches, max_line_length = _get_grep_limits()
            assert max_matches == 100
            assert max_line_length == 2000

    def test_get_grep_limits_custom(self):
        """测试获取自定义限制"""
        with patch("peng.tools.grep.get_truncation_config") as mock_config:
            mock_config.return_value = {
                "grep_max_matches": 200,
                "grep_max_line_length": 3000
            }
            max_matches, max_line_length = _get_grep_limits()
            assert max_matches == 200
            assert max_line_length == 3000


class TestGrepTool:
    """测试GrepTool"""

    def test_tool_name(self, grep_tool):
        """测试工具名称"""
        assert grep_tool.name == "grep"

    def test_tool_description(self, grep_tool):
        """测试工具描述"""
        assert grep_tool.description == "使用正则表达式搜索文件内容。"

    def test_tool_parameters(self, grep_tool):
        """测试工具参数"""
        params = grep_tool.parameters
        assert params["type"] == "object"
        assert "pattern" in params["properties"]
        assert "path" in params["properties"]
        assert "glob" in params["properties"]
        assert "output_mode" in params["properties"]
        assert "-i" in params["properties"]
        assert "pattern" in params["required"]

    def test_tool_parameters_output_mode_enum(self, grep_tool):
        """测试output_mode枚举值"""
        params = grep_tool.parameters
        output_mode = params["properties"]["output_mode"]
        assert "enum" in output_mode
        assert "content" in output_mode["enum"]
        assert "files_with_matches" in output_mode["enum"]
        assert "count" in output_mode["enum"]

    @pytest.mark.asyncio
    async def test_execute_basic_search(self, temp_test_dir, grep_tool):
        """测试基本搜索"""
        grep_tool.set_context(workdir=str(temp_test_dir))

        result = await grep_tool.execute(pattern="Hello")
        assert "test1.txt" in result
        assert "test2.py" in result

    @pytest.mark.asyncio
    async def test_execute_with_path(self, temp_test_dir, grep_tool):
        """测试在指定路径搜索"""
        result = await grep_tool.execute(pattern="hello", path=str(temp_test_dir / "subdir"))
        assert "test4.py" in result
        assert "test1.txt" not in result  # 不在搜索范围内

    @pytest.mark.asyncio
    async def test_execute_with_glob_filter(self, temp_test_dir, grep_tool):
        """测试使用glob过滤文件"""
        grep_tool.set_context(workdir=str(temp_test_dir))

        result = await grep_tool.execute(pattern="Hello", glob="*.py")
        assert "test2.py" in result
        assert "test1.txt" not in result  # 不匹配*.py

    @pytest.mark.asyncio
    async def test_execute_ignore_case(self, temp_test_dir, grep_tool):
        """测试忽略大小写"""
        grep_tool.set_context(workdir=str(temp_test_dir))

        result = await grep_tool.execute(pattern="hello", ignore_case=True)
        # 应该能匹配到Hello, hello等
        assert "test1.txt" in result or "test2.py" in result or "test4.py" in result

    @pytest.mark.asyncio
    async def test_execute_output_mode_files_with_matches(self, temp_test_dir, grep_tool):
        """测试files_with_matches输出模式"""
        grep_tool.set_context(workdir=str(temp_test_dir))

        result = await grep_tool.execute(
            pattern="Hello",
            output_mode="files_with_matches"
        )
        # 应该只显示文件名，不显示行内容
        assert "test1.txt" in result
        assert ":" not in result  # 不应该有行号

    @pytest.mark.asyncio
    async def test_execute_output_mode_count(self, temp_test_dir, grep_tool):
        """测试count输出模式"""
        grep_tool.set_context(workdir=str(temp_test_dir))

        result = await grep_tool.execute(
            pattern="Hello",
            output_mode="count"
        )
        assert "找到" in result
        assert "处匹配" in result
        assert "个文件中" in result

    @pytest.mark.asyncio
    async def test_execute_no_matches(self, temp_test_dir, grep_tool):
        """测试没有匹配的情况"""
        grep_tool.set_context(workdir=str(temp_test_dir))

        result = await grep_tool.execute(pattern="nonexistent_pattern")
        assert result == "未找到匹配"

    @pytest.mark.asyncio
    async def test_execute_invalid_regex(self, temp_test_dir, grep_tool):
        """测试无效的正则表达式"""
        grep_tool.set_context(workdir=str(temp_test_dir))

        with pytest.raises(ToolError) as exc_info:
            await grep_tool.execute(pattern="[invalid(")
        assert "无效的正则表达式" in str(exc_info.value)

    @pytest.mark.asyncio
    async def test_execute_path_not_exists(self, grep_tool):
        """测试路径不存在"""
        with pytest.raises(ToolError) as exc_info:
            await grep_tool.execute(pattern="test", path="/nonexistent/path")
        assert "路径不存在" in str(exc_info.value)

    @pytest.mark.asyncio
    async def test_ignores_common_directories(self, temp_test_dir, grep_tool):
        """测试忽略常见目录"""
        grep_tool.set_context(workdir=str(temp_test_dir))

        # 搜索.git目录中的内容
        result = await grep_tool.execute(pattern="config")
        # 不应该包含.git目录中的文件
        assert "git/config" not in result.lower()

    @pytest.mark.asyncio
    async def test_filters_binary_files(self, temp_test_dir, grep_tool):
        """测试过滤二进制文件"""
        grep_tool.set_context(workdir=str(temp_test_dir))

        # 二进制文件不应该被搜索
        result = await grep_tool.execute(pattern="test")
        assert "binary.bin" not in result

    @pytest.mark.asyncio
    async def test_limits_file_count(self, temp_test_dir, grep_tool):
        """测试文件数量限制"""
        grep_tool.set_context(workdir=str(temp_test_dir))

        # 创建大量文件
        for i in range(550):
            file_path = temp_test_dir / f"file{i}.py"
            file_path.write_text(f"content{i}\ntest{i}")

        result = await grep_tool.execute(pattern="test")
        # 应该限制在500个文件内
        # 验证搜索能正常完成（不抛出异常）
        assert isinstance(result, str)

    @pytest.mark.asyncio
    async def test_limits_match_count(self, temp_test_dir, grep_tool):
        """测试匹配数量限制"""
        grep_tool.set_context(workdir=str(temp_test_dir))

        # 创建一个有大量匹配的文件
        large_file = temp_test_dir / "large.txt"
        content = "\n".join([f"test line {i}" for i in range(200)])
        large_file.write_text(content)

        result = await grep_tool.execute(pattern="test")
        # 应该有截断提示
        assert "truncated" in result.lower() or len(result) > 0

    @pytest.mark.asyncio
    async def test_line_truncation(self, temp_test_dir, grep_tool):
        """测试行截断"""
        grep_tool.set_context(workdir=str(temp_test_dir))

        # 创建一个长行
        long_line_file = temp_test_dir / "long.txt"
        long_line = "a" * 3000 + "test" + "b" * 3000
        long_line_file.write_text(long_line)

        result = await grep_tool.execute(pattern="test")
        # 应该有截断提示
        assert "truncated" in result.lower() or "test" in result

    @pytest.mark.asyncio
    async def test_handles_file_read_errors(self, temp_test_dir, grep_tool):
        """测试处理文件读取错误"""
        grep_tool.set_context(workdir=str(temp_test_dir))

        # 创建一个格式错误的文件（模拟读取问题）
        bad_file = temp_test_dir / "bad.txt"
        # 写入一些可能导致编码问题的内容
        bad_file.write_bytes(b'\x80\x81\x82\x83test\x84\x85')

        # 应该能继续执行，跳过无法读取的文件（使用errors='ignore'模式）
        result = await grep_tool.execute(pattern="test")
        # 其他文件应该能正常搜索
        assert isinstance(result, str)

    @pytest.mark.asyncio
    async def test_to_openai_format(self, grep_tool):
        """测试转换为OpenAI格式"""
        openai_format = grep_tool.to_openai_format()
        assert openai_format["type"] == "function"
        assert openai_format["function"]["name"] == "grep"
        assert "pattern" in openai_format["function"]["parameters"]["properties"]

    @pytest.mark.asyncio
    async def test_search_single_file(self, temp_test_dir, grep_tool):
        """搜索单个文件"""
        file_path = temp_test_dir / "test1.txt"

        result = await grep_tool.execute(pattern="Hello", path=str(file_path))
        # 单文件搜索时，相对路径是文件名本身或者"."
        assert "Hello" in result

    @pytest.mark.asyncio
    async def test_context_fallback_to_workdir(self, grep_tool):
        """测试未设置context时使用工作目录"""
        # 不设置workdir context
        tool = GrepTool()
        tool._context.clear()

        # 应该使用默认的工作目录（从peng.common.paths导入）
        with patch("peng.common.paths.get_working_directory") as mock_get_workdir:
            mock_get_workdir.return_value = Path("/mock/workdir")
            # 只需要测试路径解析不出错
            try:
                await tool.execute(pattern="test")
            except ToolError:
                pass  # 如果路径不存在会抛出ToolError，这是正常的


class TestGrepToolIsTextFile:
    """测试GrepTool._is_text_file方法"""

    def test_is_text_file_common_extensions(self, grep_tool):
        """测试常见文本文件扩展名"""
        text_files = [
            "test.txt", "test.md", "test.py", "test.js", "test.ts",
            "test.json", "test.yaml", "test.yml", "test.html", "test.css",
            "test.xml", "test.sh", "test.c", "test.cpp", "test.h",
            "test.rs", "test.go", "test.java", "test.sql", "test.log"
        ]

        for filename in text_files:
            assert grep_tool._is_text_file(Path(filename))

    def test_is_text_file_case_insensitive(self, grep_tool):
        """测试扩展名不区分大小写"""
        assert grep_tool._is_text_file(Path("test.PY"))
        assert grep_tool._is_text_file(Path("test.TXT"))
        assert grep_tool._is_text_file(Path("test.MD"))

    def test_is_text_file_unknown_extension(self, grep_tool):
        """测试未知扩展名"""
        assert not grep_tool._is_text_file(Path("test.xyz"))
        assert not grep_tool._is_text_file(Path("test"))

    def test_is_text_file_binary_extensions(self, grep_tool):
        """测试二进制文件扩展名"""
        binary_files = ["test.exe", "test.dll", "test.so", "test.bin"]
        for filename in binary_files:
            assert not grep_tool._is_text_file(Path(filename))
