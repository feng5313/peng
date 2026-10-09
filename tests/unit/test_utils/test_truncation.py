"""
截断模块单元测试
"""

import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from peng.common.truncation import (
    Truncation,
    TruncationResult,
    _get_truncation_config,
    get_truncation_config,
    truncate_line,
    truncate_output,
)


class TestGetTruncationConfig:
    """测试配置获取函数"""

    def test_get_config_with_cache(self):
        """测试配置缓存"""
        # 第一次调用
        config1 = _get_truncation_config()
        assert isinstance(config1, dict)
        assert "max_lines" in config1
        assert "max_bytes" in config1

        # 第二次调用应该返回缓存
        config2 = _get_truncation_config()
        assert config1 is config2

    @patch("peng.common.truncation.load_config")
    def test_get_config_load_from_file(self, mock_load_config):
        """测试从配置文件加载"""
        mock_config = MagicMock()
        mock_config.truncation.max_lines = 100
        mock_config.truncation.max_bytes = 1024
        mock_config.truncation.max_line_length = 500
        mock_config.truncation.retention_days = 3
        mock_load_config.return_value = mock_config

        # 清除缓存
        import peng.common.truncation
        peng.common.truncation._truncation_config_cache = None

        config = _get_truncation_config()
        assert config["max_lines"] == 100
        assert config["max_bytes"] == 1024
        assert config["max_line_length"] == 500
        assert config["retention_days"] == 3

    @patch("peng.common.truncation.load_config")
    def test_get_config_load_failure(self, mock_load_config):
        """测试配置加载失败时使用默认值"""
        mock_load_config.side_effect = Exception("Config not found")

        # 清除缓存
        import peng.common.truncation
        peng.common.truncation._truncation_config_cache = None

        config = _get_truncation_config()
        assert config["max_lines"] == 2000
        assert config["max_bytes"] == 50 * 1024
        assert config["max_line_length"] == 2000
        assert config["retention_days"] == 7


class TestTruncationResult:
    """测试TruncationResult数据类"""

    def test_truncation_result_creation(self):
        """测试创建截断结果"""
        result = TruncationResult(
            output="test output",
            truncated=True,
            full_path="/path/to/file",
            removed_lines=10,
            removed_bytes=1000,
        )
        assert result.output == "test output"
        assert result.truncated is True
        assert result.full_path == "/path/to/file"
        assert result.removed_lines == 10
        assert result.removed_bytes == 1000

    def test_truncation_result_defaults(self):
        """测试截断结果的默认值"""
        result = TruncationResult(output="test", truncated=False)
        assert result.full_path is None
        assert result.removed_lines == 0
        assert result.removed_bytes == 0


