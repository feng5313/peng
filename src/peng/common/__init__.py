"""
共享模块

提供通用的类型定义、颜色代码等功能。
"""

from peng.common.colors import (ANSI_COLORS, Colors, colorize, get_bash_colors,
                                get_color_code, get_colors, get_file_colors,
                                get_fly_colors, get_message_colors,
                                get_task_colors, get_web_colors, reset_code)
from peng.common.types import MetaSingleton, ToolCall
from peng.common.logging_config import (get_logger, get_logs_config,
                                        setup_llm_logging, setup_logging,
                                        setup_logging_from_config, should_log)
# truncation 模块不在此处导入，因为它依赖 peng.config 会造成循环导入
# 使用时请直接 from peng.common.truncation import ...

__all__ = [
    "ANSI_COLORS",
    "CacheDecorator",
    "CacheKey",
    "CacheManager",
    "CacheStats",
    "Colors",
    "SessionManager",
    "ToolCall",
    "MetaSingleton",
    "colorize",
    "create_cache",
    "create_session_manager",
    "get_bash_colors",
    "get_color_code",
    "get_colors",
    "get_file_colors",
    "get_fly_colors",
    "get_message_colors",
    "get_task_colors",
    "get_web_colors",
    "reset_code",
]
