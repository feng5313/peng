"""
测试 colors.py 模块
"""

import json
from pathlib import Path
from unittest.mock import Mock, patch, MagicMock

import pytest

from peng.common.colors import (
    ANSI_COLORS,
    Colors,
    colorize,
    get_bash_colors,
    get_color_code,
    get_colors,
    get_fly_colors,
    get_file_colors,
    get_message_colors,
    get_task_colors,
    get_web_colors,
    reset_code,
)


class TestColorize:
    """测试 colorize 函数"""

    def test_colorize_with_valid_color(self):
        """测试使用有效颜色"""
        result = colorize("Hello", "red")
        expected = f"{ANSI_COLORS['red']}Hello{ANSI_COLORS['reset']}"
        assert result == expected

    def test_colorize_with_none_color(self):
        """测试使用 none 颜色"""
        result = colorize("Hello", "none")
        assert result == "Hello"

    def test_colorize_with_empty_color(self):
        """测试使用空颜色"""
        result = colorize("Hello", "")
        assert result == "Hello"

    def test_colorize_with_invalid_color(self):
        """测试使用无效颜色"""
        result = colorize("Hello", "invalid_color")
        assert result == "Hello"


class TestGetColorCode:
    """测试 get_color_code 函数"""

    def test_get_ansi_color(self):
        """测试获取 ANSI 颜色代码"""
        result = get_color_code("red")
        assert result == ANSI_COLORS["red"]

    def test_get_none_color(self):
        """测试获取 none 颜色"""
        result = get_color_code("none")
        assert result == ""

    def test_get_empty_color(self):
        """测试获取空颜色"""
        result = get_color_code("")
        assert result == ""

    def test_get_rgb_color(self):
        """测试获取 RGB 格式颜色"""
        result = get_color_code("rgb:140,170,140")
        assert result == "\033[38;2;140;170;140m"

    def test_get_rgb_invalid_format(self):
        """测试无效的 RGB 格式"""
        result = get_color_code("rgb:invalid")
        assert result == ""

    def test_get_color256(self):
        """测试获取 256 色格式"""
        result = get_color_code("color256:108")
        assert result == "\033[38;5;108m"

    def test_get_color256_invalid_number(self):
        """测试无效的 256 色数字"""
        result = get_color_code("color256:invalid")
        assert result == ""

    def test_get_color256_out_of_range(self):
        """测试超出范围的 256 色数字"""
        result = get_color_code("color256:999")
        assert result == ""

    def test_get_invalid_color(self):
        """测试获取无效颜色"""
        result = get_color_code("invalid_color")
        assert result == ""


class TestResetCode:
    """测试 reset_code 函数"""

    def test_reset_code(self):
        """测试获取重置代码"""
        result = reset_code()
        assert result == ANSI_COLORS["reset"]


class TestColors:
    """测试 Colors 类"""

    def test_singleton_pattern(self):
        """测试单例模式"""
        colors1 = Colors()
        colors2 = Colors()
        assert colors1 is colors2

    @patch("peng.config.get_project_root")
    def test_load_config_from_file(self, mock_get_project_root):
        """测试从文件加载配置"""
        # 重置单例
        Colors._instance = None
        Colors._config = {}

        # Mock 项目根目录和配置文件
        mock_root = Path("/mock/root")
        mock_get_project_root.return_value = mock_root

        config_data = {"ui": {"error": "bright_red"}}
        mock_config_file = MagicMock()
        mock_config_file.exists.return_value = True
        mock_config_file.__enter__ = Mock(return_value=mock_config_file)
        mock_config_file.__exit__ = Mock(return_value=False)
        mock_config_file.read.return_value = json.dumps(config_data)

        with patch("builtins.open", return_value=mock_config_file):
            colors = Colors()
            assert colors.get("ui", "error") == "bright_red"

    @patch("peng.config.get_project_root")
    def test_load_config_default_when_file_not_found(self, mock_get_project_root):
        """测试配置文件不存在时使用默认配置"""
        # 重置单例
        Colors._instance = None
        Colors._config = {}

        mock_root = Path("/mock/root")
        mock_get_project_root.return_value = mock_root

        with patch.object(Path, "exists", return_value=False):
            colors = Colors()
            assert colors.get("ui", "error") == "bright_red"  # 默认值

    def test_get_default_config(self):
        """测试获取默认配置"""
        colors = Colors()
        default_config = colors._get_default_config()

        assert "ui" in default_config
        assert "tools" in default_config
        assert "models" in default_config
        assert default_config["ui"]["error"] == "bright_red"
        assert default_config["ui"]["success"] == "bright_green"

    def test_get_method(self):
        """测试 get 方法"""
        colors = Colors()
        result = colors.get("ui", "error")
        assert result == "bright_red"

    def test_get_invalid_category(self):
        """测试获取不存在的类别"""
        colors = Colors()
        result = colors.get("invalid_category", "invalid_name")
        assert result == "reset"

    def test_get_invalid_name(self):
        """测试获取不存在的名称"""
        colors = Colors()
        result = colors.get("ui", "invalid_name")
        assert result == "reset"

    def test_shortcut_methods(self):
        """测试快捷方法"""
        colors = Colors()

        assert colors.models("big_model") == "white"
        assert colors.ui("error") == "bright_red"
        assert colors.tools("tool_call") == "bright_magenta"
        assert colors.agent("thinking") == "bright_black"
        assert colors.bash("error") == "bright_red"
        assert colors.web("info") == "bright_blue"
        assert colors.file("error") == "bright_red"
        assert colors.message("error") == "bright_red"
        assert colors.task("error") == "bright_red"
        assert colors.fly("error") == "red"


