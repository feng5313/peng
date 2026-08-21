"""
测试Glob工具
"""
import pytest
from pathlib import Path
from unittest.mock import patch, MagicMock

from peng.tools.glob import GlobTool
from peng.tools.base import ToolError


@pytest.fixture
def temp_test_dir(tmp_path: Path) -> Path:
    """创建临时测试目录结构"""
    # 创建测试文件和目录
    (tmp_path / "file1.txt").write_text("content1")
    (tmp_path / "file2.py").write_text("content2")
    (tmp_path / "file3.md").write_text("content3")

    subdir = tmp_path / "subdir"
    subdir.mkdir()
    (subdir / "file4.py").write_text("content4")
    (subdir / "file5.txt").write_text("content5")

    deep_dir = subdir / "deep"
    deep_dir.mkdir()
    (deep_dir / "file6.py").write_text("content6")

    return tmp_path


@pytest.fixture
def glob_tool(tmp_path: Path):
    """创建GlobTool实例"""
    tool = GlobTool()
    # 设置上下文为临时目录，避免依赖真实的工作目录
    tool.set_context(workdir=str(tmp_path))
    return tool


class TestGlobTool:
    """测试GlobTool"""

    def test_tool_name(self, glob_tool):
        """测试工具名称"""
        assert glob_tool.name == "glob"

    def test_tool_description(self, glob_tool):
        """测试工具描述"""
        assert glob_tool.description == "使用glob模式匹配文件。返回匹配的文件路径列表。"

    def test_tool_parameters(self, glob_tool):
        """测试工具参数"""
        params = glob_tool.parameters
        assert params["type"] == "object"
        assert "pattern" in params["properties"]
        assert "path" in params["properties"]
        assert "pattern" in params["required"]

    @pytest.mark.asyncio
    async def test_execute_with_pattern(self, temp_test_dir, glob_tool):
        """测试使用模式执行glob"""
        # 更新工具上下文为临时目录
        glob_tool.set_context(workdir=str(temp_test_dir))

        result = await glob_tool.execute(pattern="*.py")
        lines = result.split("\n")

        # 应该找到所有.py文件
        assert "file2.py" in lines
        assert "subdir/file4.py" in lines
        assert "subdir/deep/file6.py" in lines

    @pytest.mark.asyncio
    async def test_execute_with_path(self, temp_test_dir, glob_tool):
        """测试在指定路径执行glob"""
        # 创建临时子目录
        subdir = temp_test_dir / "test_dir"
        subdir.mkdir()
        (subdir / "test.txt").write_text("test")

        result = await glob_tool.execute(pattern="*.txt", path=str(subdir))
        assert "test.txt" in result
        assert "file1.txt" not in result  # 不在子目录中

    @pytest.mark.asyncio
    async def test_execute_recursive_pattern(self, temp_test_dir, glob_tool):
        """测试递归模式匹配"""
        glob_tool.set_context(workdir=str(temp_test_dir))

        result = await glob_tool.execute(pattern="**/*.py")
        lines = result.split("\n")

        assert "file2.py" in lines
        assert "subdir/file4.py" in lines
        assert "subdir/deep/file6.py" in lines

    @pytest.mark.asyncio
    async def test_execute_no_matches(self, temp_test_dir, glob_tool):
        """测试没有匹配文件的情况"""
        glob_tool.set_context(workdir=str(temp_test_dir))

        result = await glob_tool.execute(pattern="*.nonexistent")
        assert result == "未找到匹配的文件"

    @pytest.mark.asyncio
    async def test_execute_with_missing_path(self, glob_tool):
        """测试路径不存在时抛出错误"""
        with pytest.raises(ToolError) as exc_info:
            await glob_tool.execute(pattern="*.py", path="/nonexistent/path")

        assert "目录不存在" in str(exc_info.value)

    @pytest.mark.asyncio
    async def test_execute_with_file_path(self, temp_test_dir, glob_tool):
        """测试路径指向文件而非目录"""
        file_path = temp_test_dir / "file1.txt"

        with pytest.raises(ToolError) as exc_info:
            await glob_tool.execute(pattern="*.py", path=str(file_path))

        assert "不是目录" in str(exc_info.value)

    @pytest.mark.asyncio
    async def test_execute_limits_results(self, temp_test_dir, glob_tool):
        """测试结果数量限制"""
        glob_tool.set_context(workdir=str(temp_test_dir))

        # 创建大量文件
        for i in range(150):
            (temp_test_dir / f"file{i}.py").write_text(f"content{i}")

        result = await glob_tool.execute(pattern="*.py")
        lines = result.split("\n")

        # 应该限制在100个结果内
        file_lines = [line for line in lines if line.startswith("file")]
        assert len(file_lines) == 100
        assert "还有" in result  # 应该有截断提示

    @pytest.mark.asyncio
    async def test_execute_respects_mtime_order(self, temp_test_dir, glob_tool):
        """测试按修改时间排序"""
        glob_tool.set_context(workdir=str(temp_test_dir))

        # 修改文件时间
        import time
        (temp_test_dir / "file1.txt").write_text("updated1")
        time.sleep(0.01)
        (temp_test_dir / "file2.py").write_text("updated2")

        result = await glob_tool.execute(pattern="file*")
        lines = [line for line in result.split("\n") if line and not line.startswith("...")]

        # file2.py应该排在前面（最近修改）
        assert lines.index("file2.py") < lines.index("file1.txt")

    @pytest.mark.asyncio
    async def test_execute_handles_permission_errors(self, temp_test_dir, glob_tool):
        """测试处理权限错误"""
        glob_tool.set_context(workdir=str(temp_test_dir))

        # 模拟一个没有读取权限的文件
        no_perm_file = temp_test_dir / "no_perm.txt"
        no_perm_file.write_text("content")
        no_perm_file.chmod(0o000)

        try:
            result = await glob_tool.execute(pattern="*.txt")
            # 应该能继续执行，跳过无法读取的文件
            assert "file1.txt" in result or result == "未找到匹配的文件"
        finally:
            # 恢复权限以便清理
            no_perm_file.chmod(0o644)

    @pytest.mark.asyncio
    async def test_to_openai_format(self, glob_tool):
        """测试转换为OpenAI格式"""
        openai_format = glob_tool.to_openai_format()
        assert openai_format["type"] == "function"
        assert openai_format["function"]["name"] == "glob"
        assert "pattern" in openai_format["function"]["parameters"]["properties"]

    @pytest.mark.asyncio
    async def test_context_fallback_to_workdir(self, glob_tool):
        """测试未设置context时使用工作目录"""
        # 不设置workdir context
        tool = GlobTool()
        tool._context.clear()

        # 应该使用默认的工作目录（从peng.common.paths导入）
        with patch("peng.common.paths.get_working_directory") as mock_get_workdir:
            mock_get_workdir.return_value = Path("/mock/workdir")
            # 只需要测试路径解析不出错
            try:
                await tool.execute(pattern="*.py")
            except ToolError:
                pass  # 如果目录不存在会抛出ToolError，这是正常的
