"""
共享工具函数、常量和 Pydantic 模型

供各路由模块复用，避免循环导入。
"""

from __future__ import annotations

import logging
import os
import sys
import time
import traceback
from pathlib import Path
from typing import Any, Callable

from fastapi import HTTPException
from pydantic import BaseModel

from peng.config import get_agent_name, get_master_name, get_path
from peng.common.paths import get_project_root, get_temp_directory, ensure_dir_with_owner

logger = logging.getLogger(__name__)
AGENT_NAME = get_agent_name()


# ==================== 工具函数 ====================


def _build_python_cmd(script_path: Path, args: list[str] | None = None) -> list[str]:
    """构建跨平台的 Python 脚本执行命令

    在 Windows 上，无 .py 扩展名的 Python 脚本无法直接执行，
    需要通过 sys.executable 来运行。在 Linux/macOS 上可直接执行。

    Args:
        script_path: Python 脚本路径
        args: 额外参数列表

    Returns:
        命令列表，可直接传给 subprocess.run
    """
    cmd = [str(script_path)]
    if args:
        cmd.extend(args)

    # Windows 上无扩展名的脚本需要通过 python 解释器执行
    if sys.platform == "win32":
        cmd = [sys.executable] + cmd

    return cmd


# ==================== 颜色命令环境变量 ====================

_COLOR_FLAG_COMMANDS: dict[str, str] = {
    "ls": "--color=always",
    "lsd": "--color=always",
    "eza": "--color=always",
    "exa": "--color=always",
    "grep": "--color=always",
    "egrep": "--color=always",
    "fgrep": "--color=always",
    "rg": "--color=always",
    "fd": "--color=always",
    "findfd": "--color=always",
    "tree": "--color=always",
    "bat": "--color=always",
    "diff": "--color=always",
    "ip": "--color=always",
    "git diff": "--color=always",
    "git log": "--color=always",
    "git branch": "--color=always",
    "docker": "",
    "kubectl": "",
    "cargo": "",
}


def _get_color_env() -> dict[str, str]:
    """获取启用颜色输出的环境变量"""
    return {
        "TERM": "xterm-256color",
        "COLORTERM": "truecolor",
        "CLICOLOR_FORCE": "1",
        "CLICOLOR": "1",
        "FORCE_COLOR": "1",
        "GREP_COLORS": "mt=01;31:fn=35:ln=32:se=36",
        "GCC_COLORS": "error=01;31:warning=01;35:note=01;36:caret=01;32:locus=01;34:quote=01",
        "COLUMNS": "320",
    }


def _add_color_flag(cmd_list: list[str]) -> list[str]:
    """为支持颜色的命令自动添加颜色参数"""
    if not cmd_list:
        return cmd_list

    base_cmd = cmd_list[0]
    cmd_name = os.path.basename(base_cmd)

    for arg in cmd_list:
        if arg.startswith("--color") or arg.startswith("--colour"):
            return cmd_list

    cmd = " ".join(cmd_list)
    for k, v in _COLOR_FLAG_COMMANDS.items():
        if cmd.startswith(k):
            cmd = cmd.replace(k, k + " " + v, 1)
            cmd_list = cmd.split(" ")
            print(cmd)
            break
    return cmd_list


# ==================== Nest 远程文件操作辅助 ====================


def _get_selected_nest_conn(nest_name: str | None):
    """获取指定nest的连接（用于文件/终端远程操作）

    Args:
        nest_name: nest display_key，None或空字符串表示本地操作

    Returns:
        NestConnection | None: nest连接，None表示本地操作或未连接
    """
    if not nest_name:
        return None

    try:
        from peng.nest.bridge import NestBridgeManager
        manager = NestBridgeManager.get_instance()
        conn = manager.get_connection(nest_name)
        if conn:
            logger.debug(f"获取nest连接成功: {nest_name}")
        else:
            available = [c["display_key"] for c in manager.list_connections()]
            logger.warning(f"nest连接未找到: '{nest_name}', 可用连接: {available}")
        return conn
    except Exception as e:
        logger.warning(f"获取nest连接失败: {nest_name}, error={e}")
        return None


def _extract_bash_stdout(response) -> str | None:
    """从 nest bash 响应中提取 stdout 文本"""
    data = response.data
    if isinstance(data, dict):
        exit_code = data.get("exit_code", -1)
        if exit_code != 0:
            return None
        return data.get("stdout", "")
    return str(data) if data else None


def _get_nest_project_root(nest_conn) -> str | None:
    """从 nest 连接的注册信息中获取项目根目录"""
    info = getattr(nest_conn, 'info', {}) or {}
    return info.get("project_root")