class TestGetColors:
    """测试 get_colors 函数"""

    def test_get_colors_singleton(self):
        """测试 get_colors 返回单例"""
        colors1 = get_colors()
        colors2 = get_colors()
        assert colors1 is colors2


class TestColorConstants:
    """测试获取颜色常量的函数"""

    def test_get_message_colors(self):
        """测试获取消息颜色常量"""
        colors = get_message_colors()
        assert isinstance(colors, dict)
        assert "ERROR" in colors
        assert "SUCCESS" in colors
        assert "WARNING" in colors

    def test_get_task_colors(self):
        """测试获取事务颜色常量"""
        colors = get_task_colors()
        assert isinstance(colors, dict)
        assert "ERROR" in colors
        assert "SUCCESS" in colors

    def test_get_file_colors(self):
        """测试获取文件颜色常量"""
        colors = get_file_colors()
        assert isinstance(colors, dict)
        assert "ERROR" in colors
        assert "SUCCESS" in colors

    def test_get_bash_colors(self):
        """测试获取 Bash 颜色常量"""
        colors = get_bash_colors()
        assert isinstance(colors, dict)
        assert "ERROR" in colors
        assert "SUCCESS" in colors

    def test_get_web_colors(self):
        """测试获取 Web 颜色常量"""
        colors = get_web_colors()
        assert isinstance(colors, dict)
        assert "INFO" in colors

    def test_get_fly_colors(self):
        """测试获取 Fly 颜色常量"""
        colors = get_fly_colors()
        assert isinstance(colors, dict)
        assert "ERROR" in colors
        assert "SUCCESS" in colors

    def test_color_constants_ansi_codes(self):
        """测试颜色常量返回 ANSI 代码"""
        colors = get_message_colors()
        # 检查返回的是 ANSI 代码
        assert colors["ERROR"].startswith("\033[")


class TestANSIColors:
    """测试 ANSI_COLORS 常量"""

    def test_standard_colors(self):
        """测试标准颜色"""
        assert "black" in ANSI_COLORS
        assert "red" in ANSI_COLORS
        assert "green" in ANSI_COLORS
        assert "yellow" in ANSI_COLORS
        assert "blue" in ANSI_COLORS
        assert "magenta" in ANSI_COLORS
        assert "cyan" in ANSI_COLORS
        assert "white" in ANSI_COLORS

    def test_bright_colors(self):
        """测试亮色"""
        assert "bright_black" in ANSI_COLORS
        assert "bright_red" in ANSI_COLORS
        assert "bright_green" in ANSI_COLORS
        assert "bright_yellow" in ANSI_COLORS
        assert "bright_blue" in ANSI_COLORS
        assert "bright_magenta" in ANSI_COLORS
        assert "bright_cyan" in ANSI_COLORS
        assert "bright_white" in ANSI_COLORS

    def test_styles(self):
        """测试样式"""
        assert "reset" in ANSI_COLORS
        assert "bold" in ANSI_COLORS
        assert "dim" in ANSI_COLORS
        assert "underline" in ANSI_COLORS

    def test_aliases(self):
        """测试别名"""
        assert "gray" in ANSI_COLORS
        assert "grey" in ANSI_COLORS
        assert ANSI_COLORS["gray"] == ANSI_COLORS["grey"]
