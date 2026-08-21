"""
日志配置模块

初始化日志系统，根据配置文件设置日志级别、文件和格式。
所有日志输出到 logs 目录中。
"""

from __future__ import annotations

import logging
import sys
import traceback
from pathlib import Path
from typing import Any


def setup_logging(
    level: str = "INFO",
    log_file: str | None = None,
    log_format: str | None = None,
    project_root: Path | None = None,
) -> None:
    """
    初始化日志系统

    Args:
        level: 日志级别 (DEBUG, INFO, WARNING, ERROR, CRITICAL)
        log_file: 日志文件路径（相对于项目根目录或绝对路径）
        log_format: 日志格式
        project_root: 项目根目录（用于解析相对路径）
    """
    # 获取项目根目录
    if project_root is None:
        try:
            from peng.config import get_project_root

            project_root = get_project_root()
        except Exception as e:
            # 配置加载失败,使用默认值
            print(
                f"❌ setup_logging() - logging_config.py:38: 配置加载失败 - {e}\n{traceback.format_exc()}",
            )
            project_root = Path(__file__).parent.parent.parent.parent

    # 从配置读取 logs 目录
    from peng.config import get_path

    logs_dir = get_path("logs")

    logs_dir.mkdir(parents=True, exist_ok=True)
    from peng.common.paths import chown_to_project_owner
    chown_to_project_owner(logs_dir)

    # 默认值：统一使用 logs/peng.log
    if log_file is None:
        from peng.config import get_path

        log_file = str(get_path("log_file"))
    if log_format is None:
        log_format = "%(asctime)s | %(levelname)-8s | %(name)-30s | %(message)s"

    # 解析日志文件路径
    log_path = Path(log_file)
    if not log_path.is_absolute():
        log_path = project_root / log_path

    # 确保日志文件的父目录存在
    log_path.parent.mkdir(parents=True, exist_ok=True)
    chown_to_project_owner(log_path.parent)

    # 转换日志级别
    level_map = {
        "DEBUG": logging.DEBUG,
        "INFO": logging.INFO,
        "WARNING": logging.WARNING,
        "WARN": logging.WARNING,
        "ERROR": logging.ERROR,
        "CRITICAL": logging.CRITICAL,
    }
    log_level = level_map.get(level.upper(), logging.INFO)

    # 配置根日志器
    root_logger = logging.getLogger()

    # 清除现有处理器（避免重复）
    root_logger.handlers.clear()

    # 设置日志级别
    root_logger.setLevel(log_level)

    # 创建格式器
    formatter = logging.Formatter(log_format)

    # 添加文件处理器
    file_handler = logging.FileHandler(log_path, encoding="utf-8")
    chown_to_project_owner(log_path)
    file_handler.setLevel(log_level)
    file_handler.setFormatter(formatter)
    root_logger.addHandler(file_handler)

    # 添加控制台处理器：显示关键服务日志（简洁格式）
    console_handler = logging.StreamHandler(sys.stderr)
    console_handler.setLevel(log_level)  # 修改为INFO，让关键日志能进入filter
    console_handler.setFormatter(
        logging.Formatter("%(asctime)s | %(levelname)-8s | %(message)s", "%H:%M:%S")
    )

    # 允许以下日志通过：
    # 1. WARNING 及以上级别（必须显示！）
    # 2. uvicorn 启动日志
    # 3. peng.ui.service 的关键日志（服务启动/停止）
    # 4. peng.webui 的关键日志
    def _console_filter(record: logging.LogRecord) -> bool:
        # WARNING 及以上级别必须显示！
        if record.levelno > logging.INFO:
            return True
        msg = record.getMessage()
        # 只显示包含关键标记的日志
        # 服务相关日志（包含 emoji 的关键日志）
        if msg[0] in ["🪽", "🤖", "🛑", "🌐", "🗃️", "🛠️", "📄", "🔍", "🔎", "🚀","❌","⚠️", "🔄", "🧹", "💓", "📬", "📨", "📦",]:
            return True
        return False

    console_handler.addFilter(_console_filter)
    root_logger.addHandler(console_handler)

    # 记录初始化信息
    logger = logging.getLogger(__name__)
    logger.info(f"日志系统已初始化：level={level}, file={log_path}")


def setup_logging_from_config(config=None, level=None) -> None:
    """
    从配置对象初始化日志系统

    Args:
        config: PengConfig 对象（如果为 None，自动加载）
    """
    if config is None:
        try:
            from peng.config import load_config

            config = load_config()
        except Exception as e:
            # 配置加载失败，使用默认值
            print(
                f"❌ setup_logging_from_config() - logging_config.py:112: 配置加载失败 - {e}\n{traceback.format_exc()}",
            )
            setup_logging()
            return

    project_root = Path(config.paths.project_root_path)

    setup_logging(
        level=level if level else config.logging.level,
        log_file=config.logging.file,
        log_format=config.logging.format,
        project_root=project_root,
    )


