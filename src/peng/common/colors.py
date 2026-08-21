"""
ANSI颜色代码 - 统一颜色管理模块

所有颜色定义统一由 config/colors.json 配置，通过此模块提供访问接口。
避免在各处硬编码颜色代码。

用法:
    from peng.common.colors import colorize, get_color, Colors

    # 基础用法：使用 ANSI 颜色名
    print(colorize("Hello", "red"))

    # 从配置获取颜色
    colors = Colors()
    print(colorize("Error", colors.get("ui", "error")))

    # 直接获取 ANSI 代码
    code = get_color_code("bright_red")
"""

from __future__ import annotations

import logging
import traceback
from pathlib import Path
from typing import Any

from peng.common import jsonp as json

# ==================== ANSI 颜色代码映射（基础定义）====================

logger = logging.getLogger(__name__)
ANSI_COLORS: dict[str, str] = {
    # 标准颜色
    "black": "\033[30m",
    "red": "\033[31m",
    "green": "\033[32m",
    "yellow": "\033[33m",
    "blue": "\033[34m",
    "magenta": "\033[35m",
    "cyan": "\033[36m",
    "white": "\033[37m",
    # 亮色
    "bright_black": "\033[90m",
    "bright_red": "\033[91m",
    "bright_green": "\033[92m",
    "bright_yellow": "\033[93m",
    "bright_blue": "\033[94m",
    "bright_magenta": "\033[95m",
    "bright_cyan": "\033[96m",
    "bright_white": "\033[97m",
    # 样式
    "reset": "\033[0m",
    "bold": "\033[1m",
    "dim": "\033[2m",
    "underline": "\033[4m",
    # 常用别名
    "gray": "\033[90m",
    "grey": "\033[90m",
    # 自定义颜色
    # context_red: 上下文过长警告专用红色，亮度拉满，HSV饱和度~61%
    # 在此前 RGB(255,130,130) HSV S≈49% 基础上提高25%饱和度
    "context_red": "\033[38;2;255;100;100m",
}


def colorize(text: str, color: str) -> str:
    """
    给文本添加颜色

    Args:
        text: 要着色的文本
        color: 颜色名称（ANSI 颜色名或 'none'）

    Returns:
        着色后的文本
    """
    if color == "none" or not color:
        return text
    ansi_code = ANSI_COLORS.get(color, "")
    reset = ANSI_COLORS["reset"] if ansi_code else ""
    return f"{ansi_code}{text}{reset}"


def get_color_code(color: str) -> str:
    """
    获取颜色的 ANSI 代码

    支持格式：
    - ANSI颜色名: red, green, blue, bright_red 等
    - RGB格式: rgb:R,G,B (如 rgb:140,170,140)
    - 256色格式: color256:N (如 color256:108)
    - none: 不着色

    Args:
        color: 颜色名称或RGB格式

    Returns:
        ANSI 转义码，如果颜色不存在则返回空字符串
    """
    if color == "none" or not color:
        return ""

    # RGB格式: rgb:R,G,B
    if color.startswith("rgb:"):
        try:
            parts = color[4:].split(",")
            if len(parts) == 3:
                r, g, b = int(parts[0]), int(parts[1]), int(parts[2])
                return f"\033[38;2;{r};{g};{b}m"
        except (ValueError, IndexError):
            pass

    # 256色格式: color256:N
    if color.startswith("color256:"):
        try:
            n = int(color[9:])
            if 0 <= n <= 255:
                return f"\033[38;5;{n}m"
        except ValueError:
            pass

    return ANSI_COLORS.get(color, "")


def reset_code() -> str:
    """获取重置代码"""
    return ANSI_COLORS["reset"]