def detect_language(file_path: str) -> str:
    """根据文件扩展名检测语言"""
    # CMakeLists.txt 特殊处理（无扩展名的文件）
    filename = Path(file_path).name.lower()
    if filename == "cmakelists.txt":
        return "cmake"
    ext = Path(file_path).suffix.lower()
    language_map = {
        ".py": "python",
        ".pyw": "python",
        ".pyi": "python",
        ".kn": "python",
        ".js": "javascript",
        ".mjs": "javascript",
        ".cjs": "javascript",
        ".ts": "typescript",
        ".tsx": "typescript",
        ".jsx": "javascript",
        ".html": "html",
        ".htm": "html",
        ".css": "css",
        ".scss": "scss",
        ".less": "less",
        ".json": "json",
        ".md": "markdown",
        ".yaml": "yaml",
        ".yml": "yaml",
        ".toml": "toml",
        ".ini": "ini",
        ".sh": "shell",
        ".bash": "shell",
        ".sql": "sql",
        ".xml": "xml",
        ".java": "java",
        ".c": "c",
        ".cpp": "cpp",
        ".c++": "cpp",
        ".h": "c",
        ".hpp": "cpp",
        ".go": "go",
        ".rs": "rust",
        ".rb": "ruby",
        ".php": "php",
        ".zig": "zig",
        ".lua": "lua",
        ".cs": "csharp",
        ".kt": "kotlin",
        ".swift": "swift",
        ".dart": "dart",
        ".scala": "scala",
        ".vue": "html",
        ".svelte": "html",
        ".txt": "plaintext",
        ".cmake": "cmake",
    }
    return language_map.get(ext, "markdown")  # 🔧 Task#866：未匹配扩展名默认markdown高亮


# ==================== 共享常量 ====================

# 文件大小限制 (10MB)
MAX_FILE_SIZE = 10 * 1024 * 1024

# 默认输出历史行数
DEFAULT_OUTPUT_HISTORY_LIMIT = 500

# 上传目录
UPLOAD_DIR = get_temp_directory() / "peng_uploads"
ensure_dir_with_owner(UPLOAD_DIR)

# 编辑器临时文件目录
EDITOR_TEMP_DIR = get_temp_directory() / "peng_edits"
ensure_dir_with_owner(EDITOR_TEMP_DIR)

# 允许的文件类型
ALLOWED_EXTENSIONS = {
    ".txt", ".md", ".csv", ".json", ".xml", ".yaml", ".yml",
    ".py", ".js", ".ts", ".java", ".c", ".cpp", ".h", ".hpp",
    ".html", ".css", ".sql", ".sh", ".bash",
    ".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg", ".pdf",
    ".zip", ".tar", ".gz",
}

# 支持的图片扩展名
IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".svg"}

# 上传重试配置
MAX_RETRY_COUNT = 3
RETRY_DELAY = 1.0
RETRY_BACKOFF = 2.0

# 项目结构缓存
_project_structure_cache: dict[str, Any] = {
    "workdir": None,
    "dirs": [],
    "files": [],
    "scan_time": 0.0,
}
_PROJECT_STRUCTURE_CACHE_TTL = 100000000


# ==================== 全局状态 ====================

# 当前工作目录（由 workdir_config 模块管理）
_current_workdir: str | None = None


# ==================== 共享 Pydantic 模型 ====================


class CreateMessageRequest(BaseModel):
    """创建消息请求"""
    content: str
    reply_to: int | None = None
    files: list[dict[str, Any]] | None = None
    model: str | None = None
    thinking_mode: str | None = None
    thinking_budget: int | None = None
    search_enabled: bool = False
    workdir: str | None = None
    nest: str | None = None


class ReplyMessageRequest(BaseModel):
    """回复消息请求"""
    content: str
    files: list[dict[str, Any]] | None = None
    model: str | None = None
    thinking_mode: str | None = None
    thinking_budget: int | None = None
    search_enabled: bool = False
    workdir: str | None = None
    nest: str | None = None


class TaskActionRequest(BaseModel):
    """事务操作请求"""
    action: str


class MoveTaskRequest(BaseModel):
    """事务移动请求"""
    target_id: int | None = None


class MessageMoveRequest(BaseModel):
    """消息移动请求"""
    target_id: int | None = None


class BroadcastTaskRequest(BaseModel):
    """子进程广播请求"""
    task_id: int
    action: str
    details: dict[str, Any] = {}


class BroadcastMessageRequest(BaseModel):
    """子进程消息广播请求"""
    msg_id: int
    action: str
    details: dict[str, Any] = {}
    source: str = "unknown"


class CommandRequest(BaseModel):
    command: str


class SwitchModelRequest(BaseModel):
    """模型切换请求"""
    model_name: str