def get_logger(name: str) -> logging.Logger:
    """
    获取指定名称的日志器

    Args:
        name: 日志器名称（通常使用 __name__）

    Returns:
        logging.Logger 对象
    """
    return logging.getLogger(name)


def setup_llm_logging(llm_logs_config=None):
    """
    设置 LLM 调用日志器（使用统一日志文件）

    LLM 日志现在统一记录到 logs/peng.log 中，不再使用单独文件。
    此函数仅返回配置好的 logger 对象，不创建独立文件处理器。

    Args:
        llm_logs_config: LLM 日志配置对象（LLMLogsConfig）

    Returns:
        llm_logger: LLM 日志器
    """
    llm_logger = logging.getLogger("peng.llm")
    llm_logger.setLevel(logging.DEBUG)

    # 不再创建独立文件处理器，使用根日志器的处理器（统一输出到 logs/peng.log）
    # 存储配置到 logger 属性，方便后续使用
    llm_logger.llm_logs_config = llm_logs_config

    return llm_logger


def should_log(category: str, subcategory: str | None = None, logs_config=None) -> bool:
    """
    根据配置判断是否应该记录某类日志

    Args:
        category: 日志分类（llm, task, message, process, agent, session, database, git, debug）
        subcategory: 日志子分类（如 request, create, inbox 等），对于简单布尔配置可为 None
        logs_config: 日志配置对象（LogsConfig 或 LLMLogsConfig）

    Returns:
        bool: 是否应该记录

    Examples:
        # LLM 日志（嵌套配置）
        should_log('llm', 'request.messages', logs_config)
        should_log('llm', 'response.content', logs_config)

        # 简单布尔配置
        should_log('task', 'create', logs_config)
        should_log('message', 'inbox', logs_config)
    """
    if logs_config is None:
        return True  # 默认记录

    try:
        # 处理 LLM 日志的特殊情况（向后兼容）
        if category == "llm" and hasattr(logs_config, "llm"):
            # 新的 LogsConfig 结构
            llm_config = getattr(logs_config, "llm", None)
            if llm_config is None:
                return True

            # 解析子分类（支持点号分隔，如 'request.messages'）
            if subcategory and "." in subcategory:
                parts = subcategory.split(".", 1)
                category_config = getattr(llm_config, parts[0], None)
                if category_config is None:
                    return True
                return getattr(category_config, parts[1], True)
            if subcategory:
                # 直接子分类
                category_config = getattr(llm_config, subcategory, None)
                if category_config is None:
                    return True
                # 如果是嵌套配置（如 request），需要进一步检查
                if hasattr(category_config, "__dict__"):
                    # 返回整个分类的默认值（True）
                    return True
                return category_config

            return True

        # 向后兼容：直接使用 llm_logs_config（LLMLogsConfig 对象）
        if hasattr(logs_config, "request") or hasattr(logs_config, "response"):
            # 这是旧的 LLMLogsConfig 对象
            category_config = getattr(logs_config, category, None)
            if category_config is None:
                return True

            if subcategory and "." in subcategory:
                parts = subcategory.split(".", 1)
                sub_config = getattr(category_config, parts[0], None)
                if sub_config is None:
                    return True
                return getattr(sub_config, parts[1], True)
            if subcategory:
                return getattr(category_config, subcategory, True)

            return True

        # 新的 LogsConfig 结构（简单布尔配置）
        config_obj = getattr(logs_config, category, None)
        if config_obj is None:
            return True

        # 如果有子分类，支持多级嵌套（如 llm_processor.tools_loaded）
        if subcategory:
            parts = subcategory.split(".")
            current = config_obj
            for part in parts:
                if current is None:
                    return True
                current = getattr(current, part, None)
            return current if current is not None else True

        # 如果没有子分类，返回整个分类的默认值
        return True

    except Exception as e:
        print(f"❌ logging_config.py:252: 异常 - {e}\n{traceback.format_exc()}")
        return True


def get_logs_config() -> Any:
    """
    获取日志配置对象

    Returns:
        LogsConfig 对象，如果加载失败则返回 None
    """
    try:
        from peng.config import load_config

        config = load_config()
        return config.logging.get_logs_config()
    except Exception as e:
        print(
            f"❌ get_logs_config() - logging_config.py:269: 配置加载失败 - {e}\n{traceback.format_exc()}"
        )
        return None