class Colors:
    """
    颜色配置管理类

    从 config/colors.json 加载颜色配置，提供统一的颜色访问接口。

    用法:
        colors = Colors()
        error_color = colors.get("ui", "error")  # 返回 "bright_red"
        print(colorize("Error!", error_color))

        # 或者使用快捷方法
        print(colors.ui("error"))    # 等同于 colors.get("ui", "error")
        print(colors.tools("tool_call"))
    """

    _instance: Colors | None = None
    _config: dict[str, Any] = {}

    def __new__(cls) -> Colors:
        """单例模式，只加载一次配置"""
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._load_config()
        return cls._instance

    def _load_config(self) -> None:
        """加载颜色配置文件"""
        from peng.config import get_project_root

        # 尝试多个可能的配置文件路径
        config_paths = [
            get_project_root() / "config" / "colors.json",
            Path.cwd() / "config" / "colors.json",
        ]

        for config_path in config_paths:
            if config_path.exists():
                try:
                    with open(config_path, encoding="utf-8") as f:
                        self._config = json.load(f) or {}
                    return
                except Exception as e:
                    import traceback

                    logger.error(
                        f"❌ _load_config() - colors.py:167: 异常 - {e}\n{traceback.format_exc()}"
                    )

        # 如果没有找到配置文件，使用默认值
        self._config = self._get_default_config()

    def _get_default_config(self) -> dict[str, Any]:
        """获取默认颜色配置"""
        return {
            "models": {"big_model": "white", "small_model": "bright_black"},
            "ui": {
                "prompt": "cyan",
                "agent_name": "cyan",
                "system": "yellow",
                "error": "bright_red",
                "success": "bright_green",
                "warning": "yellow",
                "dim": "bright_black",
                "info": "cyan",
                "goodbye": "green",
                "title": "cyan",
            },
            "tools": {
                "tool_call": "bright_magenta",
                "tool_result": "bright_green",
                "tool_error": "bright_red",
                "tool_executing": "bright_magenta",
            },
            "agent": {
                "thinking": "bright_black",
                "tool_call": "bright_blue",
                "result": "bright_green",
            },
            "bash": {
                "error": "bright_red",
                "success": "bright_green",
                "info": "bright_blue",
                "reset": "reset",
            },
            "web": {"info": "bright_blue", "reset": "reset"},
            "file": {
                "error": "bright_red",
                "success": "bright_green",
                "warning": "bright_yellow",
                "info": "bright_blue",
                "highlight": "bright_cyan",
                "bold": "bold",
                "dim": "dim",
                "reset": "reset",
            },
            "message": {
                "error": "bright_red",
                "success": "bright_green",
                "warning": "bright_yellow",
                "info": "bright_blue",
                "highlight": "bright_magenta",
                "title": "bright_cyan",
                "content": "bright_white",
                "bold": "bold",
                "dim": "dim",
                "reset": "reset",
            },
            "task": {
                "error": "bright_red",
                "success": "bright_green",
                "warning": "bright_yellow",
                "info": "bright_blue",
                "highlight": "bright_magenta",
                "title": "bright_cyan",
                "content": "bright_white",
                "bold": "bold",
                "dim": "dim",
                "reset": "reset",
            },
            "fly": {
                "error": "red",
                "success": "green",
                "warning": "yellow",
                "info": "blue",
                "reset": "reset",
            },
        }

    def get(self, category: str, name: str) -> str:
        """
        获取指定类别和名称的颜色

        Args:
            category: 颜色类别（如 ui, tools, agent 等）
            name: 颜色名称（如 error, success 等）

        Returns:
            ANSI 颜色名称
        """
        category_config = self._config.get(category, {})
        return category_config.get(name, "reset")

    # ==================== 快捷方法 ====================

    def models(self, name: str) -> str:
        """获取模型颜色"""
        return self.get("models", name)

    def ui(self, name: str) -> str:
        """获取 UI 颜色"""
        return self.get("ui", name)

    def tools(self, name: str) -> str:
        """获取工具颜色"""
        return self.get("tools", name)

    def agent(self, name: str) -> str:
        """获取 Agent 颜色"""
        return self.get("agent", name)

    def bash(self, name: str) -> str:
        """获取 Bash 颜色"""
        return self.get("bash", name)

    def web(self, name: str) -> str:
        """获取 Web 颜色"""
        return self.get("web", name)

    def file(self, name: str) -> str:
        """获取文件操作颜色"""
        return self.get("file", name)

    def message(self, name: str) -> str:
        """获取消息颜色"""
        return self.get("message", name)

    def task(self, name: str) -> str:
        """获取事务颜色"""
        return self.get("task", name)

    def fly(self, name: str) -> str:
        """获取 Fly 颜色"""
        return self.get("fly", name)


# 全局颜色实例（懒加载）
_colors_instance: Colors | None = None


def get_colors() -> Colors:
    """获取全局颜色配置实例"""
    global _colors_instance
    if _colors_instance is None:
        _colors_instance = Colors()
    return _colors_instance


# ==================== 便捷常量（向后兼容）====================
# 这些常量提供给需要直接使用颜色代码的场景


def _get_color_constants(category: str) -> dict[str, str]:
    """获取指定类别的颜色常量字典"""
    colors = get_colors()
    category_config = colors._config.get(category, {})
    result = {}
    for name, color_name in category_config.items():
        result[name.upper()] = get_color_code(color_name) if color_name != "reset" else reset_code()
    return result


def get_message_colors() -> dict[str, str]:
    """获取消息颜色常量（用于 bin/msg）"""
    return _get_color_constants("message")


def get_task_colors() -> dict[str, str]:
    """获取事务颜色常量（用于 bin/task）"""
    return _get_color_constants("task")


def get_file_colors() -> dict[str, str]:
    """获取文件操作颜色常量（用于 tools/file.py）"""
    return _get_color_constants("file")


def get_bash_colors() -> dict[str, str]:
    """获取 Bash 颜色常量（用于 tools/bash.py）"""
    return _get_color_constants("bash")


def get_web_colors() -> dict[str, str]:
    """获取 Web 颜色常量（用于 tools/web.py）"""
    return _get_color_constants("web")


def get_fly_colors() -> dict[str, str]:
    """获取 Fly 颜色常量（用于 bin/fly）"""
    return _get_color_constants("fly")