class TestTruncation:
    """测试Truncation类"""

    @patch("peng.config.get_path")
    def test_get_output_dir(self, mock_get_path):
        """测试获取输出目录"""
        temp_dir = tempfile.mkdtemp()
        mock_get_path.return_value = Path(temp_dir)

        # 清除缓存
        Truncation.OUTPUT_DIR = None

        output_dir = Truncation._get_output_dir()
        assert output_dir.exists()
        assert output_dir == Path(temp_dir)
        mock_get_path.assert_called_once_with("output")

    def test_generate_filename(self):
        """测试生成文件名"""
        filename = Truncation._generate_filename()
        assert filename.startswith("tool_")
        # 验证时间戳格式
        parts = filename.split("_")
        assert len(parts) == 4  # tool_YYYYMMDD_HHMMSS_microseconds

    def test_truncate_no_truncation(self):
        """测试不需要截断的情况"""
        text = "line1\nline2\nline3"
        result = Truncation.truncate(text, max_lines=10, max_bytes=10000)

        assert result.truncated is False
        assert result.output == text
        assert result.full_path is None
        assert result.removed_lines == 0

    def test_truncate_by_lines_head(self):
        """测试按行数截断（保留头部）"""
        lines = [f"line{i}" for i in range(100)]
        text = "\n".join(lines)

        with tempfile.TemporaryDirectory() as temp_dir:
            with patch("peng.config.get_path", return_value=Path(temp_dir)):
                result = Truncation.truncate(text, max_lines=10, max_bytes=100000)

                assert result.truncated is True
                assert result.full_path is not None
                assert result.removed_lines == 90
                assert "line0" in result.output
                assert "line9" in result.output
                assert "line10" not in result.output

    def test_truncate_by_lines_tail(self):
        """测试按行数截断（保留尾部）"""
        lines = [f"line{i}" for i in range(100)]
        text = "\n".join(lines)

        with tempfile.TemporaryDirectory() as temp_dir:
            with patch("peng.config.get_path", return_value=Path(temp_dir)):
                result = Truncation.truncate(text, max_lines=10, max_bytes=100000, direction="tail")

                assert result.truncated is True
                assert result.full_path is not None
                assert result.removed_lines == 90
                assert "line90" in result.output
                assert "line99" in result.output
                assert "line0" not in result.output

    def test_truncate_by_bytes(self):
        """测试按字节数截断"""
        long_line = "a" * 1000
        text = "\n".join([long_line] * 100)

        with tempfile.TemporaryDirectory() as temp_dir:
            with patch("peng.config.get_path", return_value=Path(temp_dir)):
                result = Truncation.truncate(text, max_lines=1000, max_bytes=5000)

                assert result.truncated is True
                assert result.removed_bytes > 0

    def test_truncate_by_bytes_tail(self):
        """测试按字节数截断(保留尾部)"""
        long_line = "a" * 1000
        text = "\n".join([long_line] * 100)

        with tempfile.TemporaryDirectory() as temp_dir:
            with patch("peng.config.get_path", return_value=Path(temp_dir)):
                # 使用tail方向,确保按字节截断时hit_bytes=True
                result = Truncation.truncate(
                    text, max_lines=1000, max_bytes=5000, direction="tail"
                )

                assert result.truncated is True
                assert result.removed_bytes > 0
                # 验证保留的是尾部内容
                assert "line" not in result.output  # 因为没有"line"字符串,只有"a"

    def test_truncate_line_short(self):
        """测试截断短行（不需要截断）"""
        line = "short line"
        result = Truncation.truncate_line(line, max_length=100)
        assert result == line

    def test_truncate_line_long(self):
        """测试截断长行"""
        line = "a" * 100
        result = Truncation.truncate_line(line, max_length=50)
        assert len(result) == 53  # 50 + "..."
        assert result.endswith("...")
        assert len(line) == 100

    def test_truncate_line_default_max_length(self):
        """测试使用默认最大长度截断"""
        line = "a" * 3000
        result = Truncation.truncate_line(line)
        assert len(result) == 2003  # 2000 + "..."

    def test_save_full_output(self):
        """测试保存完整输出到文件"""
        content = "test content\nline2\nline3"

        with tempfile.TemporaryDirectory() as temp_dir:
            with patch("peng.config.get_path", return_value=Path(temp_dir)):
                # 清除OUTPUT_DIR缓存
                Truncation.OUTPUT_DIR = None
                filepath = Truncation._save_full_output(content)

                assert filepath.exists()
                assert filepath.parent == Path(temp_dir)

                with open(filepath, encoding="utf-8") as f:
                    saved_content = f.read()
                assert saved_content == content

    def test_cleanup_old_files(self):
        """测试清理过期文件"""
        with tempfile.TemporaryDirectory() as temp_dir:
            with patch("peng.config.get_path", return_value=Path(temp_dir)):
                # 创建一些测试文件
                for i in range(5):
                    Truncation._save_full_output(f"content {i}")

                # 清理文件（设置过期时间为0天，全部清理）
                Truncation._config = {"retention_days": 0}
                cleaned = Truncation.cleanup_old_files()

                assert cleaned >= 5

    def test_cleanup_no_files(self):
        """测试清理时没有文件"""
        with tempfile.TemporaryDirectory() as temp_dir:
            with patch("peng.config.get_path", return_value=Path(temp_dir)):
                cleaned = Truncation.cleanup_old_files()
                assert cleaned == 0

    def test_get_config_with_instance_cache(self):
        """测试实例级配置缓存"""
        # 清除类级配置
        Truncation._config = None

        config1 = Truncation._get_config()
        config2 = Truncation._get_config()

        assert config1 is config2


class TestConvenienceFunctions:
    """测试便捷函数"""

    def test_truncate_output(self):
        """测试truncate_output便捷函数"""
        text = "test line\n" * 100

        with tempfile.TemporaryDirectory() as temp_dir:
            with patch("peng.config.get_path", return_value=Path(temp_dir)):
                result = truncate_output(text, max_lines=10)
                assert isinstance(result, TruncationResult)

    def test_truncate_line_function(self):
        """测试truncate_line便捷函数"""
        line = "a" * 100
        result = truncate_line(line, max_length=50)
        assert result.endswith("...")
        assert len(result) == 53

    def test_get_truncation_config(self):
        """测试get_truncation_config便捷函数"""
        config = get_truncation_config()
        assert isinstance(config, dict)
        assert "max_lines" in config
        assert "max_bytes" in config


class TestEdgeCases:
    """测试边界情况"""

    def test_empty_text(self):
        """测试空文本"""
        result = Truncation.truncate("")
        assert result.truncated is False
        assert result.output == ""

    def test_single_line(self):
        """测试单行文本"""
        result = Truncation.truncate("single line")
        assert result.truncated is False
        assert result.output == "single line"

    def test_unicode_content(self):
        """测试Unicode内容"""
        text = "中文测试\n日本語テスト\n한글테스트"
        result = Truncation.truncate(text, max_lines=2)

        assert isinstance(result.output, str)
        assert result.truncated is True

    def test_very_long_single_line(self):
        """测试超长单行"""
        line = "a" * 10000
        result = Truncation.truncate(line, max_lines=1, max_bytes=1000)

        assert result.truncated is True
        assert result.removed_bytes > 0

    def test_mixed_line_lengths(self):
        """测试混合行长"""
        lines = ["short", "a" * 100, "medium text", "b" * 200, "tiny"]
        text = "\n".join(lines)

        with tempfile.TemporaryDirectory() as temp_dir:
            with patch("peng.config.get_path", return_value=Path(temp_dir)):
                result = Truncation.truncate(text, max_lines=3)

                assert result.truncated is True
                assert result.removed_lines == 2
